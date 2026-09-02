"""Tests the browser automation module's pure logic and step functions
using lightweight fake Playwright-like objects -- no real browser, no
real network call. This does NOT prove the DOM selectors match the live
Tableau UI (impossible without a real browser and live access); it
proves our own orchestration, fallback-matching, file-saving, and
credential-handling logic is correct. See CLAUDE.md for what remains
unverified live.
"""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.exportbot.browser_crosstab import (
    _ViewDataSessionCapture,
    _attach_export_network_diagnostics,
    _debug_dump_view_data_popup,
    _discover_active_vizql_session,
    _export_crosstab_csv,
    _export_view_data_via_vizql_commands,
    _extract_filename_from_content_disposition,
    _fetch_view_data_dialog_tab_pres_model,
    _find_first,
    _find_key_recursive,
    _find_vizql_session_in_resource_urls,
    _find_xsrf_token_in_cookies,
    _inspect_frame,
    _is_export_related_url,
    _login_if_needed,
    _redact_url,
    _safe_response_text_snippet,
    _sanitize_filename,
    _save_tempfile_response,
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


class FakeAPIResponse:
    """Fake for a Playwright Response/APIResponse -- both share the same
    shape this module relies on (status/headers/body()/json()/ok), so one
    fake stands in for the HAR-confirmed `.../tempfile/...` response
    (consumed by _save_tempfile_response()) and for the
    export-view-data-summary-to-csv-server response returned by our own
    `page.context.request.post()` call (consumed directly by
    _export_view_data_via_vizql_commands())."""

    def __init__(
        self, url, status=200, headers=None, body=b"Product,Region\nWidget,East\n", json_body=None, text_body=None
    ):
        self.url = url
        self.status = status
        self.headers = headers if headers is not None else {"content-type": "text/csv"}
        self._body = body
        self._json_body = json_body
        self._text_body = text_body

    @property
    def ok(self):
        return self.status < 400

    def body(self):
        return self._body

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body configured on this fake response")
        return self._json_body

    def text(self):
        if self._text_body is not None:
            return self._text_body
        if self._json_body is not None:
            return json.dumps(self._json_body)
        return ""


class FakeAPIRequestContext:
    """Fake for Playwright's APIRequestContext (page.context.request) --
    records every .post()/.get() call (url/headers/multipart/params) for
    assertions, and returns pre-scripted responses (consumed in call
    order for .post(), since production code now issues two POSTs --
    get-view-data-dialog-tab-pres-model, then the export command) or
    raises a scripted exception."""

    def __init__(self, post_responses=None, get_response=None, post_raises=None, get_raises=None):
        self.post_calls = []
        self.get_calls = []
        self._post_responses = list(post_responses) if post_responses is not None else []
        self._get_response = get_response
        self._post_raises = post_raises
        self._get_raises = get_raises

    def post(self, url, headers=None, multipart=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "multipart": multipart})
        if self._post_raises is not None:
            raise self._post_raises
        if self._post_responses:
            return self._post_responses.pop(0)
        return None

    def get(self, url, params=None, timeout=None):
        self.get_calls.append({"url": url, "params": params})
        if self._get_raises is not None:
            raise self._get_raises
        return self._get_response


class FakeBrowserContext:
    """Fake for a Playwright BrowserContext -- exposes `.request` (a
    FakeAPIRequestContext), `.cookies()` (a fixed list of cookie dicts,
    standing in for the browser's real cookie jar -- see
    _find_xsrf_token_in_cookies()), and `.on()` (used only by the
    unrelated, still-kept _attach_export_network_diagnostics)."""

    def __init__(self, request_context=None, cookies=None, on_raises=False):
        self.request = request_context if request_context is not None else FakeAPIRequestContext()
        self._cookies = cookies if cookies is not None else []
        self._on_raises = on_raises

    def cookies(self):
        return self._cookies

    def on(self, event, handler):
        if self._on_raises:
            raise RuntimeError("on() failed")


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


class ScriptedPage:
    """Returns pre-scripted FakeLocators in call order, regardless of
    which get_by_*/locator method is used -- lets tests control exactly
    which candidate in an ordered fallback list "matches" without
    simulating real DOM text matching."""

    def __init__(self, script, popup=None, context=None, resource_urls=None):
        self._script = list(script)
        self._pos = 0
        self.frames = []
        self._popup = popup
        self.wait_for_timeout = MagicMock()
        self.wait_for_load_state = MagicMock()
        self.goto = MagicMock()
        self.screenshot = MagicMock()
        self.on = MagicMock()
        self.context = context if context is not None else FakeBrowserContext()
        self._resource_urls = resource_urls if resource_urls is not None else []

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

    def expect_popup(self, timeout=None):
        return _PopupCM(self._popup)

    def evaluate(self, script):
        return self._resource_urls


