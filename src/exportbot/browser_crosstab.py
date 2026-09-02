"""Browser-automated Tableau Crosstab CSV export via Playwright.

Login and dashboard navigation reproduce the exact manual workflow
already confirmed to work in the live Tableau UI: open Tableau, log in,
load the dashboard, click Download -> Data. That click opens a separate
"View Data" popup window (titled "Hybrid Popup Window") whose own JS
runs Tableau's real VizQL export machinery -- this project never issues
any VizQL command itself; it drives the same UI a person would and
captures the resulting file over the network (see below).

A real HAR capture of the exact manual "Full Data" workflow (open View
Data -> click "Full Data" -> click "Download") is this project's ground
truth for what that popup actually needs (see CLAUDE.md, "The Full Data
export needs real UI clicks"). It showed two things a prior version got
wrong:

  - The popup does NOT default to Full Data. Left alone, it exports
    "Summary" (`export-view-data-summary-to-csv-server`), which never
    appears at all in the manual Full Data HAR. The real target is
    `export-view-data-underlying-to-csv-server`, fired only after
    switching to the "Full Data" tab.
  - Reaching it requires two real clicks inside the popup -- "Full
    Data", then "Download". A one-shot DOM diagnostic
    (`_debug_dump_view_data_popup()`, still kept below) once found zero
    buttons immediately after `domcontentloaded`, but that only proved
    the popup hadn't finished hydrating at that exact instant -- the
    same class of timing bug this project already hit once before with
    the login form. `_switch_to_full_data_and_download()` polls for each
    control instead of checking once, and deliberately does not fall
    back to clicking Download if "Full Data" can't be found first (that
    would silently export Summary).

Whichever export actually fires, this project never constructs the
VizQL command itself -- no `viewDataTableId`, no reconstructed column
list, no manual POST. `_attach_export_network_diagnostics()` takes an
optional `_TempfileCapture` and, alongside its existing request/response
logging, snapshots the first real `.../tempfile/...` response it sees
via `_capture_tempfile_response()`. `_wait_for_tempfile_capture()` polls
(no fixed sleep) until that capture is populated or times out.
`_export_crosstab_csv()` attaches this before the "Data" click (so
nothing the popup's JS does afterward can be missed), drives the two
clicks above, then waits for the capture. `_save_tempfile_response()`
(unchanged) validates and saves the captured response's body.

NOT exercised against the real server from this environment: no browser
binary is installed here (Chromium download is blocked by this sandbox's
network policy) and network access to insight.erajaya.com is blocked
too. The DOM selectors below (login, dashboard, Download menu, "Data"
item, "Full Data" tab, the popup's own Download button) are best-effort,
built from accessible roles/names per Tableau's general UI conventions,
refined against real local runs. If a selector doesn't match the live
page:
  - run with HEADLESS=false to watch it interactively, and
  - check <EXPORT_DIR>/debug_<step>.png, saved automatically on failure,
  - check the [NETDEBUG]-prefixed log lines for export/tempfile network
    activity, and
  - then adjust the candidate locator lists in _login_if_needed() /
    _wait_for_dashboard() / _export_crosstab_csv() /
    _switch_to_full_data_and_download() below to match what you see.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .exceptions import BrowserAutomationError

# Query parameter names redacted from any URL before it's printed --
# Tableau's own popup/tempfile URLs embed session/ticket-like values
# under names like these (e.g. the "View Data" popup's own
# ".../index.html?id=...&moduleId=view_data").
SENSITIVE_URL_PARAM_NAMES = {
    "id",
    "key",
    "session",
    "sessionid",
    "session_id",
    "token",
    "auth",
    "authtoken",
    "ticket",
    "sid",
    "xsrf",
    "csrf",
}

# Every VizQL command/tempfile URL this module builds carries the session
# id as a PATH segment (".../sessions/{id}/..."), not a query parameter --
# SENSITIVE_URL_PARAM_NAMES above only covers the query string, so this
# catches the path form too before a URL is ever printed.
SESSION_PATH_SEGMENT_RE = re.compile(r"(/sessions/)[^/?]+")

DASHBOARD_URL = "https://insight.erajaya.com/#/views/ReturnRotate/Dashboard1?:id=1"
EXPORT_FILENAME_PREFIX = "ReturnRotate"

GOTO_TIMEOUT_MS = 30_000
LOGIN_DETECT_TIMEOUT_MS = 15_000
LOGIN_SUBMIT_TIMEOUT_MS = 20_000
DASHBOARD_LOAD_TIMEOUT_MS = 60_000
POLL_INTERVAL_MS = 500
VIEW_DATA_POPUP_TIMEOUT_MS = 30_000
NETWORK_EXPORT_TIMEOUT_MS = 30_000

# The HAR-confirmed URL fragments of the two network calls that matter for
# the actual export, regardless of which page (main dashboard or the
# "View Data" popup) they're observed on: the VizQL command that generates
# the server-side CSV, and the tempfile GET that actually delivers it.
# Used both for diagnostic logging (redacted) and for the tempfile-response
# wait that replaces Playwright's `download` event -- see
# "Network-based export" in CLAUDE.md for why.
#
# A real HAR capture of the exact manual Full Data flow (see CLAUDE.md,
# "The Full Data export needs real UI clicks") confirmed the command
# actually needed is "export-view-data-underlying-to-csv-server" --
# "export-view-data-summary-to-csv-server" never appears in that HAR at
# all. The "summary" command is what fires if the popup is left on its
# default Summary tab; it is deliberately NOT treated as this project's
# target export.
EXPORT_COMMAND_URL_SUBSTRING = "export-view-data-underlying-to-csv-server"
TEMPFILE_URL_SUBSTRING = "/tempfile/"

DOWNLOAD_BUTTON_CANDIDATES = [
    lambda s: s.get_by_role("button", name=re.compile(r"^download$", re.I)),
    lambda s: s.locator('[data-tb-test-id="download-Button"]'),
    lambda s: s.locator('button[aria-label="Download" i]'),
]

# The "View Data" popup's own UI -- confirmed by the HAR to require two
# real clicks to reach Full Data (it is not the popup's default tab):
# switch to "Full Data", then click "Download". An earlier one-shot DOM
# diagnostic run immediately after domcontentloaded found zero buttons,
# but that only proves the popup hadn't finished hydrating at that exact
# instant -- not that it has no DOM (see module docstring). Best-effort,
# accessible-role-first like every other selector list in this module;
# not yet confirmed against the live popup's actual markup.
FULL_DATA_TAB_CANDIDATES = [
    lambda s: s.get_by_role("tab", name=re.compile(r"^full data$", re.I)),
    lambda s: s.get_by_role("button", name=re.compile(r"^full data$", re.I)),
    lambda s: s.get_by_role("menuitem", name=re.compile(r"^full data$", re.I)),
    lambda s: s.get_by_text(re.compile(r"^full data$", re.I)),
]

VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES = [
    lambda s: s.get_by_role("button", name=re.compile(r"^download$", re.I)),
    lambda s: s.locator('[data-tb-test-id="download-Button"]'),
    lambda s: s.locator('button[aria-label="Download" i]'),
]

PASSWORD_FIELD_CANDIDATES = [
    lambda s: s.get_by_label(re.compile("password", re.I)),
    lambda s: s.locator('input[type="password"]'),
    lambda s: s.locator("#password"),
    lambda s: s.locator('input[name="password"]'),
]

USERNAME_FIELD_CANDIDATES = [
    lambda s: s.get_by_label(re.compile("username", re.I)),
    lambda s: s.locator('input[name="username"]'),
    lambda s: s.locator("#username"),
    lambda s: s.locator('input[type="email"]'),
    lambda s: s.locator('input[type="text"]').first,
]

SIGN_IN_BUTTON_CANDIDATES = [
    lambda s: s.get_by_role("button", name=re.compile(r"sign\s*in", re.I)),
    lambda s: s.get_by_text(re.compile(r"^sign in$", re.I)),
    lambda s: s.locator('button[type="submit"]'),
]

def generate_export_filename(now=None) -> str:
    from datetime import datetime

    now = now or datetime.now()
    return f"{EXPORT_FILENAME_PREFIX}_{now:%Y%m%d_%H%M%S}.csv"


def resolve_headless() -> bool:
    value = os.environ.get("HEADLESS", "true").strip().lower()
    return value not in ("false", "0", "no")


def resolve_executable_path() -> str | None:
    value = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH", "").strip()
    return value or None


def _find_first(scopes, locator_factories):
    """Tries each locator factory against each scope (page, then each
    frame) in order; returns the first match with at least one visible
    element, else None. Never raises.

    This is a single, instant check -- Locator.count() does not wait for
    anything to render. Use _find_first_with_wait() for anything that
    might still be rendering (e.g. right after a navigation)."""
    for scope in scopes:
        for factory in locator_factories:
            try:
                locator = factory(scope)
                if locator.count() > 0:
                    return locator.first
            except Exception:
                continue
    return None


def _find_first_with_wait(page, locator_factories, timeout_ms):
    """Polls _find_first() across the main page and all frames until a
    match appears or the timeout elapses, returning None on timeout.

    Needed because a freshly-navigated single-page app may not have
    rendered its form yet at the instant we check -- an instant
    _find_first() call can false-negative on a login form that is
    visually present a moment later (the original cause of this
    function's login-detection bug: the check ran before Tableau's login
    page had finished rendering, so it looked like there was no login
    form at all and login was silently skipped)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        scopes = [page, *page.frames]
        found = _find_first(scopes, locator_factories)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(POLL_INTERVAL_MS)


