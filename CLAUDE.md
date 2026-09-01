# Architecture & status

## Goal

Export the `ReturnRotate/Dashboard1` Tableau view
(`https://insight.erajaya.com/#/views/ReturnRotate/Dashboard1`) to a
date-based, timestamped Excel file, runnable as `python main.py`.

## Architecture

Layered, each layer swappable via a small interface:

- `config.py` — loads all settings (server URL, credentials, view ID,
  export dir, log level) from environment variables. Raises `ConfigError`
  if anything required is missing. No secrets or defaults for credentials
  are baked in.
- `auth.py` — `Authenticator` ABC + `TableauServerAuthenticator`, which
  POSTs to Tableau's REST `auth/signin` endpoint and returns a
  `TableauSession` (token, site id, server url, api version).
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

## Network blocker (unresolved)

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
general internet egress at all, only an allowlist).

Consequence: **no code that talks to the real Tableau server has been run
or validated.** This includes:

- Whether `TABLEAU_USERNAME`/`TABLEAU_PASSWORD` sign-in works, or whether
  the instance actually requires SSO/SAML (would need a different auth
  flow, likely browser-based, if so).
- Whether the Tableau REST API is even enabled/reachable on this server,
  and which API version it speaks.
- Whether the "view data" endpoint returns the CSV shape assumed by
  `tableau_client.py`, or whether crosstab/export endpoints work better.
- The real `TABLEAU_VIEW_ID` for `ReturnRotate/Dashboard1` (the URL only
  gives the human-readable view path, not the GUID the API needs).

`auth.py` and `tableau_client.py` are written to match Tableau's
documented REST API, but are explicitly unverified — treat them as a
starting point to debug from a machine with real network access, not as
working code.

## What is tested vs. not

Tested in this environment (`pytest`, no network):
- `config.py` — required/optional env vars, missing/blank value errors.
- `validation.py` — empty/no-columns/all-null/valid DataFrame cases.
- `exporter.py` — folder/filename format, round-trip read of written xlsx.
- `pipeline.py` — wiring, using `FakeAuthenticator`/`FakeTableauClient`
  test doubles defined in `tests/test_pipeline.py`. These prove the
  pipeline calls its collaborators correctly; they do **not** simulate or
  validate real Tableau API behavior.

Not tested, requires a machine with access to `insight.erajaya.com`:
- `TableauServerAuthenticator.authenticate()` against the real server.
- `TableauServerClient.get_view_data()` against the real server.
- End-to-end `python main.py` run with real credentials.
- Discovering the real `TABLEAU_VIEW_ID` and confirming the REST API path
  (vs. needing Playwright-based session capture if SSO or the REST API is
  disabled).

## Next steps once network access exists

1. `curl <TABLEAU_SERVER_URL>/api/3.22/serverinfo` to confirm the REST API
   is enabled and to get the real API version.
2. Attempt `TableauServerAuthenticator.authenticate()` with real
   credentials; if it fails with a redirect to an SSO login page, the
   username/password flow won't work and a browser-based (Playwright)
   login capturing session cookies/tokens will be needed instead.
3. Find the view's GUID (Tableau REST `views` list endpoint, or browser
   dev tools) to set `TABLEAU_VIEW_ID`.
4. Run `TableauServerClient.get_view_data()` and check the returned CSV
   against what's expected; adjust `validation.py` if the real schema
   needs specific column checks.
5. Only after that: run `python main.py` end-to-end.