class ViewDataPopupPage:
    """Fake for the "View Data" popup window opened by Download -> Data.

    A real-server diagnostic showed this popup's own DOM is entirely
    empty (a "Hybrid Popup Window" wrapper) -- see the module docstring
    in browser_crosstab.py. Its remaining roles are: the polling driver
    for _discover_active_vizql_session() (.wait_for_timeout), a second
    source of Performance Resource Timing URLs for that same discovery
    (.evaluate), and the target of the still-kept
    _debug_dump_view_data_popup(); it is never searched for a Download
    control or clicked any more."""

    def __init__(
        self,
        url="https://insight.example.com/vizql/w/ReturnRotate/v/Dashboard1/hybrid-window/min/index.html",
        resource_urls=None,
    ):
        self.frames = []
        self.url = url
        self.wait_for_timeout = MagicMock()
        self.wait_for_load_state = MagicMock()
        self.goto = MagicMock()
        self.screenshot = MagicMock()
        self._resource_urls = resource_urls if resource_urls is not None else []

        def _title():
            return "Hybrid Popup Window"

        self.title = _title

    def evaluate(self, script):
        return self._resource_urls


def test_redact_url_redacts_sensitive_params_but_keeps_others():
    url = (
        "https://insight.example.com/vizql/v_test/hybrid-window/min/index.html"
        "?id=8C4A1AEB8A024C0AA761D90468368731&moduleId=view_data&sheet=Summary"
    )
    redacted = _redact_url(url)
    assert "8C4A1AEB8A024C0AA761D90468368731" not in redacted
    assert "id=REDACTED" in redacted
    assert "moduleId=view_data" in redacted
    assert "sheet=Summary" in redacted
    assert redacted.startswith("https://insight.example.com/vizql/v_test/hybrid-window/min/index.html")


def test_redact_url_never_raises_on_unparseable_input():
    assert _redact_url(object()) == "[unparseable URL]"


def test_redact_url_redacts_session_id_in_path():
    """Every VizQL command/tempfile URL this module builds carries the
    session id as a PATH segment (.../sessions/{id}/...), not a query
    parameter -- this must be redacted too, or it leaks straight through
    the [NETDEBUG] logs printed around the export/tempfile requests."""
    url = (
        "https://insight.example.com/vizql/w/ReturnRotate/v/Dashboard1"
        "/sessions/SUPERSECRETSESSIONID/commands/tabsrv/export-view-data-summary-to-csv-server"
    )
    redacted = _redact_url(url)
    assert "SUPERSECRETSESSIONID" not in redacted
    assert "/sessions/REDACTED/" in redacted
    assert redacted.startswith("https://insight.example.com/vizql/w/ReturnRotate/v/Dashboard1")


def test_redact_url_redacts_session_id_in_path_and_key_in_query_together():
    url = (
        "https://insight.example.com/vizql/w/ReturnRotate/v/Dashboard1"
        "/tempfile/sessions/SUPERSECRETSESSIONID?key=THERESULTKEY&keepfile=yes"
    )
    redacted = _redact_url(url)
    assert "SUPERSECRETSESSIONID" not in redacted
    assert "THERESULTKEY" not in redacted
    assert "/sessions/REDACTED" in redacted
    assert "key=REDACTED" in redacted
    assert "keepfile=yes" in redacted


class FakeFrame:
    def __init__(self, url="https://insight.example.com/frame", name="", evaluate_result=None, evaluate_raises=False):
        self.url = url
        self.name = name
        self._evaluate_result = evaluate_result if evaluate_result is not None else {}
        self._evaluate_raises = evaluate_raises

    def evaluate(self, script):
        if self._evaluate_raises:
            raise RuntimeError("evaluate failed")
        return self._evaluate_result


def test_inspect_frame_returns_evaluate_result():
    expected = {"body_inner_html_length": 42, "buttons": []}
    frame = FakeFrame(evaluate_result=expected)
    assert _inspect_frame(frame) == expected


def test_inspect_frame_returns_error_dict_on_exception():
    frame = FakeFrame(evaluate_raises=True)
    result = _inspect_frame(frame)
    assert "error" in result
    assert "evaluate failed" in result["error"]


class FakeContextPage:
    def __init__(self, url, title, title_raises=False):
        self.url = url
        self._title = title
        self._title_raises = title_raises

    def title(self):
        if self._title_raises:
            raise RuntimeError("title failed")
        return self._title


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class RaisingContext:
    """A .context stand-in whose .pages access always raises -- used to
    confirm context-page enumeration failures are caught, without
    polluting shared classes like MagicMock."""

    @property
    def pages(self):
        raise RuntimeError("context failed")


