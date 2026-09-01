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
  credentials never printed), `_wait_for_dashboard()` (success + timeout),
  and `_export_crosstab_csv()` (full click sequence through to
  `download.save_as()` with the correct timestamped filename, the
  optional crosstab-tab click, and the debug-screenshot-on-failure path),
  and `run_browser_crosstab_export()`'s orchestration (goto order,
  browser launch args, browser always closed), all using hand-built fake
  Playwright-like objects -- no real browser, no real Tableau DOM.
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
- Whether Download -> Data -> Crosstab actually triggers a download the
  way `page.expect_download()` expects.

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
