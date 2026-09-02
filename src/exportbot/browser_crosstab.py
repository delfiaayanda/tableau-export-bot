"""Browser-automated Tableau Crosstab CSV export via Playwright.

Reproduces the exact manual workflow already confirmed to work in the
live Tableau UI: Download -> Data -> (a separate "View Data" popup
window opens) -> the CSV export is captured over the network, not a DOM
click inside that popup (see below) -> saved to EXPORT_DIR. This exists
because the REST/VizQL investigation (see CLAUDE.md) could not establish
a session without a real browser -- the VizQL session id and per-visual
context are minted client-side during interactive page rendering.
Browser automation sidesteps that entirely by driving an actual browser
through Tableau's native login and viz UI, exactly as a person would.

A real-server diagnostic found the "View Data" popup's own top-level DOM
has zero visible buttons and zero "Download"-text elements -- it's a
"Hybrid Popup Window" wrapper, not a real dialog. So the final export
step (_export_from_view_data_popup()) does not rely on clicking a DOM
button and catching Playwright's `download` event at all: it waits
directly for the HAR-confirmed `.../tempfile/...` network response (at
the browser-context level, covering both the main dashboard page and the
popup, since either could originate it) and saves that response's body.
See "Network-based export" in CLAUDE.md for the full investigation.

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
  - check the [NETDEBUG]-prefixed log lines for export/tempfile network
    activity, and
  - then adjust the candidate locator lists in _login_if_needed() /
    _wait_for_dashboard() / _export_crosstab_csv() /
    _export_from_view_data_popup() below to match what you see.
"""

from __future__ import annotations

import os
import re
import time
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
EXPORT_COMMAND_URL_SUBSTRING = "export-view-data-summary-to-csv-server"
TEMPFILE_URL_SUBSTRING = "/tempfile/"

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


def _attach_export_network_diagnostics(pages) -> None:
    """Attaches the request/response loggers above to each given page
    (typically the main dashboard page and the "View Data" popup page --
    a real-server diagnostic could not tell which one the export command
    actually fires on, so both are covered per CLAUDE.md's "Network-based
    export"). Playwright's page-level "request"/"response" events also
    bubble to the browser context, so this alone is enough to observe
    traffic regardless of which of these two pages originates it. Safe to
    call on lightweight test fakes that don't support `.on()` -- failures
    are reported but never raised."""
    for page in pages:
        try:
            page.on("request", _log_export_request)
            page.on("response", _log_export_response)
        except Exception as exc:
            print(f"[NETDEBUG] could not attach network listeners to a page: {exc}")


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


def _find_first_with_wait_across_pages(pages, locator_factories, timeout_ms):
    """Same polling strategy as _find_first_with_wait(), but searches the
    main dashboard page and the "View Data" popup page (plus every frame
    of both) against a single shared deadline, instead of waiting the
    full timeout against one page before ever checking the other. Needed
    because a real-server diagnostic showed the popup's own top-level
    document can be empty of buttons entirely -- the real control, if any
    exists as a plain DOM element at all, could be on either page (see
    CLAUDE.md, "Network-based export")."""
    deadline = time.monotonic() + timeout_ms / 1000
    driver_page = pages[0]
    while True:
        scopes = []
        for page in pages:
            scopes.append(page)
            scopes.extend(page.frames)
        found = _find_first(scopes, locator_factories)
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            return None
        driver_page.wait_for_timeout(POLL_INTERVAL_MS)


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

    # --- TEMPORARY DEBUG ONLY -- kept per explicit instruction even though
    # a real-server run already showed this popup's own DOM is effectively
    # empty (see CLAUDE.md, "Network-based export"); still useful context
    # if a future Tableau update changes that. Never touches
    # credentials/cookies/tokens. The actual export below no longer
    # depends on this popup's DOM -- see _export_from_view_data_popup().
    _debug_dump_view_data_popup(view_data_page, export_dir)

    print("Exporting Crosstab...")
    return _export_from_view_data_popup(page, view_data_page, export_dir)


def _redact_url(url: str) -> str:
    """Redacts query parameter VALUES whose name looks session/auth/token
    related (see SENSITIVE_URL_PARAM_NAMES), so a URL is safe to print
    even though it may embed Tableau's own internal session/ticket
    identifiers (as seen in the "View Data" popup's own URL). Never
    touches the scheme/host/path, and never raises -- an unparseable URL
    is reported as such rather than printed raw."""
    try:
        parts = urlsplit(url)
        redacted_params = [
            (name, "REDACTED" if name.lower() in SENSITIVE_URL_PARAM_NAMES else value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
        redacted_query = urlencode(redacted_params)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, redacted_query, parts.fragment))
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


def _export_from_view_data_popup(page, view_data_page, export_dir: Path) -> Path:
    """Performs the CSV export triggered by the "View Data" popup window
    opened by Download -> Data.

    A real-server diagnostic (see CLAUDE.md, "Network-based export")
    found the popup's own top-level document has zero visible buttons and
    zero "Download"-text elements -- it's effectively a "Hybrid Popup
    Window" wrapper, not a real dialog, so no DOM-based click target can
    be reliably found inside it alone. Rather than invent a new locator,
    this now:

    1. Attaches diagnostic network logging (redacted) for the two
       HAR-confirmed URL fragments that matter -- the
       export-view-data-summary-to-csv-server VizQL command and the
       tempfile GET that delivers the actual file -- covering both this
       popup and the main dashboard page, since either could originate
       the request.
    2. Searches for a click target using the *existing*
       VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES list against BOTH the popup
       and the main dashboard page (never a new/guessed selector) --
       covering the hypothesis that the main page, not this popup, owns
       the real View Data UI and the popup is only a communication shell.
    3. Waits for the tempfile network response directly (at the browser
       context level, so it's caught regardless of which page it
       originates from) instead of Playwright's `download` event -- a
       fetch/blob-based export may never raise that event even on
       success, but the tempfile HTTP response fires either way.
    4. Reads that response's body directly and saves it -- never relying
       on Playwright's download manager at all.

    If no click target is found on either page, this raises rather than
    guessing -- the [NETDEBUG] log above (from step 1) is the next place
    to look, along with CLAUDE.md."""
    _attach_export_network_diagnostics([page, view_data_page])

    click_target = _find_first_with_wait_across_pages(
        [view_data_page, page], VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES, DASHBOARD_LOAD_TIMEOUT_MS
    )
    if click_target is None:
        _save_debug_screenshot(view_data_page, export_dir, "view-data-download-button-not-found")
        raise BrowserAutomationError(
            'Could not find a "Download" control in the View Data window or on the main '
            "dashboard page (see the [NETDEBUG] log above for any export-related network "
            "activity observed)."
        )

    try:
        # Same glass-pane/dialog overlay risk as the main dashboard --
        # wait for it to clear before attempting the click.
        _wait_for_overlays_to_clear(view_data_page)
        click_target.wait_for(state="visible", timeout=OVERLAY_CLEAR_TIMEOUT_MS)
        with view_data_page.context.expect_event(
            "response",
            predicate=lambda response: _is_tempfile_url(response.url),
            timeout=NETWORK_EXPORT_TIMEOUT_MS,
        ) as response_info:
            click_target.click()
        tempfile_response = response_info.value
    except Exception as exc:
        print("[NETDEBUG] tempfile response observed: False")
        _save_debug_screenshot(view_data_page, export_dir, "view-data-download-click-failed")
        raise BrowserAutomationError(f"Could not export CSV from the View Data window: {exc}") from exc

    print("[NETDEBUG] tempfile response observed: True")
    return _save_tempfile_response(tempfile_response, export_dir)


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
