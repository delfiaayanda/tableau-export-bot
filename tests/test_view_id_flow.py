"""Diagnoses the reported bug: a request URL containing
"/views/TABLEAU_VIEW_ID=<guid>/data" instead of "/views/<guid>/data".

Traces the value end-to-end (os.getenv -> Config -> request URL) with a
synthetic test GUID -- never the user's real value -- to prove our own
code passes it through unmodified, and that require_view_id() now catches
the malformed shape before it ever reaches the HTTP layer.
"""

import os
from unittest.mock import patch

import pytest

from src.exportbot.auth import TableauSession
from src.exportbot.config import ConfigError, load_config, require_view_id
from src.exportbot.extraction_probe import probe_view_data

TEST_GUID = "11111111-2222-3333-4444-555555555555"

REQUIRED_VARS = {
    "TABLEAU_SERVER_URL": "https://insight.example.com",
    "TABLEAU_USERNAME": "someuser",
    "TABLEAU_PASSWORD": "somepassword",
}


class FakeResponse:
    def __init__(self, status_code=200, text="a\n1\n", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"Content-Type": "text/csv"}


def _set_required_env(monkeypatch):
    for key in [*REQUIRED_VARS, "TABLEAU_VIEW_ID"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_VARS.items():
        monkeypatch.setenv(key, value)


def test_view_id_flows_unmodified_from_env_to_request_url(monkeypatch):
    """os.getenv -> Config -> URL, each checked in turn against a known
    synthetic GUID with no '=' or extra characters introduced anywhere."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("TABLEAU_VIEW_ID", TEST_GUID)

    # 1. os.getenv sees exactly the GUID.
    assert os.getenv("TABLEAU_VIEW_ID") == TEST_GUID

    # 2. load_config() carries it through unmodified (field is `view_id`).
    config = load_config()
    assert config.view_id == TEST_GUID

    view_id = require_view_id(config)
    assert view_id == TEST_GUID

    # 3. The request URL uses the bare GUID, not "TABLEAU_VIEW_ID=<guid>".
    session = TableauSession(
        auth_token="fake-token",
        site_id="site-1",
        server_url="https://insight.example.com",
        api_version="3.29",
    )
    with patch("src.exportbot.extraction_probe.requests.get", return_value=FakeResponse()) as mock_get:
        probe_view_data(session, view_id)

    called_url = mock_get.call_args.args[0]
    assert called_url == "https://insight.example.com/api/3.29/sites/site-1/views/11111111-2222-3333-4444-555555555555/data"
    assert "TABLEAU_VIEW_ID=" not in called_url


def test_require_view_id_rejects_the_exact_reported_paste_mistake(monkeypatch):
    """Reproduces the reported symptom directly: TABLEAU_VIEW_ID in the
    environment already containing 'TABLEAU_VIEW_ID=<guid>' as its value
    (e.g. discover_view.py's suggested line pasted as the value of an
    existing TABLEAU_VIEW_ID= entry in .env). require_view_id() must
    catch this before it reaches any URL, instead of silently building
    ".../views/TABLEAU_VIEW_ID=<guid>/data"."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("TABLEAU_VIEW_ID", f"TABLEAU_VIEW_ID={TEST_GUID}")

    config = load_config()
    assert config.view_id == f"TABLEAU_VIEW_ID={TEST_GUID}"  # loaded as-is, unmodified

    with pytest.raises(ConfigError, match="malformed"):
        require_view_id(config)


def test_require_view_id_rejects_any_value_containing_equals(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("TABLEAU_VIEW_ID", "foo=bar")

    config = load_config()

    with pytest.raises(ConfigError, match="malformed"):
        require_view_id(config)
