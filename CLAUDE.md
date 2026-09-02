# Architecture & status

## Goal

Export the `ReturnRotate/Dashboard1` Tableau view
(`https://insight.erajaya.com/#/views/ReturnRotate/Dashboard1`) to a
date-based, timestamped Excel file, runnable as `python main.py`.

## Architecture

Layered, each layer swappable via a small interface:

- `config.py` — loads all settings (server URL, credentials, view ID,
  optional API version override, export dir, log level) from environment
  variables. Raises `ConfigError` if anything required is missing. No
  secrets or defaults for credentials are baked in.
- `serverinfo.py` — `discover_api_version()`, an unauthenticated GET to
  Tableau's `serverinfo` endpoint that returns the server's real REST API
  version. Used by `main.py` when `TABLEAU_API_VERSION` isn't set, so the
  API version doesn't have to be hardcoded or guessed.
- `auth.py` — `Authenticator` ABC + `TableauServerAuthenticator`, which
  POSTs to Tableau's REST `auth/signin` endpoint and returns a
  `TableauSession` (token, site id, server url, api version). Takes
  `api_version` as a constructor argument (default `3.29`, the version
  confirmed against the real server — see below); `main.py` overrides it
  with the configured or auto-detected value.
- `tableau_client.py` — `TableauClient` ABC + `TableauServerClient`, which
  GETs the REST "view data" endpoint (returns CSV) and parses it into a
  pandas DataFrame.
- `view_discovery.py` — `discover_view()`, an authenticated lookup that
  resolves a workbook/view name pair (e.g. `ReturnRotate`/`Dashboard1`) to
  the GUIDs the REST API actually needs, via `GET .../sites/{site-id}/views`
  filtered by `contentUrl`, plus a follow-up `GET .../workbooks/{id}` for
  the workbook's display name. Standalone from the pipeline; not yet wired
  into `main.py`.
- `extraction_probe.py` — `probe_view_data()`, an authenticated GET to the
  exact same "view data" endpoint `tableau_client.py` uses, but returns
  only shape metadata (HTTP status, content type, row/column counts,
  column names) instead of the full DataFrame -- used by
  `extract_smoke_test.py` to check extraction works before wiring it into
  the real pipeline. Standalone; not yet wired into `main.py`.
- `extraction_diagnostics.py` — `probe_with_accept_header()`, retries the
  same "view data" GET with a caller-specified (or absent) `Accept`
  header and reports HTTP status, content type, a small allowlist of
  response headers, and a truncated error body -- used by
  `diagnose_extraction.py` to isolate why a request fails (e.g. HTTP 406)
  without guessing. Never raises, so every variant can be tried even if
  one fails. Standalone; not yet wired into `main.py`.
- `crosstab_probe.py` — `probe_crosstab_excel()`, an authenticated GET to
  the documented "Query View Crosstab Excel" endpoint
  (`.../views/{id}/crosstab/excel`), reporting HTTP status, elapsed time,
  Content-Type, Content-Disposition, response size, and filename --
  never the file's contents. This is a *hypothesis* probe: it is NOT
  confirmed to match what the browser's "Download -> Crosstab" menu
  action actually does (see "Native UI Crosstab download" below).
  Standalone; not yet wired into `main.py`.
- `vizql_crosstab_probe.py` — reproduces the export-command ->
  tempfile-download tail of Tableau's native VizQL Crosstab flow (see
  "Native UI Crosstab download" below), given an already-valid session id
  pasted from a DevTools capture. Cannot establish a session on its own.
- `browser_crosstab.py` — Playwright browser automation: logs in, opens
  the dashboard, clicks Download -> Data -> Crosstab, and saves the
  downloaded CSV. Built after the REST/VizQL investigation confirmed a
  real browser session is required (see "Browser automation" below).
  Standalone; not wired into `main.py`/`pipeline.py`.
- `validation.py` — generic sanity checks on the DataFrame (not empty, has
  columns, not all-null) before export.
- `exporter.py` — writes the DataFrame to
  `<EXPORT_DIR>/<YYYY-MM-DD>/export_<YYYYMMDD_HHMMSS>.xlsx`.
- `pipeline.py` — wires auth → extraction → validation → export. Takes the
  authenticator and a client factory as parameters so it can be driven by
  fakes in tests, or the real Tableau implementations from `main.py`.
- `main.py` — CLI entry point: loads config, sets up logging, runs the
  pipeline with the real Tableau implementations, maps errors to exit
  codes.

## Network blocker (this development sandbox only)

This development environment's outbound network is allowlisted by org
policy and does **not** include `insight.erajaya.com`. Verified directly:

```
curl -v https://insight.erajaya.com/   →  proxy CONNECT rejected, HTTP 403
```

The agent-proxy status endpoint confirms this is a policy-level denial
(`connect_rejected`, `organization policy`), not a DNS, TLS, or
application-layer problem — and it is host-specific, not a general
internet outage (`pypi.org` succeeds through the same proxy; `google.com`
fails the same way `insight.erajaya.com` does, i.e. this sandbox has no
general internet egress at all, only an allowlist). No code has been run
against the real server from *this* environment.

**A user's local machine does have network access to
`insight.erajaya.com`** and confirmed:

```
GET https://insight.erajaya.com/api/3.22/serverinfo  →  200 OK
productVersion: 2026.2.0
restApiVersion: 3.29
```

This confirms the REST API is enabled, gives the real API version
(3.29 — `auth.py`'s `DEFAULT_API_VERSION` and `serverinfo.py`'s
auto-detection are both based on this), and that Tableau's `serverinfo`
endpoint doesn't require the version in the request path to match the
server's actual version.

A user confirmed the login page is Tableau's native username/password
form, not SSO/SAML — so `TABLEAU_USERNAME`/`TABLEAU_PASSWORD` sign-in
against `/auth/signin` is the right flow (no browser-based login needed).

**A user ran `auth_smoke_test.py` from a machine with real access and it
reported `SUCCESS: authenticated (api_version=3.29)`.** Sign-in against
the real server is confirmed working. Credentials were never seen,
logged, or committed in this process.

**A user then ran `discover_view.py` from that same machine and it
resolved the real workbook/view:**

```
workbook: Return & Rotate
view: Dashboard 1
view_id: 741ce0df-8818-4412-93ca-b34c03019219
```

`TABLEAU_VIEW_ID` is now known. This confirms the "Query Views for Site"
endpoint (filtered by `contentUrl`) and the follow-up "Query Workbook"
call both work against the real server.

