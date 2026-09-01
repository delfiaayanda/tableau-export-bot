"""Diagnoses a non-200 response from the "view data" endpoint by retrying
the exact same request with a few different `Accept` header values, and
reporting only safe metadata for each attempt -- HTTP status, content
type, a small allowlist of response headers, and a truncated/sanitized
error body. Never returns row data, and never includes the auth token or
credentials in any result.

HTTP 406 Not Acceptable is specifically a content-negotiation status (RFC
7231 SS6.5.6): the server is declining to produce a representation
matching the client's Accept header. It is not how Tableau's REST API
signals authentication (401) or permission (403) failures -- so a 406
here points first at the request's Accept header, not at credentials or
export permissions. This module exists to test that empirically against
the real server instead of guessing.

Not exercised against the real server yet -- see CLAUDE.md.
"""

from __future__ import annotations

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

# The three variants worth distinguishing: the header currently sent in
# production code, no override at all (requests defaults to "*/*"), and
# the other common explicit value.
ACCEPT_HEADER_VARIANTS: tuple[str | None, ...] = ("text/csv", None, "application/json", "*/*")


@dataclass(frozen=True)
class DiagnosticResult:
    url: str
    accept_header_sent: str
    status_code: int
    content_type: str
    safe_headers: dict[str, str]
    error_body: str | None


def probe_with_accept_header(
    session: TableauSession,
    view_id: str,
    accept: str | None,
    timeout: float = 30.0,
) -> DiagnosticResult:
    """Request the view's data with a specific (or absent) Accept header.

    Never raises: network errors are captured in `error_body` with
    status_code 0, so a caller can try every variant even if one fails to
    connect.
    """
    url = (
        f"{session.server_url.rstrip('/')}/api/{session.api_version}"
        f"/sites/{session.site_id}/views/{view_id}/data"
    )
    headers = {"X-Tableau-Auth": session.auth_token}
    if accept is not None:
        headers["Accept"] = accept

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return DiagnosticResult(
            url=url,
            accept_header_sent=accept or "(none)",
            status_code=0,
            content_type="",
            safe_headers={},
            error_body=f"network error: {exc}",
        )

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
        content_type=content_type,
        safe_headers=safe_headers,
        error_body=error_body,
    )
