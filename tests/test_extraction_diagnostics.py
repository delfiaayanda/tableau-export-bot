"""Tests probe_with_accept_header()'s handling of different HTTP responses
using mocked requests.get -- no real network call. Confirms it never
raises (so a caller can try every Accept variant even after a failure),
strips unsafe headers, and never surfaces row values.
"""

import requests
from unittest.mock import patch

from src.exportbot.auth import TableauSession
from src.exportbot.extraction_diagnostics import (
    SAFE_RESPONSE_HEADER_NAMES,
    probe_with_accept_header,
)


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


def test_probe_reports_406_with_sanitized_error_body_and_safe_headers_only():
    fake = FakeResponse(
        status_code=406,
        text="Not Acceptable: the server cannot produce a response matching the Accept header",
        headers={
            "Content-Type": "text/html",
            "Server": "nginx",
            "Set-Cookie": "session=super-secret-cookie-value",  # must never surface
        },
    )

    with patch("src.exportbot.extraction_diagnostics.requests.get", return_value=fake) as mock_get:
        result = probe_with_accept_header(_session(), "view-guid-1", accept="text/csv")

    assert result.status_code == 406
    assert result.accept_header_sent == "text/csv"
    assert "Not Acceptable" in result.error_body
    assert "Set-Cookie" not in result.safe_headers
    assert "session=super-secret-cookie-value" not in str(result)
    assert result.safe_headers["Server"] == "nginx"

    call = mock_get.call_args
    assert call.kwargs["headers"]["Accept"] == "text/csv"
    assert call.kwargs["headers"]["X-Tableau-Auth"] == "fake-token"


def test_probe_omits_accept_header_when_none_given():
    fake = FakeResponse(status_code=200, text="a,b\n1,2\n", headers={"Content-Type": "text/csv"})

    with patch("src.exportbot.extraction_diagnostics.requests.get", return_value=fake) as mock_get:
        result = probe_with_accept_header(_session(), "view-guid-1", accept=None)

    assert result.status_code == 200
    assert result.accept_header_sent == "(none)"
    assert result.error_body is None

    call = mock_get.call_args
    assert "Accept" not in call.kwargs["headers"]


def test_probe_does_not_raise_on_network_error():
    with patch(
        "src.exportbot.extraction_diagnostics.requests.get",
        side_effect=requests.ConnectionError("boom"),
    ):
        result = probe_with_accept_header(_session(), "view-guid-1", accept="text/csv")

    assert result.status_code == 0
    assert "network error" in result.error_body


def test_safe_response_header_allowlist_excludes_cookies_and_auth_material():
    assert "set-cookie" not in SAFE_RESPONSE_HEADER_NAMES
    assert "x-tableau-auth" not in SAFE_RESPONSE_HEADER_NAMES
    assert "authorization" not in SAFE_RESPONSE_HEADER_NAMES