class DebugDumpPage:
    """Fake for _debug_dump_view_data_popup()'s target page, matching the
    frame-based + context-based implementation: exposes .url, .title(),
    .screenshot(), .frames (a list of FakeFrame, each inspected via
    _inspect_frame()), and .context.pages (other pages in the same
    browser context) -- since the real popup's own top-level document
    turned out to have zero buttons/download-text and a generic
    "Hybrid Popup Window" title, meaning the real content lives in a
    frame, shadow root, or a different context page."""

    def __init__(
        self,
        url="https://insight.example.com/vizql/v_test/hybrid-window/min/index.html?id=SECRET123",
        title="Hybrid Popup Window",
        frames=None,
        context_pages=None,
        title_raises=False,
        screenshot_raises=False,
        frames_raise=False,
        context_raises=False,
    ):
        self.url = url
        self._title = title
        self._title_raises = title_raises
        self._frames = frames if frames is not None else []
        self._frames_raise = frames_raise
        self.screenshot = MagicMock(
            side_effect=RuntimeError("screenshot failed") if screenshot_raises else None
        )
        if context_raises:
            self.context = RaisingContext()
        else:
            self.context = FakeContext(context_pages if context_pages is not None else [])

    def title(self):
        if self._title_raises:
            raise RuntimeError("title failed")
        return self._title

    @property
    def frames(self):
        if self._frames_raise:
            raise RuntimeError("frames failed")
        return self._frames


def test_debug_dump_view_data_popup_prints_frame_and_context_diagnostics(tmp_path, capsys):
    main_frame = FakeFrame(
        url="https://insight.example.com/vizql/v_test/hybrid-window/min/index.html",
        name="",
        evaluate_result={
            "body_inner_html_length": 0,
            "body_inner_text": "",
            "buttons": [],
            "download_elements": [],
            "iframes": [{"src": "https://insight.example.com/inner?token=SECRET", "name": "inner", "id": "f1"}],
            "shadow_roots": [],
            "canvases": [{"width": 800, "height": 600, "client_width": 800, "client_height": 600}],
            "html_preview": "<html>...</html>",
        },
    )
    other_page = FakeContextPage(
        url="https://insight.example.com/#/views/ReturnRotate/Dashboard1?:id=1", title="Dashboard"
    )
    view_data_page = DebugDumpPage(frames=[main_frame], context_pages=[other_page])

    _debug_dump_view_data_popup(view_data_page, tmp_path)

    view_data_page.screenshot.assert_called_once_with(path=str(tmp_path / "debug_view_data_popup.png"))
    out = capsys.readouterr().out
    assert "Hybrid Popup Window" in out
    assert "popup.frames (1)" in out
    assert "SECRET" not in out
    assert "browser context has 1 page(s)" in out
    assert "Dashboard" in out


def test_debug_dump_view_data_popup_never_raises_despite_every_step_failing(tmp_path):
    view_data_page = DebugDumpPage(
        title_raises=True,
        screenshot_raises=True,
        frames_raise=True,
        context_raises=True,
    )
    _debug_dump_view_data_popup(view_data_page, tmp_path)  # must not raise


def test_debug_dump_view_data_popup_reports_per_frame_evaluate_failure(tmp_path, capsys):
    broken_frame = FakeFrame(evaluate_raises=True)
    view_data_page = DebugDumpPage(frames=[broken_frame])

    _debug_dump_view_data_popup(view_data_page, tmp_path)  # must not raise

    out = capsys.readouterr().out
    assert "evaluate() failed" in out


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


SESSION_ORIGIN = "https://insight.example.com"
SESSION_PATH_PREFIX = "w/ReturnRotate/v/Dashboard1"
SESSION_ID = "SESSIONABC"
BOOTSTRAP_RESOURCE_URL = (
    f"{SESSION_ORIGIN}/vizql/{SESSION_PATH_PREFIX}/sessions/{SESSION_ID}"
    "/commands/tabdoc/launch-hybrid-view-data-dialog"
)
TAB_PRES_MODEL_URL = (
    f"{SESSION_ORIGIN}/vizql/{SESSION_PATH_PREFIX}/sessions/{SESSION_ID}"
    "/commands/tabsrv/get-view-data-dialog-tab-pres-model"
)
EXPORT_COMMAND_URL = (
    f"{SESSION_ORIGIN}/vizql/{SESSION_PATH_PREFIX}/sessions/{SESSION_ID}"
    "/commands/tabsrv/export-view-data-summary-to-csv-server"
)
TEMPFILE_URL = f"{SESSION_ORIGIN}/vizql/{SESSION_PATH_PREFIX}/tempfile/sessions/{SESSION_ID}"