def _save_debug_screenshot(page, export_dir: Path, label: str) -> None:
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(export_dir / f"debug_{label}.png"))
    except Exception:
        pass  # best-effort only -- never let debugging aid break the real error


def _login_if_needed(page, username: str, password: str) -> None:
    # Poll for the password field rather than checking once: right after
    # navigation, Tableau's login page may not have finished rendering
    # yet, and an instant check would wrongly conclude no login is needed.
    password_field = _find_first_with_wait(page, PASSWORD_FIELD_CANDIDATES, LOGIN_DETECT_TIMEOUT_MS)
    if password_field is None:
        return  # already authenticated, or no login form present

    print("Logging in...")

    username_field = _find_first_with_wait(page, USERNAME_FIELD_CANDIDATES, LOGIN_DETECT_TIMEOUT_MS)
    if username_field is None:
        raise BrowserAutomationError("Login page detected but no username field could be found.")

    username_field.fill(username)
    password_field.fill(password)

    submit_button = _find_first_with_wait(page, SIGN_IN_BUTTON_CANDIDATES, LOGIN_DETECT_TIMEOUT_MS)
    if submit_button is None:
        raise BrowserAutomationError("Login form found but no submit button could be located.")

    submit_button.click()

    # Confirm sign-in actually completed before moving on, instead of
    # assuming the click worked: wait for the password field to go away
    # (covers both a full page navigation and an in-place SPA transition
    # to the dashboard), and best-effort let network activity settle.
    try:
        password_field.wait_for(state="hidden", timeout=LOGIN_SUBMIT_TIMEOUT_MS)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=LOGIN_SUBMIT_TIMEOUT_MS)
    except Exception:
        pass


