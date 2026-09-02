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
    OVERLAY_SELECTORS,
    _debug_dump_view_data_popup,
    _export_crosstab_csv,
    _export_from_view_data_popup,
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
        self.wait_for = MagicMock()

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


class _PopupCM:
    """Mirrors how Playwright's expect_popup() actually behaves: the
    popup wait resolves (or times out) when the `with` block exits, not
    when .value is later read -- so a popup that never opens raises here,
    in __exit__, the same place the real Playwright TimeoutError would."""

    def __init__(self, popup):
        self._popup = popup

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None and self._popup is None:
            raise TimeoutError("Timeout waiting for popup")
        return False

    @property
    def value(self):
        return self._popup


class FakeDownload:
    def __init__(self):
        self.save_as = MagicMock()


class ScriptedPage:
    """Returns pre-scripted FakeLocators in call order, regardless of
    which get_by_*/locator method is used -- lets tests control exactly
    which candidate in an ordered fallback list "matches" without
    simulating real DOM text matching."""

    def __init__(self, script, download=None, popup=None):
        self._script = list(script)
        self._pos = 0
        self.frames = []
        self._download = download
        self._popup = popup
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

    def expect_popup(self, timeout=None):
        return _PopupCM(self._popup)


class ViewDataPopupPage:
    """Fake for the "View Data" popup window opened by Download -> Data:
    provides its own Download button (findable via get_by_role, matching
    VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES' first candidate), plus
    dedicated, independently-controllable overlay locators for each
    selector in OVERLAY_SELECTORS -- reachable only via .locator(), never
    consumed by the role-based button lookup."""

    def __init__(self, overlay_wait_side_effect=None, download=None, button_present=True):
        self.frames = []
        self.url = "https://insight.example.com/vizql/v_test/hybrid-window/min/index.html"
        self.download_button = FakeLocator(present=button_present)
        self.wait_for_timeout = MagicMock()
        self.wait_for_load_state = MagicMock()
        self.goto = MagicMock()
        self.screenshot = MagicMock()
        self._download = download

        def _title():
            return "View Data: Test Sheet"

        self.title = _title
        self.evaluate = MagicMock(return_value=[])

        self.overlay_locators = {}
        for selector in OVERLAY_SELECTORS:
            locator = FakeLocator()
            if overlay_wait_side_effect is not None:
                locator.wait_for = MagicMock(side_effect=overlay_wait_side_effect)
            self.overlay_locators[selector] = locator

    def get_by_role(self, role, name=None, **kwargs):
        if name is not None:
            return self.download_button
        return FakeLocator(present=False)

    def get_by_text(self, *args, **kwargs):
        return FakeLocator(present=False)

    def get_by_label(self, *args, **kwargs):
        return FakeLocator(present=False)

    def locator(self, selector, **kwargs):
        if selector in self.overlay_locators:
            return self.overlay_locators[selector]
        return FakeLocator(present=False)

    def expect_download(self, timeout=None):
        return _DownloadCM(self._download)


class DebugDumpPage:
    """Fake for _debug_dump_view_data_popup()'s target page: exposes
    .url, .title(), .screenshot(), and .evaluate() (dispatching the three
    expected calls -- buttons, download-text elements, toolbar HTML -- by
    call order), each independently able to raise to prove the dump never
    lets a diagnostic failure break the caller."""

    def __init__(
        self,
        url="https://insight.example.com/vizql/v_test/hybrid-window/min/index.html",
        title="View Data: Persentase Nasional",
        buttons=None,
        download_elements=None,
        toolbar_html=None,
        title_raises=False,
        screenshot_raises=False,
        evaluate_raises=False,
    ):
        self.url = url
        self._title = title
        self._buttons = buttons if buttons is not None else []
        self._download_elements = download_elements if download_elements is not None else []
        self._toolbar_html = toolbar_html
        self._title_raises = title_raises
        self._evaluate_raises = evaluate_raises
        self._evaluate_call_count = 0
        self.screenshot = MagicMock(
            side_effect=RuntimeError("screenshot failed") if screenshot_raises else None
        )

    def title(self):
        if self._title_raises:
            raise RuntimeError("title failed")
        return self._title

    def evaluate(self, script):
        if self._evaluate_raises:
            raise RuntimeError("evaluate failed")
        self._evaluate_call_count += 1
        if self._evaluate_call_count == 1:
            return self._buttons
        if self._evaluate_call_count == 2:
            return self._download_elements
        return self._toolbar_html


