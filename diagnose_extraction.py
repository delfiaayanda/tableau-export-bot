#!/usr/bin/env python3
"""Diagnoses a non-200 or hanging response from Tableau's "view" data
endpoints by retrying the same request with a few different Accept
header values against each candidate endpoint, to distinguish:

  A. an incorrect/inappropriate Accept header in our request (code fix)
  B. a missing Tableau "Export Data" permission on the view (admin action)
  C. some other Tableau-Server-specific behavior (needs further digging)

HTTP 406 Not Acceptable is specifically about content negotiation on the
Accept header -- Tableau's REST API signals permission problems with 403,
not 406. A live run previously showed Accept: text/csv failing fast with
406 while every other Accept value timed out with no response at all;
that asymmetry (fast structured rejection vs. silent hang) is tested here
with a short per-request timeout and elapsed time reported for every
attempt, so the pattern is visible rather than inferred.

Also tries the "Query View Crosstab Excel" endpoint alongside "Query View
Data": they are different server-side code paths (detail/summary rows vs.
a pivoted crosstab), and a dashboard view may behave differently under
one than the other.

Prints, for each endpoint and Accept variant tried: the endpoint used,
the Accept header sent, HTTP status, elapsed time, response Content-Type,
a small allowlist of safe response headers, and a truncated/sanitized
error body. Never prints credentials, the auth token, cookies, or row
data.

Usage: python diagnose_extraction.py
(requires TABLEAU_VIEW_ID to be set -- see discover_view.py)
"""

from __future__ import annotations

import sys

from src.exportbot.auth import AuthenticationError, TableauServerAuthenticator, redact_secrets
from src.exportbot.config import Config, ConfigError, load_config, require_view_id
from src.exportbot.exceptions import ExtractionError
from src.exportbot.extraction_diagnostics import (
    ACCEPT_HEADER_VARIANTS,
    ENDPOINT_PATHS,
    probe_endpoint,
)
from src.exportbot.serverinfo import discover_api_version


def run_diagnosis(config: Config, view_id: str) -> tuple[bool, list[str]]:
    """Authenticate once, then probe each candidate endpoint with each
    Accept header variant. Returns (success, lines) -- lines is one
    sanitized summary per attempt; success is False only if we couldn't
    even authenticate or resolve the API version."""
    try:
        api_version = config.tableau_api_version or discover_api_version(config.tableau_server_url)
    except ExtractionError as exc:
        return False, [redact_secrets(f"Could not determine API version: {exc}", config)]

    try:
        session = TableauServerAuthenticator(api_version=api_version).authenticate(config)
    except AuthenticationError as exc:
        return False, [redact_secrets(str(exc), config)]

    lines = []
    for path_template, description in ENDPOINT_PATHS:
        path = path_template.format(view_id=view_id)
        lines.append(f"--- {description} ({path}) ---")
        for accept in ACCEPT_HEADER_VARIANTS:
            result = probe_endpoint(session, path, accept)
            line = (
                f"Accept={result.accept_header_sent}: HTTP {result.status_code} "
                f"in {result.elapsed_seconds:.1f}s, content-type={result.content_type or 'n/a'}, "
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