def _wait_for_dashboard(page) -> None:
    deadline = time.monotonic() + DASHBOARD_LOAD_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        scopes = [page, *page.frames]
        if _find_first(scopes, DOWNLOAD_BUTTON_CANDIDATES) is not None:
            return
        page.wait_for_timeout(POLL_INTERVAL_MS)
    raise BrowserAutomationError(
        "Timed out waiting for the Tableau dashboard to load (Download button never appeared)."
    )


def _is_export_command_url(url: str) -> bool:
    return EXPORT_COMMAND_URL_SUBSTRING in url


def _is_tempfile_url(url: str) -> bool:
    return TEMPFILE_URL_SUBSTRING in url


def _is_export_related_url(url: str) -> bool:
    return _is_export_command_url(url) or _is_tempfile_url(url)


def _response_content_type(response) -> str:
    try:
        return (response.headers or {}).get("content-type", "")
    except Exception:
        return ""


def _log_export_request(request) -> None:
    """Diagnostic-only request logger: fires for every request on a page
    this is attached to, but only prints (redacted) ones matching the
    HAR-confirmed export/tempfile URL fragments. Never logs request
    bodies (per instruction -- they may carry runtime Tableau state) or
    headers. Never raises -- a broken accessor here must not break the
    real export attempt."""
    try:
        if _is_export_related_url(request.url):
            print(f"[NETDEBUG] request  method={request.method} url={_redact_url(request.url)}")
    except Exception:
        pass


