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
    tableau_api_version: str
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
        tableau_api_version=os.environ.get("TABLEAU_API_VERSION", "").strip(),
        view_id=os.environ.get("TABLEAU_VIEW_ID", "").strip(),
        export_dir=os.environ.get("EXPORT_DIR", "exports"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )


def require_view_id(config: Config) -> str:
    """Raise ConfigError if view_id isn't set or looks malformed.

    Kept separate from load_config() so tools that only need to
    authenticate (e.g. auth_smoke_test.py) aren't forced to have
    TABLEAU_VIEW_ID configured -- it's only needed once extraction starts.

    Also guards against a real observed .env mistake: pasting the whole
    "TABLEAU_VIEW_ID=<guid>" line suggested by discover_view.py's output
    as the *value* of an existing TABLEAU_VIEW_ID= entry, producing
    TABLEAU_VIEW_ID=TABLEAU_VIEW_ID=<guid> in .env. That value would
    otherwise flow untouched into the request URL
    (.../views/TABLEAU_VIEW_ID=<guid>/data) and fail far from the cause.
    A real view id is a bare GUID and never contains '=' or its own
    variable name.
    """
    if not config.view_id:
        raise ConfigError("Missing required environment variable: TABLEAU_VIEW_ID")

    if "=" in config.view_id or "TABLEAU_VIEW_ID" in config.view_id.upper():
        raise ConfigError(
            f"TABLEAU_VIEW_ID looks malformed: '{config.view_id}'. This usually "
            "means .env has the variable name pasted into its own value, e.g. "
            "TABLEAU_VIEW_ID=TABLEAU_VIEW_ID=<guid> instead of just "
            "TABLEAU_VIEW_ID=<guid>. Fix .env to contain only the bare GUID."
        )

    return config.view_id
