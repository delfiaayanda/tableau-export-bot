"""Tests discover_api_version()'s handling of HTTP responses shaped like
Tableau's documented serverinfo JSON. This does NOT claim these shapes
match the live server byte-for-byte, and makes no real network call —
requests.get is mocked purely to test our own parsing/error-handling
logic. See CLAUDE.md for what remains to verify against the real server.
"""

from unittest.mock import patch

import pytest

from src.exportbot.exceptions import ExtractionError
from src.exportbot.serverinfo import discover_api_version


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


def test_discover_api_version_parses_documented_shape():
    fake = FakeResponse(
        status_code=200,
        json_body={
            "serverInfo": {
                "productVersion": {"value": "2026.2.0"},
                "restApiVersion": "3.29",
            }
        },
    )

    with patch("src.exportbot.serverinfo.requests.get", return_value=fake) as mock_get:
        version = discover_api_version("https://insight.example.com")

    assert version == "3.29"
    called_url = mock_get.call_args.args[0]
    assert called_url == "https://insight.example.com/api/2.4/serverinfo"


def test_discover_api_version_rejects_non_200():
    fake = FakeResponse(status_code=503, text="Service Unavailable")

    with patch("src.exportbot.serverinfo.requests.get", return_value=fake):
        with pytest.raises(ExtractionError, match="503"):
            discover_api_version("https://insight.example.com")


def test_discover_api_version_rejects_malformed_body():
    fake = FakeResponse(status_code=200, json_body={"unexpected": "shape"})

    with patch("src.exportbot.serverinfo.requests.get", return_value=fake):
        with pytest.raises(ExtractionError, match="Unexpected serverinfo response shape"):
            discover_api_version("https://insight.example.com")


def test_discover_api_version_strips_trailing_slash_from_server_url():
    fake = FakeResponse(
        status_code=200,
        json_body={"serverInfo": {"restApiVersion": "3.29"}},
    )

    with patch("src.exportbot.serverinfo.requests.get", return_value=fake) as mock_get:
        discover_api_version("https://insight.example.com/")

    called_url = mock_get.call_args.args[0]
    assert called_url == "https://insight.example.com/api/2.4/serverinfo"