def _make_completed_capture(**overrides):
    capture = _ViewDataSessionCapture()
    capture.origin = SESSION_ORIGIN
    capture.command_path_prefix = SESSION_PATH_PREFIX
    capture.session_id = SESSION_ID
    capture.xsrf_token = "xsrf-value-123"
    capture.visual_id_pres_model = "visual-abc"
    capture.columns = ["Product", "Region"]
    for key, value in overrides.items():
        setattr(capture, key, value)
    return capture


def _make_tab_pres_model_response():
    return FakeAPIResponse(
        url=TAB_PRES_MODEL_URL,
        json_body={"visualIdPresModel": "visual-abc", "columns": ["Product", "Region"]},
    )


def test_export_crosstab_csv_full_flow_reproduces_vizql_export_commands(tmp_path):
    """End-to-end through _export_crosstab_csv(): clicking Download ->
    Data opens the "View Data" popup (kept, confirmed working against
    the real server), but the CSV export itself is now reproduced by
    discovering the already-active VizQL session from browser state
    (Performance Resource Timing entries + cookies) and issuing
    Tableau's own VizQL commands directly -- see the module docstring in
    browser_crosstab.py for why every DOM-based approach (the popup's
    own empty DOM, the main dashboard's Download button blocked by
    Tableau's own dialog-glass overlay) and every listener-based capture
    approach were ruled out."""
    download_btn = FakeLocator()
    data_item = FakeLocator()
    popup = ViewDataPopupPage(resource_urls=[BOOTSTRAP_RESOURCE_URL])

    tab_pres_model_response = _make_tab_pres_model_response()
    export_response = FakeAPIResponse(url=EXPORT_COMMAND_URL, json_body={"resultKey": "KEY123"})
    tempfile_response = FakeAPIResponse(
        url=TEMPFILE_URL,
        headers={"content-type": "text/csv"},
        body=b"Product,Region\nWidget,East\n",
    )
    request_ctx = FakeAPIRequestContext(
        post_responses=[tab_pres_model_response, export_response], get_response=tempfile_response
    )
    context = FakeBrowserContext(
        request_context=request_ctx, cookies=[{"name": "XSRF-TOKEN", "value": "xsrf-value-123"}]
    )
    page = ScriptedPage(script=[download_btn, data_item], popup=popup, context=context)

    result = _export_crosstab_csv(page, tmp_path)

    download_btn.click.assert_called_once()
    data_item.click.assert_called_once()

    assert len(request_ctx.post_calls) == 2
    tab_pres_call, export_call = request_ctx.post_calls
    assert tab_pres_call["url"] == TAB_PRES_MODEL_URL
    assert export_call["url"] == EXPORT_COMMAND_URL
    assert export_call["headers"]["X-XSRF-TOKEN"] == "xsrf-value-123"
    assert export_call["multipart"]["visualIdPresModel"] == "visual-abc"
    assert json.loads(export_call["multipart"]["columns"]) == ["Product", "Region"]

    assert len(request_ctx.get_calls) == 1
    get_call = request_ctx.get_calls[0]
    assert get_call["url"] == TEMPFILE_URL
    assert get_call["params"]["key"] == "KEY123"

    assert result.parent == tmp_path
    assert result.name.startswith("ReturnRotate_")
    assert result.read_bytes() == b"Product,Region\nWidget,East\n"


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


def test_find_key_recursive_finds_nested_dict_key():
    obj = {"a": {"b": {"visualIdPresModel": "visual-xyz"}}}
    assert _find_key_recursive(obj, "visualIdPresModel") == "visual-xyz"


def test_find_key_recursive_finds_key_inside_list():
    obj = {"notifications": [{"other": 1}, {"columns": ["A", "B"]}]}
    assert _find_key_recursive(obj, "columns") == ["A", "B"]


def test_find_key_recursive_returns_none_when_absent():
    assert _find_key_recursive({"a": {"b": 1}}, "missing") is None
    assert _find_key_recursive("not a container", "missing") is None


def test_view_data_session_capture_has_session_requires_core_fields():
    capture = _ViewDataSessionCapture()
    assert not capture.has_session()

    capture.origin = SESSION_ORIGIN
    capture.command_path_prefix = SESSION_PATH_PREFIX
    capture.session_id = SESSION_ID
    assert not capture.has_session()  # xsrf_token still missing

    capture.xsrf_token = "token"
    assert capture.has_session()  # visualIdPresModel/columns not required here


def test_find_vizql_session_in_resource_urls_extracts_session_context():
    result = _find_vizql_session_in_resource_urls(
        ["https://insight.example.com/static/app.js", BOOTSTRAP_RESOURCE_URL]
    )
    assert result == (SESSION_ORIGIN, SESSION_PATH_PREFIX, SESSION_ID)