def test_debug_dump_view_data_popup_prints_diagnostics_and_saves_screenshot(tmp_path, capsys):
    buttons = [
        {
            "tag": "BUTTON",
            "inner_text": "Download",
            "aria_label": "Download",
            "role": None,
            "data_tb_test_id": "download-Button",
            "title": None,
        }
    ]
    download_elements = [
        {
            "tag": "DIV",
            "text": "Download",
            "aria_label": None,
            "title": None,
            "data_tb_test_id": None,
            "class_name": "toolbar-item",
        }
    ]
    toolbar_html = {
        "element_html": "<div>Download</div>",
        "parent_html": '<div class="toolbar">...</div>',
        "grandparent_html": None,
    }
    page = DebugDumpPage(buttons=buttons, download_elements=download_elements, toolbar_html=toolbar_html)

    _debug_dump_view_data_popup(page, tmp_path)

    page.screenshot.assert_called_once_with(path=str(tmp_path / "debug_view_data_popup.png"))
    out = capsys.readouterr().out
    assert "popup.url" in out
    assert "View Data: Persentase Nasional" in out
    assert "download-Button" in out
    assert "toolbar-item" in out
    assert "<div class=\"toolbar\">" in out


def test_debug_dump_view_data_popup_never_raises_despite_every_step_failing(tmp_path):
    page = DebugDumpPage(title_raises=True, screenshot_raises=True, evaluate_raises=True)
    _debug_dump_view_data_popup(page, tmp_path)  # must not raise


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


class DelayedFieldPage:
    """Simulates a login form that only appears in the DOM after a
    couple of polling ticks -- reproduces the actual reported bug: the
    SPA hadn't finished rendering the login form at the instant the
    original code checked for it (a single Locator.count() call, which
    does not wait), so detection silently reported no login form at all
    and the whole login step was skipped."""

    def __init__(self, appears_after_ticks=2):
        self._ticks = 0
        self._appears_after = appears_after_ticks
        self.frames = []
        self.password_field = FakeLocator()
        self.username_field = FakeLocator()
        self.submit_button = FakeLocator()
        self.wait_for_timeout = MagicMock(side_effect=self._tick)
        self.wait_for_load_state = MagicMock()
        self.goto = MagicMock()
        self.screenshot = MagicMock()

    def _tick(self, *args, **kwargs):
        self._ticks += 1

    def _ready(self):
        return self._ticks >= self._appears_after

    def get_by_label(self, pattern, **kwargs):
        text = pattern.pattern.lower() if hasattr(pattern, "pattern") else str(pattern).lower()
        if not self._ready():
            return FakeLocator(present=False)
        if "password" in text:
            return self.password_field
        if "username" in text:
            return self.username_field
        return FakeLocator(present=False)

    def get_by_role(self, role, name=None, **kwargs):
        if not self._ready() or name is None:
            return FakeLocator(present=False)
        text = name.pattern.lower() if hasattr(name, "pattern") else str(name).lower()
        if "sign" in text:
            return self.submit_button
        return FakeLocator(present=False)

    def get_by_text(self, *args, **kwargs):
        return FakeLocator(present=False)

    def locator(self, *args, **kwargs):
        return FakeLocator(present=False)


def test_login_if_needed_skips_when_no_password_field(monkeypatch):
    import src.exportbot.browser_crosstab as bc

    monkeypatch.setattr(bc, "LOGIN_DETECT_TIMEOUT_MS", 10)
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


def test_login_if_needed_raises_when_username_field_missing(monkeypatch):
    import src.exportbot.browser_crosstab as bc

    monkeypatch.setattr(bc, "LOGIN_DETECT_TIMEOUT_MS", 10)
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


def test_login_if_needed_waits_for_slow_rendering_login_form():
    """The actual reported bug: the login form isn't in the DOM yet at
    the instant of the first check (SPA still rendering). Detection must
    poll/wait rather than check once, or login gets silently skipped."""
    page = DelayedFieldPage(appears_after_ticks=2)

    _login_if_needed(page, "the-user", "the-pass")

    page.username_field.fill.assert_called_once_with("the-user")
    page.password_field.fill.assert_called_once_with("the-pass")
    page.submit_button.click.assert_called_once()


def test_login_if_needed_waits_for_form_to_disappear_after_submit():
    password_field = FakeLocator()
    username_field = FakeLocator()
    submit_button = FakeLocator()
    page = ScriptedPage(script=[password_field, username_field, submit_button])

    _login_if_needed(page, "user", "pass")

    password_field.wait_for.assert_called_once_with(state="hidden", timeout=20_000)


