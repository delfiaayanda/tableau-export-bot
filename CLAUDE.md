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

Still unverified (needs a machine with real access, i.e. not this
sandbox):

- Whether `TABLEAU_USERNAME`/`TABLEAU_PASSWORD` sign-in works, or whether
  the instance actually requires SSO/SAML (would need a different auth
  flow, likely browser-based, if so). **No sign-in attempt has been made
  yet — this is still unauthenticated.**
- Whether the "view data" endpoint returns the CSV shape assumed by
  `tableau_client.py`, or whether crosstab/export endpoints work better.
- The real `TABLEAU_VIEW_ID` for `ReturnRotate/Dashboard1` (the URL only
  gives the human-readable view path, not the GUID the API needs).

`auth.py` and `tableau_client.py` are written to match Tableau's
documented REST API, but the sign-in and data-extraction calls themselves
are still explicitly unverified — treat them as a starting point to debug
from a machine with real network access, not as working code.

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

Not tested, requires a machine with access to `insight.erajaya.com`:
- `discover_api_version()` against the real `serverinfo` endpoint (a user
  confirmed the endpoint itself works via manual `curl`, restApiVersion
  3.29 — but our function that calls and parses it has not been run
  against the live server yet).
- `TableauServerAuthenticator.authenticate()` against the real server —
  **no sign-in attempt has been made with real credentials.**
- `TableauServerClient.get_view_data()` against the real server.
- End-to-end `python main.py` run with real credentials.
- Discovering the real `TABLEAU_VIEW_ID` for `ReturnRotate/Dashboard1`.

## Next steps

1. ~~Confirm the REST API is enabled and get the real API version~~ — done:
   `GET /api/3.22/serverinfo` → `productVersion 2026.2.0`,
   `restApiVersion 3.29`. The API version is no longer hardcoded as the
   only option: `main.py` now calls `discover_api_version()` at startup
   (or uses `TABLEAU_API_VERSION` from `.env` if set).
2. From a machine with real access, run `discover_api_version()` itself
   (not just a manual curl) to confirm our parsing works against the live
   response.
3. Attempt `TableauServerAuthenticator.authenticate()` with real
   credentials; if it fails with a redirect to an SSO login page, the
   username/password flow won't work and a browser-based (Playwright)
   login capturing session cookies/tokens will be needed instead.
4. Find the view's GUID (Tableau REST `views` list endpoint, or browser
   dev tools) to set `TABLEAU_VIEW_ID`.
5. Run `TableauServerClient.get_view_data()` and check the returned CSV
   against what's expected; adjust `validation.py` if the real schema
   needs specific column checks.
6. Only after that: run `python main.py` end-to-end.
