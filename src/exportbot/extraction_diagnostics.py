"""Diagnoses a non-200 (or hanging) response from a Tableau "view"
sub-resource by retrying the exact same request with a few different
`Accept` header values against multiple candidate endpoints, reporting
only safe metadata for each attempt -- HTTP status, elapsed time, content
type, a small allowlist of response headers, and a truncated/sanitized
error body. Never returns row data, and never includes the auth token or
credentials in any result.

HTTP 406 Not Acceptable is specifically a content-negotiation status (RFC
7231 SS6.5.6): the server is declining to produce a representation
matching the client's Accept header. It is not how Tableau's REST API
signals authentication (401) or permission (403) failures -- so a 406
here points first at the request's Accept header, not at credentials or
export permissions. A live run showed an asymmetric pattern though:
Accept: text/csv gets a fast 406, while every other Accept variant (none,
application/json, */*) times out with no response at all -- a fast,
deterministic rejection for one shape and a silent hang for every other
is not itself typical of plain server-side content negotiation (which
would reject an unacceptable Accept quickly, the same way, every time).
It's more consistent with a routing-dependent intermediary (reverse
proxy/gateway) or a Tableau Server-side handler that behaves very
differently depending on the exact Accept value -- something this module
exists to test empirically, not guess.

Not exercised against the real server yet -- see CLAUDE.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from .auth import TableauSession

# Small, deliberately conservative allowlist: never includes Set-Cookie or
# any header that could carry session/auth material.
SAFE_RESPONSE_HEADER_NAMES = {
    "content-type",
    "content-length",
    "date",
    "server",
    "vary",
    "cache-control",
    "allow",
    "warning",
}

# The variants worth distinguishing: the header currently sent in
# production code, no override at all (requests defaults to "*/*"), and
# other common explicit values.
ACCEPT_HEADER_VARIANTS: tuple[str | None, ...] = ("text/csv", None, "application/json", "*/*")

# Candidate "view" sub-resources documented by the Tableau REST API that
# could plausibly supply the table data visible in a dashboard: the
# summary/detail data behind the view (used first), and the crosstab
# export (a pivoted, Excel-native alternative -- a genuinely different
# server-side code path from /data, worth testing separately rather than
# assumed to fail the same way).
ENDPOINT_PATHS: tuple[tuple[str, str], ...] = (
    ("/views/{view_id}/data", "Query View Data (CSV)"),
    ("/views/{view_id}/crosstab/excel", "Query View Crosstab Excel"),
)


@dataclass(frozen=True)
class DiagnosticResult:
    url: str
    accept_header_sent: str
    status_code: int
    elapsed_seconds: float
    content_type: str
    safe_headers: dict[str, str]
    error_body: str | None


def probe_endpoint(
    session: TableauSession,
    path: str,
    accept: str | None,
    timeout: float = 10.0,
) -> DiagnosticResult:
    """Request a "view" sub-resource path (e.g. "/views/{id}/data") with a
    specific (or absent) Accept header, and report safe metadata only.

    Never raises: network errors and timeouts are captured in
    `error_body` with status_code 0, so a caller can try every variant
    even after one fails or hangs. Uses a short default timeout (10s)
    specifically so a hang shows up quickly as a distinct, labeled result
    rather than stalling the whole diagnostic run.
    """
    url = f"{session.server_url.rstrip('/')}/api/{session.api_version}/sites/{session.site_id}{path}"
    headers = {"X-Tableau-Auth": session.auth_token}
    if accept is not None:
        headers["Accept"] = accept

    start = time.monotonic()
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.Timeout:
        elapsed = time.monotonic() - start
        return DiagnosticResult(
            url=url,
            accept_header_sent=accept or "(none)",
            status_code=0,
            elapsed_seconds=elapsed,
            content_type="",
            safe_headers={},
            error_body=f"timeout after {elapsed:.1f}s (no response received)",
        )
    except requests.RequestException as exc:
        elapsed = time.monotonic() - start
        return DiagnosticResult(
            url=url,
            accept_header_sent=accept or "(none)",
            status_code=0,
            elapsed_seconds=elapsed,
            content_type="",
            safe_headers={},
            error_body=f"network error: {exc}",
        )
    elapsed = time.monotonic() - start

    safe_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in SAFE_RESPONSE_HEADER_NAMES
    }
    content_type = response.headers.get("Content-Type", "unknown")
    error_body = response.text[:300] if response.status_code >= 400 else None

    return DiagnosticResult(
        url=url,
        accept_header_sent=accept or "(none)",
        status_code=response.status_code,
        elapsed_seconds=elapsed,
        content_type=content_type,
        safe_headers=safe_headers,
        error_body=error_body,
    )


def probe_with_accept_header(
    session: TableauSession,
    view_id: str,
    accept: str | None,
    timeout: float = 10.0,
) -> DiagnosticResult:
    """Probe the "Query View Data" endpoint specifically (kept as a named
    wrapper around probe_endpoint() for the common case)."""
    return probe_endpoint(session, f"/views/{view_id}/data", accept, timeout)
