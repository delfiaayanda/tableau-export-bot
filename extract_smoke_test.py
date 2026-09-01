#!/usr/bin/env python3
"""Isolated Tableau data-extraction smoke test: requests the confirmed
view's data and reports only safe metadata -- HTTP status, response
content type, row/column counts, and column names. Never prints
credentials, the auth token, cookies, or any row data.

Uses GET /api/{version}/sites/{site-id}/views/{view-id}/data (the same
endpoint tableau_client.TableauServerClient.get_view_data() calls),
Accept: text/csv. No alternative endpoint is attempted; if this one
fails, the exact sanitized status/error and the URL attempted are
reported.

Usage: python extract_smoke_test.py
(requires TABLEAU_VIEW_ID to be set -- see discover_view.py)
"""

from __future__ import annotations

import sys

from src.exportbot.auth import AuthenticationError, TableauServerAuthenticator, redact_secrets
from src.exportbot.config import Config, ConfigError, load_config, require_view_id
from src.exportbot.exceptions import ExtractionError
from src.exportbot.extraction_probe import probe_view_data
from src.exportbot.serverinfo import discover_api_version


def run_extraction_smoke_test(config: Config, view_id: str) -> tuple[bool, str]:
    """Authenticate, then request the view's data. Returns (success,
    sanitized message). Never includes credentials, the auth token, or
    row data in the message."""
    try:
        api_version = config.tableau_api_version or discover_api_version(config.tableau_server_url)
    except ExtractionError as exc:
        return False, redact_secrets(f"Could not determine API version: {exc}", config)

    try:
        session = TableauServerAuthenticator(api_version=api_version).authenticate(config)
    except AuthenticationError as exc:
        return False, redact_secrets(str(exc), config)

    try:
        result = probe_view_data(session, view_id)
    except ExtractionError as exc:
        return False, redact_secrets(str(exc), config)

    parts = [
        f"GET {result.url}",
        f"HTTP {result.status_code}",
        f"content-type={result.content_type}",
    ]
    if result.row_count is not None:
        parts.append(f"rows={result.row_count}")
        parts.append(f"columns={result.column_count}")
        parts.append(f"column_names={result.column_names}")
    else:
        parts.append("response body did not parse as tabular CSV data")

    return True, ", ".join(parts)


def main() -> int:
    try:
        config = load_config()
        view_id = require_view_id(config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    success, message = run_extraction_smoke_test(config, view_id)

    if success:
        print(f"SUCCESS: {message}")
        return 0

    print(f"FAILURE: {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
