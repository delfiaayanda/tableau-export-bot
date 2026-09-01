# tableau-export-bot

Automates exporting the `ReturnRotate/Dashboard1` Tableau view to a
date-stamped Excel file.

**Status: in progress.** Authentication against the real Tableau server
(`insight.erajaya.com`) is confirmed working (native username/password
sign-in, REST API 3.29), and the view's GUID is confirmed
(`741ce0df-8818-4412-93ca-b34c03019219` for workbook "Return & Rotate" /
view "Dashboard 1"). Actual data extraction is still untested — network
access to that host is blocked from the current development environment.
See `CLAUDE.md` for details and what's left to verify.

## Project layout

```
main.py                        CLI entry point
auth_smoke_test.py             Isolated sign-in check (no data extraction)
discover_view.py               Isolated workbook/view GUID lookup (no data extraction)
extract_smoke_test.py          Isolated view-data probe (metadata only, no row data printed)
diagnose_extraction.py         Retries the view-data request with different Accept headers
src/exportbot/
  config.py                    Env-var configuration loading
  auth.py                      Authenticator interface + Tableau REST sign-in
  tableau_client.py            TableauClient interface + REST view-data fetch
  view_discovery.py            Resolves a workbook/view name pair to its GUID
  extraction_probe.py          Probes view-data endpoint, reports shape metadata only
  extraction_diagnostics.py    Retries view-data with different Accept headers, reports status/headers
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

```bash
python main.py
```

This will fail today with an authentication/connection error outside a
network that can reach `insight.erajaya.com` — that's expected until it's
run from such a machine (see `CLAUDE.md`).

## Testing

```bash
pytest
```

All current tests run without network access. They cover configuration
loading, validation logic, Excel export, and pipeline wiring using fake
authenticator/client test doubles — they do not simulate real Tableau
responses.
