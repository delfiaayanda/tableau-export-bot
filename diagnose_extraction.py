#!/usr/bin/env python3
"""Diagnoses a non-200 response (e.g. HTTP 406) from the "view data"
endpoint by retrying the exact same request with a few different Accept
header values, to distinguish:

  A. an incorrect/inappropriate Accept header in our request (code fix)
  B. a missing Tableau "Export Data" permission on the view (admin action)
  C. some other Tableau-Server-specific behavior (needs further digging)

HTTP 406 Not Acceptable is specifically about content negotiation on the
Accept header -- Tableau's REST API signals permission problems with 403,
not 406 -- so this starts by testing whether a different Accept value (or
none at all) changes the outcome, before assuming anything else.

Prints, for each Accept variant tried: the endpoint used, the Accept
header sent, HTTP status, response Content-Type, a small allowlist of
safe response headers, and a truncated/sanitized error body. Never
prints credentials, the auth token, cookies, or row data.

Usage: python diagnose_extraction.py
(requires TABLEAU_VIEW_ID to be set -- see discover_view.py)
"""

from __future__ import annotations

import sys

from src.exportbot.auth import AuthenticationError, TableauServerAuthenticator, redact_secrets
from src.exportbot.config import Config, ConfigError, load_config, require_view_id
from src.exportbot.exceptions import ExtractionError
from src.exportbot.extraction_diagnostics import ACCEPT_HEADER_VARIANTS, probe_with_accept_header
from src.exportbot.serverinfo import discover_api_version


def run_diagnosis(config: Config, view_id: str) -> tuple[bool, list[str]]:
    """Authenticate once, then probe the view-data endpoint with each
    Accept header variant. Returns (success, lines) -- lines is one
    sanitized summary per variant tried; success is False only if we
    couldn't even authenticate or resolve the API version."""
    try:
        api_version = config.tableau_api_version or discover_api_version(config.tableau_server_url)
    except ExtractionError as exc:
        return False, [redact_secrets(f"Could not determine API version: {exc}", config)]

    try:
        session = TableauServerAuthenticator(api_version=api_version).authenticate(config)
    except AuthenticationError as exc:
        return False, [redact_secrets(str(exc), config)]

    lines = []
    for accept in ACCEPT_HEADER_VARIANTS:
        result = probe_with_accept_header(session, view_id, accept)
        line = (
            f"Accept={result.accept_header_sent}: GET {result.url} -> "
            f"HTTP {result.status_code}, content-type={result.content_type or 'n/a'}, "
            f"headers={result.safe_headers}"
        )
        if result.error_body:
            line += f", error_body={result.error_body!r}"
        lines.append(line)

    return True, lines


def main() -> int:
    try:
        config = load_config()
        view_id = require_view_id(config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    success, lines = run_diagnosis(config, view_id)

    for line in lines:
        print(line)

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
