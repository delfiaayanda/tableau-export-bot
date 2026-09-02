"""Probes the Tableau "view data" REST endpoint and reports only safe,
non-sensitive metadata about the response -- HTTP status, content type,
and (if the body parses as tabular CSV data) row/column counts and column
names. Never returns row values.

Uses the exact same endpoint as
tableau_client.TableauServerClient.get_view_data():
`GET /api/{version}/sites/{site-id}/views/{view-id}/data`,
`Accept: text/csv`. No alternative endpoint is attempted here -- if this
one fails, the caller reports the sanitized status/error and which URL
was attempted (see extract_smoke_test.py).

Not exercised against the real server yet -- see CLAUDE.md.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pandas as pd
import requests

from .auth import TableauSession
from .exceptions import ExtractionError


@dataclass(frozen=True)
class ExtractionProbeResult:
    url: str
    status_code: int
    content_type: str
    row_count: int | None
    column_count: int | None
    column_names: list[str] | None


def probe_view_data(session: TableauSession, view_id: str, timeout: float = 60.0) -> ExtractionProbeResult:
    """Request the view's data and report shape metadata only -- never the
    row values themselves. Raises ExtractionError (naming this URL) if the
    request fails outright (network error or non-200 status)."""
    url = (
        f"{session.server_url.rstrip('/')}/api/{session.api_version}"
        f"/sites/{session.site_id}/views/{view_id}/data"
    )
    headers = {"X-Tableau-Auth": session.auth_token, "Accept": "text/csv"}

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ExtractionError(f"Could not reach Tableau server at {url}: {exc}") from exc

    if response.status_code != 200:
        raise ExtractionError(
            f"View data request to {url} failed with status {response.status_code}: {response.text[:500]}"
        )

    content_type = response.headers.get("Content-Type", "unknown")

    try:
        df = pd.read_csv(io.StringIO(response.text))
        row_count, column_count = df.shape
        column_names = list(df.columns)
    except Exception:  # pandas raises several distinct error types
        row_count = column_count = column_names = None

    return ExtractionProbeResult(
        url=url,
        status_code=response.status_code,
        content_type=content_type,
        row_count=row_count,
        column_count=column_count,
        column_names=column_names,
    )
