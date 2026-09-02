#!/usr/bin/env python3
"""Isolated probe for the documented REST "Query View Crosstab Excel"
endpoint -- one concrete, testable hypothesis for extracting Return &
Rotate's table data using our existing authenticated REST session.

This is NOT a confirmed match for what Tableau's browser "Download ->
Crosstab" menu action actually does -- that requires a DevTools network
capture (see CLAUDE.md for exact steps). The browser's interactive web
client typically uses its own internal, cookie-session-based endpoints
for UI download actions, separate from the public REST API this probe
calls.

Does NOT save the response and never inspects its contents. Reports only
safe metadata: HTTP status, elapsed time, Content-Type,
Content-Disposition, response size, and filename if available. Never
prints credentials, cookies, the auth token, or row/file data.

Usage: python crosstab_probe.py
(requires TABLEAU_VIEW_ID to be set -- see discover_view.py)
"""

from __future__ import annotations

import sys

from src.exportbot.auth import AuthenticationError, TableauServerAuthenticator, redact_secrets
from src.exportbot.config import Config, ConfigError, load_config, require_view_id
from src.exportbot.crosstab_probe import probe_crosstab_excel
from src.exportbot.exceptions import ExtractionError
from src.exportbot.serverinfo import discover_api_version


def run_crosstab_probe(config: Config, view_id: str) -> tuple[bool, str]:
    """Authenticate, then probe the crosstab-Excel endpoint. Returns
    (success, sanitized message). Never includes credentials, the auth
    token, or file contents."""
    try:
        api_version = config.tableau_api_version or discover_api_version(config.tableau_server_url)
    except ExtractionError as exc:
        return False, redact_secrets(f"Could not determine API version: {exc}", config)

    try:
        session = TableauServerAuthenticator(api_version=api_version).authenticate(config)
    except AuthenticationError as exc:
        return False, redact_secrets(str(exc), config)

    result = probe_crosstab_excel(session, view_id)

    parts = [
        f"GET {result.url}",
        f"HTTP {result.status_code} in {result.elapsed_seconds:.1f}s",
        f"content-type={result.content_type or 'n/a'}",
        f"content-disposition={result.content_disposition or 'n/a'}",
        f"filename={result.filename or 'n/a'}",
    ]
    if result.response_size_bytes is not None:
        parts.append(f"size_bytes={result.response_size_bytes}")
    if result.error_body:
        parts.append(f"error_body={result.error_body!r}")

    return result.status_code == 200, ", ".join(parts)


def main() -> int:
    try:
        config = load_config()
        view_id = require_view_id(config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    success, message = run_crosstab_probe(config, view_id)

    if success:
        print(f"SUCCESS: {message}")
        return 0

    print(f"FAILURE: {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
