"""Reproduces the tail end of Tableau's native "Download -> Crosstab"
flow via plain HTTP, using the exact request/response structure captured
in a real browser HAR file -- not guessed.

Ground truth from the HAR (10 captured requests for one Crosstab export):

  POST /vizql/w/{workbook}/v/{view}/sessions/{session_id}/commands/tabsrv/get-view-data-csvexport-settings-server
  POST /vizql/w/{workbook}/v/{view}/sessions/{session_id}/commands/tabsrv/export-view-data-summary-to-csv-server
  GET  /vizql/w/{workbook}/v/{view}/tempfile/sessions/{session_id}?key={resultKey}&keepfile=yes&attachment=yes

Key findings from inspecting the HAR (structural inspection only -- no
cookie, token, or session id value was ever printed while producing
this):

- No `X-Tableau-Auth` header appears anywhere in this flow. Every VizQL
  command instead relies on a session id embedded directly in the URL
  path, plus an `X-XSRF-TOKEN` header (an anti-CSRF token tied to the
  browser's cookie-based session -- the HAR's Cookie headers were already
  redacted by Chrome's own HAR export, so we can't see cookie names or
  values, only that this header exists). This means the existing REST
  `X-Tableau-Auth` session CANNOT establish a VizQL session by itself --
  there is no evidence it's used, checked, or accepted here.
- The VizQL session id and a `visualIdPresModel` identifier (describing
  which visual/worksheet the dialog was opened for) are both already
  present on the *first* captured request in the flow -- meaning both
  are minted during the browser's interactive page load/bootstrap, which
  is NOT captured in this HAR and cannot be derived from any REST call
  this project has.
- The download key is not fixed or derivable from any request parameter
  we send -- it's minted by the server and returned in the
  `export-view-data-summary-to-csv-server` response, at
  `vqlCmdResponse.layoutStatus.applicationPresModel
  .presentationLayerNotification[0].presModelHolder
  .genExportFilePresModel.resultKey` (found by walking the actual
  captured response JSON; not documented anywhere). The same object also
  carries `fileName` and `mimeType`.

Because of the first two points, this probe CANNOT establish a VizQL
session on its own -- it requires an already-valid, short-lived VizQL
session id (and the matching visualIdPresModel/columns context) supplied
by the caller, most practically pasted from a fresh DevTools capture
immediately before running this. It exists to answer one narrow,
concrete question: given a live session, can the *download* tail of the
flow (export command -> tempfile fetch) be reproduced with plain HTTP?
It does NOT attempt to reproduce Tableau's web login or browser
bootstrap -- see CLAUDE.md for that decision.

Never returns or logs the session id, the export result key,
X-XSRF-TOKEN, any cookie, or file contents -- only safe response
metadata.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class VizqlSessionContext:
    """Ephemeral, browser-captured context this probe cannot derive on
    its own. None of these are credentials in the username/password
    sense, but they are short-lived and session-specific -- never
    hardcode, log, or commit them."""

    server_url: str
    workbook_content_url: str
    view_content_url: str
    session_id: str
    visual_id_pres_model: str
    columns_json: str


@dataclass(frozen=True)
class VizqlCommandResult:
    status_code: int
    elapsed_seconds: float
    error_body: str | None


@dataclass(frozen=True)
class TempfileResult:
    status_code: int
    elapsed_seconds: float
    content_type: str
    content_disposition: str | None
    filename: str | None
    response_size_bytes: int | None
    error_body: str | None


def _vizql_command_url(ctx: VizqlSessionContext, command: str) -> str:
    return (
        f"{ctx.server_url.rstrip('/')}/vizql/w/{ctx.workbook_content_url}"
        f"/v/{ctx.view_content_url}/sessions/{ctx.session_id}/commands/{command}"
    )


def _extract_filename(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    for part in content_disposition.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            value = part.split("=", 1)[1]
            return value.split("''", 1)[-1].strip('"')
        if part.lower().startswith("filename="):
            return part.split("=", 1)[1].strip('"')
    return None


def _post_command(
    url: str, fields: dict[str, str], timeout: float
) -> tuple[requests.Response | None, VizqlCommandResult]:
    """POSTs a VizQL command as multipart/form-data (matching the HAR's
    Content-Type), using simple string fields -- no file upload."""
    start = time.monotonic()
    try:
        response = requests.post(
            url, files={k: (None, v) for k, v in fields.items()}, timeout=timeout
        )
    except requests.RequestException as exc:
        elapsed = time.monotonic() - start
        return None, VizqlCommandResult(
            status_code=0, elapsed_seconds=elapsed, error_body=f"network error: {exc}"
        )
    elapsed = time.monotonic() - start
    error_body = response.text[:300] if response.status_code >= 400 else None
    return response, VizqlCommandResult(
        status_code=response.status_code, elapsed_seconds=elapsed, error_body=error_body
    )


def check_session(ctx: VizqlSessionContext, timeout: float = 15.0) -> VizqlCommandResult:
    """Cheap validity check: the settings command needs no per-visual
    context, only a valid session id. HTTP 200 here is our best
    available signal that `session_id` is still live."""
    url = _vizql_command_url(ctx, "tabsrv/get-view-data-csvexport-settings-server")
    telemetry_id = uuid.uuid4().hex[:30]
    _response, result = _post_command(url, {"telemetryCommandId": telemetry_id}, timeout)
    return result


def export_view_data_summary_to_csv(
    ctx: VizqlSessionContext, timeout: float = 30.0
) -> tuple[VizqlCommandResult, str | None, str | None, str | None]:
    """Reproduces "export-view-data-summary-to-csv-server" using the
    exact field names captured in the HAR. Returns
    (result, result_key, filename, mime_type). `result_key` is the
    tempfile download token -- callers must never log or print it."""
    url = _vizql_command_url(ctx, "tabsrv/export-view-data-summary-to-csv-server")
    fields = {
        "visualIdPresModel": ctx.visual_id_pres_model,
        "showAliases": "true",
        "columns": ctx.columns_json,
        "listSeparatorValue": ",",
        "unicodeEncodingValue": "UTF-8",
        "telemetryCommandId": uuid.uuid4().hex[:30],
    }
    response, result = _post_command(url, fields, timeout)

    if response is None or result.status_code != 200:
        return result, None, None, None

    try:
        body = response.json()
        gen = (
            body["vqlCmdResponse"]["layoutStatus"]["applicationPresModel"]
            ["presentationLayerNotification"][0]["presModelHolder"]["genExportFilePresModel"]
        )
        return result, gen.get("resultKey"), gen.get("fileName"), gen.get("mimeType")
    except (ValueError, KeyError, IndexError, TypeError):
        return result, None, None, None


def fetch_tempfile(ctx: VizqlSessionContext, result_key: str, timeout: float = 30.0) -> TempfileResult:
    """GETs the generated file using the key returned by the export
    command. Reads the body only to measure its size -- never inspects
    or returns its contents."""
    url = (
        f"{ctx.server_url.rstrip('/')}/vizql/w/{ctx.workbook_content_url}"
        f"/v/{ctx.view_content_url}/tempfile/sessions/{ctx.session_id}"
    )
    params = {"key": result_key, "keepfile": "yes", "attachment": "yes"}

    start = time.monotonic()
    try:
        response = requests.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        elapsed = time.monotonic() - start
        return TempfileResult(
            status_code=0,
            elapsed_seconds=elapsed,
            content_type="",
            content_disposition=None,
            filename=None,
            response_size_bytes=None,
            error_body=f"network error: {exc}",
        )
    elapsed = time.monotonic() - start

    content_type = response.headers.get("Content-Type", "unknown")
    content_disposition = response.headers.get("Content-Disposition")
    filename = _extract_filename(content_disposition)
    error_body = response.text[:300] if response.status_code >= 400 else None
    response_size_bytes = len(response.content) if response.status_code < 400 else None

    return TempfileResult(
        status_code=response.status_code,
        elapsed_seconds=elapsed,
        content_type=content_type,
        content_disposition=content_disposition,
        filename=filename,
        response_size_bytes=response_size_bytes,
        error_body=error_body,
    )
