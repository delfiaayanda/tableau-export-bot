"""Tableau data extraction client.

NOTE: Not exercised against a live server — network access to the target
Tableau instance is blocked from this development environment (see
CLAUDE.md). This wraps the documented "view data" REST endpoint
(`GET /api/{version}/sites/{site_id}/views/{view_id}/data`), which returns
the view's underlying data as CSV. It must be validated against the real
instance: confirm the endpoint is enabled, the returned CSV shape matches
expectations, and pagination/size limits are handled if needed.
"""

from __future__ import annotations

import io
from abc import ABC, abstractmethod

import pandas as pd
import requests

from .auth import TableauSession
from .exceptions import ExtractionError


class TableauClient(ABC):
    """Interface for retrieving a Tableau view's data as a DataFrame."""

    @abstractmethod
    def get_view_data(self, view_id: str) -> pd.DataFrame:
        raise NotImplementedError


class TableauServerClient(TableauClient):
    """Retrieves view data from Tableau Server/Online via the REST API."""

    def __init__(self, session: TableauSession, timeout: float = 60.0):
        self.session = session
        self.timeout = timeout

    def get_view_data(self, view_id: str) -> pd.DataFrame:
        url = (
            f"{self.session.server_url.rstrip('/')}/api/{self.session.api_version}"
            f"/sites/{self.session.site_id}/views/{view_id}/data"
        )
        headers = {"X-Tableau-Auth": self.session.auth_token, "Accept": "text/csv"}

        try:
            response = requests.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ExtractionError(f"Could not reach Tableau server at {url}: {exc}") from exc

        if response.status_code != 200:
            raise ExtractionError(
                f"View data request failed with status {response.status_code}: {response.text[:500]}"
            )

        try:
            return pd.read_csv(io.StringIO(response.text))
        except Exception as exc:  # pandas raises several distinct error types
            raise ExtractionError(f"Could not parse view data as CSV: {exc}") from exc
