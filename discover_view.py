#!/usr/bin/env python3
"""Isolated Tableau view discovery: resolves ReturnRotate/Dashboard1 to its
real view GUID via the authenticated REST API. Does not extract data.

Never prints the username, password, auth token, or cookies -- only
SUCCESS with safe metadata (workbook/view names and ids) or a sanitized
failure summary.

Usage: python discover_view.py
"""

from __future__ import annotations

import sys

from src.exportbot.auth import AuthenticationError, TableauServerAuthenticator, redact_secrets
from src.exportbot.config import Config, ConfigError, load_config
from src.exportbot.exceptions import ExtractionError
from src.exportbot.serverinfo import discover_api_version
from src.exportbot.view_discovery import discover_view

# The specific dashboard this project targets:
# https://insight.erajaya.com/#/views/ReturnRotate/Dashboard1
WORKBOOK_CONTENT_URL = "ReturnRotate"
VIEW_CONTENT_URL = "Dashboard1"


def run_view_discovery(config: Config) -> tuple[bool, str]:
    """Authenticate, then resolve the target workbook/view.

    Returns (success, sanitized message). Never includes credentials or
    the auth token in the message.
    """
    try:
        api_version = config.tableau_api_version or discover_api_version(config.tableau_server_url)
    except ExtractionError as exc:
        return False, redact_secrets(f"Could not determine API version: {exc}", config)

    try:
        session = TableauServerAuthenticator(api_version=api_version).authenticate(config)
    except AuthenticationError as exc:
        return False, redact_secrets(str(exc), config)

    try:
        view = discover_view(session, WORKBOOK_CONTENT_URL, VIEW_CONTENT_URL)
    except ExtractionError as exc:
        return False, redact_secrets(str(exc), config)

    message = (
        f"workbook='{view.workbook_name}' (id={view.workbook_id}) "
        f"view='{view.view_name}' (id={view.view_id}) "
        f"-> set TABLEAU_VIEW_ID={view.view_id}"
    )
    return True, message


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    success, message = run_view_discovery(config)

    if success:
        print(f"SUCCESS: {message}")
        return 0

    print(f"FAILURE: {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