def _log_export_response(response) -> None:
    """Diagnostic-only response logger, matching _log_export_request --
    reports status and Content-Type (never the body) for URLs matching
    the same export/tempfile fragments, with the tempfile match flagged
    explicitly since that's the one carrying the actual file."""
    try:
        if _is_export_related_url(response.url):
            marker = " <-- tempfile response" if _is_tempfile_url(response.url) else ""
            print(
                f"[NETDEBUG] response status={response.status} "
                f"content_type={_response_content_type(response)!r} "
                f"url={_redact_url(response.url)}{marker}"
            )
    except Exception:
        pass


class _CapturedTempfile:
    """An immutable snapshot of the tempfile response's status/headers/
    body, taken eagerly at capture time -- matches the small subset of
    Playwright's Response/APIResponse interface _save_tempfile_response()
    actually uses, so that function needs no changes. Snapshotting
    eagerly (rather than holding the live Response object) avoids
    depending on it staying readable after the page/context that
    produced it moves on."""

    def __init__(self, url: str, status: int, headers: dict, body: bytes):
        self.url = url
        self.status = status
        self.headers = headers
        self._body = body

    def body(self) -> bytes:
        return self._body


class _TempfileCapture:
    """Holds the tempfile response once observed, populated by
    _capture_tempfile_response()."""

    def __init__(self):
        self.response: _CapturedTempfile | None = None


def _capture_tempfile_response(response, capture: _TempfileCapture) -> None:
    """Passively snapshots the first observed `.../tempfile/...`
    response. A real-server run showed Tableau's own "View Data" popup
    JS fires the entire export-view-data-summary-to-csv-server ->
    tempfile sequence automatically, with no click and no command issued
    by this project at all -- this captures that real response directly
    instead of reconstructing/reissuing anything ourselves. Never logs
    the body. Never raises."""
    try:
        if capture.response is not None or not _is_tempfile_url(response.url):
            return
        capture.response = _CapturedTempfile(
            url=response.url,
            status=response.status,
            headers=dict(response.headers or {}),
            body=response.body(),
        )
    except Exception:
        pass


def _attach_export_network_diagnostics(pages, tempfile_capture: _TempfileCapture | None = None) -> None:
    """Attaches the request/response loggers above to each given page
    (typically the main dashboard page and the "View Data" popup page --
    a real-server diagnostic could not tell which one the export command
    actually fires on, so both are covered per CLAUDE.md's "Network-based
    export"). Playwright's page-level "request"/"response" events also
    bubble to the browser context, so this alone is enough to observe
    traffic regardless of which of these two pages originates it.

    When `tempfile_capture` is given, also attaches
    _capture_tempfile_response() alongside the loggers -- confirmed by a
    real-server run to reliably observe the real, complete tempfile
    response the popup's own JS fires on its own. Safe to call on
    lightweight test fakes that don't support `.on()` -- failures are
    reported but never raised."""
    for page in pages:
        try:
            page.on("request", _log_export_request)
            page.on("response", _log_export_response)
            if tempfile_capture is not None:
                page.on("response", lambda response: _capture_tempfile_response(response, tempfile_capture))
        except Exception as exc:
            print(f"[NETDEBUG] could not attach network listeners to a page: {exc}")


def _wait_for_tempfile_capture(driver_page, capture: _TempfileCapture, timeout_ms) -> None:
    """Polls (no fixed sleep) until _capture_tempfile_response() has
    observed the real tempfile response, or raises on timeout."""
    deadline = time.monotonic() + timeout_ms / 1000
    while capture.response is None:
        if time.monotonic() >= deadline:
            raise BrowserAutomationError(
                "Never observed the View Data tempfile response (see the [NETDEBUG] log "
                "above for whatever export-related network activity was actually observed)."
            )
        driver_page.wait_for_timeout(POLL_INTERVAL_MS)


