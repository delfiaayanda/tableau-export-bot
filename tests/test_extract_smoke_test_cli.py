"""Tests the extraction smoke-test CLI's success/failure reporting via
mocked HTTP calls -- no real network call, no claim that live extraction
has been verified. Confirms credentials, the auth token, and row values
never appear in the reported message.
"""

from unittest.mock import patch

from extract_smoke_test import run_extraction_smoke_test
from src.exportbot.config import Config

USERNAME = "smoke-test-user"
PASSWORD = "smoke-test-password"
FAKE_TOKEN = "super-secret-token-value"
VIEW_ID = "741ce0df-8818-4412-93ca-b34c03019219"


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_body


def _config() -> Config:
    return Config(
        tableau_server_url="https://insight.example.com",
        tableau_site="",
        tableau_username=USERNAME,
        tableau_password=PASSWORD,
        tableau_api_version="3.29",  # skip serverinfo auto-detection
        view_id=VIEW_ID,
        export_dir="exports",
        log_level="INFO",
    )


def _signin_response() -> FakeResponse:
    return FakeResponse(
        status_code=200,
        json_body={"credentials": {"token": FAKE_TOKEN, "site": {"id": "site-1"}}},
    )


def test_run_extraction_smoke_test_reports_success_without_row_values_or_secrets():
    view_data_response = FakeResponse(
        status_code=200,
        text="Region,Amount,CustomerName\nWest,100,Jane Doe\n",
        headers={"Content-Type": "text/csv"},
    )

    with patch("src.exportbot.auth.requests.post", return_value=_signin_response()):
        with patch("src.exportbot.extraction_probe.requests.get", return_value=view_data_response):
            success, message = run_extraction_smoke_test(_config(), VIEW_ID)

    assert success is True
    assert "HTTP 200" in message
    assert "text/csv" in message
    assert "rows=1" in message
    assert "columns=3" in message
    assert "Region" in message and "Amount" in message and "CustomerName" in message
    # row values must never appear, only column names
    assert "Jane Doe" not in message
    assert "West" not in message
    assert USERNAME not in message
    assert PASSWORD not in message
    assert FAKE_TOKEN not in message


def test_run_extraction_smoke_test_reports_sanitized_failure_and_names_endpoint():
    view_data_response = FakeResponse(status_code=403, text="Forbidden")

    with patch("src.exportbot.auth.requests.post", return_value=_signin_response()):
        with patch("src.exportbot.extraction_probe.requests.get", return_value=view_data_response):
            success, message = run_extraction_smoke_test(_config(), VIEW_ID)

    assert success is False
    assert "403" in message
    assert f"views/{VIEW_ID}/data" in message
    assert USERNAME not in message
    assert PASSWORD not in message


def test_run_extraction_smoke_test_reports_sanitized_auth_failure():
    signin_failure = FakeResponse(status_code=401, text="Login Error")

    with patch("src.exportbot.auth.requests.post", return_value=signin_failure):
        success, message = run_extraction_smoke_test(_config(), VIEW_ID)

    assert success is False
    assert "401" in message
    assert USERNAME not in message
    assert PASSWORD not in message
