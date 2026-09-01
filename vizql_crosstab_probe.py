#!/usr/bin/env python3
"""Isolated, experimental probe: attempts to reproduce the tail end of
Tableau's native "Download -> Crosstab" flow (export command -> tempfile
download) via plain HTTP, using the exact structure captured in a real
browser HAR. See src/exportbot/vizql_crosstab_probe.py's module docstring
and CLAUDE.md for the full writeup and important limitations.

This CANNOT establish a VizQL session on its own -- the session id and
per-visual context are minted during the browser's interactive page load
and are not derivable from any REST call this project has. It requires
those values supplied as environment variables, captured fresh from
DevTools immediately before running (they are short-lived and expire
quickly). Never commit or reuse them.

Required environment variables (this script does NOT use the usual
TABLEAU_USERNAME/PASSWORD/API auth -- see module docstring for why):
  TABLEAU_SERVER_URL
  TABLEAU_VIZQL_SESSION_ID           (from the captured URL path)
  TABLEAU_VIZQL_VISUAL_ID_PRES_MODEL (from the captured request body)
  TABLEAU_VIZQL_COLUMNS_JSON         (from the captured request body's
                                       "columns" field, as JSON)

Reports only: session established (yes/no + HTTP status), export request
HTTP status, tempfile request HTTP status, content type, content
disposition, response size, elapsed time, and filename. Never prints the
session id, X-XSRF-TOKEN, cookies, the export result key, request
bodies, or file contents.

Usage: python vizql_crosstab_probe.py
"""

from __future__ import annotations

import os
import sys

from src.exportbot.vizql_crosstab_probe import (
    VizqlSessionContext,
    check_session,
    export_view_data_summary_to_csv,
    fetch_tempfile,
)

# The specific dashboard this project targets.
WORKBOOK_CONTENT_URL = "ReturnRotate"
VIEW_CONTENT_URL = "Dashboard1"


def _build_context(server_url: str) -> VizqlSessionContext | None:
    session_id = os.environ.get("TABLEAU_VIZQL_SESSION_ID", "").strip()
    visual_id_pres_model = os.environ.get("TABLEAU_VIZQL_VISUAL_ID_PRES_MODEL", "").strip()
    columns_json = os.environ.get("TABLEAU_VIZQL_COLUMNS_JSON", "").strip()

    if not (session_id and visual_id_pres_model and columns_json):
        return None

    return VizqlSessionContext(
        server_url=server_url,
        workbook_content_url=WORKBOOK_CONTENT_URL,
        view_content_url=VIEW_CONTENT_URL,
        session_id=session_id,
        visual_id_pres_model=visual_id_pres_model,
        columns_json=columns_json,
    )


def run_probe(server_url: str) -> tuple[bool, list[str]]:
    """Returns (success, lines). Never includes the session id, result
    key, or any cookie/token in the returned lines."""
    ctx = _build_context(server_url)
    if ctx is None:
        return False, [
            "session established: no (TABLEAU_VIZQL_SESSION_ID / "
            "TABLEAU_VIZQL_VISUAL_ID_PRES_MODEL / TABLEAU_VIZQL_COLUMNS_JSON "
            "not all set -- these can only come from a fresh DevTools "
            "capture, not from REST authentication; see CLAUDE.md)"
        ]

    lines = []

    check_result = check_session(ctx)
    session_ok = check_result.status_code == 200
    lines.append(f"session established: {'yes' if session_ok else 'no'} (HTTP {check_result.status_code})")
    if not session_ok:
        return False, lines

    export_result, result_key, filename, _mime_type = export_view_data_summary_to_csv(ctx)
    export_line = f"export request: HTTP {export_result.status_code} in {export_result.elapsed_seconds:.1f}s"
    if export_result.error_body:
        export_line += f", error_body={export_result.error_body!r}"
    lines.append(export_line)

    if export_result.status_code != 200 or not result_key:
        lines.append("tempfile request: not attempted (no download key returned)")
        return False, lines

    tempfile_result = fetch_tempfile(ctx, result_key)
    tempfile_line = (
        f"tempfile request: HTTP {tempfile_result.status_code} in {tempfile_result.elapsed_seconds:.1f}s, "
        f"content-type={tempfile_result.content_type or 'n/a'}, "
        f"content-disposition={tempfile_result.content_disposition or 'n/a'}, "
        f"filename={tempfile_result.filename or filename or 'n/a'}"
    )
    if tempfile_result.response_size_bytes is not None:
        tempfile_line += f", size_bytes={tempfile_result.response_size_bytes}"
    if tempfile_result.error_body:
        tempfile_line += f", error_body={tempfile_result.error_body!r}"
    lines.append(tempfile_line)

    return tempfile_result.status_code == 200, lines


def main() -> int:
    # This tool intentionally does not use load_config(): it does not
    # authenticate via the REST username/password flow at all (see module
    # docstring for why), so only the server URL is needed here.
    server_url = os.environ.get("TABLEAU_SERVER_URL", "").strip()
    if not server_url:
        print("Configuration error: Missing required environment variable: TABLEAU_SERVER_URL", file=sys.stderr)
        return 2

    success, lines = run_probe(server_url)

    for line in lines:
        print(line)

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