def _extract_filename_from_content_disposition(content_disposition: str | None) -> str | None:
    """Pulls a filename out of a Content-Disposition header value (plain
    `filename=` or RFC 5987 `filename*=`). This is response metadata, not
    file contents, so it's safe to use -- but it's still attacker/
    server-controlled, so the caller must sanitize it before treating it
    as a filesystem path (see _sanitize_filename)."""
    if not content_disposition:
        return None
    for part in content_disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            value = part.split("=", 1)[1]
            return value.split("''", 1)[-1].strip('"')
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return None


def _sanitize_filename(name: str | None) -> str | None:
    """Reduces a server-supplied filename to a bare, safe basename before
    it's ever joined onto EXPORT_DIR -- strips any directory components
    (so a malicious/odd `Content-Disposition` can't write outside
    EXPORT_DIR) and replaces filesystem-hostile characters. Returns None
    if nothing usable remains, so the caller can fall back to a generated
    name instead."""
    if not name:
        return None
    base = os.path.basename(name.replace("\\", "/")).strip()
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)
    return base or None


def _save_tempfile_response(response, export_dir: Path) -> Path:
    """Saves a captured `.../tempfile/...` response -- the HAR-confirmed
    final step of the real export flow -- to EXPORT_DIR. Verifies the
    Content-Type looks like the expected CSV/text export before writing
    anything, since this is meant to replace Playwright's `download`
    event as the success signal, not blindly trust any matching URL.
    Prefers the filename Tableau itself supplied via Content-Disposition
    (sanitized -- see _sanitize_filename), falling back to a generated
    timestamped name if that header is absent or unusable."""
    if response.status >= 400:
        raise BrowserAutomationError(
            f"Tempfile response returned HTTP {response.status}; refusing to save it."
        )

    content_type = _response_content_type(response)
    if "csv" not in content_type.lower() and "text" not in content_type.lower():
        raise BrowserAutomationError(
            f"Tempfile response had an unexpected Content-Type ({content_type!r}); refusing to save it."
        )

    body = response.body()

    content_disposition = None
    try:
        content_disposition = (response.headers or {}).get("content-disposition")
    except Exception:
        pass
    filename = _sanitize_filename(_extract_filename_from_content_disposition(content_disposition))

    export_dir.mkdir(parents=True, exist_ok=True)
    target_path = export_dir / (filename or generate_export_filename())
    target_path.write_bytes(body)
    return target_path


def _export_crosstab_csv(page, export_dir: Path):
    def scopes():
        return [page, *page.frames]

    print("Opening Download menu...")
    download_button = _find_first(scopes(), DOWNLOAD_BUTTON_CANDIDATES)
    if download_button is None:
        _save_debug_screenshot(page, export_dir, "download-button-not-found")
        raise BrowserAutomationError("Could not find the Download toolbar button.")
    download_button.click()

    print("Opening Data...")
    data_item = _find_first(
        scopes(),
        [
            lambda s: s.get_by_role("menuitem", name=re.compile(r"^data$", re.I)),
            lambda s: s.get_by_text(re.compile(r"^data$", re.I)),
        ],
    )
    if data_item is None:
        _save_debug_screenshot(page, export_dir, "data-menuitem-not-found")
        raise BrowserAutomationError("Could not find the 'Data' item in the Download menu.")

    tempfile_capture = _TempfileCapture()
    _attach_export_network_diagnostics([page], tempfile_capture)

    # Clicking "Data" opens a separate "View Data" popup window/tab (a new
    # Playwright Page in the same browser context, titled
    # "Hybrid Popup Window") -- not an in-page dialog. This click is kept
    # (confirmed working against the real server). A real-server run
    # showed this popup's own JS then fires the entire HAR export
    # sequence AUTOMATICALLY on its own -- no further click, no command
    # issued by this project -- so nothing more is triggered here; see
    # _wait_for_tempfile_capture() below.
    try:
        with page.expect_popup(timeout=VIEW_DATA_POPUP_TIMEOUT_MS) as popup_info:
            data_item.click()
        view_data_page = popup_info.value
        view_data_page.wait_for_load_state("domcontentloaded", timeout=VIEW_DATA_POPUP_TIMEOUT_MS)
    except Exception as exc:
        _save_debug_screenshot(page, export_dir, "view-data-popup-not-opened")
        raise BrowserAutomationError(f"The 'View Data' popup window did not open: {exc}") from exc

    _attach_export_network_diagnostics([view_data_page], tempfile_capture)

    # --- TEMPORARY DEBUG ONLY -- kept per explicit instruction even though
    # a real-server run already showed this popup's own DOM is effectively
    # empty (see CLAUDE.md); still useful context if a future Tableau
    # update changes that. Never touches credentials/cookies/tokens.
    _debug_dump_view_data_popup(view_data_page, export_dir)

    _switch_to_full_data_and_download(view_data_page, export_dir)

    _wait_for_tempfile_capture(view_data_page, tempfile_capture, NETWORK_EXPORT_TIMEOUT_MS)
    return _save_tempfile_response(tempfile_capture.response, export_dir)


