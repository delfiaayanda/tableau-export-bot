import pytest

from src.exportbot.config import ConfigError, load_config

REQUIRED_VARS = {
    "TABLEAU_SERVER_URL": "https://insight.example.com",
    "TABLEAU_USERNAME": "someuser",
    "TABLEAU_PASSWORD": "somepassword",
    "TABLEAU_VIEW_ID": "some-view-id",
}


def _clear_env(monkeypatch):
    for key in [*REQUIRED_VARS, "TABLEAU_SITE", "EXPORT_DIR", "LOG_LEVEL"]:
        monkeypatch.delenv(key, raising=False)


def test_load_config_missing_required_var_raises(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TABLEAU_SERVER_URL", REQUIRED_VARS["TABLEAU_SERVER_URL"])

    with pytest.raises(ConfigError, match="TABLEAU_USERNAME"):
        load_config()


def test_load_config_reads_all_required_vars(monkeypatch):
    _clear_env(monkeypatch)
    for key, value in REQUIRED_VARS.items():
        monkeypatch.setenv(key, value)

    config = load_config()

    assert config.tableau_server_url == REQUIRED_VARS["TABLEAU_SERVER_URL"]
    assert config.tableau_username == REQUIRED_VARS["TABLEAU_USERNAME"]
    assert config.tableau_password == REQUIRED_VARS["TABLEAU_PASSWORD"]
    assert config.view_id == REQUIRED_VARS["TABLEAU_VIEW_ID"]
    assert config.tableau_site == ""
    assert config.export_dir == "exports"
    assert config.log_level == "INFO"


def test_load_config_applies_optional_overrides(monkeypatch):
    _clear_env(monkeypatch)
    for key, value in REQUIRED_VARS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TABLEAU_SITE", "myproject")
    monkeypatch.setenv("EXPORT_DIR", "/tmp/custom-exports")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    config = load_config()

    assert config.tableau_site == "myproject"
    assert config.export_dir == "/tmp/custom-exports"
    assert config.log_level == "DEBUG"


def test_load_config_rejects_blank_value(monkeypatch):
    _clear_env(monkeypatch)
    for key, value in REQUIRED_VARS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TABLEAU_PASSWORD", "   ")

    with pytest.raises(ConfigError, match="TABLEAU_PASSWORD"):
        load_config()
