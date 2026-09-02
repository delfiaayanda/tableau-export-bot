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

A real-server run of `_switch_to_full_data_and_download()` then reported
the popup's DOM as still completely empty (0 buttons, 0 download-text
matches, ~931 bytes of HTML, empty innerText) even after this project's
own polling wait, and the run ultimately timed out never observing the
tempfile response -- yet a real human click on the same window (per the
HAR above) does complete the export. This rules out "the locator
candidates are wrong" (an empty DOM has no candidate to find at all) and
points at a hydration/initialization gap between how a human's click
opens/primes this window and how this project's automated click does.
`_attach_popup_runtime_diagnostics()` (console messages, uncaught JS
errors, and every request/response the popup page personally fires,
unfiltered) and `_attach_message_listener()` +
`_debug_dump_hydration_signals()` (whether the popup ever receives a
window.postMessage from anywhere, plus window.opener/window.name) were
added -- all TEMPORARY DEBUG ONLY, independently try/excepted, never
touching credentials/cookies/tokens or message payload contents (only
sender host + a coarse shape) -- to answer that question directly rather
than guessing at another locator. `_describe_locator()` also logs which
actual element `_switch_to_full_data_and_download()` matched and clicked,
so a false-positive match is distinguishable from a real one.

Separately, re-analyzing both real HAR captures this project has (the
original Crosstab/Summary one and the newer Full Data one) as
cross-checking evidence: in both, every VizQL "View Data" dialog command
-- launch, tab-pres-model, the export command itself -- is an XHR/fetch
tied to the SAME long-lived VizQL session id already used for ordinary
dashboard interactions (e.g. tooltip rendering), and neither HAR contains
a navigation request for a second document (no `hybrid-window/...`
`document`-typed entry, no second `pageref`). This is not conclusive on
its own -- both HARs also carry no multi-page/`pages` metadata at all, a
generic sign of how they were captured/reduced rather than proof no
popup exists -- but it is a real, evidence-based reason to suspect the
interactive "Full Data"/"Download" controls a human actually clicks may
render as an overlay on the MAIN dashboard page rather than inside this
"Hybrid Popup Window" at all, which would also explain why the popup's
own DOM stays empty no matter how long this project waits on it. This
project has not acted on that theory (no locator search was added
against the main page for these controls) since it cannot be confirmed
without a live run; it's recorded here as the next thing to check if the
new diagnostics above show the popup truly never does anything on its
own.

**Resolved:** the "does it hydrate at all" question above turned out to
have a mundane answer once the diagnostics actually ran on current code
(an earlier "still empty" run was later found to have been executed
against a stale, pre-diagnostics checkout -- see CLAUDE.md). On the real
current code, the popup DOES hydrate: it fires its own JS bundle
requests, its console logs "Browser based plugin load finished", it has
`window.opener` set, and it receives a real `postMessage` from its
opener. `_switch_to_full_data_and_download()` then found and clicked
"Full Data" successfully -- and `_describe_locator()` revealed why the
FULL_DATA_TAB_CANDIDATES role-based entries were dead weight while
get_by_text() was the one that matched: the real element is a plain
`<span>` with no `role` and no `aria-label` at all, only visible text.
`VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES` is now updated the same way
(get_by_text() first, plus a couple of additional low-risk fallbacks --
see the comment above that list) on the reasonable, evidence-based
assumption that "Download" is built with the same UI toolkit as "Full
Data". Confirmed live: a subsequent run found and clicked both real
controls (confirmed selectors are now listed first -- see the comments
above FULL_DATA_TAB_CANDIDATES / VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES)
and the export genuinely fired (`export-view-data-underlying-to-csv-server`
-> tempfile, both HTTP 200) -- but the run still failed, and the saved
file, once inspected, turned out not to be a usable CSV at all (see
below).

