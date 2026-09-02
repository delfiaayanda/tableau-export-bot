"""Probes the *documented* public REST API "Query View Crosstab Excel"
endpoint and reports only safe metadata about the response -- HTTP
status, elapsed time, Content-Type, Content-Disposition, response size,
and a filename parsed from Content-Disposition if present. Never returns
or inspects the response body's contents, and never includes the auth
token or credentials in any result.

IMPORTANT CAVEAT: this targets
`GET /api/{version}/sites/{site-id}/views/{view-id}/crosstab/excel`,
authenticated via the existing X-Tableau-Auth session -- the one
concretely *documented* crosstab-shaped REST endpoint. This is NOT
confirmed to be what Tableau's browser "Download -> Crosstab" menu
action actually calls; the interactive web client is a separate
application (VizQL-based) that typically uses its own internal,
undocumented, cookie-session-based endpoints for UI download actions,
not the public REST API. Confirming the real browser request requires a
DevTools network capture (see CLAUDE.md). Treat this probe as one
concrete, testable hypothesis using infrastructure we already have (our
REST auth session) -- not a claim that it matches the UI's behavior.

Not exercised against the real server yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .auth import TableauSession


def _extract_filename(content_disposition: str | None) -> str | None:
    """Pulls a filename out of a Content-Disposition header value. This is
    metadata about the response, never its contents, so it's safe to
    report in full."""
    if not content_disposition:
        return None
    for part in content_disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            value = part.split("=", 1)[1]
            return value.split("''", 1)[-1].strip('"')
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return None


@dataclass(frozen=True)
class CrosstabProbeResult:
    url: str
    status_code: int
    elapsed_seconds: float
    content_type: str
    content_disposition: str | None
    filename: str | None
    response_size_bytes: int | None
    error_body: str | None


def probe_crosstab_excel(
    session: TableauSession,
    view_id: str,
    timeout: float = 30.0,
) -> CrosstabProbeResult:
    """Request the "Query View Crosstab Excel" REST endpoint and report
    safe metadata only. Never raises: network errors and timeouts are
    captured in `error_body` with status_code 0. Does not save the file;
    the body is read only to measure its length, never inspected."""
    url = (
        f"{session.server_url.rstrip('/')}/api/{session.api_version}"
        f"/sites/{session.site_id}/views/{view_id}/crosstab/excel"
    )
    headers = {"X-Tableau-Auth": session.auth_token}

    start = time.monotonic()
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.Timeout:
        elapsed = time.monotonic() - start
        return CrosstabProbeResult(
            url=url,
            status_code=0,
            elapsed_seconds=elapsed,
            content_type="",
            content_disposition=None,
            filename=None,
            response_size_bytes=None,
            error_body=f"timeout after {elapsed:.1f}s (no response received)",
        )
    except requests.RequestException as exc:
        elapsed = time.monotonic() - start
        return CrosstabProbeResult(
            url=url,
            status_code=0,
            elapsed_seconds=elapsed,
            content_type="",
            content_disposition=None,
            filename=None,
            response_size_bytes=None,
            error_body=f"network error: {exc}",
        )
    elapsed = time.monotonic() - start

    content_type = response.headers.get("Content-Type", "unknown")
    content_disposition = response.headers.get("Content-Disposition")
    filename = _extract_filename(content_disposition)
    error_body = response.text[:300] if response.status_code >= 400 else None
    response_size_bytes = len(response.content) if response.status_code < 400 else None

    return CrosstabProbeResult(
        url=url,
        status_code=response.status_code,
        elapsed_seconds=elapsed,
        content_type=content_type,
        content_disposition=content_disposition,
        filename=filename,
        response_size_bytes=response_size_bytes,
        error_body=error_body,
    )
