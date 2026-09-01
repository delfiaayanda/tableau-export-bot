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
URL — see `tests/test_view_id_flow.py`. A subsequent HTTP 406 is a
separate question, not yet investigated, since the URL had to be fixed
first.

Still unverified (needs a machine with real access, i.e. not this
sandbox):

- Whether the "view data" endpoint (`extraction_probe.py` /
  `extract_smoke_test.py`, same endpoint as `tableau_client.py`) actually
  returns data for this view, and in what shape (CSV, content type, row
  count). **Not yet run there — only mocked-response unit tests.**
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

Not tested, requires a machine with access to `insight.erajaya.com`:
- `discover_api_version()` against the real `serverinfo` endpoint (a user
  confirmed the endpoint itself works via manual `curl`, restApiVersion
  3.29 — but our function that calls and parses it has not been run
  against the live server yet).
- `probe_view_data()` / `TableauServerClient.get_view_data()` against the
  real server for view `741ce0df-8818-4412-93ca-b34c03019219` — not yet
  attempted.
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
4. Run `extract_smoke_test.py` from a machine with real access to confirm
   the "view data" endpoint actually returns data for this view, and
   check the reported shape (row/column counts, column names) against
   what's expected.
5. If that works, run `TableauServerClient.get_view_data()` (the
   production path) the same way; adjust `validation.py` if the real
   schema needs specific column checks.
6. Only after that: run `python main.py` end-to-end.