def _switch_to_full_data_and_download(view_data_page, export_dir: Path) -> None:
    """Drives the two real UI clicks a HAR capture of the exact manual
    workflow confirmed are required to get the Full Data export: the
    popup opens on its default "Summary" tab (which is what
    export-view-data-summary-to-csv-server would produce, if left
    alone), so this switches to "Full Data" first, then clicks
    "Download" -- only then does the popup's own JS fire
    export-view-data-underlying-to-csv-server (see CLAUDE.md, "The Full
    Data export needs real UI clicks").

    Polls for each control (no fixed sleep, reusing the existing
    VIEW_DATA_POPUP_TIMEOUT_MS) instead of checking once -- an earlier
    one-shot DOM dump ran immediately after domcontentloaded and found
    zero buttons, which only proves the popup hadn't finished hydrating
    at that exact instant, not that it has no DOM.

    Deliberately does NOT fall back to clicking Download when "Full
    Data" can't be found -- that would silently export Summary instead
    of failing loudly."""
    print("Waiting for View Data popup to render...")
    full_data_tab = _find_first_with_wait(view_data_page, FULL_DATA_TAB_CANDIDATES, VIEW_DATA_POPUP_TIMEOUT_MS)
    if full_data_tab is None:
        _save_debug_screenshot(view_data_page, export_dir, "full-data-tab-not-found")
        raise BrowserAutomationError(
            'Could not find the "Full Data" tab in the View Data window -- refusing to '
            "click Download without it, since that would export Summary instead."
        )

    print("Switching to Full Data...")
    full_data_tab.click()

    download_button = _find_first_with_wait(
        view_data_page, VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES, VIEW_DATA_POPUP_TIMEOUT_MS
    )
    if download_button is None:
        _save_debug_screenshot(view_data_page, export_dir, "view-data-download-button-not-found")
        raise BrowserAutomationError(
            'Could not find the "Download" button in the View Data window after switching to Full Data.'
        )

    print("Exporting Full Data...")
    download_button.click()


