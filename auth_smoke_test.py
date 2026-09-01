#!/usr/bin/env python3
"""Isolated Tableau authentication smoke test.

Confirms sign-in succeeds against the real server -- nothing else. Does
NOT retrieve view data. Never prints the username, password, auth token,
or cookies: only "SUCCESS" or a sanitized failure summary (HTTP status +
message, with any configured credential value redacted).

Usage: python auth_smoke_test.py
"""

from __future__ import annotations

import sys

from src.exportbot.auth import AuthenticationError, TableauServerAuthenticator, redact_secrets
from src.exportbot.config import Config, ConfigError, load_config
from src.exportbot.exceptions import ExtractionError
from src.exportbot.serverinfo import discover_api_version


def run_smoke_test(config: Config) -> tuple[bool, str]:
    """Attempt sign-in only. Returns (success, sanitized message)."""
    try:
        api_version = config.tableau_api_version or discover_api_version(config.tableau_server_url)
    except ExtractionError as exc:
        return False, redact_secrets(f"Could not determine API version: {exc}", config)

    try:
        TableauServerAuthenticator(api_version=api_version).authenticate(config)
    except AuthenticationError as exc:
        return False, redact_secrets(str(exc), config)

    return True, f"authenticated (api_version={api_version})"


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    success, message = run_smoke_test(config)

    if success:
        print(f"SUCCESS: {message}")
        return 0

    print(f"FAILURE: {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
