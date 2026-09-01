"""Tests the auth -> extraction -> validation -> export wiring using fake
Authenticator/TableauClient test doubles. These do NOT simulate real
Tableau API responses; they only prove the pipeline calls its
collaborators in the right order and handles their outputs correctly.
Real Tableau connectivity is untested here by design (see CLAUDE.md).
"""

import logging

import pandas as pd
import pytest

from src.exportbot.auth import Authenticator, TableauSession
from src.exportbot.config import Config, ConfigError
from src.exportbot.exceptions import ValidationError
from src.exportbot.pipeline import run_pipeline
from src.exportbot.tableau_client import TableauClient


class FakeAuthenticator(Authenticator):
    def authenticate(self, config: Config) -> TableauSession:
        return TableauSession(
            auth_token="fake-token",
            site_id="fake-site",
            server_url=config.tableau_server_url,
            api_version="3.22",
        )


class FakeTableauClient(TableauClient):
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def get_view_data(self, view_id: str) -> pd.DataFrame:
        return self._df


def _make_config(export_dir: str, view_id: str = "view-1") -> Config:
    return Config(
        tableau_server_url="https://insight.example.com",
        tableau_site="",
        tableau_username="user",
        tableau_password="pass",
        tableau_api_version="",
        view_id=view_id,
        export_dir=export_dir,
        log_level="INFO",
    )


def test_run_pipeline_exports_valid_data(tmp_path):
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    config = _make_config(str(tmp_path))
    logger = logging.getLogger("test")

    output_path = run_pipeline(
        config=config,
        authenticator=FakeAuthenticator(),
        client_factory=lambda session: FakeTableauClient(df),
        logger=logger,
    )

    assert output_path.exists()


def test_run_pipeline_raises_on_invalid_data(tmp_path):
    empty_df = pd.DataFrame()
    config = _make_config(str(tmp_path))
    logger = logging.getLogger("test")

    with pytest.raises(ValidationError):
        run_pipeline(
            config=config,
            authenticator=FakeAuthenticator(),
            client_factory=lambda session: FakeTableauClient(empty_df),
            logger=logger,
        )


def test_run_pipeline_requires_view_id_before_authenticating(tmp_path):
    """The full pipeline still needs TABLEAU_VIEW_ID -- unlike the auth
    smoke test, which never calls run_pipeline at all."""
    config = _make_config(str(tmp_path), view_id="")
    logger = logging.getLogger("test")

    class AuthenticatorThatMustNotBeCalled(Authenticator):
        def authenticate(self, config: Config) -> TableauSession:
            raise AssertionError("authenticate() should not be called before view_id is checked")

    with pytest.raises(ConfigError, match="TABLEAU_VIEW_ID"):
        run_pipeline(
            config=config,
            authenticator=AuthenticatorThatMustNotBeCalled(),
            client_factory=lambda session: FakeTableauClient(pd.DataFrame()),
            logger=logger,
        )
