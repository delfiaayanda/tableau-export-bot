# One-time setup for a new machine (e.g. moving this bot to a server PC).
# Run this from PowerShell in the folder where the repo should live:
#
#   git clone https://github.com/delfiaayanda/tableau-export-bot.git
#   cd tableau-export-bot
#   powershell -ExecutionPolicy Bypass -File scripts\setup_server.ps1
#
# This needs no Claude Code / AI assistant on the target machine -- it's
# plain Python and Playwright setup, safe to run under any Windows account
# without touching whatever else (including another Claude Code login) is
# already signed in there.
#
# It deliberately does NOT create or touch .env -- that holds real
# credentials and must be copied to this machine yourself, out of band
# (USB / secure file transfer), never through git, chat, or email.

$ErrorActionPreference = "Stop"

Write-Host "== Checking Python =="
$pythonVersion = & python --version 2>&1
Write-Host $pythonVersion
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python not found. Install Python 3.11+ first: https://www.python.org/downloads/"
    exit 1
}

Write-Host "== Creating virtual environment (.venv) =="
if (Test-Path ".venv") {
    Write-Host ".venv already exists, skipping creation."
} else {
    python -m venv .venv
}

Write-Host "== Installing dependencies =="
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\pip.exe" install -r requirements.txt

Write-Host "== Installing Playwright's Chromium browser =="
& ".venv\Scripts\python.exe" -m playwright install chromium

Write-Host ""
Write-Host "== Setup complete. Remaining manual steps: =="
if (-not (Test-Path ".env")) {
    Write-Host "  1. .env is missing -- copy it from the original machine (never via git/chat/email)."
    Write-Host "     Use .env.example as a reference for which variables are needed."
} else {
    Write-Host "  1. .env already present -- double check HEADLESS=true for unattended runs."
}
Write-Host "  2. Run once manually to confirm this machine can reach the real Tableau server:"
Write-Host "       .venv\Scripts\python.exe main.py"
Write-Host "  3. If that succeeds, set up a daily Windows Task Scheduler entry pointing at:"
Write-Host "       $(Resolve-Path .)\run_daily_export.bat"
