#!/usr/bin/env python3
"""Browser-automated Tableau Crosstab CSV export (Playwright).

Reproduces the confirmed manual workflow -- Download -> Data -> Crosstab
-> CSV download -- via an actual browser, because the REST/VizQL
investigation (see CLAUDE.md) could not establish a session without one.

Requires Playwright and a Chromium (or other) browser binary installed
locally. See README.md "Browser automation setup" for exact commands.
NOT runnable from this development sandbox: no browser binary can be
installed here and network access to insight.erajaya.com is blocked.

Usage: python browser_crosstab.py
Environment:
  TABLEAU_SERVER_URL, TABLEAU_USERNAME, TABLEAU_PASSWORD, EXPORT_DIR
                                  (from .env, same as the rest of the project)
  HEADLESS=false                 run with a visible browser window (debugging)
  PLAYWRIGHT_EXECUTABLE_PATH=... use an existing Chrome/Edge install instead
                                  of Playwright's own Chromium
"""

from __future__ import annotations

import sys

from src.exportbot.browser_crosstab import (
    resolve_executable_path,
    resolve_headless,
    run_browser_crosstab_export,
)
from src.exportbot.config import ConfigError, load_config
from src.exportbot.exceptions import BrowserAutomationError


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        result_path = run_browser_crosstab_export(
            server_url=config.tableau_server_url,
            username=config.tableau_username,
            password=config.tableau_password,
            export_dir=config.export_dir,
            headless=resolve_headless(),
            executable_path=resolve_executable_path(),
        )
    except BrowserAutomationError as exc:
        print(f"FAILURE: {exc}", file=sys.stderr)
        return 1
    except ImportError:
        print(
            "FAILURE: Playwright is not installed. Run: "
            "pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return 2

    print(f"SUCCESS: {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
