"""Configuration loading from environment variables.

No credentials or server details are hardcoded. All values come from the
process environment (optionally populated from a local .env file via
python-dotenv during development).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    tableau_server_url: str
    tableau_site: str
    tableau_username: str
    tableau_password: str
    view_id: str
    export_dir: str
    log_level: str


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    """Build a Config from environment variables.

    Loads a .env file first if python-dotenv is installed and a .env file
    is present; real environment variables always take precedence.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:
        pass

    return Config(
        tableau_server_url=_require("TABLEAU_SERVER_URL"),
        tableau_site=os.environ.get("TABLEAU_SITE", ""),
        tableau_username=_require("TABLEAU_USERNAME"),
        tableau_password=_require("TABLEAU_PASSWORD"),
        view_id=_require("TABLEAU_VIEW_ID"),
        export_dir=os.environ.get("EXPORT_DIR", "exports"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
