#!/usr/bin/env python3
"""CLI entry point: python main.py

Delegates entirely to the browser-automated export
(src/exportbot/browser_crosstab.py) -- the only extraction method
confirmed working against the real server (see CLAUDE.md, "Success --
end-to-end export confirmed"). The REST-based pipeline (auth.py /
tableau_client.py / pipeline.py) is still blocked (HTTP 406 / timeout on
the real server) and intentionally not used here; it's kept as a
starting point for a future lighter-weight extraction method, not wired
into this entry point.

Usage: python main.py
Environment: see browser_crosstab.py's docstring / README.md.
"""

from __future__ import annotations

from browser_crosstab import main as run_browser_crosstab_main


def main() -> int:
    return run_browser_crosstab_main()


if __name__ == "__main__":
    raise SystemExit(main())