**The saved file wasn't CSV: `Response.body()` conflicts with Chrome's
own native download handling.** A screenshot from that run showed
Chrome's own "Recent download history" panel had completed a real
72.5 MB download with no filename at all (a bare GUID, no extension) --
proof that Chrome's network stack recognized the tempfile response's
`Content-Disposition: attachment` header and diverted it to its native
download manager, independently of anything this project's Python code
does with that same response. This is a known Playwright limitation:
once Chrome claims a response as a download, its body is generally not
reliably available via `Response.body()` (the DevTools Protocol call
this project's own capture depends on) any more -- so the earlier fix
that stopped calling `.body()` inside the event handler (see below) was
necessary but not sufficient; calling it later, on the main flow, can
still return the wrong thing once Chrome has taken the response for its
downloads UI.

Fixed by capturing the actual `Download` object Playwright's context
emits (`_DownloadCapture` / `_capture_download` / `_attach_download_capture`
/ `_wait_for_download_capture`, attached at the browser CONTEXT level as
early as possible, mirroring `_TempfileCapture`) and reading its real
bytes from `download.path()` -- the local file Chrome already finished
writing -- instead of `response.body()`. `_save_tempfile_response()` now
takes an optional `download` parameter: when present, it's the source of
truth for both the bytes and (when Content-Disposition didn't supply a
usable name) the filename, EXCEPT that a `suggested_filename` without a
recognizable `.csv`/`.txt` extension (exactly what that GUID was) is
treated as unusable and a generated name is used instead -- otherwise
this fix would just relabel the same "not a csv file" symptom. When no
download ever fires (a future Tableau version, a different browser
config), this falls back to `response.body()` exactly as before, so nothing
regresses in that case.

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
    activity,
  - check the [POPUPDEBUG]-prefixed log lines for the new hydration
    diagnostics (console/pageerror/own-network/postMessage/opener), and
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
DOWNLOAD_EVENT_GRACE_PERIOD_MS = 5_000

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
# switch to "Full Data", then click "Download". A real-server run then
# confirmed exactly how this popup is actually built: _describe_locator()
# showed the matched "Full Data" control is a plain `<span>` with NO
# `role` and NO `aria-label` at all -- it only has visible text -- so
# every accessible-role-based candidate below it (tab/button/menuitem)
# can never match anything in this popup; only get_by_text() can. Both
# lists now try get_by_text() FIRST (confirmed to work for Full Data;
# the Download button is presumed built the same way, from the same
# popup's own UI toolkit) and keep the role-based/CSS candidates as a
# low-cost fallback in case a future Tableau version adds real ARIA
# semantics.
# Confirmed from a real-server DOM dump taken right after the Full Data
# click: the tab is <button aria-label="Full Data Table"
# data-tb-test-id="-SideTab">Full Data</button>. The test id is shared
# with the "Summary" tab so it can't be used alone; the aria-label is
# unambiguous, so it goes first, with the (also confirmed working) text
# match kept behind it.
FULL_DATA_TAB_CANDIDATES = [
    lambda s: s.locator('button[aria-label="Full Data Table"]'),
    lambda s: s.get_by_text(re.compile(r"^full data$", re.I)),
    lambda s: s.get_by_role("tab", name=re.compile(r"^full data$", re.I)),
    lambda s: s.get_by_role("button", name=re.compile(r"^full data$", re.I)),
    lambda s: s.get_by_role("menuitem", name=re.compile(r"^full data$", re.I)),
]

# Also confirmed from that same dump: the real control is
# <button data-tb-test-id="download-data-Button" title="..."> wrapping a
# <span aria-label="Download in CSV format">Download</span>. Targeting
# the BUTTON (not the inner span the text match resolves to) is what
# makes its `title` readable -- see _wait_for_full_data_to_load().
DOWNLOAD_DATA_BUTTON_SELECTOR = '[data-tb-test-id="download-data-Button"]'

# The button's own title while the Full Data grid is still loading, taken
# verbatim from that dump ("0 rows  0 fields" was showing at the time).
DOWNLOAD_BUTTON_NOT_READY_RE = re.compile(r"no column data available", re.I)

VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES = [
    lambda s: s.locator(DOWNLOAD_DATA_BUTTON_SELECTOR),
    lambda s: s.get_by_text(re.compile(r"^download$", re.I)),
    # Broader than the exact-text match above: a real-server run found
    # this popup's controls carry no ARIA role/label at all (see comment
    # above), so if "Download" turns out to be an icon button whose only
    # accessible text isn't an exact match (e.g. it's wrapped with other
    # text, or exposed via `title` instead), these catch it without
    # widening all the way to an unanchored get_by_text() (too easily
    # matches an unrelated container element and clicks the wrong thing).
    lambda s: s.get_by_role("button", name=re.compile(r"download", re.I)),
    lambda s: s.get_by_title(re.compile(r"download", re.I)),
    lambda s: s.locator('[data-tb-test-id="download-Button"]'),
    lambda s: s.locator('button[aria-label="Download" i]'),
    lambda s: s.locator('[aria-label*="download" i]'),
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


# TEMPORARY DEBUG ONLY: a real-server run showed the "View Data" popup's
# DOM is essentially empty (0 buttons, 0 text, ~931 bytes of HTML) even
# after this project's own polling wait -- yet a real human click on the
# same window completes a Full Data export. This script (idempotent --
# safe to inject more than once on the same page) records, without ever
# exposing a message's actual payload, whether that window ever receives
# a window.postMessage from anywhere -- a plausible hydration handshake
# this project's automation might not be triggering the same way a real
# user's click does.
_MESSAGE_LISTENER_SCRIPT = """
if (!window.__exportbotMessageListenerAttached) {
    window.__exportbotMessageListenerAttached = true;
    window.__exportbotMessages = [];
    window.addEventListener('message', function (event) {
        var shape;
        try {
            if (event.data === null || event.data === undefined) {
                shape = String(event.data);
            } else if (typeof event.data === 'string') {
                shape = 'string(len=' + event.data.length + ')';
            } else if (typeof event.data === 'object') {
                shape = 'object(keys=' + Object.keys(event.data).slice(0, 10).join(',') + ')';
            } else {
                shape = typeof event.data;
            }
        } catch (e) {
            shape = 'unreadable';
        }
        var originHost = 'unknown';
        try {
            originHost = new URL(event.origin).host;
        } catch (e) {}
        window.__exportbotMessages.push({ originHost: originHost, shape: shape });
    });
}
"""


def _attach_message_listener(target_page) -> None:
    """TEMPORARY DEBUG ONLY. Injects _MESSAGE_LISTENER_SCRIPT into
    `target_page` immediately (via evaluate()) -- works for an
    already-loaded page (the main dashboard) and, called again right
    after a popup's domcontentloaded, for the popup too. Never raises."""
    try:
        target_page.evaluate(_MESSAGE_LISTENER_SCRIPT)
    except Exception as exc:
        print(f"[POPUPDEBUG] could not attach message listener: {exc}")


def _read_hydration_signals(target_page) -> dict:
    """Reads back window.opener presence, window.name LENGTH (never the
    raw value -- it may embed an opaque identifier), and any
    postMessage events _attach_message_listener() observed. Never
    raises -- returns {"error": ...} instead."""
    script = (
        "() => ({"
        "hasOpener: (window.opener !== null && window.opener !== undefined), "
        "windowNameLength: (window.name || '').length, "
        "messages: window.__exportbotMessages || []"
        "})"
    )
    try:
        return target_page.evaluate(script)
    except Exception as exc:
        return {"error": str(exc)}


def _debug_dump_hydration_signals(target_page, label: str) -> None:
    """TEMPORARY DEBUG ONLY: prints the signals above. `label`
    distinguishes which page and which point in the flow this snapshot
    was taken at (e.g. "popup before Full Data click")."""
    signals = _read_hydration_signals(target_page)
    if "error" in signals:
        print(f"[POPUPDEBUG] ({label}) hydration signal read failed: {signals['error']}")
        return
    print(f"[POPUPDEBUG] ({label}) window.opener present = {signals.get('hasOpener')}")
    print(f"[POPUPDEBUG] ({label}) window.name length = {signals.get('windowNameLength')}")
    messages = signals.get("messages") or []
    print(f"[POPUPDEBUG] ({label}) postMessage events observed so far: {len(messages)}")
    for m in messages[:20]:
        print(f"[POPUPDEBUG] ({label})   from={m.get('originHost')} shape={m.get('shape')}")


def _attach_popup_runtime_diagnostics(view_data_page) -> None:
    """TEMPORARY DEBUG ONLY -- investigates why the "View Data" popup's
    DOM appears totally empty in automation despite a real human click
    on the same window completing a Full Data export (see module
    docstring). Reports, for this SPECIFIC page (not the shared
    export/tempfile-filtered logger already attached elsewhere):
    console messages and uncaught JS errors (does its JS even run
    without erroring?), and every request/response it personally
    fires (does it make ANY network call on its own, unfiltered?).
    Every attachment is independently try/excepted -- a failure here
    must never block the real export attempt. Never logs request/
    response bodies; URLs are redacted."""
    try:
        view_data_page.on("console", lambda msg: print(f"[POPUPDEBUG] console[{msg.type}] {msg.text[:300]}"))
    except Exception as exc:
        print(f"[POPUPDEBUG] could not attach console listener: {exc}")
    try:
        view_data_page.on("pageerror", lambda err: print(f"[POPUPDEBUG] pageerror: {str(err)[:300]}"))
    except Exception as exc:
        print(f"[POPUPDEBUG] could not attach pageerror listener: {exc}")
    try:
        view_data_page.on(
            "request", lambda req: print(f"[POPUPDEBUG] popup-own request {req.method} {_redact_url(req.url)}")
        )
    except Exception as exc:
        print(f"[POPUPDEBUG] could not attach popup-own request listener: {exc}")
    try:
        view_data_page.on(
            "response", lambda res: print(f"[POPUPDEBUG] popup-own response {res.status} {_redact_url(res.url)}")
        )
    except Exception as exc:
        print(f"[POPUPDEBUG] could not attach popup-own response listener: {exc}")


def _describe_locator(locator) -> str:
    """TEMPORARY DEBUG ONLY -- best-effort, safe description (tag/role/
    aria-label/short visible text -- the same class of visible-UI-only
    info _inspect_frame() already prints) of a matched locator's
    underlying element, used to confirm a click landed on something
    real and meaningful rather than a false-positive match. Never
    raises."""
    try:
        return locator.evaluate(
            "el => JSON.stringify({tag: el.tagName, role: el.getAttribute('role'), "
            "ariaLabel: el.getAttribute('aria-label'), text: (el.textContent || '').trim().slice(0, 60)})"
        )
    except Exception as exc:
        return f"<description failed: {exc}>"


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


class _TempfileCapture:
    """Holds the tempfile Response once observed, populated by
    _capture_tempfile_response()."""

    def __init__(self):
        self.response = None


def _capture_tempfile_response(response, capture: _TempfileCapture) -> None:
    """Passively records the first observed `.../tempfile/...` response
    object -- the one carrying the exported CSV.

    Deliberately reads NOTHING from the response here except its cached
    `url`. Playwright's sync API does not allow a call that round-trips
    to the browser -- `Response.body()` above all -- from inside an
    event handler; it raises. An earlier version built an eager
    status/headers/body snapshot right here inside a bare
    `except Exception: pass`, so on a real server run a genuinely
    successful `200 text/csv` tempfile response -- one this handler's
    own sibling logger had just printed to the console -- was silently
    dropped, and the run then failed with "Never observed the View Data
    tempfile response" while the evidence of it sat in the log directly
    above. The body is read afterwards, on the main flow, by
    _save_tempfile_response().

    Never raises -- but never fails silently either: a capture failure
    is exactly the bug described above, so it is always reported."""
    try:
        if capture.response is not None or not _is_tempfile_url(response.url):
            return
        capture.response = response
        print("[NETDEBUG] captured the tempfile response (body is read after the wait)")
    except Exception as exc:
        print(f"[NETDEBUG] failed to capture a tempfile response: {exc}")


class _DownloadCapture:
    """Holds the browser's own Download object once observed, populated
    by _capture_download()."""

    def __init__(self):
        self.download = None


def _capture_download(download, capture: "_DownloadCapture") -> None:
    """Passively records the first "download" event fired anywhere in
    the browser context.

    A real-server run showed Chrome for Testing's own native download
    handling claims the tempfile response as soon as it sees its
    `Content-Disposition: attachment` header -- visible in the browser's
    own "Recent download history" panel -- independently of whatever a
    Playwright script does with that same response. When that happens,
    Chrome hands the body off to its download manager instead of
    buffering it for the DevTools Protocol, so `Response.body()` can
    return the wrong thing (or fail) for that exact response -- a known
    Playwright limitation, and the reason the saved file in that run
    matched what the browser had downloaded (a bare GUID, no `.csv`
    name) rather than a real CSV. This captures the actual `Download`
    object instead; its bytes are read from `download.path()` afterwards
    on the main flow, in _save_tempfile_response(). Never raises."""
    try:
        if capture.download is not None:
            return
        capture.download = download
        print("[NETDEBUG] captured a browser download event")
    except Exception as exc:
        print(f"[NETDEBUG] failed to capture a download event: {exc}")


def _attach_download_capture(page, capture: "_DownloadCapture") -> None:
    """Attaches _capture_download() at the BROWSER CONTEXT level (reached
    via `page.context`), not a specific page -- Playwright's "download"
    event is emitted on whichever page's JS actually triggers it (the
    main dashboard or the "View Data" popup), and the context aggregates
    both, matching this module's existing "don't assume which page"
    pattern for network capture. Takes `page` rather than the context
    directly so that a fake/test page without a `.context` attribute at
    all is handled by the same try/except as a real `.on()` failure.
    Never raises."""
    try:
        page.context.on("download", lambda download: _capture_download(download, capture))
    except Exception as exc:
        print(f"[NETDEBUG] could not attach download listener: {exc}")


def _wait_for_download_capture(driver_page, capture: "_DownloadCapture", timeout_ms) -> None:
    """Best-effort poll (no fixed sleep) for _capture_download() to
    observe a real download event. Deliberately does NOT raise on
    timeout -- a download event is a bonus signal used to get reliable
    bytes when the browser claims the response as a native download;
    if none ever fires (e.g. a future Tableau version, or an
    environment where this response isn't treated as a download),
    _save_tempfile_response() falls back to reading the captured
    response's own body instead."""
    deadline = time.monotonic() + timeout_ms / 1000
    while capture.download is None:
        if time.monotonic() >= deadline:
            return
        driver_page.wait_for_timeout(POLL_INTERVAL_MS)


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


def _wait_for_download_button_ready(view_data_page, download_button, timeout_ms) -> None:
    """Waits for the Full Data grid to actually have rows before the
    export is triggered.

    Switching to "Full Data" makes the popup fetch the underlying rows
    asynchronously: a real-server DOM dump taken immediately after that
    click showed "0 rows  0 fields" and a Download button still carrying
    `title="No column data available to download"`. Clicking Download in
    that state risks exporting an empty file, so this polls that same
    button's live `title` (no fixed sleep, bounded by the caller's
    timeout) until it clears.

    Best-effort by design -- if the title can't be read at all (the
    matched element is the inner label span rather than the button, say)
    or never clears (a view that genuinely has no rows, or a future
    Tableau version wording it differently), this returns and lets the
    export proceed rather than failing the whole run on a heuristic."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            title = download_button.get_attribute("title") or ""
        except Exception as exc:
            print(f"[POPUPDEBUG] could not read the Download button's title ({exc}); continuing.")
            return
        if not DOWNLOAD_BUTTON_NOT_READY_RE.search(title):
            return
        view_data_page.wait_for_timeout(POLL_INTERVAL_MS)
    print(
        "[POPUPDEBUG] Full Data rows never finished loading (Download button still "
        "reports no column data available); attempting the export anyway."
    )


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


def _save_tempfile_response(response, export_dir: Path, download=None) -> Path:
    """Saves the exported CSV to EXPORT_DIR. Verifies the captured
    `.../tempfile/...` response's Content-Type looks like the expected
    CSV/text export and its status isn't an error before writing
    anything, since this is meant to replace Playwright's `download`
    event as the success signal, not blindly trust any matching URL.

    Bytes come from `download.path()` when a `Download` was captured
    (see _capture_download()) -- the reliable source once Chrome's own
    native download handling has claimed this exact response, which a
    real-server run showed makes `response.body()` return the browser's
    raw (sometimes GUID-named, un-suffixed) download rather than a
    proper CSV. Falls back to `response.body()` when no download was
    observed (e.g. a future Tableau version, or an environment where
    this response isn't treated as a download).

    Prefers the filename Tableau itself supplied via Content-Disposition
    (sanitized -- see _sanitize_filename); if that's absent or unusable,
    falls back to `download.suggested_filename` -- but a real-server run
    showed that can be a bare, extension-less GUID (Chrome's own
    placeholder, not a real name), so a name without a recognizable
    `.csv`/`.txt` suffix is treated the same as no name at all and a
    generated timestamped name is used instead."""
    if response.status >= 400:
        raise BrowserAutomationError(
            f"Tempfile response returned HTTP {response.status}; refusing to save it."
        )

    content_type = _response_content_type(response)
    if "csv" not in content_type.lower() and "text" not in content_type.lower():
        raise BrowserAutomationError(
            f"Tempfile response had an unexpected Content-Type ({content_type!r}); refusing to save it."
        )

    # Read the bytes HERE, on the main flow -- never inside the network
    # event handler that captured this response (see
    # _capture_tempfile_response for why that silently failed before).
    if download is not None:
        try:
            body = Path(download.path()).read_bytes()
        except Exception as exc:
            raise BrowserAutomationError(
                f"Captured a browser download but could not read its saved file: {exc}"
            ) from exc
    else:
        try:
            body = response.body()
        except Exception as exc:
            raise BrowserAutomationError(
                f"Captured the tempfile response but could not read its body: {exc}"
            ) from exc

    if not body:
        raise BrowserAutomationError("The exported file was empty; refusing to save it.")

    content_disposition = None
    try:
        content_disposition = (response.headers or {}).get("content-disposition")
    except Exception:
        pass
    filename = _sanitize_filename(_extract_filename_from_content_disposition(content_disposition))

    if not filename and download is not None:
        try:
            suggested = _sanitize_filename(download.suggested_filename)
        except Exception:
            suggested = None
        if suggested and os.path.splitext(suggested)[1].lower() in (".csv", ".txt"):
            filename = suggested

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
    _attach_message_listener(page)  # TEMPORARY DEBUG ONLY -- see module docstring

    # Attached at the browser CONTEXT level, as early as possible -- a
    # real-server run showed Chrome's own native download handling
    # claims this exact response (see _capture_download()), so the
    # actual Download object is the reliable source of bytes, not
    # response.body(). Attaching now, before the popup even opens,
    # means this can't miss the event regardless of which page it fires
    # on.
    download_capture = _DownloadCapture()
    _attach_download_capture(page, download_capture)

    # Clicking "Data" opens a separate "View Data" popup window/tab (a new
    # Playwright Page in the same browser context, titled
    # "Hybrid Popup Window") -- not an in-page dialog. This click is kept
    # (confirmed working against the real server). A HAR capture of the
    # real manual Full Data flow showed reaching the actual export
    # requires two further real clicks inside this popup -- see
    # _switch_to_full_data_and_download() below.
    try:
        with page.expect_popup(timeout=VIEW_DATA_POPUP_TIMEOUT_MS) as popup_info:
            data_item.click()
        view_data_page = popup_info.value
        _attach_popup_runtime_diagnostics(view_data_page)  # TEMPORARY DEBUG ONLY
        view_data_page.wait_for_load_state("domcontentloaded", timeout=VIEW_DATA_POPUP_TIMEOUT_MS)
        _attach_message_listener(view_data_page)  # TEMPORARY DEBUG ONLY
    except Exception as exc:
        _save_debug_screenshot(page, export_dir, "view-data-popup-not-opened")
        raise BrowserAutomationError(f"The 'View Data' popup window did not open: {exc}") from exc

    _attach_export_network_diagnostics([view_data_page], tempfile_capture)

    # --- TEMPORARY DEBUG ONLY -- kept per explicit instruction even though
    # a real-server run already showed this popup's own DOM is effectively
    # empty (see CLAUDE.md); still useful context if a future Tableau
    # update changes that. Never touches credentials/cookies/tokens.
    _debug_dump_view_data_popup(view_data_page, export_dir)
    _debug_dump_hydration_signals(view_data_page, "popup before Full Data/Download clicks")

    _switch_to_full_data_and_download(view_data_page, export_dir)

    _debug_dump_hydration_signals(view_data_page, "popup after Full Data/Download clicks")
    _debug_dump_hydration_signals(page, "main dashboard page after Full Data/Download clicks")

    _wait_for_tempfile_capture(view_data_page, tempfile_capture, NETWORK_EXPORT_TIMEOUT_MS)
    # Best-effort, short grace period for the "download" event to arrive
    # too -- it can lag slightly behind the response event while Chrome
    # finishes writing the file to disk. Never raises: if it never
    # fires, _save_tempfile_response() falls back to response.body().
    _wait_for_download_capture(view_data_page, download_capture, DOWNLOAD_EVENT_GRACE_PERIOD_MS)
    saved_path = _save_tempfile_response(
        tempfile_capture.response, export_dir, download=download_capture.download
    )
    try:
        print(f"Saved {saved_path} ({saved_path.stat().st_size} bytes)")
    except Exception:
        pass  # reporting the size must never break a successful export
    return saved_path


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
    print(f"[POPUPDEBUG] Full Data candidate matched: {_describe_locator(full_data_tab)}")  # TEMPORARY DEBUG ONLY
    full_data_tab.click()

    # TEMPORARY DEBUG ONLY -- reuses the existing DOM dump a second time,
    # now that the Full Data click should have taken effect, so a failure
    # to find Download next shows exactly what the popup's DOM actually
    # contains at that point (did the click render anything new at all?)
    # instead of another blind locator guess.
    _debug_dump_view_data_popup(view_data_page, export_dir)

    download_button = _find_first_with_wait(
        view_data_page, VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES, VIEW_DATA_POPUP_TIMEOUT_MS
    )
    if download_button is None:
        _save_debug_screenshot(view_data_page, export_dir, "view-data-download-button-not-found")
        raise BrowserAutomationError(
            'Could not find the "Download" button in the View Data window after switching to Full Data.'
        )

    print("Waiting for Full Data rows to load...")
    _wait_for_download_button_ready(view_data_page, download_button, VIEW_DATA_POPUP_TIMEOUT_MS)

    print("Exporting Full Data...")
    print(f"[POPUPDEBUG] Download candidate matched: {_describe_locator(download_button)}")  # TEMPORARY DEBUG ONLY
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
