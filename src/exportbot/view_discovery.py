"""Resolves a Tableau workbook/view name pair to the GUIDs the export
pipeline needs (TABLEAU_VIEW_ID), via the authenticated REST API.

Uses the documented "Query Views for Site" endpoint
(`GET /api/{version}/sites/{site-id}/views`), filtered by the view's
contentUrl. Tableau builds a view's contentUrl as
"{workbookContentUrl}/sheets/{viewContentUrl}" -- the same shape as the
browser URL (.../#/views/{workbook}/{view}). The workbook's display name
is confirmed with a follow-up "Query Workbook" call
(`GET .../workbooks/{workbook-id}`).

Not exercised against the real server yet -- see CLAUDE.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from .auth import TableauSession
from .exceptions import ExtractionError


@dataclass(frozen=True)
class ViewInfo:
    workbook_id: str
    workbook_name: str
    view_id: str
    view_name: str
    content_url: str


def discover_view(
    session: TableauSession,
    workbook_content_url: str,
    view_content_url: str,
    timeout: float = 30.0,
) -> ViewInfo:
    """Look up a view's GUID (and its workbook's) by name. Raises
    ExtractionError if it can't be reached, parsed, or found."""
    content_url = f"{workbook_content_url}/sheets/{view_content_url}"
    views_url = (
        f"{session.server_url.rstrip('/')}/api/{session.api_version}"
        f"/sites/{session.site_id}/views"
    )
    headers = {"X-Tableau-Auth": session.auth_token, "Accept": "application/json"}

    try:
        response = requests.get(
            views_url,
            headers=headers,
            params={"filter": f"contentUrl:eq:{content_url}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ExtractionError(f"Could not reach Tableau server at {views_url}: {exc}") from exc

    if response.status_code != 200:
        raise ExtractionError(
            f"View lookup failed with status {response.status_code}: {response.text[:500]}"
        )

    try:
        matches = response.json()["views"]["view"]
    except (ValueError, KeyError) as exc:
        raise ExtractionError(f"Unexpected view lookup response shape: {exc}") from exc

    if not matches:
        raise ExtractionError(f"No view found for contentUrl='{content_url}'")

    view = matches[0]
    try:
        view_id = view["id"]
        view_name = view["name"]
        workbook_id = view["workbook"]["id"]
    except KeyError as exc:
        raise ExtractionError(f"Unexpected view object shape: {exc}") from exc

    workbook_name = _get_workbook_name(session, workbook_id, timeout)

    return ViewInfo(
        workbook_id=workbook_id,
        workbook_name=workbook_name,
        view_id=view_id,
        view_name=view_name,
        content_url=content_url,
    )


def _get_workbook_name(session: TableauSession, workbook_id: str, timeout: float) -> str:
    url = (
        f"{session.server_url.rstrip('/')}/api/{session.api_version}"
        f"/sites/{session.site_id}/workbooks/{workbook_id}"
    )
    headers = {"X-Tableau-Auth": session.auth_token, "Accept": "application/json"}

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise ExtractionError(f"Could not reach Tableau server at {url}: {exc}") from exc

    if response.status_code != 200:
        raise ExtractionError(
            f"Workbook lookup failed with status {response.status_code}: {response.text[:500]}"
        )

    try:
        return response.json()["workbook"]["name"]
    except (ValueError, KeyError) as exc:
        raise ExtractionError(f"Unexpected workbook lookup response shape: {exc}") from exc