def test_find_vizql_session_in_resource_urls_returns_none_when_no_match():
    assert _find_vizql_session_in_resource_urls(["https://insight.example.com/static/app.js"]) is None
    assert _find_vizql_session_in_resource_urls([]) is None


def test_find_vizql_session_in_resource_urls_skips_malformed_entries_without_raising():
    """performance.getEntriesByType("resource") entries are just strings
    in practice, but this must not assume that -- a non-string/None entry
    (or one VIZQL_SESSION_ID_RE can't run against) must be skipped, not
    raise, so one bad entry can't break discovery for every other URL in
    the list."""
    result = _find_vizql_session_in_resource_urls([None, 12345, object(), BOOTSTRAP_RESOURCE_URL])
    assert result == (SESSION_ORIGIN, SESSION_PATH_PREFIX, SESSION_ID)


def test_find_xsrf_token_in_cookies_matches_known_cookie_names():
    context = FakeBrowserContext(cookies=[{"name": "XSRF-TOKEN", "value": "the-real-token"}])
    assert _find_xsrf_token_in_cookies(context) == "the-real-token"


def test_find_xsrf_token_in_cookies_returns_none_when_absent():
    context = FakeBrowserContext(cookies=[{"name": "session", "value": "irrelevant"}])
    assert _find_xsrf_token_in_cookies(context) is None


def test_find_xsrf_token_in_cookies_never_raises_when_cookies_fails():
    class BrokenContext:
        def cookies(self):
            raise RuntimeError("boom")

    assert _find_xsrf_token_in_cookies(BrokenContext()) is None


def test_find_xsrf_token_in_cookies_skips_malformed_cookie_entries():
    """A cookie entry that isn't a plain {"name": ..., "value": ...} dict
    (e.g. missing .get() entirely) must be skipped, not raise -- one
    malformed cookie can't be allowed to hide a real XSRF cookie later
    in the list."""
    context = FakeBrowserContext(cookies=[object(), {"name": "XSRF-TOKEN", "value": "the-real-token"}])
    assert _find_xsrf_token_in_cookies(context) == "the-real-token"


def test_discover_active_vizql_session_finds_session_on_second_page():
    """The main dashboard page has nothing useful, but the popup does --
    both are checked."""
    capture = _ViewDataSessionCapture()
    main_page = ScriptedPage(script=[], resource_urls=[])
    popup = ViewDataPopupPage(resource_urls=[BOOTSTRAP_RESOURCE_URL])
    main_page.context = FakeBrowserContext(cookies=[{"name": "XSRF-TOKEN", "value": "the-real-token"}])

    _discover_active_vizql_session([main_page, popup], capture)

    assert capture.origin == SESSION_ORIGIN
    assert capture.command_path_prefix == SESSION_PATH_PREFIX
    assert capture.session_id == SESSION_ID
    assert capture.xsrf_token == "the-real-token"


def test_discover_active_vizql_session_returns_immediately_when_already_complete():
    capture = _make_completed_capture()
    main_page = ScriptedPage(script=[])

    _discover_active_vizql_session([main_page], capture)  # must not raise
    main_page.wait_for_timeout.assert_not_called()


def test_discover_active_vizql_session_raises_on_timeout(monkeypatch):
    import src.exportbot.browser_crosstab as bc

    monkeypatch.setattr(bc, "VIEW_DATA_POPUP_TIMEOUT_MS", 10)
    capture = _ViewDataSessionCapture()  # never discoverable
    main_page = ScriptedPage(script=[], resource_urls=[])
    main_page.context = FakeBrowserContext(cookies=[])

    with pytest.raises(BrowserAutomationError, match="Could not discover an active VizQL session"):
        _discover_active_vizql_session([main_page], capture)


def test_safe_response_text_snippet_truncates_body():
    response = FakeAPIResponse(url=TAB_PRES_MODEL_URL, json_body={})
    response.text = lambda: "x" * 1000
    assert len(_safe_response_text_snippet(response)) == 300


def test_safe_response_text_snippet_never_raises_when_text_fails():
    response = FakeAPIResponse(url=TAB_PRES_MODEL_URL, json_body={})

    def _raise():
        raise RuntimeError("boom")

    response.text = _raise
    assert _safe_response_text_snippet(response) == "<no response body available>"


