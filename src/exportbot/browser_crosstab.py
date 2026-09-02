"""Browser-automated Tableau Crosstab CSV export via Playwright.

Reproduces the exact manual workflow already confirmed to work in the
live Tableau UI: Download -> Data -> (a separate "View Data" popup
window opens) -> Download (within that popup) -> CSV download. This
exists because the REST/VizQL investigation (see CLAUDE.md) could not
establish a session without a real browser -- the VizQL session id and
per-visual context are minted client-side during interactive page
rendering. Browser automation sidesteps that entirely by driving an
actual browser through Tableau's native login and viz UI, exactly as a
person would.

NOT exercised against the real server from this environment: no browser
binary is installed here (Chromium download is blocked by this sandbox's
network policy) and network access to insight.erajaya.com is blocked
too. The DOM selectors below are best-effort, built from accessible
roles/names per Tableau's general UI conventions, refined against real
local runs and one real screenshot of the "View Data" popup -- but full
DOM inspection of this specific dashboard was never possible from here.
If a selector doesn't match the live page:
  - run with HEADLESS=false to watch it interactively, and
  - check <EXPORT_DIR>/debug_<step>.png, saved automatically on failure,
  - then adjust the candidate locator lists in _login_if_needed() /
    _wait_for_dashboard() / _export_crosstab_csv() /
    _export_from_view_data_popup() below to match what you see.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .exceptions import BrowserAutomationError

DASHBOARD_URL = "https://insight.erajaya.com/#/views/ReturnRotate/Dashboard1?:id=1"
EXPORT_FILENAME_PREFIX = "ReturnRotate"

GOTO_TIMEOUT_MS = 30_000
LOGIN_DETECT_TIMEOUT_MS = 15_000
LOGIN_SUBMIT_TIMEOUT_MS = 20_000
DASHBOARD_LOAD_TIMEOUT_MS = 60_000
DOWNLOAD_TIMEOUT_MS = 30_000
POLL_INTERVAL_MS = 500
VIEW_DATA_POPUP_TIMEOUT_MS = 30_000

DOWNLOAD_BUTTON_CANDIDATES = [
    lambda s: s.get_by_role("button", name=re.compile(r"^download$", re.I)),
    lambda s: s.locator('[data-tb-test-id="download-Button"]'),
    lambda s: s.locator('button[aria-label="Download" i]'),
]

# The "View Data" popup window's own toolbar Download button -- a
# separate DOM/page from the main dashboard's, confirmed via a real run:
# clicking Download -> Data opens a distinct browser window/tab titled
# "View Data: <sheet name>" (URL under /vizql/.../hybrid-window/...) with
# its own "Summary"/"Full Data" tabs and this Download button. Not
# verified against the live DOM beyond the screenshot -- best-effort,
# accessible-role-first like every other selector in this module.
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

# Tableau glass-pane/dialog overlays observed in a real run to intercept
# pointer events on the final export button even while Playwright reports
# it visible/enabled/stable -- causing Locator.click() to retry-and-timeout
# instead of ever landing. Waited out before the final click only.
OVERLAY_SELECTORS = [
    "#loadingGlassPane",
    '[data-tb-test-id="Dialog-Provider-Dialog-Glass"]',
]
OVERLAY_CLEAR_TIMEOUT_MS = 30_000


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


def _wait_for_overlays_to_clear(page) -> None:
    """Waits for known Tableau glass-pane/dialog overlays to become
    hidden before the final export click is attempted.

    Locator.wait_for(state="hidden") is satisfied both by an element that
    is genuinely hidden/detached AND by a selector that matches nothing
    at all -- so an overlay that doesn't exist on this page resolves
    immediately here, never an error. Only a real, still-visible overlay
    that never clears raises (a TimeoutError), which is intentionally
    left to propagate to the caller -- that's a real, actionable failure
    worth surfacing distinctly, not something to silently swallow."""
    for selector in OVERLAY_SELECTORS:
        for scope in [page, *page.frames]:
            scope.locator(selector).first.wait_for(state="hidden", timeout=OVERLAY_CLEAR_TIMEOUT_MS)


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

    # Clicking "Data" opens a separate "View Data" popup window/tab (a new
    # Playwright Page in the same browser context, titled
    # "View Data: <sheet name>") -- not an in-page dialog. The actual CSV
    # export happens entirely within that popup, never on this page.
    try:
        with page.expect_popup(timeout=VIEW_DATA_POPUP_TIMEOUT_MS) as popup_info:
            data_item.click()
        view_data_page = popup_info.value
        view_data_page.wait_for_load_state("domcontentloaded", timeout=VIEW_DATA_POPUP_TIMEOUT_MS)
    except Exception as exc:
        _save_debug_screenshot(page, export_dir, "view-data-popup-not-opened")
        raise BrowserAutomationError(f"The 'View Data' popup window did not open: {exc}") from exc

    # --- TEMPORARY DEBUG ONLY -- remove once the real Download control in
    # the "View Data" popup is confirmed and VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES
    # is updated to match it. Never touches credentials/cookies/tokens.
    _debug_dump_view_data_popup(view_data_page, export_dir)

    print("Exporting Crosstab...")
    return _export_from_view_data_popup(view_data_page, export_dir)


def _debug_dump_view_data_popup(view_data_page, export_dir: Path) -> None:
    """TEMPORARY DEBUG ONLY: dumps DOM diagnostics for the "View Data"
    popup so its real Download control can be identified precisely
    instead of guessed. Entirely best-effort -- every failure here is
    caught and reported, never allowed to break the actual export
    attempt that follows. Prints only visible UI text/attributes and
    saves a screenshot; never touches credentials, cookies, or tokens."""
    print(f"[DEBUG] popup.url = {view_data_page.url}")
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

    buttons_script = """
