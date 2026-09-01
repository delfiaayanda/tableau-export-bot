"""Browser-automated Tableau Crosstab CSV export via Playwright.

Reproduces the exact manual workflow already confirmed to work in the
live Tableau UI: Download -> Data -> (Crosstab view) -> CSV download.
This exists because the REST/VizQL investigation (see CLAUDE.md) could
not establish a session without a real browser -- the VizQL session id
and per-visual context are minted client-side during interactive page
rendering. Browser automation sidesteps that entirely by driving an
actual browser through Tableau's native login and viz UI, exactly as a
person would.

NOT exercised against the real server from this environment: no browser
binary is installed here (Chromium download is blocked by this sandbox's
network policy) and network access to insight.erajaya.com is blocked
too. The DOM selectors below are best-effort, built from accessible
roles/names per Tableau's general UI conventions -- no DOM inspection of
this specific dashboard was possible. If a selector doesn't match the
live page:
  - run with HEADLESS=false to watch it interactively, and
  - check <EXPORT_DIR>/debug_<step>.png, saved automatically on failure,
  - then adjust the candidate locator lists in _login_if_needed() /
    _wait_for_dashboard() / _export_crosstab_csv() below to match what
    you see.
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
LOGIN_TIMEOUT_MS = 20_000
DASHBOARD_LOAD_TIMEOUT_MS = 60_000
DOWNLOAD_TIMEOUT_MS = 30_000
POLL_INTERVAL_MS = 500

DOWNLOAD_BUTTON_CANDIDATES = [
    lambda s: s.get_by_role("button", name=re.compile(r"^download$", re.I)),
    lambda s: s.locator('[data-tb-test-id="download-Button"]'),
    lambda s: s.locator('button[aria-label="Download" i]'),
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
    element, else None. Never raises."""
    for scope in scopes:
        for factory in locator_factories:
            try:
                locator = factory(scope)
                if locator.count() > 0:
                    return locator.first
            except Exception:
                continue
    return None


def _save_debug_screenshot(page, export_dir: Path, label: str) -> None:
    try:
        export_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(export_dir / f"debug_{label}.png"))
    except Exception:
        pass  # best-effort only -- never let debugging aid break the real error


def _login_if_needed(page, username: str, password: str) -> None:
    password_candidates = [
        lambda s: s.get_by_label(re.compile("password", re.I)),
        lambda s: s.locator('input[type="password"]'),
        lambda s: s.locator("#password"),
    ]
    password_field = _find_first([page], password_candidates)
    if password_field is None:
        return  # already authenticated, or no login form present

    print("Logging in...")

    username_candidates = [
        lambda s: s.get_by_label(re.compile("username", re.I)),
        lambda s: s.locator('input[name="username"]'),
        lambda s: s.locator("#username"),
        lambda s: s.locator('input[type="text"]').first,
    ]
    username_field = _find_first([page], username_candidates)
    if username_field is None:
        raise BrowserAutomationError("Login page detected but no username field could be found.")

    username_field.fill(username)
    password_field.fill(password)

    submit_candidates = [
        lambda s: s.get_by_role("button", name=re.compile("sign in", re.I)),
        lambda s: s.locator('button[type="submit"]'),
    ]
    submit_button = _find_first([page], submit_candidates)
    if submit_button is None:
        raise BrowserAutomationError("Login form found but no submit button could be located.")

    submit_button.click()
    page.wait_for_load_state("domcontentloaded", timeout=LOGIN_TIMEOUT_MS)


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
    data_item.click()

    print("Exporting Crosstab...")
    # The "View Data" dialog may default to a crosstab-style summary tab;
    # click it explicitly if present, but don't fail if it's already selected.
    crosstab_tab = _find_first(
        scopes(),
        [
            lambda s: s.get_by_role("tab", name=re.compile("crosstab", re.I)),
            lambda s: s.get_by_role("button", name=re.compile("crosstab", re.I)),
            lambda s: s.get_by_text(re.compile("crosstab", re.I)),
        ],
    )
    if crosstab_tab is not None:
        crosstab_tab.click()

    export_button = _find_first(
        scopes(),
        [
            lambda s: s.get_by_role("button", name=re.compile(r"download.*(csv|all rows|crosstab)", re.I)),
            lambda s: s.get_by_role("button", name=re.compile(r"^export$", re.I)),
            lambda s: s.get_by_role("button", name=re.compile(r"^download$", re.I)),
        ],
    )
    if export_button is None:
        _save_debug_screenshot(page, export_dir, "export-button-not-found")
        raise BrowserAutomationError("Could not find the CSV export/download button in the Data dialog.")

    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
        export_button.click()
    download = download_info.value

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
