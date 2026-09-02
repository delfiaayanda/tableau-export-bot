# tableau-export-bot

Automates exporting the `ReturnRotate/Dashboard1` Tableau view to a
date-stamped Excel file.

**Status: in progress.** Authentication against the real Tableau server
(`insight.erajaya.com`) is confirmed working (native username/password
sign-in, REST API 3.29), and the view's GUID is confirmed
(`741ce0df-8818-4412-93ca-b34c03019219` for workbook "Return & Rotate" /
view "Dashboard 1"). REST/VizQL data extraction hit a real blocker (see
`CLAUDE.md`), so `browser_crosstab.py` (Playwright) drives the confirmed
manual UI workflow instead — untested against the real server from this
development environment (no browser binary can be installed here and
network access to that host is blocked). See `CLAUDE.md` for details.

## Project layout

```
main.py                        CLI entry point
auth_smoke_test.py             Isolated sign-in check (no data extraction)
discover_view.py               Isolated workbook/view GUID lookup (no data extraction)
extract_smoke_test.py          Isolated view-data probe (metadata only, no row data printed)
diagnose_extraction.py         Retries the view-data request with different Accept headers
crosstab_probe.py              Isolated probe of the documented crosstab-Excel REST endpoint
vizql_crosstab_probe.py        Experimental: reproduces the browser's native VizQL Crosstab export
browser_crosstab.py            Playwright browser automation: Download -> Data -> Crosstab -> CSV
src/exportbot/
  config.py                    Env-var configuration loading
  auth.py                      Authenticator interface + Tableau REST sign-in
  tableau_client.py            TableauClient interface + REST view-data fetch
  view_discovery.py            Resolves a workbook/view name pair to its GUID
  extraction_probe.py          Probes view-data endpoint, reports shape metadata only
  extraction_diagnostics.py    Retries view-data with different Accept headers, reports status/headers
  crosstab_probe.py            Probes the crosstab-Excel endpoint, reports status/size/filename only
  vizql_crosstab_probe.py      Reproduces the VizQL export+tempfile commands given a live session
  browser_crosstab.py          Playwright: logs in, opens Download -> Data -> Crosstab, saves CSV
  validation.py                Pre-export data checks
  exporter.py                  Excel export (date folder + timestamped file)
  pipeline.py                  Wires the above together
  logging_setup.py             Logging configuration
  exceptions.py                Shared exception types
  serverinfo.py                Auto-detects the REST API version (unauthenticated)
tests/                         Unit tests (no network required)
```

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values; never commit .env
```

## Configuration

All settings come from environment variables (see `.env.example`):
`TABLEAU_SERVER_URL`, `TABLEAU_SITE`, `TABLEAU_USERNAME`,
`TABLEAU_PASSWORD`, `TABLEAU_VIEW_ID`, `TABLEAU_API_VERSION`,
`EXPORT_DIR`, `LOG_LEVEL`. No credentials are hardcoded anywhere in the
code.

`TABLEAU_API_VERSION` is optional — leave it blank and `main.py` will
auto-detect it at startup from the server's unauthenticated `serverinfo`
endpoint (`src/exportbot/serverinfo.py`); set it explicitly to skip that
call.

## Running

Before running the full pipeline, do an isolated authentication check
(sign-in only, no data extraction, never prints credentials/tokens):

```bash
python auth_smoke_test.py
```

Prints `SUCCESS: ...` or `FAILURE: <sanitized status/message>`.

Once sign-in succeeds, resolve the real view GUID for `TABLEAU_VIEW_ID`
(also sign-in only, no data extraction):

```bash
python discover_view.py
```

Prints `SUCCESS: workbook='...' (id=...) view='...' (id=...) -> set TABLEAU_VIEW_ID=...`
or a sanitized `FAILURE: ...`.

Once `TABLEAU_VIEW_ID` is set, probe actual data extraction from that view
(reports only HTTP status, content type, row/column counts, and column
names — never row values, credentials, or tokens):

```bash
python extract_smoke_test.py
```

If that fails with a non-200 status or hangs (e.g. HTTP 406, or a
timeout), diagnose it: retries "Query View Data" and "Query View
Crosstab Excel" with different `Accept` header values, a short 10s
timeout, and elapsed time reported per attempt (still metadata only,
never row data):

```bash
python diagnose_extraction.py
```

Tableau's native "Download -> Crosstab" browser menu is a separate,
undocumented internal endpoint (cookie-session based), not necessarily
the same as the public REST API's documented crosstab endpoint. To
capture what the browser actually does, see the DevTools steps in
`CLAUDE.md`. In the meantime, this probes the one documented,
REST-token-authenticated crosstab endpoint as a hypothesis (reports
status, elapsed time, Content-Type, Content-Disposition, size, and
filename only -- never the file's contents):

```bash
python crosstab_probe.py
```

**Update: a real browser HAR capture confirmed the native "Download ->
Crosstab" menu does NOT use the REST API at all.** It calls Tableau's
internal VizQL session commands (`/vizql/w/{workbook}/v/{view}/sessions/
{id}/commands/...`), authenticated via a browser session (cookie +
`X-XSRF-TOKEN`), not `X-Tableau-Auth`. See "Native UI Crosstab download"
in `CLAUDE.md` for the full analysis. `vizql_crosstab_probe.py` is an
experimental, isolated reproduction of the export+download tail of that
flow — it cannot establish a session on its own (the session id and a
per-visual identifier are minted during the browser's page load, not
derivable via REST), so it requires those values pasted fresh from a
DevTools capture as environment variables
(`TABLEAU_VIZQL_SESSION_ID`, `TABLEAU_VIZQL_VISUAL_ID_PRES_MODEL`,
`TABLEAU_VIZQL_COLUMNS_JSON`) — never commit or reuse them, they expire
quickly:

```bash
python vizql_crosstab_probe.py
```

```bash
python main.py
```

This will fail today with an authentication/connection error outside a
network that can reach `insight.erajaya.com` — that's expected until it's
run from such a machine (see `CLAUDE.md`).

## Browser automation setup (`browser_crosstab.py`)

The REST/VizQL investigation (see `CLAUDE.md`) found that Tableau's
native "Download -> Crosstab" flow requires a real browser session that
can't be established via REST alone. `browser_crosstab.py` drives an
actual browser through the exact confirmed manual workflow instead:
login -> open the dashboard -> Download -> Data -> Crosstab -> save CSV.

One-time local setup (not runnable from this development sandbox — no
browser binary can be installed here and network access to
`insight.erajaya.com` is blocked):

```bash
pip install -r requirements.txt   # installs the playwright package
playwright install chromium       # downloads the actual browser binary
```

If corporate policy blocks installing Chromium, point at an existing
Chrome/Edge install instead via `.env`:

```
PLAYWRIGHT_EXECUTABLE_PATH=/path/to/chrome-or-edge
```

Then run:

```bash
python browser_crosstab.py
```

Prints step-by-step progress (`Opening Tableau...`, `Logging in...`,
`Dashboard loaded.`, `Opening Download menu...`, `Opening Data...`,
`Exporting Crosstab...`) and finally `SUCCESS: <path to saved CSV>`, or
`FAILURE: ...` — never prints the password, cookies, or any
session/auth token.

The CSV is saved to `<EXPORT_DIR>/<YYYY-MM-DD>/<filename>` — the same
date-folder convention `exporter.py` uses for the REST pipeline's Excel
output. This matters for running it daily: Tableau's own filename (via
`Content-Disposition`) doesn't include a date, so without this folder a
second run on the same day would overwrite the first, but every day
still gets its own subfolder.

Set `HEADLESS=false` in `.env` to watch it run in a visible browser
window while debugging. The DOM selectors it uses are best-effort
(accessible roles/names) since this dashboard's real DOM was never
inspected directly — if a step can't find what it's looking for, it
raises a clear error and saves a screenshot to
`<EXPORT_DIR>/debug_<step>.png` so the actual page can be inspected and
the selectors adjusted in `src/exportbot/browser_crosstab.py`.

## Testing

```bash
pytest
```

All current tests run without network access. They cover configuration
loading, validation logic, Excel export, and pipeline wiring using fake
authenticator/client test doubles — they do not simulate real Tableau
responses.