def _redact_url(url: str) -> str:
    """Redacts query parameter VALUES whose name looks session/auth/token
    related (see SENSITIVE_URL_PARAM_NAMES), so a URL is safe to print
    even though it may embed Tableau's own internal session/ticket
    identifiers (as seen in the "View Data" popup's own URL). Also
    redacts the path segment immediately following "/sessions/" -- every
    VizQL command and tempfile URL this module builds itself carries the
    session id there, in the PATH, not as a query parameter, so it would
    otherwise leak straight through the [NETDEBUG] logs untouched. Never
    touches the scheme/host or any other path segment, and never raises
    -- an unparseable URL is reported as such rather than printed raw."""
    try:
        parts = urlsplit(url)
        redacted_params = [
            (name, "REDACTED" if name.lower() in SENSITIVE_URL_PARAM_NAMES else value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        redacted_query = urlencode(redacted_params)
        redacted_path = SESSION_PATH_SEGMENT_RE.sub(r"\1REDACTED", parts.path)
        return urlunsplit((parts.scheme, parts.netloc, redacted_path, redacted_query, parts.fragment))
    except Exception:
        return "[unparseable URL]"


# Evaluated once per frame (including the main frame) by _inspect_frame().
# Reads only visible UI text/attributes and structural DOM shape -- never
# cookies, storage, or anything credential-shaped. Covers plain DOM
# (buttons, "Download" text matches, nested iframes), canvas-rendered UI
# (dimensions + surrounding HTML, since Tableau sometimes renders via
# canvas/WebGL rather than plain DOM elements), and open shadow DOM
# (recursively, since a Tableau "hybrid" popup could plausibly use web
# components that hide their real content behind a shadow root).
_FRAME_INSPECTION_SCRIPT = """
() => {
    function isVisible(el) {
        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    }
    function summarize(el) {
        return {
            tag: el.tagName,
            text: (el.innerText || el.textContent || '').trim().slice(0, 80),
            aria_label: el.getAttribute('aria-label'),
            title: el.getAttribute('title'),
            data_tb_test_id: el.getAttribute('data-tb-test-id'),
            class_name: el.className && el.className.toString ? el.className.toString().slice(0, 120) : String(el.className),
        };
    }
    function looksLikeDownload(el) {
        const text = (el.innerText || el.textContent || '');
        const aria = el.getAttribute('aria-label') || '';
        const title = el.getAttribute('title') || '';
        return (/download/i.test(text) && text.trim().length < 60) || /download/i.test(aria) || /download/i.test(title);
    }

    const body = document.body;
    const bodyInnerHtmlLength = body ? body.innerHTML.length : 0;
    const bodyInnerText = body ? (body.innerText || '').slice(0, 3000) : '';

    const buttons = Array.from(document.querySelectorAll('button, [role="button"], [role="menuitem"]'))
        .filter(isVisible)
        .map(summarize);

    const downloadElements = Array.from(document.querySelectorAll('*'))
        .filter(el => looksLikeDownload(el) && isVisible(el))
        .slice(0, 20)
        .map(summarize);

    const iframes = Array.from(document.querySelectorAll('iframe')).map(f => ({
        src: f.src || null,
        name: f.name || null,
        id: f.id || null,
    }));

    function inspectShadowRoots(root, depth) {
        if (depth > 5) return [];
        const results = [];
        for (const el of root.querySelectorAll('*')) {
            if (el.shadowRoot) {
                results.push({
                    host_tag: el.tagName,
                    host_class: el.className ? String(el.className).slice(0, 120) : null,
                    buttons: Array.from(el.shadowRoot.querySelectorAll('button, [role="button"]'))
                        .filter(isVisible).map(summarize),
                    download_elements: Array.from(el.shadowRoot.querySelectorAll('*'))
                        .filter(looksLikeDownload).slice(0, 10).map(summarize),
                });
                results.push(...inspectShadowRoots(el.shadowRoot, depth + 1));
            }
        }
        return results;
    }
    const shadowRoots = inspectShadowRoots(document, 0);

    const canvases = Array.from(document.querySelectorAll('canvas')).map(c => {
        const parent = c.parentElement;
        const grandparent = parent ? parent.parentElement : null;
        return {
            width: c.width,
            height: c.height,
            client_width: c.clientWidth,
            client_height: c.clientHeight,
            parent_html: parent && parent.outerHTML ? parent.outerHTML.slice(0, 500) : null,
            grandparent_html: grandparent && grandparent.outerHTML ? grandparent.outerHTML.slice(0, 500) : null,
        };
    });

    const htmlPreview = document.documentElement ? document.documentElement.outerHTML.slice(0, 3000) : '';

    return {
        body_inner_html_length: bodyInnerHtmlLength,
        body_inner_text: bodyInnerText,
        buttons: buttons,
        download_elements: downloadElements,
        iframes: iframes,
        shadow_roots: shadowRoots,
        canvases: canvases,
        html_preview: htmlPreview,
    };
}
"""


def _inspect_frame(frame) -> dict:
    """Runs _FRAME_INSPECTION_SCRIPT against a single frame (main or
    nested), returning its result dict or {"error": "..."} on failure.
    Never raises."""
    try:
        return frame.evaluate(_FRAME_INSPECTION_SCRIPT)
    except Exception as exc:
        return {"error": str(exc)}


def _debug_dump_view_data_popup(view_data_page, export_dir: Path) -> None:
    """TEMPORARY DEBUG ONLY: dumps DOM diagnostics for the "View Data"
    popup so its real Download control can be identified precisely
    instead of guessed. Entirely best-effort -- every failure here is
    caught and reported, never allowed to break the actual export
    attempt that follows. Prints only visible UI text/attributes/structure
    and saves a screenshot; never credentials, cookies, tokens, or
    XSRF/session/auth values (URLs are redacted before printing)."""
    print(f"[DEBUG] popup.url = {_redact_url(view_data_page.url)}")
    try:
        print(f"[DEBUG] popup.title() = {view_data_page.title()}")
    except Exception as exc:
        print(f"[DEBUG] popup.title() failed: {exc}")

    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = export_dir / "debug_view_data_popup.png"
        view_data_page.screenshot(path=str(screenshot_path))
        print(f"[DEBUG] screenshot saved to {screenshot_path}")
    except Exception as exc:
        print(f"[DEBUG] screenshot failed: {exc}")

    try:
        frames = list(view_data_page.frames)
    except Exception as exc:
        frames = []
        print(f"[DEBUG] popup.frames enumeration failed: {exc}")

    print(f"[DEBUG] popup.frames ({len(frames)}):")
    for index, frame in enumerate(frames):
        try:
            print(f"[DEBUG]   frame[{index}] url={_redact_url(frame.url)} name={frame.name!r}")
        except Exception as exc:
            print(f"[DEBUG]   frame[{index}] inspection of url/name failed: {exc}")

    for index, frame in enumerate(frames):
        print(f"[DEBUG] --- frame[{index}] DOM inspection ---")
        info = _inspect_frame(frame)
        if "error" in info:
            print(f"[DEBUG]   frame[{index}] evaluate() failed: {info['error']}")
            continue

        print(f"[DEBUG]   body_inner_html_length = {info.get('body_inner_html_length')}")
        print(f"[DEBUG]   body_inner_text (truncated) = {info.get('body_inner_text')!r}")

        buttons = info.get("buttons", [])
        print(f"[DEBUG]   visible buttons/menuitems ({len(buttons)}):")
        for button in buttons:
            print(f"[DEBUG]     {button}")

        download_elements = info.get("download_elements", [])
        print(f"[DEBUG]   elements containing 'Download' ({len(download_elements)}):")
        for element in download_elements:
            print(f"[DEBUG]     {element}")

        iframes = info.get("iframes", [])
        print(f"[DEBUG]   nested iframes ({len(iframes)}):")
        for iframe in iframes:
            iframe_redacted = dict(iframe)
            if iframe_redacted.get("src"):
                iframe_redacted["src"] = _redact_url(iframe_redacted["src"])
            print(f"[DEBUG]     {iframe_redacted}")

        shadow_roots = info.get("shadow_roots", [])
        print(f"[DEBUG]   open shadow roots ({len(shadow_roots)}):")
        for shadow in shadow_roots:
            print(f"[DEBUG]     {shadow}")

        canvases = info.get("canvases", [])
        print(f"[DEBUG]   canvas elements ({len(canvases)}):")
        for canvas in canvases:
            print(f"[DEBUG]     {canvas}")

        print(f"[DEBUG]   html_preview (truncated) = {info.get('html_preview')!r}")

    try:
        context_pages = view_data_page.context.pages
        print(f"[DEBUG] browser context has {len(context_pages)} page(s):")
        for index, context_page in enumerate(context_pages):
            try:
                page_title = context_page.title()
            except Exception as exc:
                page_title = f"<title() failed: {exc}>"
            print(f"[DEBUG]   page[{index}] url={_redact_url(context_page.url)} title={page_title!r}")
    except Exception as exc:
        print(f"[DEBUG] browser context page enumeration failed: {exc}")


def run_browser_crosstab_export(
    server_url: str,
    username: str,
    password: str,
    export_dir: str,
    headless: bool = True,
    executable_path: str | None = None,
) -> Path:
    """Drives a real browser through Tableau's login + Download -> Data ->
    Crosstab flow and saves the resulting CSV. Raises BrowserAutomationError
    on any step it can't complete; never logs credentials, cookies, or
    session/download tokens."""
    from playwright.sync_api import sync_playwright

    export_dir_path = Path(export_dir)

    with sync_playwright() as playwright:
        launch_kwargs = {"headless": headless}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            print("Opening Tableau...")
            page.goto(server_url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)

            _login_if_needed(page, username, password)

            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            _wait_for_dashboard(page)
            print("Dashboard loaded.")

            return _export_crosstab_csv(page, export_dir_path)
        finally:
            browser.close()
