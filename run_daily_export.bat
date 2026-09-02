@echo off
REM Wrapper for Windows Task Scheduler: runs main.py (the browser-automated
REM export -- the only method confirmed working, see CLAUDE.md) once and
REM appends its output to a date-stamped log file. Point a Scheduled Task's
REM "Program/script" at this file's full path (no "Start in" needed -- it
REM cd's to its own folder first).
REM
REM main.py never prints credentials, cookies, or session tokens (see
REM tests/test_browser_crosstab.py's secrets-never-printed checks), so
REM this log is safe to keep around -- but it's still gitignored (see
REM logs/ in .gitignore) since it's local run history.

setlocal

cd /d "%~dp0"

if not exist "logs" mkdir "logs"

for /f %%i in ('".venv\Scripts\python.exe" -c "import datetime; print(datetime.date.today().isoformat())"') do set TODAY=%%i

echo ==== %date% %time% ==== >> "logs\%TODAY%.log"
".venv\Scripts\python.exe" main.py >> "logs\%TODAY%.log" 2>&1

exit /b %ERRORLEVEL%