def test_fetch_view_data_dialog_tab_pres_model_success():
    capture = _make_completed_capture(visual_id_pres_model=None, columns=None)
    request_ctx = FakeAPIRequestContext(post_responses=[_make_tab_pres_model_response()])

    _fetch_view_data_dialog_tab_pres_model(request_ctx, capture)

    assert capture.visual_id_pres_model == "visual-abc"
    assert capture.columns == ["Product", "Region"]
    assert request_ctx.post_calls[0]["url"] == TAB_PRES_MODEL_URL
    assert request_ctx.post_calls[0]["headers"]["X-XSRF-TOKEN"] == "xsrf-value-123"


def test_fetch_view_data_dialog_tab_pres_model_raises_when_post_fails():
    capture = _make_completed_capture()
    request_ctx = FakeAPIRequestContext(post_raises=RuntimeError("network error"))

    with pytest.raises(BrowserAutomationError, match="Could not issue the View Data tab presentation model command"):
        _fetch_view_data_dialog_tab_pres_model(request_ctx, capture)


def test_fetch_view_data_dialog_tab_pres_model_raises_on_non_ok_status():
    capture = _make_completed_capture()
    response = FakeAPIResponse(url=TAB_PRES_MODEL_URL, status=500, json_body={})
    request_ctx = FakeAPIRequestContext(post_responses=[response])

    with pytest.raises(BrowserAutomationError, match="HTTP 500"):
        _fetch_view_data_dialog_tab_pres_model(request_ctx, capture)


def test_fetch_view_data_dialog_tab_pres_model_error_includes_response_body_snippet():
    """A real failure must be diagnosable from the raised error's text
    alone, per the explicit "diagnose the actual HTTP response/status/
    body" requirement -- not just a bare status code."""
    capture = _make_completed_capture()
    response = FakeAPIResponse(
        url=TAB_PRES_MODEL_URL, status=403, text_body="Forbidden: session has expired"
    )
    request_ctx = FakeAPIRequestContext(post_responses=[response])

    with pytest.raises(BrowserAutomationError, match="session has expired"):
        _fetch_view_data_dialog_tab_pres_model(request_ctx, capture)


def test_fetch_view_data_dialog_tab_pres_model_raises_when_visual_id_pres_model_missing():
    capture = _make_completed_capture(visual_id_pres_model=None)
    response = FakeAPIResponse(url=TAB_PRES_MODEL_URL, json_body={"nothing": "here"})
    request_ctx = FakeAPIRequestContext(post_responses=[response])

    with pytest.raises(BrowserAutomationError, match="no visualIdPresModel was found"):
        _fetch_view_data_dialog_tab_pres_model(request_ctx, capture)


def test_fetch_view_data_dialog_tab_pres_model_ignores_non_string_list_columns():
    """`columns` must only be accepted when it's actually a list of
    strings -- a malformed shape (e.g. a list of numbers, or a bare
    string) must be left as None, not blindly stored and later sent to
    Tableau's export command."""
    capture = _make_completed_capture(visual_id_pres_model=None, columns=None)
    response = FakeAPIResponse(
        url=TAB_PRES_MODEL_URL, json_body={"visualIdPresModel": "visual-abc", "columns": [1, 2, 3]}
    )
    request_ctx = FakeAPIRequestContext(post_responses=[response])

    _fetch_view_data_dialog_tab_pres_model(request_ctx, capture)

    assert capture.visual_id_pres_model == "visual-abc"
    assert capture.columns is None


def test_export_view_data_via_vizql_commands_success(tmp_path):
    capture = _make_completed_capture()
    export_response = FakeAPIResponse(url=EXPORT_COMMAND_URL, json_body={"resultKey": "KEY123"})
    tempfile_response = FakeAPIResponse(
        url=TEMPFILE_URL, headers={"content-type": "text/csv"}, body=b"a,b\n1,2\n"
    )
    request_ctx = FakeAPIRequestContext(
        post_responses=[_make_tab_pres_model_response(), export_response], get_response=tempfile_response
    )
    page = ScriptedPage(script=[], context=FakeBrowserContext(request_context=request_ctx))
    popup = ViewDataPopupPage()

    result = _export_view_data_via_vizql_commands(page, popup, capture, tmp_path)

    assert request_ctx.post_calls[0]["url"] == TAB_PRES_MODEL_URL
    assert request_ctx.post_calls[1]["url"] == EXPORT_COMMAND_URL
    assert request_ctx.get_calls[0]["url"] == TEMPFILE_URL
    assert result.read_bytes() == b"a,b\n1,2\n"


def test_export_view_data_via_vizql_commands_raises_when_session_not_discoverable(tmp_path, monkeypatch):
    import src.exportbot.browser_crosstab as bc

    monkeypatch.setattr(bc, "VIEW_DATA_POPUP_TIMEOUT_MS", 10)
    capture = _ViewDataSessionCapture()
    page = ScriptedPage(script=[], resource_urls=[], context=FakeBrowserContext(cookies=[]))
    popup = ViewDataPopupPage(resource_urls=[])

    with pytest.raises(BrowserAutomationError, match="Could not discover an active VizQL session"):
        _export_view_data_via_vizql_commands(page, popup, capture, tmp_path)