**A user then hit a malformed request URL**
(`/views/TABLEAU_VIEW_ID=<guid>/data` instead of `/views/<guid>/data`)
when running `extract_smoke_test.py`. Inspection of `config.py` →
`tableau_client.py`/`extraction_probe.py` confirmed `view_id` flows
straight from `os.environ.get("TABLEAU_VIEW_ID", "")` into the URL
f-string with no concatenation of the variable's own name anywhere in
this codebase — so the value stored in the environment/`.env` itself must
already have contained `TABLEAU_VIEW_ID=<guid>` as its *value* (most
likely `discover_view.py`'s own suggested line `-> set
TABLEAU_VIEW_ID=<guid>` pasted whole into an existing `TABLEAU_VIEW_ID=`
entry, i.e. `TABLEAU_VIEW_ID=TABLEAU_VIEW_ID=<guid>` in `.env`).
`require_view_id()` now rejects a value containing `=` or the variable's
own name with a clear `ConfigError` instead of silently building a broken
URL — see `tests/test_view_id_flow.py`.

**With the URL fixed, the same request now returns HTTP 406 Not
Acceptable** for
`GET /api/3.29/sites/34494572-d6be-4fa1-8c4a-d4836d30345f/views/741ce0df-8818-4412-93ca-b34c03019219/data`.
406 is specifically an HTTP content-negotiation status (RFC 7231 §6.5.6):
the server declines to produce a representation matching the request's
`Accept` header. Tableau's REST API signals authentication failures with
401 and permission/authorization failures with 403 (typically with a
Tableau-specific XML/JSON error code) — not 406 — so this points first at
the `Accept: text/csv` header the current code sends
(`extraction_probe.py`/`tableau_client.py`), not at credentials or the
"Export Data" permission.

**A user then ran `diagnose_extraction.py` (the first version, `/data`
only) and got a striking asymmetric result:**

```
Accept=text/csv         -> HTTP 406 immediately
Accept=(none)           -> timeout after 30s, no response
Accept=application/json -> timeout after 30s, no response
Accept=*/*              -> timeout after 30s, no response
```

This rules out "just remove the Accept header" as a fix -- a plain
content-negotiation rejection would be fast and consistent for every
unacceptable Accept value, not fast for one and a silent hang for every
other. Auth/session plumbing was re-verified and is correct: `auth.py`
parses `credentials.token` -> `auth_token` and `credentials.site.id` ->
`site_id` from the real signin response unmodified (site id matches the
user-confirmed `34494572-d6be-4fa1-8c4a-d4836d30345f`), and both flow
straight into `X-Tableau-Auth` and the `/sites/{site_id}/...` path in
every probe -- no bug found there.

The leading hypothesis is a routing-dependent intermediary (reverse
proxy/gateway in front of Tableau Server) or a server-side handler that
behaves very differently depending on the exact Accept value -- not
simple client-side content negotiation, and not obviously a 403-shaped
permission problem either. `diagnose_extraction.py` was extended to (a)
use a short 10s timeout and report elapsed time for every attempt, so a
hang is visible immediately rather than after 30s, and (b) also probe the
"Query View Crosstab Excel" endpoint (`/views/{id}/crosstab/excel`) -- a
genuinely different server-side code path from `/data` (pivoted crosstab
vs. detail/summary rows) that might not share the same failure mode, and
is arguably a better match for "the table data visible in a dashboard"
than raw view data. **Not yet run against the real server.**

Separately: the user observed that opening this dashboard in a browser
requires a click before the login flow proceeds. That's very likely a
UI-layer detail (an interstitial/SSO-redirect page needing a user
gesture) unrelated to the REST API layer -- the REST calls here
authenticate directly via `X-Tableau-Auth` and never go through the
browser's VizQL session at all. It's only worth revisiting as a real
blocker if REST API access turns out to be fundamentally not viable for
this view.

**The user then confirmed visually, from the live dashboard's UI, that a
native "Download" menu exists** with entries: Image, Vector Image, Data,
Crosstab, PDF, PowerPoint, Tableau Workbook. This does NOT by itself
confirm which backend endpoint "Crosstab" calls -- see below.

### Native UI Crosstab download -- investigation status

Tableau's interactive web client (the dashboard you click around in) is
architecturally separate from the public REST API under `/api/{version}/`.
It's built on VizQL and its UI actions (including "Download -> Crosstab")
have historically been observed, across the Tableau Server community, to
call **internal, undocumented, cookie-session-based endpoints** -- not
the `X-Tableau-Auth`-token-based public REST API. The exact internal path
varies by Tableau Server version and is not part of any documented
contract, so it should not be guessed at with confidence; it must be
captured directly.

**We could not capture it in this session**: no browser automation tool
is available here, and this sandbox's network policy blocks
`insight.erajaya.com` entirely (see "Network blocker" above) even if one
were. `src/exportbot/crosstab_probe.py` / `crosstab_probe.py` (CLI) were built
instead to test the one endpoint we can concretely name and already have
infrastructure for: the *documented* public REST "Query View Crosstab
Excel" endpoint (`GET .../views/{id}/crosstab/excel`), authenticated via
our existing `X-Tableau-Auth` session. This is explicitly a **hypothesis
probe**, not a claim that it matches the browser's actual request --
**not yet run against the real server.**

**DevTools capture steps** (to determine the real browser request; see
`crosstab_probe.py`'s docstring for why this matters):

1. In Chrome (or any Chromium-based browser), navigate to
   `https://insight.erajaya.com/#/views/ReturnRotate/Dashboard1` and log
   in normally.
2. Open DevTools (F12, or Cmd+Option+I on Mac) and go to the **Network**
   tab.
3. Check **Preserve log**, then click the "clear" (🚫) button so the log
   is empty.
4. On the dashboard, click the **Download** toolbar icon, then
   **Crosstab**, and complete any sheet/format prompt Tableau shows.
5. In the Network tab, find the new request(s) that appear right after
   the click (look for a path containing something like "crosstab",
   "export", or "download", or a response `Content-Type` of
   `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` or
   `text/csv`). If more than one request fires (e.g. a setup call
   followed by the actual file), note all of them in order.
6. Click the relevant request and read its **Headers** panel. Note, and
   share back:
   - Request method (GET/POST) and full path (the host is already known,
     so that part isn't sensitive)
   - Query string parameters (names and values, unless a value looks
     like a session/auth token)
   - Which request header **names** were sent (e.g. `Cookie`,
     `X-XSRF-TOKEN`, `Accept`) -- but not their values if they look like
     session IDs, tokens, or cookies
   - Whether a request body/payload is present, and its field names
   - Response headers `Content-Type`, `Content-Disposition` (often
     contains the filename -- safe to share in full), `Content-Length`
7. **Do not** paste a raw "Copy as cURL" export to me -- it embeds live
   session cookies. If you use it to double check something locally,
   strip the `-H 'cookie: ...'` and any token-bearing headers first.
8. The "Initiator" column/tab for that request can hint at which part of
   Tableau's JS triggered it, useful context but not required.

**A user captured a real HAR file of the "Download -> Crosstab" click and
shared it locally (never committed).** Inspecting it (structurally --
every cookie, token, and session id value was redacted before being
looked at; one session id was accidentally printed in an intermediate
debugging step in this environment, never committed or exposed
elsewhere) revealed the real flow, 10 requests:

```
POST /vizql/w/{workbook}/v/{view}/sessions/{id}/commands/tabdoc/launch-hybrid-view-data-dialog
POST /vizportal/api/web/v1/reportEventUnauthenticated          (telemetry, not part of the export)
POST /vizql/w/{workbook}/v/{view}/sessions/{id}/commands/tabdoc/view-data-dialog-show-aliases
POST /vizql/w/{workbook}/v/{view}/sessions/{id}/commands/tabsrv/get-view-data-csvexport-settings-server
POST /vizql/w/{workbook}/v/{view}/sessions/{id}/commands/tabdoc/get-view-data-dialog-tab-pres-model
POST /vizql/w/{workbook}/v/{view}/sessions/{id}/commands/tabsrv/export-view-data-summary-to-csv-server
GET  /vizql/w/{workbook}/v/{view}/tempfile/sessions/{id}?key={resultKey}&keepfile=yes&attachment=yes
```

Findings:

1. **Session establishment**: NOT visible in this HAR. The VizQL session
   id (in every URL path) and a `visualIdPresModel` identifier are both
   already present on the *first* captured request -- both are minted
   during the browser's interactive page load/bootstrap, before this
   capture started. Cannot be derived from any REST call this project
   has.
2. **Headers on every VizQL call**: `X-XSRF-TOKEN` (anti-CSRF, present on
   all of them). **No `X-Tableau-Auth` header appears anywhere.** No
   `Cookie` header is visible either, but that's almost certainly
   Chrome's own HAR-export privacy redaction (strips cookies/auth by
   default), not evidence that no cookie is used.
3. **Request bodies** (`multipart/form-data`; field *names* confirmed
   directly from the HAR):
   - `get-view-data-csvexport-settings-server`: just `telemetryCommandId`
   - `export-view-data-summary-to-csv-server`: `visualIdPresModel`,
     `showAliases` (`"true"`), `columns` (JSON array of column-name
     strings), `listSeparatorValue` (`","`), `unicodeEncodingValue`
     (`"UTF-8"`), `telemetryCommandId`
4. **Session id origin**: unknown -- established before this capture
   began (see point 1).
5. **Can existing REST auth (`X-Tableau-Auth`) establish this session?**
   Based on direct evidence: **no** -- it's absent from all 10 requests.
   This is architecturally a separate, cookie/session-based mechanism
   from the public REST API.
6. **Tokens confirmed required**: `X-XSRF-TOKEN` header (present,
   value never inspected); very likely a session cookie too (redacted by
   Chrome before we ever saw the file).
7. **Tempfile URL reproducibility**: the `key` query parameter is minted
   server-side and returned in the export command's own JSON response, at
   `vqlCmdResponse.layoutStatus.applicationPresModel
   .presentationLayerNotification[0].presModelHolder
   .genExportFilePresModel.resultKey` (found by walking the actual
   response structure -- not documented anywhere). The same object also
   carries `fileName` and `mimeType`, confirming the user-reported
   `Persentase Nasional_data.csv` / `text/csv` result exactly.

**The hard blocker**: `visualIdPresModel` and the session id are both
minted client-side during interactive page rendering. Without a browser
(or a full reproduction of Tableau's cookie-based web login + bootstrap,
which is a bigger, separate decision -- see "Next steps"), this project
cannot obtain them on its own. `src/exportbot/vizql_crosstab_probe.py` /
`vizql_crosstab_probe.py` (CLI) were built to test the one thing that
*is* answerable without solving that: given an already-valid session id
and visual context (pasted fresh from a DevTools capture as
`TABLEAU_VIZQL_SESSION_ID` / `TABLEAU_VIZQL_VISUAL_ID_PRES_MODEL` /
`TABLEAU_VIZQL_COLUMNS_JSON` env vars -- never committed, expire
quickly), can the export-command -> tempfile-download tail of the flow
be reproduced with plain `requests`? **Not yet run against the real
server.**

Still unverified (needs a machine with real access, i.e. not this
sandbox):

- Whether `vizql_crosstab_probe.py`, given a live session id pasted from
  DevTools, can actually reproduce the export + tempfile download.
- Whether the documented `/crosstab/excel` REST endpoint (as opposed to
  whatever the browser calls) behaves any differently than `/data` for
  this specific dashboard view -- `crosstab_probe.py` has not been run
  there yet.
- Whether the "view data" endpoint (`extraction_probe.py` /
  `extract_smoke_test.py`, same endpoint as `tableau_client.py`) actually
  returns data for this view once/if the 406/timeout pattern is resolved.
- End-to-end `python main.py` run with real credentials and the real
  `TABLEAU_VIEW_ID`.

`tableau_client.py` is written to match Tableau's documented REST API,
but the data-extraction call itself is still explicitly unverified —
treat it as a starting point to debug from a machine with real network
access, not as working code.

### Browser automation (`browser_crosstab.py`) -- pivot for a working MVP

Given the time pressure of needing a working end-to-end export and the
REST/VizQL session-establishment blocker above, the priority shifted to
browser automation: `browser_crosstab.py` (Playwright) drives an actual
browser through the exact manual workflow already confirmed to work —
login, open the dashboard, Download -> Data -> Crosstab, save the CSV.
This sidesteps the whole "how is a VizQL session minted" question
entirely, since a real browser mints it the normal way.

**Not exercised against the real server from this environment**: the
`playwright` Python package installs fine here (PyPI is reachable), but
`playwright install chromium` fails — `cdn.playwright.dev` isn't on this
sandbox's egress allowlist, confirmed directly (`403`,
`"no rule or allowlist entry allows host \"cdn.playwright.dev\""`). Even
with a browser binary, network access to `insight.erajaya.com` is
blocked here too. So this code is untested against the live dashboard;
only its own orchestration/fallback-matching/file-saving logic is unit
tested (mocked Playwright objects, see below).

**DOM selectors are best-effort**, built from accessible roles/names
(Tableau's own accessibility conventions), not from an actual DOM
inspection of this specific dashboard — that inspection was never
possible from here. Each step tries a short list of fallback locators
(role/name, then common Tableau test-id/aria-label patterns) across the
main page and any iframes, in case the viz is embedded. If a step can't
find what it's looking for, it raises a clear error and saves a
screenshot to `<EXPORT_DIR>/debug_<step>.png` — check that screenshot
and adjust the candidate locator lists in
`src/exportbot/browser_crosstab.py` (`_login_if_needed()`,
`_wait_for_dashboard()`, `_export_crosstab_csv()`) to match the real
page.

**A user ran it locally and hit a real bug**: the browser opened
Tableau's login page (visually confirmed: username input, password
input, blue "Sign In" button) but the script never attempted to log in —
it printed `Opening Tableau...` then immediately failed waiting for the
dashboard. Root cause: `_login_if_needed()` checked for the password
field with a single instant `Locator.count()` call, but `count()` does
not wait for anything to render — on a JS-rendered SPA, the login form
likely wasn't in the DOM yet at that exact instant, so detection
false-negatived as "no login form present" and login was silently
skipped entirely. Fixed by polling for the login form the same way
`_wait_for_dashboard()` already polls for the Download button
(`_find_first_with_wait()`, checking across frames too), and by
confirming the form actually disappears (`password_field.wait_for(state=
"hidden", ...)`) plus letting network activity settle after clicking
Sign In, instead of assuming the click worked. A regression test
(`test_login_if_needed_waits_for_slow_rendering_login_form`) reproduces
the exact bug shape (field appears only after a couple of polling ticks)
and confirms the fix.

**A user then confirmed login, dashboard load, and Download -> Data ->
Crosstab all work** — real progress, driven entirely from a real local
run this environment couldn't produce itself. The flow got stuck only on
the final export-button click:
`Locator.click: Timeout 30000ms exceeded`, with Playwright's own call log
showing the button as visible/enabled/stable but blocked by
`<div class="wcGlassPane" id="loadingGlassPane">` and then
`[data-tb-test-id="Dialog-Provider-Dialog-Glass"]` intercepting pointer
events. Fixed by waiting for both overlays to reach Playwright's
`"hidden"` state (`Locator.wait_for(state="hidden")`, which already
resolves immediately if a selector matches nothing — so an overlay that
never appears is not an error) immediately before the final click,
wrapped so any failure at this step (overlay never clears, click still
fails) produces a clear `BrowserAutomationError` and a debug screenshot
instead of a raw, uncaught Playwright traceback.

**A user then shared a screenshot revealing the real "final export"
shape was wrong.** Clicking Download -> Data does not open an in-page
dialog with a "Crosstab" tab (the assumption the overlay fix above was
still built on) — it opens a **separate browser window/tab**, titled
"View Data: `<sheet name>`", at a URL under
`/vizql/v_.../hybrid-window/min/index.html?id=...` (matching the
`launch-hybrid-view-data-dialog` VizQL command identified earlier from
the HAR). That window has its own "Summary"/"Full Data" tabs (not
"Crosstab" — "Summary" is active by default) and its own toolbar
"Download" button; the actual CSV export happens entirely inside it,
confirmed via the HAR's `export-view-data-summary-to-csv-server` +
tempfile-GET sequence. The main dashboard can separately show "The View
Data window is still open. Close the View Data window?" — a cleanup
prompt, not part of the export, intentionally left alone.

`_export_crosstab_csv()` was split: it now only handles Download -> Data
and catches the new popup via `page.expect_popup()` (an event-based
Playwright wait, not a sleep) — never `page.expect_download()` against
the main dashboard page. A new `_export_from_view_data_popup()` does the
actual export *inside* that popup page: finds its Download button
(`VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES`, best-effort/unverified beyond
the screenshot), reuses `_wait_for_overlays_to_clear()` against the
popup (the same overlay classes could plausibly appear there too), and
wraps `expect_download()` around the popup's own button. The old
"Crosstab tab" click was removed entirely — it doesn't correspond to
anything in the real UI. Regression tests cover: the full flow through
both functions via a fake popup page, the popup failing to open, the
popup's Download button being blocked by an overlay (clearing normally
and never clearing), and the popup's Download button being missing.
**Still not run against the real server from here** — this targets the
exact popup shape from the screenshot, but whether the click actually
completes the download has not been confirmed yet.

**A user then confirmed the popup detection itself works**
(`Opening Data...` / `Exporting Crosstab...` printed correctly) but hit
`Could not find the "Download" button in the View Data window.` --
`VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES` was reused verbatim from the main
dashboard's toolbar without any confirmation it matches the popup's DOM,
and it doesn't. Rather than guess a second time, `_debug_dump_view_data_popup()`
was added (called unconditionally, right after the popup loads, before
`_export_from_view_data_popup()`) as **temporary debug-only
instrumentation** -- it prints the popup's URL/title, every visible
button's `inner_text`/`aria-label`/`role`/`data-tb-test-id`/`title`,
every visible element whose text matches `/download/i`, a screenshot at
`<EXPORT_DIR>/debug_view_data_popup.png`, and the outerHTML of the first
Download-text match plus its parent/grandparent (all via
`Page.evaluate()`, reading only visible UI text/attributes -- never
credentials, cookies, or tokens). Every step is independently
try/excepted so a diagnostic failure never blocks the real export
attempt that follows. **Not yet run against the real server** -- this is
the very diagnostic meant to produce that data; once its output is
shared back, `VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES` should be corrected
to match and `_debug_dump_view_data_popup()` (and its call site) removed.

**A user then ran that diagnostic against the real server and got a
striking result: zero visible buttons, zero "Download"-text elements,
and `popup.title() == "Hybrid Popup Window"`** -- not the expected
"View Data: <sheet name>" title assumed since the earlier screenshot.
The popup opens correctly (`Opening Data...` / `Exporting Crosstab...`
print as expected), but its top-level document has no matching content
at all. This rules out "the locator candidates are just slightly wrong"
and points at the real content living somewhere the top-level-document
scan can't see: a nested iframe, an open shadow root, a canvas-rendered
UI, or even a separate page in the same browser context.

`_debug_dump_view_data_popup()` was extended (still temporary,
debug-only instrumentation, still never touching credentials/cookies/
tokens) to investigate exactly that, without guessing another locator:

- `_redact_url()` -- redacts query-parameter *values* whose name looks
  session/auth/token-shaped (`id`, `key`, `session`, `token`, `ticket`,
  `sid`, `xsrf`, `csrf`, etc. -- see `SENSITIVE_URL_PARAM_NAMES`) before
  any URL is printed, since the popup's own URL embeds an opaque `id`
  value that behaves like a session/ticket identifier. Never raises --
  an unparseable value prints as `[unparseable URL]`.
- `_FRAME_INSPECTION_SCRIPT` + `_inspect_frame()` -- a single JS snippet,
  evaluated per-frame (main and nested), that reports: body
  `innerHTML` length and a 3000-char-truncated `innerText`, every
  visible button/`[role="button"]`/`[role="menuitem"]` (tag, text,
  aria-label, title, `data-tb-test-id`, class), every visible element
  whose text/aria-label/title matches `/download/i`, nested `<iframe>`
  `src`/`name`/`id`, a recursive (depth-capped) walk of open shadow
  roots for the same button/download-text info, every `<canvas>`'s
  dimensions plus its parent/grandparent `outerHTML` (truncated) --
  covering the "it's canvas-rendered, not real DOM" hypothesis -- and a
  3000-char `outerHTML` preview of the whole document. `_inspect_frame()`
  wraps this in try/except and returns `{"error": ...}` instead of
  raising, so one broken frame never stops the others from being
  inspected.
- `_debug_dump_view_data_popup()` now enumerates `view_data_page.frames`
  and runs `_inspect_frame()` on each one (redacting `src` on any
  reported iframe before printing it), and separately enumerates
  `view_data_page.context.pages` -- other pages open in the same browser
  context -- printing each one's (redacted) URL and title, to check
  whether the real "View Data" UI is actually a distinct page rather
  than a frame within this popup at all. Every step remains
  independently try/excepted, so a single failure (e.g. `.frames`
  enumeration itself failing) is reported inline and never blocks the
  rest of the dump or the real export attempt that follows.

Regression tests (`tests/test_browser_crosstab.py`) cover `_redact_url()`
(sensitive params redacted, non-sensitive ones like a `sheet` param
preserved, and a genuinely unparseable input never raising), `_inspect_frame()`
(returns the frame's `evaluate()` result normally, and `{"error": ...}`
without raising when `evaluate()` throws), and the extended
`_debug_dump_view_data_popup()` (prints per-frame and context-page
diagnostics with a redacted URL, and never raises even when title(),
screenshot(), frame enumeration, per-frame evaluation, and context-page
enumeration all fail at once). **Not yet run against the real server**
-- this is, again, the very diagnostic meant to produce that data; once
its output (which frame/shadow-root/canvas/context-page actually holds
the Download control) is shared back, `VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES`
should be corrected to target it precisely, and the debug instrumentation
(`_redact_url`'s callers inside `_debug_dump_view_data_popup`,
`_FRAME_INSPECTION_SCRIPT`, `_inspect_frame`, `_debug_dump_view_data_popup`
itself, and its call site in `_export_crosstab_csv`) removed.

### Network-based export -- pivot away from the popup's DOM entirely

**A user ran the extended frame/shadow-DOM/canvas/context-page diagnostic
against the real server and got a conclusive answer: the "View Data"
popup is only a thin wrapper.** `popup.frames` had exactly 1 entry (the
same hybrid-window URL as the popup itself, not a distinct nested
document), that single frame's `body_inner_html_length` was 931 but
`body_inner_text` was empty, and it had zero visible buttons, zero
"Download"-text matches, zero nested iframes, zero open shadow roots,
and zero canvas elements. The browser context had exactly 2 pages: the
main Tableau dashboard and this popup -- no third, hidden page hosting
the real UI either. In short: there is no DOM control anywhere in this
popup to click, on any real level (top document, nested frame, shadow
DOM, canvas, or a sibling page) -- confirming this popup is a
"communication shell" (its own title, "Hybrid Popup Window", already
hinted at this), not a rendered dialog.

Continuing to search for a DOM locator was therefore abandoned in favor
of the one thing already known to be real and reproducible: the HAR
capture of the actual "Download -> Crosstab" flow (see "Native UI
Crosstab download" above), whose last two steps are a VizQL command
(`.../commands/tabsrv/export-view-data-summary-to-csv-server`) and a GET
to a `.../tempfile/sessions/{id}?key={resultKey}&keepfile=yes&attachment=yes`
URL that returns the actual CSV (`Content-Type: text/csv`,
`Content-Disposition: attachment`). Whatever triggers that VizQL command
in the real UI, the tempfile response is the one network event that
reliably signals "the export happened" -- so `_export_from_view_data_popup()`
was rewritten around capturing it directly, instead of a DOM click ->
`page.expect_download()` flow:

1. `_attach_export_network_diagnostics()` attaches request/response
   listeners (`_log_export_request()` / `_log_export_response()`) to
   both the main dashboard page and the popup page. Playwright's
   page-level request/response events also bubble to the shared browser
   context, so attaching to either page observes traffic from both --
   this is still done explicitly on both pages for clarity and so the
   `[NETDEBUG]` log lines are attributable. Only requests/responses whose
   URL contains `export-view-data-summary-to-csv-server` or
   `/tempfile/` are logged (method, status, Content-Type, and the
   `_redact_url()`-redacted URL) -- never headers beyond Content-Type,
   never bodies, and every accessor is wrapped so a broken listener can
   never break the real export attempt.
2. A click target is still searched for -- not because the popup's DOM
   has one (it doesn't), but because *something* must trigger the VizQL
   export command, and the only non-speculative candidate available is
   the existing `VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES` list. Rather than
   invent a new locator (explicitly ruled out), `_find_first_with_wait_across_pages()`
   searches that same list against both the popup and the main dashboard
   page together, on one shared deadline -- covering the hypothesis that
   the main page, not this popup, owns the real View Data UI and the
   popup is only a target window for the eventual download. If neither
   page has a match, this raises `BrowserAutomationError` rather than
   guessing further, pointing at the `[NETDEBUG]` log for whatever
   traffic (if any) was actually observed.
3. The eventual click is wrapped in
   `view_data_page.context.expect_event("response", predicate=...)`
   matching `/tempfile/`, waited at the **browser context** level (not
   a specific page) so it's caught regardless of which of the two pages
   the request actually originates from -- addressing directly whether
   the existing "Exporting Crosstab..." step could wait for this response
   instead of Playwright's `download` event: yes, and this is more
   robust, since a fetch/blob-based export may never raise Playwright's
   `download` event at all even on success, while the tempfile HTTP
   response fires either way.
4. `_save_tempfile_response()` reads that response directly (`response.body()`),
   verifies its status is not an error and its Content-Type contains
   "csv" or "text" before writing anything (refusing to save anything
   else), and picks a filename from the response's own
   `Content-Disposition` header when present (parsed by
   `_extract_filename_from_content_disposition()`, matching both the
   plain and RFC 5987 forms) -- sanitized by `_sanitize_filename()`
   (bare basename only, hostile characters replaced) before ever being
   joined onto `EXPORT_DIR`, since that header value is server-supplied
   and must never be trusted as a path. Falls back to the existing
   generated `ReturnRotate_<timestamp>.csv` name when no usable
   `Content-Disposition` is present.

The `_debug_dump_view_data_popup()` DOM diagnostic from the previous
round is kept in place per explicit instruction, even though the
real-server run already answered the question it was built to answer --
it's cheap, still runs before the network-based export attempt, and
would still be useful if a future Tableau update changes this behavior.
No fixed sleeps were added anywhere in this flow. Login/dashboard
navigation and popup detection (`page.expect_popup()`) are unchanged.

Regression tests (`tests/test_browser_crosstab.py`) cover: `_is_export_related_url()`
matching exactly the two HAR-confirmed URL fragments and nothing else;
`_extract_filename_from_content_disposition()` (plain and RFC 5987
forms, absent header); `_sanitize_filename()` (path components and
hostile characters stripped, empty/unusable input returns `None`);
`_save_tempfile_response()` (Content-Disposition-derived filename,
fallback to a generated name, rejecting a non-CSV/text Content-Type,
rejecting an HTTP error status); `_attach_export_network_diagnostics()`
(registers both listeners, never raises when `.on()` itself fails);
`_find_first_with_wait_across_pages()` (finds a match on either page
sharing one deadline, returns `None` on timeout); and the rewritten
`_export_from_view_data_popup()` end-to-end (overlay-wait-before-click
preserved, tempfile response captured and saved, timeout when no
tempfile response ever arrives, `BrowserAutomationError` when neither
page has a click target, and the click target found on the main page
instead of the popup).

**A user then ran this against the real server, and the result ruled out
every DOM-based click entirely** (see "Reproducing the export command
directly" below): the click-target search above found the *real* main
dashboard Download button (`#download` /
`data-tb-test-id="viz-viewer-toolbar-button-download"`, confirmed by
Playwright resolving the locator), but clicking it while the View Data
dialog is open hung and timed out -- Tableau's own
`Dialog-Provider-Dialog-Glass` overlay intentionally blocks the main
toolbar while a dialog is active, so that click was never going to
trigger anything. `VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES`,
`_find_first_with_wait_across_pages()`, `OVERLAY_SELECTORS`, and
`_wait_for_overlays_to_clear()` were all removed as a result -- there is
no DOM click, anywhere, that triggers this export. `_export_from_view_data_popup()`
was replaced entirely by `_export_view_data_via_vizql_commands()`.

### Reproducing the export command directly -- no click, anywhere

**The two real-server findings above (an entirely empty "View Data"
popup DOM, and the main dashboard's Download button blocked by
Tableau's own dialog overlay while that popup is open) rule out every
DOM-based trigger for the export command.** There is no button, in the
popup or on the main page, that a real automated click can use. Rather
than guess at some other UI element (explicitly ruled out), the fix
reproduces Tableau's own VizQL command flow directly, using the HAR
capture (see "Native UI Crosstab download" above) as ground truth:

Opening the View Data dialog (the existing "Data" click -- kept, already
confirmed working) already fires four VizQL commands automatically, with
**no further click needed**: `launch-hybrid-view-data-dialog`,
`view-data-dialog-show-aliases`, `get-view-data-csvexport-settings-server`,
`get-view-data-dialog-tab-pres-model`. Rather than try to trigger the
fifth command (`export-view-data-summary-to-csv-server`) via some
unknown UI element, `browser_crosstab.py` now issues it directly:

- `_attach_vizql_bootstrap_listeners()` attaches request/response
  listeners at the **browser context** level (`page.context.on(...)`),
  not a specific page -- Playwright's context-level events aggregate
  traffic from every page in the context, including the "View Data"
  popup page, which doesn't exist yet at the moment this is attached.
  This must happen *before* the "Data" click (not after, as the earlier,
  now-removed network-diagnostic-only version did) since that click is
  what fires the four bootstrap commands, per the HAR.
- `_capture_bootstrap_request()` passively pulls the VizQL session id and
  the workbook/view path prefix (e.g. `w/ReturnRotate/v/Dashboard1`) out
  of the URL *path* of any of those four commands (every VizQL command
  URL carries them), and the `X-XSRF-TOKEN` header value out of the
  request itself -- reusing the exact, already-valid token the browser
  is already sending, rather than guessing which cookie it's derived
  from.
- `_capture_bootstrap_response()` passively searches each bootstrap
  command's JSON response body for two exact field names confirmed by
  the HAR -- `visualIdPresModel` and `columns` -- via a generic recursive
  key search (`_find_key_recursive()`), not a hardcoded nested path,
  since the real response shape was never captured beyond those field
  names.
- Once `_wait_for_view_data_session_capture()` confirms enough context
  has been observed (polling, no fixed sleep; `columns` is treated as
  best-effort and not required), `_export_view_data_via_vizql_commands()`
  POSTs `export-view-data-summary-to-csv-server` and GETs the resulting
  `tempfile` URL itself, using `page.context.request` -- a Playwright
  `APIRequestContext` that **automatically reuses the real browser
  session's cookies** ("The method will populate request cookies from
  the context", per Playwright's own documentation), with the captured
  `X-XSRF-TOKEN` set explicitly as a header (the one thing Playwright
  does not attach automatically). This is a meaningfully different
  mechanism from the earlier, never-tested `vizql_crosstab_probe.py`
  module, which used plain `requests` with **no cookies at all** -- a
  likely reason it was never expected to work against the real server.
  `resultKey` is pulled from the export response the same
  generic-recursive-search way. The tempfile response is validated and
  saved by the existing `_save_tempfile_response()`, unchanged, since
  Playwright's `APIResponse` and page `Response` objects expose the same
  `status`/`headers`/`body()` shape.
- A known, explicit limitation: `columns` is sent as an empty JSON array
  when it could not be captured. Whether Tableau's server treats that as
  "all columns" or requires the real list is unverified -- if the real
  run's exported CSV is missing columns, this is the first place to
  look.

Login, dashboard navigation, the Download-menu click, the "Data" click,
popup detection (`page.expect_popup()`), and the still-kept
`_debug_dump_view_data_popup()` DOM diagnostic are all unchanged. No
fixed sleeps were added. Regression tests (`tests/test_browser_crosstab.py`)
cover: `_find_key_recursive()` (nested dict, nested list, not found);
`_ViewDataSessionCapture.is_complete()` (all core fields required,
`columns` deliberately not); `_capture_bootstrap_request()` (extracts
session id/origin/prefix/XSRF token, ignores non-VizQL and
export/tempfile URLs, never raises when header access fails);
`_capture_bootstrap_response()` (extracts `visualIdPresModel`/`columns`
via recursive search, rejects a non-string-list `columns`, never raises
on an unparseable body); `_attach_vizql_bootstrap_listeners()` (never
raises when `.on()` fails); `_wait_for_view_data_session_capture()`
(returns immediately when already complete, raises on timeout); and
`_export_view_data_via_vizql_commands()` (full success path asserting
the exact POST/GET calls made and the saved file's contents, plus the
network-error, non-OK-status, missing-`resultKey`, and
tempfile-fetch-failure cases) -- plus an updated
`_export_crosstab_csv()` end-to-end test exercising the whole flow
through a fake browser context. **This has not yet been run against the
real server** -- the next real run is what will show whether the
captured session context is actually sufficient (right session id,
right XSRF token, right presModel) to make Tableau accept the export
command, and whether the empty-`columns` fallback produces a complete
CSV. The `[NETDEBUG]` log lines from that run (which report only
booleans -- whether each piece was captured -- and response
status/Content-Type, never the actual values) are the next thing to
share back.

**A user then ran this against the real server and it timed out**:
`Timed out waiting to observe Tableau's own View Data bootstrap VizQL
commands`. Nothing was captured at all. Root cause: the bootstrap
listeners were attached inside `_export_crosstab_csv()`, immediately
before the "Data" click, on the assumption that click alone is what
fires Tableau's VizQL bootstrap commands. But a VizQL session isn't
specific to the View Data dialog -- any interactive Tableau viz has to
establish its own VizQL session just to render at all, and that
session's id and `X-XSRF-TOKEN` are already carried on commands fired
during the *dashboard's own* initial load, well before "Data" is ever
clicked. Attaching only around that click starts listening after the
traffic that actually carries the session context already happened, so
the capture could never complete.

Fixed by moving `_attach_vizql_bootstrap_listeners()` out of
`_export_crosstab_csv()` and into `run_browser_crosstab_export()`,
attached right after the dashboard navigation (`page.goto(DASHBOARD_URL,
...)`), before even `_wait_for_dashboard()` -- as early as this project
can possibly listen. `_export_crosstab_csv()` now takes the resulting
`capture` object as a parameter instead of creating its own. The View
Data dialog's own four bootstrap commands (still fired by the kept
"Data" click) remain a second chance to observe `visualIdPresModel`/
`columns` if the dashboard's own load didn't already carry them, since
`_wait_for_view_data_session_capture()` (unchanged, still polling, no
fixed sleep) is still called before issuing the export command.

Separately, `VIZQL_SESSION_ID_RE` was broadened to tolerate an optional
`t/{site}/` segment (`/vizql/(?:t/[^/]+/)?w/.../v/.../sessions/...`) --
Tableau's own documented URL convention for a non-default site, omitted
only for the Default site. This project has not confirmed which applies
here (the plain `/#/views/...` dashboard URL suggests the Default site,
but `TABLEAU_SITE` may be set), so the regex now accepts either rather
than assuming -- a low-risk generalization of a known Tableau convention,
not a guess about this server's specific response shapes.

Regression tests were updated to match: `_export_crosstab_csv()` now
takes `capture` as a parameter (tests pass a pre-populated one for the
success path, and an empty one for the error paths that fail before
reaching capture-dependent code), and a new
`test_run_browser_crosstab_export_attaches_vizql_bootstrap_listeners_before_wait_for_dashboard`
pins the corrected ordering (`context.on` called twice, then
`_wait_for_dashboard`) via a shared call-order mock, so a future change
can't silently move the attachment later again. **Still not run against
the real server** -- the next run is what will show whether attaching
this early actually captures the session context in practice.

### Discovering the session instead of observing it -- listeners abandoned entirely

**A user then ran the earlier-attachment fix against the real server and
it timed out again, having captured nothing at all** -- even though the
dashboard loaded and the View Data popup opened correctly. Two separate
real-server runs of a listener-based approach (first attached right
before the "Data" click, then moved to right after the dashboard
navigation) both failed to observe a single VizQL bootstrap command.
This is strong evidence the problem was never about *when* the listener
was attached: these commands are not reliably observable as ordinary
Playwright page-level network events at all in this environment, for
reasons this project could not pin down further without another live
round trip (a different network domain/target than Playwright's
page-level CDP hookup, a Service Worker, or something else entirely).

Rather than attempt a third listener-timing variant, the strategy
changed completely: instead of trying to *observe* the VizQL session
being established, **discover it from browser state that already exists
once the page has loaded** -- no listener, no timing race, no "was
anything listening at the right moment" question at all:

- `_discover_active_vizql_session()` reads the standard **Performance
  Resource Timing API** (`performance.getEntriesByType("resource")`,
  evaluated via `page.evaluate()`) on both the main dashboard page and
  the View Data popup. Every browser keeps this list for the life of the
  page regardless of whether a Playwright listener existed when the
  request happened -- and any interactive Tableau viz *must* have issued
  at least one VizQL session-scoped command just to render, so a
  matching URL is already sitting in this list by the time the dashboard
  (or popup) has visibly loaded. `_find_vizql_session_in_resource_urls()`
  searches those URLs with the same `VIZQL_SESSION_ID_RE` used
  previously, extracting the origin, workbook/view path prefix, and
  session id.
- The `X-XSRF-TOKEN` value is read directly from its matching cookie
  (`_find_xsrf_token_in_cookies()`, via `context.cookies()`) instead of
  waited for on some outgoing request's headers. Tableau's own header
  name is exactly Angular's default double-submit-cookie header, which
  Angular's `HttpClientXsrfModule` only ever populates from a
  same-named cookie (`XSRF-TOKEN` / `X-XSRF-TOKEN`) -- so the value is
  already sitting in the cookie jar too, no request needed.
- This runs as a short, bounded poll (`_ViewDataSessionCapture.has_session()`
  checked each tick, reusing the existing `VIEW_DATA_POPUP_TIMEOUT_MS` --
  no new or longer timeout added) purely as a safety margin for the
  Performance buffer not having recorded anything at the *exact* instant
  this is first called, not as a network-event wait.

Once a session is discovered, the code no longer waits to *observe* the
`get-view-data-dialog-tab-pres-model` bootstrap command either -- it
issues that HAR-confirmed command **itself**,
`_fetch_view_data_dialog_tab_pres_model()`, via the browser's own
authenticated request context, and reads `visualIdPresModel`/`columns`
directly from its own response (same `_find_key_recursive()` search as
before). Its only field is `telemetryCommandId` -- the one field
confirmed common to sibling `tabsrv` commands in the HAR notes above; no
other field is invented. `launch-hybrid-view-data-dialog` and
`view-data-dialog-show-aliases` are deliberately **not** issued by this
project: their exact request bodies were never captured, and the "Data"
click already opens the real popup, whose own internal JS (even though
it renders nothing visible) very plausibly already performs any
server-side "launch" step on its own -- issuing them ourselves would
mean inventing payloads for commands whose fields are unconfirmed,
which was explicitly ruled out.

`_export_view_data_via_vizql_commands()` now sequences all of this:
discover the session, fetch the tab presentation model, POST the export
command, GET the tempfile, save it (`_save_tempfile_response()`,
unchanged). All of the old listener machinery
(`_attach_vizql_bootstrap_listeners`, `_capture_bootstrap_request`,
`_capture_bootstrap_response`, `_wait_for_view_data_session_capture`,
and the early-attachment code this added to `run_browser_crosstab_export()`)
was removed as dead code -- `_export_crosstab_csv()` and
`run_browser_crosstab_export()` are back to their simpler, pre-listener
call shape (`_export_crosstab_csv(page, export_dir)`), with `capture`
now created and populated entirely within the export step itself.

Error messages for both the tab-pres-model command and the export
command now include the real HTTP status **and a truncated response
body** (`_safe_response_text_snippet()`, capped at 300 chars -- the same
convention already used in `crosstab_probe.py`) directly in the
exception text, so a real failure is diagnosable from the error alone,
without a further debug round trip.

Regression tests (`tests/test_browser_crosstab.py`) cover:
`_find_vizql_session_in_resource_urls()` (extracts session context from
a matching URL, returns `None` when nothing matches); `_find_xsrf_token_in_cookies()`
(matches known cookie names, returns `None` when absent, never raises
when `.cookies()` fails); `_discover_active_vizql_session()` (finds the
session via a *second* page's resource URLs when the first has none,
returns immediately when the capture is already complete, raises on
timeout); `_safe_response_text_snippet()` (truncates to 300 chars, never
raises when reading the body fails); `_fetch_view_data_dialog_tab_pres_model()`
(success path asserting the exact request made, and the network-error,
non-OK-status, and missing-`visualIdPresModel` failure cases); and the
full `_export_view_data_via_vizql_commands()` / `_export_crosstab_csv()`
flows re-verified end to end through the two-POST sequence (tab
presentation model, then export) via fake Performance-resource-URL and
cookie state. **Still not run against the real server** -- the next run
is what will show whether the Performance Resource Timing API and
cookie jar actually contain what this expects in practice, and whether
issuing `get-view-data-dialog-tab-pres-model` without a prior
`launch-hybrid-view-data-dialog` call from this project (relying on the
popup's own click-triggered JS to have done it) is accepted by the
server.

### The popup already does the whole export automatically -- discovery abandoned entirely

**A user ran the session-discovery version against the real server, and
the result was conclusive.** The run got further than ever before:
login succeeded, the dashboard loaded, Download -> Data opened the real
popup -- and this project's own diagnostic-only network logger
(`_attach_export_network_diagnostics()`, unchanged, kept purely for
visibility) recorded, in order:

```
[NETDEBUG] request  method=POST url=.../sessions/REDACTED/commands/tabsrv/export-view-data-summary-to-csv-server
[NETDEBUG] response status=200 content_type='application/octet-stream;charset=UTF-8' url=.../sessions/REDACTED/commands/tabsrv/export-view-data-summary-to-csv-server
[NETDEBUG] request  method=GET url=.../tempfile/sessions/REDACTED?key=REDACTED&keepfile=yes&attachment=yes
[NETDEBUG] response status=200 content_type='text/csv' url=.../tempfile/sessions/REDACTED?key=REDACTED&keepfile=yes&attachment=yes <-- tempfile response
FAILURE: Could not discover an active VizQL session from the dashboard or View Data pages (checked Performance Resource Timing entries and cookies).
```

The real export -- the exact HAR-documented `export-view-data-summary-to-csv-server`
command **and** its `tempfile` GET -- fired and succeeded **on their
own**, entirely from the popup's own internal JS, with no click, no
VizQL session discovery, and no command issued by this project at all.
Meanwhile the `_discover_active_vizql_session()` machinery this project
had built to *reconstruct* that exact flow failed and aborted the run,
throwing the already-successful, already-observed CSV data away.

This retroactively explains every confusing finding from the DOM
investigation: the popup's body is empty and it's titled "Hybrid Popup
Window" because it is a background worker with no human-facing UI at
all -- its entire job is to run this export automatically the moment it
opens, not to host a dialog. There was never a button to find because
there was never anything to click.

The fix removed the entire reconstruction machinery --
`_ViewDataSessionCapture`, `_find_vizql_session_in_resource_urls()`,
`_find_xsrf_token_in_cookies()`, `_discover_active_vizql_session()`,
`_safe_response_text_snippet()`, `_fetch_view_data_dialog_tab_pres_model()`,
`_export_view_data_via_vizql_commands()`, and `_find_key_recursive()` --
and replaced it with something much smaller that captures the
already-succeeding response instead of rebuilding it:

- `_capture_tempfile_response()` -- a passive handler, structurally
  identical to the existing `_log_export_response()` diagnostic logger,
  that snapshots the first observed `.../tempfile/...` response
  (`status`/`headers`/`body()`, via a small `_CapturedTempfile` value
  object) into a `_TempfileCapture` holder. Never logs the body.
- `_attach_export_network_diagnostics()` now takes an optional
  `tempfile_capture` parameter and registers this handler alongside its
  existing (unchanged) loggers -- the same attachment point that already
  proved, live, that it reliably observes this traffic.
- `_wait_for_tempfile_capture()` polls (no fixed sleep, reusing the
  existing `NETWORK_EXPORT_TIMEOUT_MS`) until the capture is populated
  or raises a clear `BrowserAutomationError` on timeout.
- `_export_crosstab_csv()` attaches the capture before the "Data" click
  (so nothing the popup does afterward can be missed), and after the
  popup opens simply *waits* -- no click inside the popup, no command
  issued, no session to discover. `_save_tempfile_response()`
  (unchanged) validates and saves the captured body.

Login, dashboard navigation, the Download-menu and "Data" clicks, popup
detection, and the existing DOM diagnostic are all unchanged. No fixed
sleeps, no bootstrap listeners, no popup DOM scraping, no timeout
increases.

Regression tests (`tests/test_browser_crosstab.py`) were rewritten to
match: `_capture_tempfile_response()` (stores the first matching
response, ignores the export command's own response, keeps only the
first match, never raises on a broken response object);
`_wait_for_tempfile_capture()` (returns immediately when already
captured, raises on timeout); `_attach_export_network_diagnostics()`
(registers the logger pair with or without a capture, and -- exercising
the exact real mechanism, not just asserting registration -- firing a
real "response" event through the registered handler and confirming the
capture is populated); and a full `_export_crosstab_csv()` end-to-end
test that simulates the popup firing the real tempfile response event
mid-poll (via a `wait_for_timeout` side effect, the same mechanism a
real Playwright wait dispatches queued events through), plus a
never-observed-timeout case and a dedicated secrets-never-printed check.
**This is the version that produced the real successful `[NETDEBUG]`
trace above** -- but that trace came from the *previous* (now-removed)
implementation reaching the export step by coincidence, not from this
fix; the fix itself has not yet been run against the real server. The
next run is what will confirm the capture mechanism saves the file
end-to-end.

### The Full Data export needs real UI clicks -- wrong endpoint, false "automatic" premise

**A user then ran the tempfile-capture version against the real server,
requested `python browser_crosstab.py` end-to-end, and reported a run
that succeeded on its own terms** (login, dashboard, popup, and a real
`export-view-data-summary-to-csv-server` + `tempfile` pair observed and
saved) **followed by a user-supplied HAR file, captured from the exact
manual "Full Data" workflow** (Open dashboard -> Download -> Data -> wait
for the popup -> click **Full Data** -> click **Download** -> CSV
produced), with explicit instructions not to assume
`export-view-data-summary-to-csv-server` was the right target and to
analyze the HAR as source of truth before changing anything.

**The HAR analysis was conclusive and overturned two assumptions this
project had been building on:**

1. **Wrong endpoint.** `export-view-data-summary-to-csv-server` never
   appears anywhere in the Full Data HAR. The command that actually
   fires is `export-view-data-underlying-to-csv-server` -- a distinct
   VizQL command, not a variant of the same one. The "automatic" export
   observed in the previous round (see "The popup already does the whole
   export automatically" above) was real, but it was the popup's
   **default Summary tab** firing on its own -- not a Full Data export at
   all. This project had been correctly capturing the wrong file.
2. **False "no click needed" premise.** The HAR shows exactly one export
   in the whole capture, and it only happens after two explicit UI
   actions inside the popup: clicking the **Full Data** tab (the popup
   opens on Summary by default), then clicking **Download**. It is not
   automatic for Full Data -- only Summary fires without user
   interaction. The earlier "0 visible buttons" DOM diagnostic finding
   was therefore very likely a timing artifact (checked immediately after
   `domcontentloaded`, before the popup's SPA UI had hydrated) -- the
   same root-cause shape as the earlier login-form timing bug -- not
   proof the popup has no clickable DOM.

**Fix, per explicit instruction to make the minimum correct change and
not redesign the architecture:**

- `EXPORT_COMMAND_URL_SUBSTRING` changed to
  `"export-view-data-underlying-to-csv-server"`. `_is_export_related_url()`
  no longer treats the Summary command as the target export.
- New locator lists `FULL_DATA_TAB_CANDIDATES` and
  `VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES` -- best-effort,
  accessible-role-first (tab/button/menuitem name "Full Data"; button
  named "Download"), consistent with every other selector list in this
  module, not yet confirmed against the live popup's actual markup (no
  DOM inspection of it was ever possible from this HAR, which is
  network-only).
- New `_switch_to_full_data_and_download(view_data_page, export_dir)`
  drives the two real clicks: polls (via the existing
  `_find_first_with_wait()`, reusing `VIEW_DATA_POPUP_TIMEOUT_MS`, no
  fixed sleep) for the Full Data tab, clicks it, then polls for the
  Download button and clicks it. If the Full Data tab can't be found, it
  raises `BrowserAutomationError` immediately and deliberately does
  **not** fall back to clicking Download -- doing so would silently
  export Summary instead of failing loudly, which is exactly the bug
  this fix exists to prevent.
- `_export_crosstab_csv()` now calls this after the existing
  `_debug_dump_view_data_popup()` diagnostic (kept, unchanged) and before
  the existing, unchanged `_wait_for_tempfile_capture()` /
  `_save_tempfile_response()` pair -- the capture mechanism itself needed
  no changes, since it was already generic (it captures whatever tempfile
  response appears, regardless of which command produced it); only *what
  triggers* that response was wrong.

Login, dashboard navigation, the Download-menu and "Data" clicks, popup
detection, the tempfile-capture mechanism, and the kept DOM diagnostic
are all unchanged.

Regression tests (`tests/test_browser_crosstab.py`) were updated to
match: the `ViewDataPopupPage` fake gained the same scripted-locator
support `ScriptedPage` already had (`script=[...]`, `get_by_role()`/
`get_by_text()`/`locator()` returning pre-scripted `FakeLocator`s in
order) so tests can control exactly which candidate matches for each
click; `test_is_export_related_url_matches_command_and_tempfile_only`
now asserts the new endpoint substring matches and explicitly asserts
the old Summary endpoint does **not**; the three `_export_crosstab_csv()`
end-to-end tests were updated to script both popup clicks before the
tempfile wait; and new focused tests were added directly against
`_switch_to_full_data_and_download()`: a `DelayedViewDataPopupPage` fake
(mirroring the existing `DelayedFieldPage` login-timing regression test)
proving the Full Data tab is found even when it only appears after a
couple of poll ticks; the normal find-both-and-click-both path; a case
proving Download is **never** clicked when the Full Data tab can't be
found (the explicit anti-Summary-fallback guarantee); and a case where
Full Data is found but Download is missing. Full suite: 133 passed.
**Not yet run against the real server** -- the next run is what will
show whether these locator candidates actually match the live popup's
Full Data tab and Download button, and whether the resulting CSV is
genuinely the Full Data export (larger than the previously-captured
Summary file, matching the HAR's Full Data filename/content
characteristics).

### The popup's DOM never hydrates in automation -- root cause still unconfirmed, high-signal diagnostics added

**A user then ran the click-driven Full Data version against the real
server.** Login, dashboard load, and the Download -> Data popup all
worked, but the popup's own DOM stayed completely empty even after this
project's polling wait (0 buttons, 0 "Download"-text matches, ~931 bytes
of HTML, empty innerText -- the same shape as the very first "0 buttons"
finding from an earlier round, but this time confirmed NOT to be a
one-shot timing artifact, since `_switch_to_full_data_and_download()`
polls for up to `VIEW_DATA_POPUP_TIMEOUT_MS` before giving up), and the
run ultimately failed with "Never observed the View Data tempfile
response." The user's explicit instruction was to stop guessing more
locators (an empty DOM has no candidate to find, so a wrong locator
isn't the failure mode here) and instead investigate why the popup never
hydrates for automation the way it does for a real human click covering:
window.open parameters, postMessage/opener/window.name, console/page
errors, the popup's own network requests, and whether automation opens
the popup differently than a human click does.

**Diagnostics added (all TEMPORARY DEBUG ONLY, independently
try/excepted, never touching credentials/cookies/tokens or message
payload contents):**

- `_attach_popup_runtime_diagnostics(view_data_page)` -- attaches
  page-scoped (not the shared export/tempfile-filtered logger already in
  place) `console`, `pageerror`, `request`, and `response` listeners
  directly on the popup, reporting every console message, uncaught JS
  error, and every request/response it personally fires, unfiltered --
  answering directly whether the popup's own JS runs and does anything
  network-wise at all.
- `_attach_message_listener(target_page)` + `_MESSAGE_LISTENER_SCRIPT`
  (idempotent -- safe to inject on both the main page and the popup) --
  injects a `window.addEventListener('message', ...)` handler that
  records only a message's sender host and a coarse shape (`typeof`, or
  for objects, its top-level key names) into `window.__exportbotMessages`
  -- never the actual payload, which could carry session-scoped state.
  `_read_hydration_signals()` / `_debug_dump_hydration_signals()` read
  and print that array back, plus `window.opener` presence and
  `window.name`'s LENGTH only (never the raw value, in case it embeds an
  opaque identifier) -- together answering whether a postMessage
  handshake with the opener is the popup's actual hydration mechanism,
  and whether the popup even has a live `window.opener` reference to use
  for one (a real category of automation-vs-human divergence: browser
  popup/COOP/site-isolation behavior can null out `window.opener` in
  ways that differ between a human-driven and an automation-driven
  popup).
- `_describe_locator(locator)` -- prints the actual matched element's
  tag/role/aria-label/short text right before
  `_switch_to_full_data_and_download()` clicks it, so a real click is
  distinguishable from an accidental false-positive match, if one ever
  occurs.
- Both the main dashboard page and the popup get the message listener
  and a hydration-signal dump (before and after the two clicks), and the
  popup additionally gets the console/pageerror/own-network listeners,
  attached as early as this project can reach them (right after the
  popup is captured via `expect_popup()`, before even
  `wait_for_load_state("domcontentloaded")` for the runtime listeners).

**A cross-check of both real HAR captures this project already has**
(the original Crosstab/Summary HAR and the newer Full Data one) surfaced
something worth recording even though it wasn't acted on: in both, every
single VizQL "View Data" dialog command -- `launch-hybrid-view-data-dialog`,
both `get-view-data-dialog-tab-pres-model` calls, and the export command
itself -- is an XHR/fetch tied to the exact same long-lived VizQL session
id already used for ordinary dashboard interactions (e.g.
`render-tooltip-server`), and neither HAR contains a `document`-typed
navigation entry for a second window at all -- no `hybrid-window/...`
page load, no second `pageref`. The Full Data HAR additionally revealed
an async job pattern this project hadn't seen before: after the export
command, there's a `progress/sessions/{id}` polling loop (many GETs)
before the final `tempfile` response -- unlike the Summary flow, which
goes straight from the export command to the tempfile response. This
doesn't change `_wait_for_tempfile_capture()` at all (it already waits
for any matching tempfile response, regardless of what happens in
between), but it is worth knowing the Full Data export can legitimately
take longer server-side than Summary did.

This HAR evidence is **not conclusive** -- both files also carry no
`log.pages` array and no `pageref` on any entry at all, which is
consistent with how they were captured/reduced (DevTools attached only
to the main tab, or a reduction step that stripped page-context
metadata) rather than proof that no second window exists. It was
deliberately **not acted on** by changing where
`_switch_to_full_data_and_download()` looks for its controls -- doing so
without live confirmation would be exactly the kind of guess this round
was told to avoid. It's recorded here as the next concrete thing to
check if the diagnostics above show the popup's own JS truly never runs
or never receives anything: the real "Full Data"/"Download" controls a
human clicks may render as an overlay on the MAIN dashboard page rather
than inside this "Hybrid Popup Window."

Regression tests (`tests/test_browser_crosstab.py`) cover the new
functions: `_attach_message_listener()` (never raises when `evaluate()`
fails), `_read_hydration_signals()` (returns the evaluated result,
`{"error": ...}` on failure, never raises), `_debug_dump_hydration_signals()`
(prints observed message count/sender host without the payload, and the
read-failure case), `_attach_popup_runtime_diagnostics()` (registers all
four listeners, never raises when `.on()` fails, and a dedicated
secrets-never-printed check firing a real console/request event through
the registered handlers), and `_describe_locator()` (returns the
evaluated description, `"<description failed: ...>"` on failure). Full
suite: 143 passed. **Not yet run against the real server** -- this is,
again, the very diagnostic meant to produce that data; once its
[POPUPDEBUG] output (console/pageerror/own-network activity, postMessage
count and sender hosts, opener presence, window.name length, and which
element `_switch_to_full_data_and_download()` actually matched and
clicked) is shared back, the actual root cause -- and a real, evidence-
based fix -- can finally be identified instead of guessed at.

### Stale local checkout, then the real Download locator gap

**A user then ran `browser_crosstab.py` and got output with zero
`[POPUPDEBUG]` lines** -- just the old `[DEBUG]` popup dump followed by
`"Exporting Crosstab..."` and a bare tempfile timeout. That exact string
does not exist anywhere in this codebase past commit `5981b6c` (it was
removed when `_switch_to_full_data_and_download()` was added), which
proved the run was against a stale local checkout at least 3 commits
behind `origin` -- not a defect in the diagnostics. Confirmed via `git
log -S "Exporting Crosstab"` showing the string was only ever present
between the very first commit and `5981b6c`. Fixed by syncing (`git
fetch` + `git pull`) rather than any code change.

**On the correct code, the diagnostics immediately answered the
hydration question posed in the previous round, decisively: the popup
DOES hydrate.** The real run showed: `window.opener present = True`,
`window.name length = 50`, one `postMessage` received from
`insight.erajaya.com` (shape `object(keys=windowId,messageType,data)`),
the popup's own JS bundle requests (`hybrid-web-window.js`,
`hybrid-window/min/0.js`/`1.js`, locale files) firing and succeeding,
and a console log reading `"Browser based plugin load finished elapsed
time: 1099ms"`. There was never a hydration bug -- the empty-DOM
readings from the two prior rounds were from the stale checkout too (no
`_switch_to_full_data_and_download()` at all in that code, so nothing
had ever actually waited for or clicked anything).

`_switch_to_full_data_and_download()` then found and clicked "Full
Data" successfully. `_describe_locator()`'s output explained exactly why
only one candidate in `FULL_DATA_TAB_CANDIDATES` could ever have worked:
the matched element is `{"tag":"SPAN","role":null,"ariaLabel":null,"text":"Full
Data"}` -- a plain span with no ARIA role or accessible name, matched
only by `get_by_text()`. The three role-based candidates (`tab`,
`button`, `menuitem`) could never match anything in this popup, since
Playwright's `get_by_role()` requires an actual (or implicit) ARIA role
to exist. The run then failed at the very next step: `Could not find the
"Download" button in the View Data window after switching to Full
Data.` -- because `VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES` had no
`get_by_text()` fallback at all, only role/CSS-attribute candidates that
this popup's markup apparently never satisfies.

**Fix:** both candidate lists now try `get_by_text()` first (confirmed
to work for "Full Data"; "Download" is presumed built with the same UI
toolkit, since it's the same popup). `VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES`
also gained two additional narrow fallbacks -- a `get_by_role("button",
name=~"download")` substring match and a `get_by_title(~"download")`
match -- in case the real control turns out to be an icon-only button
whose accessible name isn't an exact "Download" text match; a fully
unanchored `get_by_text("download")` was deliberately NOT added, since
without ARIA scoping that's the kind of broad match that can grab an
unrelated container element and click the wrong thing. `_switch_to_full_data_and_download()`
also now calls the existing `_debug_dump_view_data_popup()` a second
time, right after the "Full Data" click and before searching for
Download -- reusing already-tested instrumentation rather than adding
new instrumentation, so that if the widened Download candidates still
don't match on the next run, the exact post-click DOM state (button
list, download-text matches, HTML preview) is already captured instead
of requiring another guess-and-rerun cycle.

Regression tests: `DelayedViewDataPopupPage` gained `.url`/`.title()` so
the new mid-function `_debug_dump_view_data_popup()` call doesn't crash
in that timing-regression test; all other existing tests needed no
changes, since the fakes route every `get_by_*`/`locator()` call through
the same scripted-locator mechanism regardless of which specific method
is called. Full suite: 143 passed. **Not yet run against the real
server** -- the next run is what will show whether the widened Download
candidates actually match, and if not, the reused DOM dump's output is
the next concrete evidence to act on.

### The real bug: `Response.body()` inside a Playwright sync event handler

**The next real-server run drove the whole flow correctly for the first
time** -- login, dashboard, popup, Full Data click, Download click --
and the export genuinely succeeded on Tableau's side:

```
[POPUPDEBUG] Full Data candidate matched: {"tag":"SPAN","role":null,"ariaLabel":null,"text":"Full Data"}
[DEBUG] popup.title() = View Data: Persentase Nasional      <-- popup fully hydrated
[POPUPDEBUG] Download candidate matched: {"tag":"SPAN","ariaLabel":"Download in CSV format","text":"Download"}
[NETDEBUG] request  method=POST url=.../commands/tabsrv/export-view-data-underlying-to-csv-server
[NETDEBUG] response status=200 ... export-view-data-underlying-to-csv-server
[NETDEBUG] request  method=GET url=.../tempfile/sessions/REDACTED?key=REDACTED&keepfile=yes&attachment=yes
[NETDEBUG] response status=200 content_type='text/csv' ... <-- tempfile response
FAILURE: Never observed the View Data tempfile response
```

The run failed claiming it never saw the tempfile response **on the line
directly below the log proving it had**. Root cause, found by reading
`_capture_tempfile_response()`: it built an eager status/headers/body
snapshot (`_CapturedTempfile`) *inside the network event handler*, and
`Response.body()` requires a round-trip to the browser -- which
Playwright's **sync** API does not permit from inside an event handler.
It raised every time, and the handler's bare `except Exception: pass`
swallowed it, so the capture silently stayed empty while its sibling
logger (which only touches cached attributes) printed the response
happily. Two bugs in one: the illegal call, and the silent except that
hid it for three rounds.

**Fix:**
- `_capture_tempfile_response()` now reads nothing but the response's
  cached `url` and stores the live `Response` object itself. The body is
  read afterwards, on the main flow, by `_save_tempfile_response()` --
  which works unchanged, since a Playwright `Response` exposes the same
  `status`/`headers`/`body()` surface the old snapshot class did.
  `_CapturedTempfile` was deleted as dead code.
- That handler no longer fails silently: it prints on capture, and
  prints the exception if capture ever fails.
- `_save_tempfile_response()` wraps its `body()` read so a genuine read
  failure surfaces as a clear `BrowserAutomationError` rather than a raw
  traceback.
- `_export_crosstab_csv()` prints the saved path and byte size.

**Also fixed, from the same run's DOM dump** (the first one ever taken
*after* the Full Data click, which showed the popup fully rendered:
`body_inner_html_length` 10297, title "View Data: Persentase Nasional",
7 real buttons): the dump caught the grid mid-load -- `"0 rows  0
fields"` with the Download button carrying
`title="No column data available to download"` -- meaning Download was
being clicked before the underlying rows had arrived, risking an empty
export. `_wait_for_download_button_ready()` now polls that same button's
live `title` until the "no column data available" wording clears, then
clicks. Deliberately best-effort: if the title can't be read, or never
clears (a view that genuinely has no rows), it proceeds anyway rather
than failing the run on a heuristic.

The dump also gave the real markup for both controls, so both candidate
lists now lead with confirmed selectors --
`button[aria-label="Full Data Table"]` and
`[data-tb-test-id="download-data-Button"]` -- ahead of the text matches
that were working. Targeting the button rather than the inner label span
is also what makes the readiness `title` readable.

Regression tests: `_capture_tempfile_response()` now has a test pinning
the exact bug shape -- a response whose `status`/`headers`/`body()` all
raise (what the sync-API restriction looks like from inside a handler)
must **still** be captured -- plus the unreadable-URL case;
`_save_tempfile_response()` has a body-read-failure test; and
`_wait_for_download_button_ready()` is covered for the polls-until-ready,
unreadable-title, and never-clears paths. Full suite: 148 passed.

### The saved file wasn't CSV: Chrome's own download handling beats `Response.body()`

**The next real-server run drove the whole flow through a real,
confirmed-successful export** -- `export-view-data-underlying-to-csv-server`
then the tempfile GET, both HTTP 200, exactly as before -- but the
resulting file wasn't usable. A screenshot showed the real "View Data"
window with Full Data genuinely loaded (200 rows, 27 fields) and,
overlaid on it, Chrome's own "Recent download history" panel reporting a
completed **72.5 MB** download named only with a bare GUID -- no `.csv`,
no descriptive name at all.

That panel is Chrome's native download manager, entirely independent of
anything this project's Python code does with the network response --
its presence means Chrome's own network stack saw the tempfile
response's `Content-Disposition: attachment` header and diverted the
body to its downloads UI on its own. This is a real, known Playwright
limitation: once Chrome claims a response as a download, `Response.body()`
(the DevTools Protocol call `_save_tempfile_response()` depends on) is
not reliably able to read it any more -- so the earlier fix (moving the
`.body()` call out of the event handler, per the previous round) was
necessary but not sufficient. Calling it later, on the main flow, can
still return the wrong thing once Chrome has taken the response for its
own downloads UI -- consistent with a 72.5 MB file with no name showing
up instead of a properly named CSV.

**Fix:** capture the actual Playwright `Download` object instead of
relying on the response body at all, mirroring the existing
`_TempfileCapture` pattern:

- `_DownloadCapture` / `_capture_download()` -- passively records the
  first "download" event observed, storing the live `Download` object
  (never reading anything from it inside the handler, for the same
  sync-API reason established in the previous round).
- `_attach_download_capture(page, capture)` -- attaches at the
  **browser context** level (`page.context.on("download", ...)`), not a
  specific page, since the download could be triggered by JS running in
  either the main dashboard or the "View Data" popup, and attached as
  early as possible (right where the tempfile capture is set up, before
  even the "Data" click) so it can't be missed regardless of timing.
- `_wait_for_download_capture()` -- a short, deliberately non-raising
  grace period (`DOWNLOAD_EVENT_GRACE_PERIOD_MS`, 5s) after the tempfile
  response is already confirmed, since the download event can lag
  slightly behind while Chrome finishes writing to disk. If it never
  fires, nothing breaks -- `_save_tempfile_response()` just falls back
  to the old `response.body()` path.
- `_save_tempfile_response()` now takes an optional `download` parameter.
  When present, its bytes -- read from `download.path()`, the local file
  Chrome already finished writing -- are the source of truth, not
  `response.body()`. Its `suggested_filename` is used only as a
  fallback name when Content-Disposition didn't supply one, and only if
  it has a recognizable `.csv`/`.txt` extension -- a bare GUID (exactly
  what the real run showed) is treated as no name at all, so the fix
  doesn't just relabel the same "downloaded something, not a csv file"
  symptom under a different code path. An empty download is now also
  explicitly rejected rather than silently written as a 0-byte file.

Regression tests (`tests/test_browser_crosstab.py`) cover: `_capture_download()`
(keeps only the first event); `_attach_download_capture()` (registers at
the context level via `page.context`, and never raises when `.context`
itself is missing -- covering a test fake the same way a real attribute
error would be); `_wait_for_download_capture()` (returns immediately
when already captured, gives up without raising on timeout);
`_save_tempfile_response()` extended with the download-preferring path
(bytes come from the download even when the response body would give a
different, wrong answer; a GUID `suggested_filename` is rejected in
favor of a generated name; a real `.csv`-suffixed suggested name IS
used; an empty download is rejected; a `download.path()` failure
surfaces a clear error); and a new end-to-end
`_export_crosstab_csv()` test that fires both the tempfile response and
a real "download" event (mirroring exactly what the live run showed)
and confirms the saved file's bytes and name come from the download, not
the response body or its meaningless suggested name. Full suite: 159
passed. **Not yet run against the real server** -- the next run is what
will show whether the saved file is finally a real, correctly-named CSV
with the full Full Data row count.

### Success -- end-to-end export confirmed, one cosmetic filename bug fixed

**A user ran `browser_crosstab.py` and it worked end to end**: login,
dashboard, popup, Full Data click, Download click, the real
`export-view-data-underlying-to-csv-server` + tempfile pair (both HTTP
200), a `[NETDEBUG] captured a browser download event`, and finally:

```
Saved exports\Persentase%20Nasional_Full%20Data_data.csv (76070210 bytes)
SUCCESS: exports\Persentase%20Nasional_Full%20Data_data.csv
```

76 MB -- consistent with the earlier 72.5 MB seen in Chrome's own
download history for the same export, confirming this is genuinely the
full underlying dataset, not a truncated or wrong file. **This is the
first fully successful real-server run of the whole pipeline.**

The one visible issue was cosmetic: the saved filename contained literal
`%20` instead of spaces. Root cause: `_extract_filename_from_content_disposition()`'s
RFC 5987 `filename*=UTF-8''...` branch (the form Tableau's own tempfile
response uses) is percent-encoded by spec, but the code was never
calling `unquote()` on it -- it had been taking the encoded value
verbatim since this function was first written, and the existing test
`test_extract_filename_from_content_disposition_rfc5987` had pinned that
buggy behavior as correct rather than catching it (asserting the `%20`
in its expected output). Fixed by decoding with `urllib.parse.unquote()`
after stripping the `UTF-8''` charset/language prefix; the test now
asserts the decoded value (`"Persentase Nasional_data.csv"`). Full
suite: 159 passed.

**Next**: confirm the corrected code produces a real-spaces filename on
the next run, and spot-check the CSV's row count against what the "View
Data" popup showed ("200 rows" was only the on-screen preview cap, not
necessarily the full underlying row count).

## What is tested vs. not

Tested in this environment (`pytest`, no network):
- `config.py` — required/optional env vars, missing/blank value errors.
- `serverinfo.py` — `discover_api_version()` response parsing, using a
  mocked HTTP response shaped like Tableau's documented `serverinfo` JSON
  (not a claim that it matches the live server byte-for-byte — only that
  our parsing handles that documented shape, and rejects bad status codes
  and malformed bodies).
- `validation.py` — empty/all-null/valid DataFrame cases.
- `exporter.py` — folder/filename format, round-trip read of written xlsx.
- `pipeline.py` — wiring, using `FakeAuthenticator`/`FakeTableauClient`
  test doubles defined in `tests/test_pipeline.py`. These prove the
  pipeline calls its collaborators correctly; they do **not** simulate or
  validate real Tableau API behavior.

- `view_discovery.py` — `discover_view()`'s handling of Tableau's
  documented Views/Workbook JSON shapes, using mocked HTTP responses (not
  a claim that it matches the live server byte-for-byte).
- `discover_view.py` (CLI) — success/failure reporting, using mocked
  auth + view-lookup responses; confirms the auth token and credentials
  never appear in its output.
- `extraction_probe.py` — `probe_view_data()`'s handling of CSV and
  non-CSV response bodies, non-200 statuses, and network errors, using
  mocked HTTP responses; confirms row values are never surfaced.
- `extract_smoke_test.py` (CLI) — success/failure reporting, using mocked
  auth + view-data responses; confirms row values, the auth token, and
  credentials never appear in its output.
- `extraction_diagnostics.py` — `probe_endpoint()`/`probe_with_accept_header()`'s
  handling of a 406 response, a 200 response, an unsafe header
  (`Set-Cookie`) being excluded, a timeout (distinguished from a generic
  network error), and the crosstab-Excel path, using mocked HTTP
  responses.
- `diagnose_extraction.py` (CLI) — reports one sanitized line per
  endpoint/Accept combination tried, using a mocked server that
  reproduces the exact reported live pattern (fast 406 on `Accept:
  text/csv`, timeout on every other variant) across both the "Query View
  Data" and "Query View Crosstab Excel" endpoints; confirms
  credentials/token/row values never appear in its output.
- `crosstab_probe.py` (module) — `probe_crosstab_excel()`'s handling of a
  successful response with a `Content-Disposition` filename (both the
  plain and RFC 5987 `filename*=` forms), a 403, a timeout, and a network
  error, using mocked HTTP responses; confirms the response body's
  contents are never surfaced, only metadata about it.
- `crosstab_probe.py` (CLI) — success/failure reporting, using mocked
  auth + crosstab responses; confirms credentials, the auth token, and
  file contents never appear in its output.
- `vizql_crosstab_probe.py` (module) — `check_session()`,
  `export_view_data_summary_to_csv()` (field names sent, result key/
  filename/mimeType extraction from the real captured JSON response
  shape, malformed-response handling, network errors), and
  `fetch_tempfile()` (metadata extraction, network errors), using mocked
  HTTP responses; confirms the session id and export result key are
  never surfaced in any returned object.
- `vizql_crosstab_probe.py` (CLI) — reports "session established: no"
  and makes **zero** network calls when the required ephemeral context
  isn't supplied; full success/failure reporting otherwise, using mocked
  responses shaped like the real captured JSON; confirms the session id,
  export result key, and row values never appear in its output.
- `browser_crosstab.py` (module) — `_find_first()`'s fallback matching,
  `_login_if_needed()` (skip/fill/submit/error paths, confirms
  credentials never printed, and the slow-rendering-form regression case),
  `_wait_for_dashboard()` (success + timeout), `_switch_to_full_data_and_download()`
  (finds and clicks the Full Data tab then the Download button, waits for
  a slow-hydrating popup UI via a `DelayedViewDataPopupPage` fake
  mirroring the login-timing regression test, raises without ever
  clicking Download when Full Data can't be found -- the anti-Summary-
  fallback guarantee -- and raises when Download is missing after
  switching), `_export_crosstab_csv()` (Download -> Data click sequence,
  catching the "View Data" popup via a fake `expect_popup()`, the
  popup-never-opens error path, debug-screenshot-on-failure, the full
  flow through both the Full Data/Download popup clicks and on to a saved
  CSV by simulating the popup firing a real tempfile network event
  mid-poll, a never-observed timeout case, and a dedicated
  secrets-never-printed check), `_capture_tempfile_response()` (stores the first matching
  tempfile response as a `_CapturedTempfile` snapshot, ignores the
  export command's own response, keeps only the first match, never
  raises on a broken response object), `_wait_for_tempfile_capture()`
  (returns immediately when already captured, raises on timeout),
  `_attach_export_network_diagnostics()` (registers the logger pair with
  or without a `_TempfileCapture`, and -- firing a real event through
  the registered handler, not just asserting registration -- confirms
  the capture is actually populated), `_is_export_related_url()`
  (matches only the two HAR-confirmed URL
  fragments), `_extract_filename_from_content_disposition()` (plain and
  RFC 5987 forms, absent header), `_sanitize_filename()` (strips path
  components and hostile characters, returns `None` for unusable input),
  `_save_tempfile_response()` (Content-Disposition-derived filename,
  fallback to a generated name, rejects a non-CSV/text Content-Type,
  rejects an HTTP error status), `_attach_export_network_diagnostics()`
  (registers both listeners, never raises when `.on()` fails),
  `_redact_url()` (sensitive query-parameter values redacted,
  non-sensitive ones preserved, never raises on unparseable input),
  `_inspect_frame()` (returns a frame's `evaluate()` result normally, or
  `{"error": ...}` without raising when `evaluate()` throws),
  `_debug_dump_view_data_popup()` (temporary debug-only instrumentation,
  kept unchanged: prints URL/title, per-frame DOM diagnostics -- buttons,
  Download-text matches, iframes, shadow roots, canvases -- and other
  browser-context pages, with URLs redacted, saves a screenshot, all
  independently try/excepted so any single failure here is reported but
  never blocks the real export), `_attach_message_listener()` /
  `_read_hydration_signals()` / `_debug_dump_hydration_signals()`
  (temporary popup/main-page hydration diagnostics: never raise when
  `evaluate()` fails, and report observed postMessage count/sender host
  without ever printing a message's payload), `_attach_popup_runtime_diagnostics()`
  (temporary popup-scoped console/pageerror/own-request/own-response
  diagnostics: registers all four listeners, never raises when `.on()`
  fails, and a dedicated secrets-never-printed check), `_describe_locator()`
  (temporary: returns a matched element's safe description, or a
  failure string, never raises), `_wait_for_download_button_ready()`
  (polls a button's `title` until "no column data available" clears,
  proceeds anyway when unreadable or never clearing), `_capture_download()`
  / `_attach_download_capture()` / `_wait_for_download_capture()` (keeps
  only the first observed download event, registers at the browser
  context level and never raises when `.context` is missing, gives up
  without raising on timeout), `_save_tempfile_response()`'s
  download-preferring path (bytes come from a captured `Download`'s
  `download.path()` rather than `response.body()` when one was
  captured, a GUID-shaped `suggested_filename` is rejected in favor of a
  generated name, a real `.csv`-suffixed suggested name is used, an
  empty download is rejected, a `download.path()` failure surfaces a
  clear error), and `run_browser_crosstab_export()`'s orchestration
  (goto order, browser launch args, browser always closed), all using
  hand-built fake Playwright-like objects -- no real browser, no real
  Tableau DOM.
- `browser_crosstab.py` (CLI) — config-error/success/failure reporting,
  mocking the export function entirely; confirms credentials never
  appear in output.

Not tested, requires a local machine with a real browser and access to
`insight.erajaya.com` (impossible from this sandbox — see "Browser
automation" above for exactly why):
- Whether the DOM selectors in `browser_crosstab.py` actually match the
  live dashboard's real markup — entirely unverified; they're
  best-effort accessible-role/name guesses, not confirmed by inspection.
- Whether the login form is detected/filled correctly on the real page.
- Whether `FULL_DATA_TAB_CANDIDATES` / `VIEW_DATA_DOWNLOAD_BUTTON_CANDIDATES`
  (see "The Full Data export needs real UI clicks" above) actually match
  the live popup's real "Full Data" tab and "Download" button -- entirely
  unverified; they're best-effort accessible-role/name guesses built from
  a HAR (network-only, no DOM) rather than confirmed by inspection.
- Whether the tempfile-capture approach actually saves the file end to
  end against the real server once triggered by the new Full Data ->
  Download click sequence, and whether the resulting CSV is genuinely the
  Full Data export (substantially larger than, and structurally
  different from, the previously-captured Summary file).
- Whether the popup's DOM ever hydrates at all in automation, and if not,
  why: this is exactly what the new [POPUPDEBUG] diagnostics (see "The
  popup's DOM never hydrates in automation" above) exist to answer --
  console/page errors, the popup's own network activity, postMessage
  events it receives, `window.opener`/`window.name` state, and which
  element (if any) `_switch_to_full_data_and_download()` actually
  matches and clicks. None of this can be observed without a real
  browser against the live server.

Not tested, requires a machine with access to `insight.erajaya.com`:
- `discover_api_version()` against the real `serverinfo` endpoint (a user
  confirmed the endpoint itself works via manual `curl`, restApiVersion
  3.29 — but our function that calls and parses it has not been run
  against the live server yet).
- `probe_with_accept_header()` / `diagnose_extraction.py` against the real
  server — the 406/timeout investigation is entirely unverified live so
  far.
- `probe_endpoint()` against `/crosstab/excel` — untested live; unknown
  whether it shares the same failure mode as `/data`.
- `probe_view_data()` / `TableauServerClient.get_view_data()` against the
  real server for view `741ce0df-8818-4412-93ca-b34c03019219` — blocked
  on the 406/timeout above.
- `vizql_crosstab_probe.py` against the real server with a live,
  DevTools-captured session id — entirely unverified; we don't yet know
  if the export/tempfile commands succeed when reproduced via plain
  `requests` instead of a real browser (e.g. TLS fingerprinting, header
  ordering, or other browser-specific signals could still block it).
- End-to-end `python main.py` run with real credentials.

## Next steps

**Immediate priority: run `browser_crosstab.py` locally tonight** (see
README.md "Browser automation setup"). It has not been run against the
real server from anywhere in this process — this is the first real test
of it. If a selector doesn't match, the error names which step failed
and a screenshot is saved to `<EXPORT_DIR>/debug_<step>.png`; adjust the
candidate locator lists in `src/exportbot/browser_crosstab.py` and rerun.

The REST/VizQL investigation below is paused, not abandoned — resume it
if/when there's time to pursue a lighter-weight (non-browser) extraction
method.

1. ~~Confirm the REST API is enabled and get the real API version~~ — done:
   `GET /api/3.22/serverinfo` → `productVersion 2026.2.0`,
   `restApiVersion 3.29`. The API version is no longer hardcoded as the
   only option: `main.py` now calls `discover_api_version()` at startup
   (or uses `TABLEAU_API_VERSION` from `.env` if set).
2. ~~Attempt `TableauServerAuthenticator.authenticate()` with real
   credentials~~ — done: `auth_smoke_test.py` run from a machine with real
   access reported `SUCCESS: authenticated (api_version=3.29)`. Native
   username/password sign-in works; no SSO/browser flow needed.
3. ~~Resolve the real GUID for `ReturnRotate`/`Dashboard1`~~ — done:
   `discover_view.py` resolved it to
   `741ce0df-8818-4412-93ca-b34c03019219` (workbook "Return & Rotate",
   view "Dashboard 1").
4. ~~Run `extract_smoke_test.py` against the real server~~ — done, but it
   returned HTTP 406, not data.
5. ~~Run `diagnose_extraction.py` (v1, `/data` only, 30s timeout)~~ —
   done: revealed the asymmetric 406-vs-timeout pattern above, ruling out
   a simple "drop the Accept header" fix. `diagnose_extraction.py` was
   extended (10s timeout + elapsed time reported per attempt, plus a
   `/crosstab/excel` probe alongside `/data`) — **not yet run against the
   real server.**
6. Run the extended `diagnose_extraction.py` from a machine with real
   access.
7. ~~The user confirmed visually that the dashboard's UI has a native
   "Download -> Crosstab" menu~~ — done. This does not by itself confirm
   which backend endpoint it calls; that requires the DevTools capture
   documented above.
8. Run `crosstab_probe.py` from a machine with real access, to test the
   documented REST `/crosstab/excel` endpoint.
9. ~~Capture the real browser "Download -> Crosstab" request~~ — done, via
   a HAR file. It's VizQL session commands
   (`/vizql/w/{workbook}/v/{view}/sessions/{id}/commands/...`),
   cookie/`X-XSRF-TOKEN`-authenticated, NOT the REST API. Full analysis
   above. `vizql_crosstab_probe.py` was built to test reproducing the
   export+tempfile tail of that flow given a live, DevTools-pasted
   session id.
10. Run `vizql_crosstab_probe.py` from a machine with real access,
    immediately after a fresh DevTools capture (the session id expires
    quickly). Depending on the result, decide the extraction method:
    - If it succeeds, that confirms the VizQL tail is reproducible via
      plain `requests` given a valid session -- but a full pipeline still
      needs a way to *obtain* that session (id + visualIdPresModel)
      without a human doing it via DevTools each time. That means either
      reproducing Tableau's cookie-based web login + page bootstrap (a
      new, non-trivial auth model -- needs an explicit decision before
      writing that code) or browser automation (Playwright) to drive the
      real login + click flow. Neither should be started without
      checking in first, per the priority order in the original
      investigation (REST > authenticated HTTP > Playwright > full
      browser automation).
    - If it fails even with a valid session (e.g. still times out, or
      403s), that's a strong signal of something more fundamental --
      TLS/header fingerprinting, IP/network-based gating, or a genuine
      permission gap on "Export Data"/"Download Crosstab" for this
      account on this workbook -- worth checking with a Tableau admin.
    - Separately, if `crosstab_probe.py` (step 8) or the extended
      `diagnose_extraction.py` (step 6) succeed via the plain REST API,
      that's a simpler method than VizQL reproduction and should be
      preferred -- update `tableau_client.py` to match, with a mocked
      regression test locking in the correction.
11. Run `extract_smoke_test.py` (or the equivalent for whichever endpoint
    was confirmed) again to see real data come back, and check the
    reported shape (row/column counts, column names) against what's
    expected.
12. If that works, run `TableauServerClient.get_view_data()` (the
    production path) the same way; adjust `validation.py` if the real
    schema needs specific column checks.
13. Only after that: run `python main.py` end-to-end.
