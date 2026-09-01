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

Still unverified (needs a machine with real access, i.e. not this
sandbox):

- Which `Accept` header (if any) the real server actually accepts for
  `/data` or `/crosstab/excel` -- the extended `diagnose_extraction.py`
  has not been run there yet.
- Whether `/crosstab/excel` behaves any differently than `/data` for this
  specific dashboard view.
- Whether the "view data" endpoint (`extraction_probe.py` /
  `extract_smoke_test.py`, same endpoint as `tableau_client.py`) actually
  returns data for this view once/if the 406/timeout pattern is resolved.
- End-to-end `python main.py` run with real credentials and the real
  `TABLEAU_VIEW_ID`.

`tableau_client.py` is written to match Tableau's documented REST API,
but the data-extraction call itself is still explicitly unverified —
treat it as a starting point to debug from a machine with real network
access, not as working code.

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
- End-to-end `python main.py` run with real credentials.

## Next steps

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
   access. Depending on the result:
   - If any `/data` variant now returns 200 fast, that confirms (A) — a
     code fix to `tableau_client.py`'s Accept header is enough.
   - If `/crosstab/excel` succeeds where `/data` doesn't, that's the
     extraction method to adopt for this dashboard instead.
   - If every variant on both endpoints times out identically, that
     points to (B)/(C) — a permission or server/gateway-level issue
     unrelated to Accept headers, worth checking with a Tableau admin
     (does this account have "Export Data"/"Download Crosstab" on this
     specific workbook?) before considering anything more invasive.
7. Once a working endpoint+Accept combination (or confirmed permission
   fix) is found, update `tableau_client.py` to match, with a mocked
   regression test locking in the correction.
8. Run `extract_smoke_test.py` again to confirm real data comes back, and
   check the reported shape (row/column counts, column names) against
   what's expected.
9. If that works, run `TableauServerClient.get_view_data()` (the
   production path) the same way; adjust `validation.py` if the real
   schema needs specific column checks.
10. Only after that: run `python main.py` end-to-end.