def test_export_view_data_via_vizql_commands_raises_when_post_fails(tmp_path):
    capture = _make_completed_capture()
    request_ctx = FakeAPIRequestContext(post_raises=RuntimeError("network error"))
    page = ScriptedPage(script=[], context=FakeBrowserContext(request_context=request_ctx))
    popup = ViewDataPopupPage()

    with pytest.raises(BrowserAutomationError, match="Could not issue the View Data tab presentation model command"):
        _export_view_data_via_vizql_commands(page, popup, capture, tmp_path)


def test_export_view_data_via_vizql_commands_raises_on_non_ok_status(tmp_path):
    capture = _make_completed_capture()
    export_response = FakeAPIResponse(url=EXPORT_COMMAND_URL, status=500, json_body={})
    request_ctx = FakeAPIRequestContext(post_responses=[_make_tab_pres_model_response(), export_response])
    page = ScriptedPage(script=[], context=FakeBrowserContext(request_context=request_ctx))
    popup = ViewDataPopupPage()

    with pytest.raises(BrowserAutomationError, match="HTTP 500"):
        _export_view_data_via_vizql_commands(page, popup, capture, tmp_path)


def test_export_view_data_via_vizql_commands_error_includes_response_body_snippet(tmp_path):
    """Same "diagnosable from the error alone" requirement as the
    tab-pres-model command -- the export command's own failure text must
    carry the real response body, not just a status code."""
    capture = _make_completed_capture()
    export_response = FakeAPIResponse(
        url=EXPORT_COMMAND_URL, status=400, text_body="Bad Request: invalid columns"
    )
    request_ctx = FakeAPIRequestContext(post_responses=[_make_tab_pres_model_response(), export_response])
    page = ScriptedPage(script=[], context=FakeBrowserContext(request_context=request_ctx))
    popup = ViewDataPopupPage()

    with pytest.raises(BrowserAutomationError, match="invalid columns"):
        _export_view_data_via_vizql_commands(page, popup, capture, tmp_path)


def test_export_view_data_via_vizql_commands_raises_when_result_key_missing(tmp_path):
    capture = _make_completed_capture()
    export_response = FakeAPIResponse(url=EXPORT_COMMAND_URL, json_body={"nothing": "here"})
    request_ctx = FakeAPIRequestContext(post_responses=[_make_tab_pres_model_response(), export_response])
    page = ScriptedPage(script=[], context=FakeBrowserContext(request_context=request_ctx))
    popup = ViewDataPopupPage()

    with pytest.raises(BrowserAutomationError, match="no resultKey was found"):
        _export_view_data_via_vizql_commands(page, popup, capture, tmp_path)


def test_export_view_data_via_vizql_commands_raises_when_tempfile_fetch_fails(tmp_path):
    capture = _make_completed_capture()
    export_response = FakeAPIResponse(url=EXPORT_COMMAND_URL, json_body={"resultKey": "KEY123"})
    request_ctx = FakeAPIRequestContext(
        post_responses=[_make_tab_pres_model_response(), export_response],
        get_raises=RuntimeError("network error"),
    )
    page = ScriptedPage(script=[], context=FakeBrowserContext(request_context=request_ctx))
    popup = ViewDataPopupPage()

    with pytest.raises(BrowserAutomationError, match="Could not fetch the exported tempfile"):
        _export_view_data_via_vizql_commands(page, popup, capture, tmp_path)


def test_export_view_data_via_vizql_commands_never_prints_secrets(tmp_path, capsys):
    """The [NETDEBUG] status lines printed around session discovery, the
    tab presentation model fetch, the export command, and the tempfile
    GET must never print the actual session id, XSRF token,
    visualIdPresModel, or result key -- only booleans and
    _redact_url()-redacted URLs. This is what actually caught a real
    leak: the export/tempfile URLs carry the session id as a PATH
    segment, which _redact_url() did not originally strip (it only
    covered query parameters) -- fixed there, verified here end to end
    rather than by asserting _redact_url() in isolation."""
    capture = _make_completed_capture()
    export_response = FakeAPIResponse(
        url=EXPORT_COMMAND_URL, json_body={"resultKey": "THE-SECRET-RESULT-KEY"}
    )
    tempfile_response = FakeAPIResponse(
        url=TEMPFILE_URL, headers={"content-type": "text/csv"}, body=b"a,b\n1,2\n"
    )
    request_ctx = FakeAPIRequestContext(
        post_responses=[_make_tab_pres_model_response(), export_response], get_response=tempfile_response
    )
    page = ScriptedPage(script=[], context=FakeBrowserContext(request_context=request_ctx))
    popup = ViewDataPopupPage()

    _export_view_data_via_vizql_commands(page, popup, capture, tmp_path)

    out = capsys.readouterr().out
    assert capture.session_id not in out
    assert capture.xsrf_token not in out
    assert capture.visual_id_pres_model not in out
    assert "THE-SECRET-RESULT-KEY" not in out


