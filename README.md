# tableau-export-bot

Automates exporting the `ReturnRotate/Dashboard1` Tableau view to a
date-stamped Excel file.

**Status: scaffolding only.** Authentication and extraction against the
real Tableau server (`insight.erajaya.com`) have not been tested — network
access to that host is blocked from the current development environment.
See `CLAUDE.md` for details and what's left to verify.

## Project layout

```
main.py                        CLI entry point
src/exportbot/
  config.py                    Env-var configuration loading
  auth.py                      Authenticator interface + Tableau REST sign-in
  tableau_client.py            TableauClient interface + REST view-data fetch
  validation.py                Pre-export data checks
  exporter.py                  Excel export (date folder + timestamped file)
  pipeline.py                  Wires the above together
  logging_setup.py             Logging configuration
  exceptions.py                Shared exception types
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
`TABLEAU_PASSWORD`, `TABLEAU_VIEW_ID`, `EXPORT_DIR`, `LOG_LEVEL`. No
credentials are hardcoded anywhere in the code.

## Running

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
