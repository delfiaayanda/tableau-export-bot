"""Tests discover_view()'s handling of HTTP responses shaped like
Tableau's documented Views/Workbook REST responses. No real network call
is made -- requests.get is mocked purely to test our own parsing and
error-handling logic. This does NOT claim live view discovery has
succeeded against the real server (see CLAUDE.md for what's verified).
"""

from unittest.mock import patch

import pytest

from src.exportbot.auth import TableauSession
from src.exportbot.exceptions import ExtractionError
from src.exportbot.view_discovery import discover_view


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        return self._json_body


def _session() -> TableauSession:
    return TableauSession(
        auth_token="fake-token",
        site_id="site-1",
        server_url="https://insight.example.com",
        api_version="3.29",
    )


def test_discover_view_returns_ids_and_names_on_match():
    views_response = FakeResponse(
        status_code=200,
        json_body={
            "views": {
                "view": [
                    {
                        "id": "view-guid-1",
                        "name": "Dashboard1",
                        "workbook": {"id": "workbook-guid-1"},
                    }
                ]
            }
        },
    )
    workbook_response = FakeResponse(
        status_code=200,
        json_body={"workbook": {"id": "workbook-guid-1", "name": "ReturnRotate"}},
    )

    def fake_get(url, **kwargs):
        return workbook_response if "/workbooks/" in url else views_response

    with patch("src.exportbot.view_discovery.requests.get", side_effect=fake_get) as mock_get:
        info = discover_view(_session(), "ReturnRotate", "Dashboard1")

    assert info.view_id == "view-guid-1"
    assert info.view_name == "Dashboard1"
    assert info.workbook_id == "workbook-guid-1"
    assert info.workbook_name == "ReturnRotate"
    assert info.content_url == "ReturnRotate/sheets/Dashboard1"

    views_call = mock_get.call_args_list[0]
    assert views_call.kwargs["params"] == {"filter": "contentUrl:eq:ReturnRotate/sheets/Dashboard1"}
    assert views_call.kwargs["headers"]["X-Tableau-Auth"] == "fake-token"


def test_discover_view_raises_when_not_found():
    views_response = FakeResponse(status_code=200, json_body={"views": {"view": []}})

    with patch("src.exportbot.view_discovery.requests.get", return_value=views_response):
        with pytest.raises(ExtractionError, match="No view found"):
            discover_view(_session(), "ReturnRotate", "Dashboard1")


def test_discover_view_raises_on_non_200():
    views_response = FakeResponse(status_code=403, text="Forbidden")

    with patch("src.exportbot.view_discovery.requests.get", return_value=views_response):
        with pytest.raises(ExtractionError, match="403"):
            discover_view(_session(), "ReturnRotate", "Dashboard1")


def test_discover_view_raises_on_malformed_body():
    views_response = FakeResponse(status_code=200, json_body={"unexpected": "shape"})

    with patch("src.exportbot.view_discovery.requests.get", return_value=views_response):
        with pytest.raises(ExtractionError, match="Unexpected view lookup response shape"):
            discover_view(_session(), "ReturnRotate", "Dashboard1")
