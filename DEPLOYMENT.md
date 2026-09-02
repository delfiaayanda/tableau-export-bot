# Moving this bot to another machine (e.g. a server PC)

This is plain Python + Git + Playwright. It needs no Claude Code / AI
assistant on the target machine, so it will never touch or log out an
existing Claude Code session there -- Claude Code's login is stored per
Windows user profile, not per machine, so as long as this setup runs
under any account (its own, or the one already there), a different
Claude Code login elsewhere on that machine is unaffected.

## Steps

1. **Clone the repo** (don't copy files/folders manually -- this keeps
   the target machine in sync with `main` and naturally excludes
   everything gitignored, like `.venv` and `.env`):

   ```bash
   git clone https://github.com/delfiaayanda/tableau-export-bot.git
   cd tableau-export-bot
   ```

2. **Run the setup script** -- creates `.venv`, installs dependencies,
   installs Playwright's Chromium:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup_server.ps1
   ```

3. **Copy `.env` to this machine manually**, out of band (USB / secure
   file transfer) -- never via git, chat, or email, since it holds real
   Tableau credentials. Use `.env.example` as a reference for which
   variables are needed. Make sure `HEADLESS=true` for unattended runs.

4. **Run once manually** to confirm this machine can actually reach the
   real Tableau server (`insight.erajaya.com`) -- some networks block it:

   ```powershell
   .venv\Scripts\python.exe main.py
   ```

5. **Set up daily automation** via Windows Task Scheduler, pointing a
   Basic Task's "Program/script" at `run_daily_export.bat` in this
   folder (it cd's to its own location, so no "Start in" is needed).
   Output/logs land in `logs\<date>.log`; exports land in
   `exports\<date>\`.

## Keeping it updated later

```bash
git pull
```

No need to re-clone. Re-run `scripts\setup_server.ps1` only if
`requirements.txt` changed.