def test_is_export_related_url_matches_command_and_tempfile_only():
    assert _is_export_related_url(
        "https://insight.example.com/vizql/w/x/v/y/sessions/1/commands/tabsrv/export-view-data-summary-to-csv-server"
    )
    assert _is_export_related_url(
        "https://insight.example.com/vizql/w/x/v/y/tempfile/sessions/1?key=abc"
    )
    assert not _is_export_related_url("https://insight.example.com/vizql/w/x/v/y/commands/tabdoc/launch-hybrid-view-data-dialog")


def test_extract_filename_from_content_disposition_plain():
    assert (
        _extract_filename_from_content_disposition('attachment; filename="Persentase Nasional_data.csv"')
        == "Persentase Nasional_data.csv"
    )


def test_extract_filename_from_content_disposition_rfc5987():
    assert (
        _extract_filename_from_content_disposition("attachment; filename*=UTF-8''Persentase%20Nasional_data.csv")
        == "Persentase%20Nasional_data.csv"
    )


def test_extract_filename_from_content_disposition_none_when_absent():
    assert _extract_filename_from_content_disposition(None) is None
    assert _extract_filename_from_content_disposition("attachment") is None


def test_sanitize_filename_strips_path_components_and_hostile_characters():
    assert _sanitize_filename("../../etc/passwd") == "passwd"
    assert _sanitize_filename("..\\..\\Windows\\evil.csv") == "evil.csv"
    assert _sanitize_filename('weird"name?.csv') == "weird_name_.csv"


def test_sanitize_filename_returns_none_for_empty_or_missing():
    assert _sanitize_filename(None) is None
    assert _sanitize_filename("") is None
    assert _sanitize_filename("../") is None


def test_save_tempfile_response_uses_content_disposition_filename(tmp_path):
    response = FakeAPIResponse(
        url="https://insight.example.com/vizql/tempfile/sessions/1?key=abc",
        headers={"content-type": "text/csv", "content-disposition": 'attachment; filename="Persentase Nasional_data.csv"'},
        body=b"a,b\n1,2\n",
    )
    path = _save_tempfile_response(response, tmp_path)
    assert path == tmp_path / "Persentase Nasional_data.csv"
    assert path.read_bytes() == b"a,b\n1,2\n"


def test_save_tempfile_response_falls_back_to_generated_filename_without_disposition(tmp_path):
    response = FakeAPIResponse(
        url="https://insight.example.com/vizql/tempfile/sessions/1?key=abc",
        headers={"content-type": "text/csv"},
    )
    path = _save_tempfile_response(response, tmp_path)
    assert path.parent == tmp_path
    assert path.name.startswith("ReturnRotate_")
    assert path.name.endswith(".csv")


def test_save_tempfile_response_rejects_non_csv_content_type(tmp_path):
    response = FakeAPIResponse(
        url="https://insight.example.com/vizql/tempfile/sessions/1?key=abc",
        headers={"content-type": "application/json"},
    )
    with pytest.raises(BrowserAutomationError, match="Content-Type"):
        _save_tempfile_response(response, tmp_path)


def test_save_tempfile_response_rejects_error_status(tmp_path):
    response = FakeAPIResponse(
        url="https://insight.example.com/vizql/tempfile/sessions/1?key=abc",
        status=404,
    )
    with pytest.raises(BrowserAutomationError, match="HTTP 404"):
        _save_tempfile_response(response, tmp_path)


class FakeNetworkPage:
    """Minimal fake exposing only .on(), used to test
    _attach_export_network_diagnostics() in isolation."""

    def __init__(self, on_raises=False):
        self.registered = []
        self._on_raises = on_raises

    def on(self, event, handler):
        if self._on_raises:
            raise RuntimeError("on() failed")
        self.registered.append((event, handler))


def test_attach_export_network_diagnostics_registers_request_and_response_handlers():
    page = FakeNetworkPage()
    _attach_export_network_diagnostics([page])
    events = [event for event, _handler in page.registered]
    assert events == ["request", "response"]


def test_attach_export_network_diagnostics_never_raises_when_on_fails():
    page = FakeNetworkPage(on_raises=True)
    _attach_export_network_diagnostics([page])  # must not raise


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
