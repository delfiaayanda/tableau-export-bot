#!/usr/bin/env python3
"""CLI entry point: python main.py"""

from __future__ import annotations

import sys

from src.exportbot.auth import TableauServerAuthenticator
from src.exportbot.config import ConfigError, load_config
from src.exportbot.exceptions import ExportBotError
from src.exportbot.logging_setup import setup_logging
from src.exportbot.pipeline import run_pipeline
from src.exportbot.serverinfo import discover_api_version
from src.exportbot.tableau_client import TableauServerClient


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logging(config.log_level)

    try:
        if config.tableau_api_version:
            api_version = config.tableau_api_version
            logger.info("Using configured Tableau API version: %s", api_version)
        else:
            api_version = discover_api_version(config.tableau_server_url)
            logger.info("Discovered Tableau API version via serverinfo: %s", api_version)

        run_pipeline(
            config=config,
            authenticator=TableauServerAuthenticator(api_version=api_version),
            client_factory=lambda session: TableauServerClient(session),
            logger=logger,
        )
    except ExportBotError as exc:
        logger.error("Export failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