() => Array.from(document.querySelectorAll('button, [role="button"], [role="menuitem"]'))
    .filter(el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length))
    .map(el => ({
        tag: el.tagName,
        inner_text: (el.innerText || '').trim().slice(0, 80),
        aria_label: el.getAttribute('aria-label'),
        role: el.getAttribute('role'),
        data_tb_test_id: el.getAttribute('data-tb-test-id'),
        title: el.getAttribute('title'),
    }))
"""
    try:
        buttons = view_data_page.evaluate(buttons_script)
    except Exception as exc:
        buttons = []
        print(f"[DEBUG] button enumeration failed: {exc}")

    print(f"[DEBUG] visible buttons/menuitems ({len(buttons)}):")
    for button in buttons:
        print(f"[DEBUG]   {button}")

    download_text_script = """
() => Array.from(document.querySelectorAll('*'))
    .filter(el => {
        const text = (el.innerText || el.textContent || '');
        return text && /download/i.test(text) && text.trim().length < 60
            && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    })
    .slice(0, 20)
    .map(el => ({
        tag: el.tagName,
        text: (el.innerText || el.textContent || '').trim().slice(0, 80),
        aria_label: el.getAttribute('aria-label'),
        title: el.getAttribute('title'),
        data_tb_test_id: el.getAttribute('data-tb-test-id'),
        class_name: el.className && el.className.toString ? el.className.toString().slice(0, 120) : String(el.className),
    }))
"""
    try:
        download_elements = view_data_page.evaluate(download_text_script)
    except Exception as exc:
        download_elements = []
        print(f"[DEBUG] 'download' text element search failed: {exc}")

    print(f"[DEBUG] elements containing 'Download' text ({len(download_elements)}):")
    for element in download_elements:
        print(f"[DEBUG]   {element}")

    toolbar_html_script = """
() => {
    const candidates = Array.from(document.querySelectorAll('*')).filter(el => {
        const text = (el.innerText || el.textContent || '');
        return text && /download/i.test(text) && text.trim().length < 60
            && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    });
    if (candidates.length === 0) return null;
    const el = candidates[0];
    const parent = el.parentElement;
    const grandparent = parent ? parent.parentElement : null;
    return {
        element_html: el.outerHTML ? el.outerHTML.slice(0, 500) : null,
        parent_html: parent && parent.outerHTML ? parent.outerHTML.slice(0, 1500) : null,
        grandparent_html: grandparent && grandparent.outerHTML ? grandparent.outerHTML.slice(0, 2000) : null,
    };
}
"""
    try:
        toolbar_html = view_data_page.evaluate(toolbar_html_script)
    except Exception as exc:
        toolbar_html = None
        print(f"[DEBUG] toolbar HTML dump failed: {exc}")

    print(f"[DEBUG] toolbar HTML around first 'Download' match: {toolbar_html}")


def _export_from_view_data_popup(view_data_page, export_dir: Path) -> Path:
    """Performs the CSV export from within the "View Data" popup window
    opened by Download -> Data. This popup has its own toolbar Download
    button, entirely separate from the main dashboard's -- the download
    itself must be caught on this page, never on the original dashboard
    page. The main dashboard may show a "The View Data window is still
    open. Close the View Data window?" prompt after this -- that's a
    secondary cleanup dialog, not part of the export, and is intentionally
    left alone."""
    download_button = _find_first_with_wait(
        view_data_page, VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES, DASHBOARD_LOAD_TIMEOUT_MS
    )
    if download_button is None:
        _save_debug_screenshot(view_data_page, export_dir, "view-data-download-button-not-found")
        raise BrowserAutomationError('Could not find the "Download" button in the View Data window.')

    try:
        # Same glass-pane/dialog overlay risk as the main dashboard --
        # wait for it to clear before attempting the click.
        _wait_for_overlays_to_clear(view_data_page)
        download_button.wait_for(state="visible", timeout=OVERLAY_CLEAR_TIMEOUT_MS)
        with view_data_page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            download_button.click()
        download = download_info.value
    except Exception as exc:
        _save_debug_screenshot(view_data_page, export_dir, "view-data-download-click-failed")
        raise BrowserAutomationError(f"Could not export CSV from the View Data window: {exc}") from exc

    export_dir.mkdir(parents=True, exist_ok=True)
    target_path = export_dir / generate_export_filename()
    download.save_as(str(target_path))
    return target_path


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
