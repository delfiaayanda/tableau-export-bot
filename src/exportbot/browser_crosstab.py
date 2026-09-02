"""Browser-automated Tableau Crosstab CSV export via Playwright.

Login and dashboard navigation reproduce the exact manual workflow
already confirmed to work in the live Tableau UI. The final export step
does NOT reproduce the workflow's *click* on "Download -> Data ->
Download-within-the-popup" -- two separate real-server runs ruled that
out completely (see CLAUDE.md, "Reproducing the export command
directly"):

  - The "View Data" popup opened by Download -> Data is a "Hybrid Popup
    Window" wrapper with an entirely empty DOM (0 buttons, 0 frames with
    content, 0 shadow roots, 0 canvases) -- there is nothing to click
    inside it.
  - The main dashboard's own Download button IS the real, correct DOM
    node (confirmed by a real Playwright locator resolving to
    `#download` / `data-tb-test-id="viz-viewer-toolbar-button-download"`),
    but while the View Data dialog is open, Tableau's own
    `Dialog-Provider-Dialog-Glass` overlay intentionally blocks it --
    clicking it again just hangs, it does not trigger anything.

Instead, this reproduces Tableau's own VizQL command flow directly, per
the HAR capture in CLAUDE.md ("Native UI Crosstab download") -- by
*discovering* the browser's already-established VizQL session from
state that already exists, not by *observing* its creation via network
listeners. Two earlier versions attached `page`/context-level
"request"/"response" listeners (first right before the "Data" click,
then moved to right after the dashboard navigation) hoping to capture
the session id, `X-XSRF-TOKEN`, and `visualIdPresModel` as Tableau's own
JS fired its bootstrap VizQL commands. Both timed out against the real
server having captured nothing, even though the dashboard loaded and the
View Data popup opened correctly -- proving the commands are not
reliably observable as ordinary Playwright network events at all (this
Tableau version may issue them in a way outside Playwright's page-level
CDP Network domain, or on a timing/target this project could not pin
down after two attempts).

`_discover_active_vizql_session()` instead reads state that already
exists once the dashboard (and/or the View Data popup) has loaded, no
listener required:
  - The VizQL session id and workbook/view path, from the URL of any
    already-completed VizQL command -- recovered via the standard
    Performance Resource Timing API (`performance.getEntriesByType
    ("resource")`), which every browser keeps for the life of the page
    regardless of whether anything was "listening" when the request
    happened. Any interactive Tableau viz must have issued at least one
    such command just to render, so this exists by the time the
    dashboard (or popup) is visibly loaded.
  - The `X-XSRF-TOKEN` value, read directly from its matching cookie
    (`context.cookies()`) -- Tableau's own header name is exactly
    Angular's default double-submit-cookie header, which is only ever
    populated from a same-named cookie, so the value doesn't need to be
    captured off any particular outgoing request.

Once a session is discovered, `_fetch_view_data_dialog_tab_pres_model()`
issues that one HAR-confirmed command itself (`get-view-data-dialog-tab-pres-model`)
via the browser's own authenticated request context to obtain
`visualIdPresModel`/`columns` directly from its response, rather than
waiting to observe the popup's own copy of that same call.
`_export_view_data_via_vizql_commands()` then issues the final two HAR
steps -- POST `export-view-data-summary-to-csv-server`, GET the
resulting `tempfile` URL -- the same way: via `page.context.request`,
which reuses the real session's cookies automatically, never a separate
login or credential flow. None of this depends on any DOM element
existing, in the popup or anywhere else, and none of it depends on a
network listener catching anything at the right moment.

NOT exercised against the real server from this environment: no browser
binary is installed here (Chromium download is blocked by this sandbox's
network policy) and network access to insight.erajaya.com is blocked
too. The DOM selectors below (login, dashboard, Download menu, "Data"
item) are best-effort, built from accessible roles/names per Tableau's
general UI conventions, refined against real local runs. If a selector
doesn't match the live page:
  - run with HEADLESS=false to watch it interactively, and
  - check <EXPORT_DIR>/debug_<step>.png, saved automatically on failure,
  - check the [NETDEBUG]-prefixed log lines for export/tempfile network
    activity, and
  - then adjust the candidate locator lists in _login_if_needed() /
    _wait_for_dashboard() / _export_crosstab_csv() below to match what
    you see.
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
EXPORT_COMMAND_URL_SUBSTRING = "export-view-data-summary-to-csv-server"
TEMPFILE_URL_SUBSTRING = "/tempfile/"

# The leading "t/{site}/" segment is Tableau's own documented convention
# for a non-default site (omitted entirely for the Default site, which
# is what every URL confirmed so far in this project -- e.g. the plain
# `/#/views/...` dashboard URL -- is consistent with); made optional here
# rather than assumed, so this still matches if TABLEAU_SITE is set.
VIZQL_SESSION_ID_RE = re.compile(r"/vizql/((?:t/[^/]+/)?w/[^/]+/v/[^/]+)/sessions/([^/?]+)/")
VISUAL_ID_PRES_MODEL_KEY = "visualIdPresModel"
COLUMNS_KEY = "columns"

# Standard Performance Resource Timing API -- every browser keeps this
# for the life of the page regardless of whether a Playwright listener
# was attached at the right moment. Returns only URLs (never bodies,
# headers, or timing values we don't use), so it's safe to read without
# any redaction concern beyond the existing _redact_url() used before
# printing anything.
PERFORMANCE_RESOURCE_URLS_SCRIPT = "() => performance.getEntriesByType('resource').map(e => e.name)"

# Tableau's own VizQL commands send an X-XSRF-TOKEN header -- exactly
# Angular's default double-submit-cookie header name, which Angular's
# HttpClientXsrfModule only ever populates from a same-named cookie by
# default. Reading the cookie directly avoids needing to observe any
# particular outgoing request's headers.
XSRF_COOKIE_NAMES = {"xsrf-token", "x-xsrf-token"}

DOWNLOAD_BUTTON_CANDIDATES = [
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

    _attach_export_network_diagnostics([page])

    # Clicking "Data" opens a separate "View Data" popup window/tab (a new
    # Playwright Page in the same browser context, titled
    # "View Data: <sheet name>") -- not an in-page dialog. This click is
    # kept (confirmed working against the real server); what happens next
    # is not a further click inside this popup, but reproducing Tableau's
    # own VizQL export command directly -- see _export_view_data_via_vizql_commands().
    try:
        with page.expect_popup(timeout=VIEW_DATA_POPUP_TIMEOUT_MS) as popup_info:
            data_item.click()
        view_data_page = popup_info.value
        view_data_page.wait_for_load_state("domcontentloaded", timeout=VIEW_DATA_POPUP_TIMEOUT_MS)
    except Exception as exc:
        _save_debug_screenshot(page, export_dir, "view-data-popup-not-opened")
        raise BrowserAutomationError(f"The 'View Data' popup window did not open: {exc}") from exc

    _attach_export_network_diagnostics([view_data_page])

    # --- TEMPORARY DEBUG ONLY -- kept per explicit instruction even though
    # a real-server run already showed this popup's own DOM is effectively
    # empty (see CLAUDE.md, "Network-based export"); still useful context
    # if a future Tableau update changes that. Never touches
    # credentials/cookies/tokens. The actual export below does not depend
    # on this popup's DOM at all -- see _export_view_data_via_vizql_commands().
    _debug_dump_view_data_popup(view_data_page, export_dir)

    print("Exporting Crosstab...")
    capture = _ViewDataSessionCapture()
    return _export_view_data_via_vizql_commands(page, view_data_page, capture, export_dir)


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


def _find_key_recursive(obj, key: str):
    """Recursively searches a JSON-like structure (nested dicts/lists) for
    the first occurrence of `key`, returning its value or None.

    Tableau's VizQL command responses were never captured beyond the
    field NAMES confirmed in the original HAR (see CLAUDE.md) -- their
    exact nesting is unknown and was never guessed. A targeted key search
    uses only the one fact that IS certain (the field's name), rather
    than a hardcoded path that would break the moment the real response
    shape differs even slightly."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_key_recursive(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key_recursive(item, key)
            if found is not None:
                return found
    return None


class _ViewDataSessionCapture:
    """Accumulates the pieces needed to reproduce Tableau's own View Data
    VizQL commands -- discovered from already-existing browser state (see
    _discover_active_vizql_session()), never typed, guessed, or
    hardcoded. `visual_id_pres_model`/`columns` are filled in later, by
    _fetch_view_data_dialog_tab_pres_model()."""

    def __init__(self):
        self.origin: str | None = None
        self.command_path_prefix: str | None = None  # e.g. "w/ReturnRotate/v/Dashboard1"
        self.session_id: str | None = None
        self.xsrf_token: str | None = None
        self.visual_id_pres_model: str | None = None
        self.columns: list | None = None

    def has_session(self) -> bool:
        """True once enough is known to address any VizQL command on
        this session (before visualIdPresModel/columns are fetched)."""
        return bool(self.origin and self.command_path_prefix and self.session_id and self.xsrf_token)


def _find_vizql_session_in_resource_urls(urls):
    """Searches a list of URLs (from the Performance Resource Timing API)
    for the first one that looks like a VizQL command, returning
    (origin, command_path_prefix, session_id) or None. Never raises --
    a malformed URL is simply skipped."""
    for url in urls:
        try:
            match = VIZQL_SESSION_ID_RE.search(url)
            if not match:
                continue
            parts = urlsplit(url)
            return f"{parts.scheme}://{parts.netloc}", match.group(1), match.group(2)
        except Exception:
            continue
    return None


def _find_xsrf_token_in_cookies(context) -> str | None:
    """Reads the browser context's own cookies for the one matching
    Tableau's X-XSRF-TOKEN header name (see XSRF_COOKIE_NAMES). Never
    raises, never logs the cookie jar."""
    try:
        cookies = context.cookies()
    except Exception:
        return None
    for cookie in cookies:
        try:
            if cookie.get("name", "").lower() in XSRF_COOKIE_NAMES:
                value = cookie.get("value")
                if value:
                    return value
        except Exception:
            continue
    return None


def _discover_active_vizql_session(pages, capture: _ViewDataSessionCapture) -> None:
    """Recovers the VizQL session the browser has *already* established
    (during the dashboard's own load, and/or the View Data popup's) from
    existing browser state, instead of trying to observe its creation via
    a network listener -- two real-server runs of the listener-based
    approach captured nothing at all, even though the dashboard and
    popup both loaded correctly, showing these commands are not reliably
    observable as ordinary Playwright network events here. See the
    module docstring.

    Polls each given page's Performance Resource Timing entries (no
    fixed sleep -- a brief retry loop only in case the buffer hasn't
    recorded anything at the exact instant this is first called) and the
    browser context's cookies, until a session is found or the existing
    VIEW_DATA_POPUP_TIMEOUT_MS elapses (no new/longer timeout added)."""
    context = pages[0].context
    deadline = time.monotonic() + VIEW_DATA_POPUP_TIMEOUT_MS / 1000
    while True:
        if not capture.has_session():
            for page in pages:
                try:
                    urls = page.evaluate(PERFORMANCE_RESOURCE_URLS_SCRIPT)
                except Exception:
                    continue
                found = _find_vizql_session_in_resource_urls(urls)
                if found:
                    capture.origin, capture.command_path_prefix, capture.session_id = found
                    break
            if capture.xsrf_token is None:
                capture.xsrf_token = _find_xsrf_token_in_cookies(context)
        if capture.has_session():
            return
        if time.monotonic() >= deadline:
            raise BrowserAutomationError(
                "Could not discover an active VizQL session from the dashboard or View "
                "Data pages (checked Performance Resource Timing entries and cookies)."
            )
        pages[0].wait_for_timeout(POLL_INTERVAL_MS)


def _safe_response_text_snippet(response, limit: int = 300) -> str:
    """A short, truncated text snippet of a response body, safe to
    include directly in an error message -- this is Tableau's own
    status/error text, the same kind of thing crosstab_probe.py already
    surfaces this way (response.text[:300]), not session/user data.
    Never raises -- an unreadable body is reported as such."""
    try:
        return response.text()[:limit]
    except Exception:
        return "<no response body available>"


def _fetch_view_data_dialog_tab_pres_model(request_context, capture: _ViewDataSessionCapture) -> None:
    """Issues the HAR-confirmed `get-view-data-dialog-tab-pres-model`
    VizQL command directly, via the browser's own authenticated request
    context, to obtain `visualIdPresModel`/`columns` from its own
    response -- rather than waiting to observe the View Data popup's own
    copy of this same call (the approach that failed against the real
    server). Its exact request body was never captured beyond the
    `telemetryCommandId` field already confirmed as common to sibling
    tabsrv commands (see CLAUDE.md) -- no other field is invented.
    Raises BrowserAutomationError with the real HTTP status and a
    truncated response body on any failure, so the actual server
    response is visible from the error alone."""
    url = (
        f"{capture.origin}/vizql/{capture.command_path_prefix}"
        f"/sessions/{capture.session_id}/commands/tabsrv/get-view-data-dialog-tab-pres-model"
    )
    try:
        response = request_context.post(
            url,
            headers={"X-XSRF-TOKEN": capture.xsrf_token},
            multipart={"telemetryCommandId": uuid.uuid4().hex[:30]},
            timeout=NETWORK_EXPORT_TIMEOUT_MS,
        )
    except Exception as exc:
        raise BrowserAutomationError(
            f"Could not issue the View Data tab presentation model command: {exc}"
        ) from exc

    if not response.ok:
        raise BrowserAutomationError(
            "The View Data tab presentation model command returned HTTP "
            f"{response.status}: {_safe_response_text_snippet(response)}"
        )

    try:
        body = response.json()
    except Exception as exc:
        raise BrowserAutomationError(
            "The View Data tab presentation model command returned an unparseable "
            f"response: {exc}"
        ) from exc

    value = _find_key_recursive(body, VISUAL_ID_PRES_MODEL_KEY)
    if isinstance(value, str) and value:
        capture.visual_id_pres_model = value

    columns = _find_key_recursive(body, COLUMNS_KEY)
    if isinstance(columns, list) and columns and all(isinstance(item, str) for item in columns):
        capture.columns = columns

    if not capture.visual_id_pres_model:
        raise BrowserAutomationError(
            "The View Data tab presentation model command succeeded but no "
            "visualIdPresModel was found in its response (see CLAUDE.md's documented "
            "field name -- it may have changed)."
        )


def _export_view_data_via_vizql_commands(
    page, view_data_page, capture: _ViewDataSessionCapture, export_dir: Path
) -> Path:
    """Reproduces Tableau's own View Data CSV export by issuing its VizQL
    commands directly over HTTP, instead of clicking anything -- see the
    module docstring for why every DOM-based approach, and every
    listener-based capture approach, was ruled out.

    1. `_discover_active_vizql_session()` recovers the session id,
       workbook/view path, and X-XSRF-TOKEN from browser state that
       already exists (Performance Resource Timing entries + cookies).
    2. `_fetch_view_data_dialog_tab_pres_model()` issues that HAR command
       itself to obtain `visualIdPresModel`/`columns` directly from its
       response.
    3. This then POSTs `export-view-data-summary-to-csv-server` and GETs
       the resulting `tempfile` URL, using `page.context.request` -- a
       Playwright APIRequestContext that automatically reuses the real
       browser session's cookies (Playwright: "The method will populate
       request cookies from the context"), so no separate login, cookie
       handling, or credential logic is needed. The X-XSRF-TOKEN header
       is the one thing Playwright does not attach automatically -- it's
       set from the value discovered in step 1.

    `columns` is sent as an empty list when the tab-pres-model command
    doesn't yield one -- a known limitation (see CLAUDE.md): it's
    unverified whether Tableau accepts that as "all columns" or requires
    the real list."""
    _discover_active_vizql_session([page, view_data_page], capture)
    print(f"[NETDEBUG] discovered VizQL session (session={bool(capture.session_id)}, xsrf={bool(capture.xsrf_token)})")

    request_context = page.context.request

    _fetch_view_data_dialog_tab_pres_model(request_context, capture)
    print(f"[NETDEBUG] fetched tab presentation model (columns={bool(capture.columns)})")

    export_url = (
        f"{capture.origin}/vizql/{capture.command_path_prefix}"
        f"/sessions/{capture.session_id}/commands/tabsrv/export-view-data-summary-to-csv-server"
    )
    multipart = {
        "visualIdPresModel": capture.visual_id_pres_model,
        "showAliases": "true",
        "columns": json.dumps(capture.columns or []),
        "listSeparatorValue": ",",
        "unicodeEncodingValue": "UTF-8",
        "telemetryCommandId": uuid.uuid4().hex[:30],
    }

    try:
        export_response = request_context.post(
            export_url,
            headers={"X-XSRF-TOKEN": capture.xsrf_token},
            multipart=multipart,
            timeout=NETWORK_EXPORT_TIMEOUT_MS,
        )
    except Exception as exc:
        _save_debug_screenshot(view_data_page, export_dir, "vizql-export-command-failed")
        raise BrowserAutomationError(f"Could not issue the View Data export command: {exc}") from exc

    print(
        f"[NETDEBUG] export command response status={export_response.status} "
        f"url={_redact_url(export_url)}"
    )
    if not export_response.ok:
        _save_debug_screenshot(view_data_page, export_dir, "vizql-export-command-failed")
        raise BrowserAutomationError(
            "The View Data export command returned HTTP "
            f"{export_response.status}: {_safe_response_text_snippet(export_response)}"
        )

    try:
        export_body = export_response.json()
    except Exception as exc:
        raise BrowserAutomationError(
            f"The View Data export command returned an unparseable response: {exc}"
        ) from exc

    result_key = _find_key_recursive(export_body, "resultKey")
    if not result_key:
        raise BrowserAutomationError(
            "The View Data export command succeeded but no resultKey was found in its "
            "response (see CLAUDE.md's documented response shape -- it may have changed)."
        )

    tempfile_url = f"{capture.origin}/vizql/{capture.command_path_prefix}/tempfile/sessions/{capture.session_id}"
    try:
        tempfile_response = request_context.get(
            tempfile_url,
            params={"key": result_key, "keepfile": "yes", "attachment": "yes"},
            timeout=NETWORK_EXPORT_TIMEOUT_MS,
        )
    except Exception as exc:
        raise BrowserAutomationError(f"Could not fetch the exported tempfile: {exc}") from exc

    print(
        f"[NETDEBUG] tempfile response status={tempfile_response.status} "
        f"url={_redact_url(tempfile_url)}"
    )
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
