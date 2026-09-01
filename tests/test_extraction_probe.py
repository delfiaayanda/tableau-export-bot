"""Tests probe_view_data()'s handling of HTTP responses from the "view
data" endpoint using mocked requests.get -- no real network call, and no
claim that live extraction has been verified (see CLAUDE.md). Confirms
row values are never surfaced, only shape metadata.
"""

from unittest.mock import patch

import pytest

from src.exportbot.auth import TableauSession
from src.exportbot.exceptions import ExtractionError
from src.exportbot.extraction_probe import probe_view_data


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def _session() -> TableauSession:
    return TableauSession(
        auth_token="fake-token",
        site_id="site-1",
        server_url="https://insight.example.com",
        api_version="3.29",
    )


def test_probe_view_data_reports_shape_for_csv_body():
    csv_body = "Region,Amount\nWest,100\nEast,200\n"
    fake = FakeResponse(status_code=200, text=csv_body, headers={"Content-Type": "text/csv"})

    with patch("src.exportbot.extraction_probe.requests.get", return_value=fake) as mock_get:
        result = probe_view_data(_session(), "view-guid-1")

    assert result.status_code == 200
    assert result.content_type == "text/csv"
    assert result.row_count == 2
    assert result.column_count == 2
    assert result.column_names == ["Region", "Amount"]
    assert "West" not in str(result)  # never carries row values

    call = mock_get.call_args
    assert call.args[0] == "https://insight.example.com/api/3.29/sites/site-1/views/view-guid-1/data"
    assert call.kwargs["headers"]["X-Tableau-Auth"] == "fake-token"
    assert call.kwargs["headers"]["Accept"] == "text/csv"


def test_probe_view_data_handles_unparseable_body_gracefully():
    # An empty body is valid to receive (200 OK) but pandas cannot parse it
    # as CSV (raises EmptyDataError) -- probe_view_data must not crash on
    # this, just report the shape fields as unknown.
    fake = FakeResponse(status_code=200, text="", headers={"Content-Type": "text/html"})

    with patch("src.exportbot.extraction_probe.requests.get", return_value=fake):
        result = probe_view_data(_session(), "view-guid-1")

    assert result.status_code == 200
    assert result.content_type == "text/html"
    assert result.row_count is None
    assert result.column_count is None
    assert result.column_names is None


def test_probe_view_data_raises_on_non_200_and_names_url():
    fake = FakeResponse(status_code=403, text="Forbidden")

    with patch("src.exportbot.extraction_probe.requests.get", return_value=fake):
        with pytest.raises(ExtractionError, match="403") as exc_info:
            probe_view_data(_session(), "view-guid-1")

    assert "views/view-guid-1/data" in str(exc_info.value)


def test_probe_view_data_raises_on_network_error():
    import requests

    with patch(
        "src.exportbot.extraction_probe.requests.get",
        side_effect=requests.ConnectionError("boom"),
    ):
        with pytest.raises(ExtractionError, match="Could not reach"):
            probe_view_data(_session(), "view-guid-1")
