"""Tests the browser automation module's pure logic and step functions
using lightweight fake Playwright-like objects -- no real browser, no
real network call. This does NOT prove the DOM selectors match the live
Tableau UI (impossible without a real browser and live access); it
proves our own orchestration, fallback-matching, file-saving, and
credential-handling logic is correct. See CLAUDE.md for what remains
unverified live.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.exportbot.browser_crosstab import (
    _export_crosstab_csv,
    _find_first,
    _login_if_needed,
    _wait_for_dashboard,
    generate_export_filename,
    resolve_executable_path,
    resolve_headless,
    run_browser_crosstab_export,
)
from src.exportbot.exceptions import BrowserAutomationError


class FakeLocator:
    def __init__(self, present=True):
        self._present = present
        self.click = MagicMock()
        self.fill = MagicMock()

    def count(self):
        return 1 if self._present else 0

    @property
    def first(self):
        return self


class _DownloadCM:
    def __init__(self, download):
        self._download = download

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def value(self):
        return self._download


class FakeDownload:
    def __init__(self):
        self.save_as = MagicMock()


class ScriptedPage:
    """Returns pre-scripted FakeLocators in call order, regardless of
    which get_by_*/locator method is used -- lets tests control exactly
    which candidate in an ordered fallback list "matches" without
    simulating real DOM text matching."""

    def __init__(self, script, download=None):
        self._script = list(script)
        self._pos = 0
        self.frames = []
        self._download = download
        self.wait_for_timeout = MagicMock()
        self.wait_for_load_state = MagicMock()
        self.goto = MagicMock()
        self.screenshot = MagicMock()

    def _next(self, *args, **kwargs):
        if self._pos >= len(self._script):
            return FakeLocator(present=False)
        loc = self._script[self._pos]
        self._pos += 1
        return loc

    def get_by_role(self, *args, **kwargs):
        return self._next()

    def get_by_text(self, *args, **kwargs):
        return self._next()

    def get_by_label(self, *args, **kwargs):
        return self._next()

    def locator(self, *args, **kwargs):
        return self._next()

    def expect_download(self, timeout=None):
        return _DownloadCM(self._download)


def test_generate_export_filename_format():
    name = generate_export_filename(datetime(2026, 9, 1, 14, 30, 45))
    assert name == "ReturnRotate_20260901_143045.csv"


def test_resolve_headless_defaults_true(monkeypatch):
    monkeypatch.delenv("HEADLESS", raising=False)
    assert resolve_headless() is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no"])
def test_resolve_headless_false_variants(monkeypatch, value):
    monkeypatch.setenv("HEADLESS", value)
    assert resolve_headless() is False


def test_resolve_executable_path_reads_env(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_EXECUTABLE_PATH", raising=False)
    assert resolve_executable_path() is None
    monkeypatch.setenv("PLAYWRIGHT_EXECUTABLE_PATH", "/usr/bin/chromium")
    assert resolve_executable_path() == "/usr/bin/chromium"


def test_find_first_returns_first_present_across_candidates():
    present = FakeLocator()
    absent = FakeLocator(present=False)
    result = _find_first([MagicMock()], [lambda s: absent, lambda s: present])
    assert result is present


def test_find_first_returns_none_when_nothing_matches():
    absent = FakeLocator(present=False)
    result = _find_first([MagicMock()], [lambda s: absent])
    assert result is None


def test_find_first_swallows_exceptions_from_factories():
    def bad(_scope):
        raise RuntimeError("boom")

    present = FakeLocator()
    result = _find_first([MagicMock()], [bad, lambda s: present])
    assert result is present


def test_login_if_needed_skips_when_no_password_field():
    page = ScriptedPage(script=[])
    _login_if_needed(page, "user", "pass")  # must not raise


def test_login_if_needed_fills_and_submits():
    password_field = FakeLocator()
    username_field = FakeLocator()
    submit_button = FakeLocator()
    page = ScriptedPage(script=[password_field, username_field, submit_button])

    _login_if_needed(page, "the-user", "the-pass")

    username_field.fill.assert_called_once_with("the-user")
    password_field.fill.assert_called_once_with("the-pass")
    submit_button.click.assert_called_once()


def test_login_if_needed_raises_when_username_field_missing():
    password_field = FakeLocator()
    page = ScriptedPage(script=[password_field])  # username absent
    with pytest.raises(BrowserAutomationError, match="username field"):
        _login_if_needed(page, "user", "pass")


def test_login_if_needed_never_prints_credentials(capsys):
    password_field = FakeLocator()
    username_field = FakeLocator()
    submit_button = FakeLocator()
    page = ScriptedPage(script=[password_field, username_field, submit_button])

    _login_if_needed(page, "super-secret-user", "super-secret-pass")

    captured = capsys.readouterr()
    assert "super-secret-user" not in captured.out
    assert "super-secret-pass" not in captured.out


def test_wait_for_dashboard_returns_when_button_present():
    page = ScriptedPage(script=[FakeLocator()])
    _wait_for_dashboard(page)  # must not raise


def test_wait_for_dashboard_raises_on_timeout(monkeypatch):
    import src.exportbot.browser_crosstab as bc

    monkeypatch.setattr(bc, "DASHBOARD_LOAD_TIMEOUT_MS", 10)
    page = ScriptedPage(script=[])  # never present
    with pytest.raises(BrowserAutomationError, match="Timed out"):
        _wait_for_dashboard(page)


def test_export_crosstab_csv_full_flow_saves_file(tmp_path):
    download_btn = FakeLocator()
    data_item = FakeLocator()
    crosstab_tab = FakeLocator()
    export_btn = FakeLocator()
    download = FakeDownload()
    page = ScriptedPage(script=[download_btn, data_item, crosstab_tab, export_btn], download=download)

    result = _export_crosstab_csv(page, tmp_path)

    download_btn.click.assert_called_once()
    data_item.click.assert_called_once()
    crosstab_tab.click.assert_called_once()
    export_btn.click.assert_called_once()
    download.save_as.assert_called_once()

    saved_path = download.save_as.call_args.args[0]
    assert saved_path.startswith(str(tmp_path))
    assert saved_path.endswith(".csv")
    assert "ReturnRotate_" in saved_path
    assert result == tmp_path / Path(saved_path).name


def test_export_crosstab_csv_raises_and_screenshots_when_download_button_missing(tmp_path):
    page = ScriptedPage(script=[])
    with pytest.raises(BrowserAutomationError, match="Download toolbar button"):
        _export_crosstab_csv(page, tmp_path)
    page.screenshot.assert_called_once()


def test_export_crosstab_csv_proceeds_without_optional_crosstab_tab(tmp_path):
    """The crosstab tab click is best-effort -- if it's not found, export
    should still proceed using whatever tab is already selected."""
    download_btn = FakeLocator()
    data_item = FakeLocator()
    export_btn = FakeLocator()
    download = FakeDownload()
    # After download_btn and data_item are each found on their first
    # candidate, the crosstab-tab lookup tries all 3 of its candidates
    # (must all come back absent here) before export_btn is found on the
    # export lookup's first candidate.
    page = ScriptedPage(
        script=[
            download_btn,
            data_item,
            FakeLocator(present=False),
            FakeLocator(present=False),
            FakeLocator(present=False),
            export_btn,
        ],
        download=download,
    )

    result = _export_crosstab_csv(page, tmp_path)

    export_btn.click.assert_called_once()
    assert result.name.startswith("ReturnRotate_")


def test_run_browser_crosstab_export_orchestrates_and_closes_browser(tmp_path):
    fake_browser = MagicMock()
    fake_context = MagicMock()
    fake_page = MagicMock()
    fake_context.new_page.return_value = fake_page
    fake_browser.new_context.return_value = fake_context

    fake_chromium = MagicMock()
    fake_chromium.launch.return_value = fake_browser

    fake_driver = MagicMock()
    fake_driver.chromium = fake_chromium

    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_driver
    fake_cm.__exit__.return_value = False

    expected_path = tmp_path / "ReturnRotate_20260101_000000.csv"

    with patch("playwright.sync_api.sync_playwright", return_value=fake_cm):
        with patch("src.exportbot.browser_crosstab._login_if_needed") as mock_login:
            with patch("src.exportbot.browser_crosstab._wait_for_dashboard") as mock_wait:
                with patch(
                    "src.exportbot.browser_crosstab._export_crosstab_csv", return_value=expected_path
                ) as mock_export:
                    result = run_browser_crosstab_export(
                        server_url="https://insight.example.com",
                        username="user",
                        password="pass",
                        export_dir=str(tmp_path),
                        headless=True,
                    )

    assert result == expected_path
    fake_page.goto.assert_any_call(
        "https://insight.example.com", wait_until="domcontentloaded", timeout=30_000
    )
    mock_login.assert_called_once_with(fake_page, "user", "pass")
    mock_wait.assert_called_once_with(fake_page)
    mock_export.assert_called_once_with(fake_page, tmp_path)
    fake_browser.close.assert_called_once()
    fake_chromium.launch.assert_called_once_with(headless=True)


def test_run_browser_crosstab_export_passes_executable_path(tmp_path):
    fake_browser = MagicMock()
    fake_context = MagicMock()
    fake_page = MagicMock()
    fake_context.new_page.return_value = fake_page
    fake_browser.new_context.return_value = fake_context
    fake_chromium = MagicMock()
    fake_chromium.launch.return_value = fake_browser
    fake_driver = MagicMock()
    fake_driver.chromium = fake_chromium
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_driver
    fake_cm.__exit__.return_value = False

    with patch("playwright.sync_api.sync_playwright", return_value=fake_cm):
        with patch("src.exportbot.browser_crosstab._login_if_needed"):
            with patch("src.exportbot.browser_crosstab._wait_for_dashboard"):
                with patch(
                    "src.exportbot.browser_crosstab._export_crosstab_csv",
                    return_value=tmp_path / "x.csv",
                ):
                    run_browser_crosstab_export(
                        server_url="https://insight.example.com",
                        username="user",
                        password="pass",
                        export_dir=str(tmp_path),
                        headless=False,
                        executable_path="/usr/bin/chromium-browser",
                    )

    fake_chromium.launch.assert_called_once_with(
        headless=False, executable_path="/usr/bin/chromium-browser"
    )