def test_wait_for_dashboard_returns_when_button_present():
    page = ScriptedPage(script=[FakeLocator()])
    _wait_for_dashboard(page)  # must not raise


def test_wait_for_dashboard_raises_on_timeout(monkeypatch):
    import src.exportbot.browser_crosstab as bc

    monkeypatch.setattr(bc, "DASHBOARD_LOAD_TIMEOUT_MS", 10)
    page = ScriptedPage(script=[])  # never present
    with pytest.raises(BrowserAutomationError, match="Timed out"):
        _wait_for_dashboard(page)


def test_export_crosstab_csv_full_flow_saves_file_via_popup(tmp_path):
    """End-to-end through both _export_crosstab_csv() and
    _export_from_view_data_popup(): clicking Download -> Data opens a
    separate "View Data" popup page, and the actual CSV export happens
    entirely within it -- confirmed via a real local run and screenshot,
    not the old (incorrect) assumption of an in-page Crosstab tab."""
    download_btn = FakeLocator()
    data_item = FakeLocator()
    download = FakeDownload()
    popup = ViewDataPopupPage(download=download)
    page = ScriptedPage(script=[download_btn, data_item], popup=popup)

    result = _export_crosstab_csv(page, tmp_path)

    download_btn.click.assert_called_once()
    data_item.click.assert_called_once()
    popup.download_button.click.assert_called_once()
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


def test_export_crosstab_csv_raises_when_data_menuitem_missing(tmp_path):
    download_btn = FakeLocator()
    page = ScriptedPage(script=[download_btn])  # data item absent
    with pytest.raises(BrowserAutomationError, match="'Data' item"):
        _export_crosstab_csv(page, tmp_path)
    page.screenshot.assert_called_once()


def test_export_crosstab_csv_raises_when_view_data_popup_does_not_open(tmp_path):
    """If clicking "Data" never actually opens the "View Data" popup
    (e.g. a slow/blocked window.open), the raw Playwright TimeoutError
    must not leak out uncaught."""
    download_btn = FakeLocator()
    data_item = FakeLocator()
    page = ScriptedPage(script=[download_btn, data_item], popup=None)

    with pytest.raises(BrowserAutomationError, match="View Data' popup window did not open"):
        _export_crosstab_csv(page, tmp_path)
    page.screenshot.assert_called_once()


def test_export_from_view_data_popup_waits_for_overlays_before_click(tmp_path):
    """Reproduces the real reported bug: the popup's Download button is
    visible/enabled/stable but a Tableau glass-pane/dialog overlay
    intercepts pointer events, so Playwright's click() retries and times
    out. The fix must explicitly wait for each known overlay to clear
    (via Locator.wait_for(state="hidden")) before attempting the click."""
    download = FakeDownload()
    popup = ViewDataPopupPage(download=download)  # overlays resolve immediately (default MagicMock)

    result = _export_from_view_data_popup(popup, tmp_path)

    for selector in OVERLAY_SELECTORS:
        popup.overlay_locators[selector].wait_for.assert_called_once_with(
            state="hidden", timeout=30_000
        )
    popup.download_button.click.assert_called_once()
    download.save_as.assert_called_once()
    assert result.name.startswith("ReturnRotate_")


def test_export_from_view_data_popup_raises_and_screenshots_when_overlay_never_clears(tmp_path):
    """If an overlay genuinely never clears (real stuck-dialog case), the
    raw Playwright TimeoutError must not leak out uncaught -- it should
    be turned into a clear BrowserAutomationError with a debug
    screenshot, same as every other failure mode in this module."""

    def always_times_out(*args, **kwargs):
        raise TimeoutError("Timeout 30000ms exceeded waiting for element to be hidden")

    popup = ViewDataPopupPage(overlay_wait_side_effect=always_times_out)

    with pytest.raises(BrowserAutomationError, match="Could not export CSV from the View Data window"):
        _export_from_view_data_popup(popup, tmp_path)

    popup.download_button.click.assert_not_called()
    popup.screenshot.assert_called_once()


def test_export_from_view_data_popup_raises_when_download_button_missing(monkeypatch, tmp_path):
    import src.exportbot.browser_crosstab as bc

    monkeypatch.setattr(bc, "DASHBOARD_LOAD_TIMEOUT_MS", 10)
    popup = ViewDataPopupPage(button_present=False)

    with pytest.raises(BrowserAutomationError, match='"Download" button in the View Data window'):
        _export_from_view_data_popup(popup, tmp_path)
    popup.screenshot.assert_called_once()


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
