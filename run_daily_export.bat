@echo off
REM Wrapper for Windows Task Scheduler: runs browser_crosstab.py once and
REM appends its output to a date-stamped log file. Point a Scheduled Task's
REM "Program/script" at this file's full path (no "Start in" needed -- it
REM cd's to its own folder first).
REM
REM browser_crosstab.py never prints credentials, cookies, or session
REM tokens (see tests/test_browser_crosstab.py's secrets-never-printed
REM checks), so this log is safe to keep around -- but it's still
REM gitignored (see logs/ in .gitignore) since it's local run history.

setlocal

cd /d "%~dp0"

if not exist "logs" mkdir "logs"

for /f %%i in ('".venv\Scripts\python.exe" -c "import datetime; print(datetime.date.today().isoformat())"') do set TODAY=%%i

echo ==== %date% %time% ==== >> "logs\%TODAY%.log"
".venv\Scripts\python.exe" browser_crosstab.py >> "logs\%TODAY%.log" 2>&1

exit /b %ERRORLEVEL%
