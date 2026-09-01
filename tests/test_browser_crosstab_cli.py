"""Tests the browser_crosstab.py CLI's config/success/failure reporting,
mocking run_browser_crosstab_export entirely -- no real browser, no real
network call. Confirms credentials never appear in output.
"""

from unittest.mock import patch

from browser_crosstab import main
from src.exportbot.exceptions import BrowserAutomationError

USERNAME = "smoke-test-user"
PASSWORD = "smoke-test-password"


def _set_required_env(monkeypatch, export_dir):
    monkeypatch.setenv("TABLEAU_SERVER_URL", "https://insight.example.com")
    monkeypatch.setenv("TABLEAU_USERNAME", USERNAME)
    monkeypatch.setenv("TABLEAU_PASSWORD", PASSWORD)
    monkeypatch.setenv("EXPORT_DIR", str(export_dir))


def test_main_returns_config_error_when_env_missing(monkeypatch, capsys):
    for key in ("TABLEAU_SERVER_URL", "TABLEAU_USERNAME", "TABLEAU_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    exit_code = main()

    assert exit_code == 2
    assert "Configuration error" in capsys.readouterr().err


def test_main_reports_success(monkeypatch, tmp_path, capsys):
    _set_required_env(monkeypatch, tmp_path)
    expected_path = tmp_path / "ReturnRotate_20260101_000000.csv"

    with patch("browser_crosstab.run_browser_crosstab_export", return_value=expected_path) as mock_run:
        exit_code = main()

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"SUCCESS: {expected_path}" in out
    assert USERNAME not in out
    assert PASSWORD not in out

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["username"] == USERNAME
    assert call_kwargs["password"] == PASSWORD


def test_main_reports_failure_without_leaking_credentials(monkeypatch, tmp_path, capsys):
    _set_required_env(monkeypatch, tmp_path)

    with patch(
        "browser_crosstab.run_browser_crosstab_export",
        side_effect=BrowserAutomationError("Could not find the Download toolbar button."),
    ):
        exit_code = main()

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "FAILURE" in err
    assert USERNAME not in err
    assert PASSWORD not in err


def test_main_reports_missing_playwright_install(monkeypatch, tmp_path, capsys):
    _set_required_env(monkeypatch, tmp_path)

    with patch("browser_crosstab.run_browser_crosstab_export", side_effect=ImportError("no playwright")):
        exit_code = main()

    assert exit_code == 2
    assert "playwright install" in capsys.readouterr().err
