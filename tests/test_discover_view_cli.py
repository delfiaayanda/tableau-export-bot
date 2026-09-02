"""Tests the view-discovery CLI's success/failure reporting via mocked
HTTP calls -- no real network call is made. Confirms credentials and the
auth token never appear in the reported message.
"""

from unittest.mock import patch

from discover_view import run_view_discovery
from src.exportbot.config import Config

USERNAME = "smoke-test-user"
PASSWORD = "smoke-test-password"
FAKE_TOKEN = "super-secret-token-value"


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        return self._json_body


def _config() -> Config:
    return Config(
        tableau_server_url="https://insight.example.com",
        tableau_site="",
        tableau_username=USERNAME,
        tableau_password=PASSWORD,
        tableau_api_version="3.29",  # skip serverinfo auto-detection
        view_id="",
        export_dir="exports",
        log_level="INFO",
    )


def _signin_response() -> FakeResponse:
    return FakeResponse(
        status_code=200,
        json_body={"credentials": {"token": FAKE_TOKEN, "site": {"id": "site-1"}}},
    )


def test_run_view_discovery_reports_success_without_leaking_secrets():
    views_response = FakeResponse(
        status_code=200,
        json_body={
            "views": {
                "view": [{"id": "view-guid-1", "name": "Dashboard1", "workbook": {"id": "wb-1"}}]
            }
        },
    )
    workbook_response = FakeResponse(status_code=200, json_body={"workbook": {"name": "ReturnRotate"}})

    def fake_get(url, **kwargs):
        return workbook_response if "/workbooks/" in url else views_response

    with patch("src.exportbot.auth.requests.post", return_value=_signin_response()):
        with patch("src.exportbot.view_discovery.requests.get", side_effect=fake_get):
            success, message = run_view_discovery(_config())

    assert success is True
    assert "view-guid-1" in message
    assert "ReturnRotate" in message
    assert "Dashboard1" in message
    assert USERNAME not in message
    assert PASSWORD not in message
    assert FAKE_TOKEN not in message


def test_run_view_discovery_reports_sanitized_failure_when_not_found():
    views_response = FakeResponse(status_code=200, json_body={"views": {"view": []}})

    with patch("src.exportbot.auth.requests.post", return_value=_signin_response()):
        with patch("src.exportbot.view_discovery.requests.get", return_value=views_response):
            success, message = run_view_discovery(_config())

    assert success is False
    assert "No view found" in message


def test_run_view_discovery_reports_sanitized_auth_failure():
    signin_failure = FakeResponse(status_code=401, text="Login Error")

    with patch("src.exportbot.auth.requests.post", return_value=signin_failure):
        success, message = run_view_discovery(_config())

    assert success is False
    assert "401" in message
    assert USERNAME not in message
    assert PASSWORD not in message
