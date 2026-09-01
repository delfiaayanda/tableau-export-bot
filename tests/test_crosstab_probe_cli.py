"""Tests the crosstab-probe CLI's reporting via mocked HTTP calls -- no
real network call, no claim that live extraction has been verified.
Confirms credentials, the auth token, and file contents never appear in
the reported message.
"""

from unittest.mock import patch

from crosstab_probe import run_crosstab_probe
from src.exportbot.config import Config

USERNAME = "smoke-test-user"
PASSWORD = "smoke-test-password"
FAKE_TOKEN = "super-secret-token-value"
VIEW_ID = "741ce0df-8818-4412-93ca-b34c03019219"


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json_body


def _config() -> Config:
    return Config(
        tableau_server_url="https://insight.example.com",
        tableau_site="",
        tableau_username=USERNAME,
        tableau_password=PASSWORD,
        tableau_api_version="3.29",
        view_id=VIEW_ID,
        export_dir="exports",
        log_level="INFO",
    )


def _signin_response() -> FakeResponse:
    return FakeResponse(
        status_code=200,
        json_body={"credentials": {"token": FAKE_TOKEN, "site": {"id": "site-1"}}},
    )


def test_run_crosstab_probe_reports_success_without_leaking_secrets_or_body():
    crosstab_response = FakeResponse(
        status_code=200,
        content=b"fake-xlsx-bytes-with-a-customer-name-in-it",
        headers={
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Content-Disposition": 'attachment; filename="Dashboard 1.xlsx"',
        },
    )

    with patch("src.exportbot.auth.requests.post", return_value=_signin_response()):
        with patch("src.exportbot.crosstab_probe.requests.get", return_value=crosstab_response):
            success, message = run_crosstab_probe(_config(), VIEW_ID)

    assert success is True
    assert "HTTP 200" in message
    assert "Dashboard 1.xlsx" in message
    assert "size_bytes=" in message
    assert USERNAME not in message
    assert PASSWORD not in message
    assert FAKE_TOKEN not in message
    assert "customer-name" not in message


def test_run_crosstab_probe_reports_sanitized_failure():
    crosstab_response = FakeResponse(status_code=403, text="Forbidden")

    with patch("src.exportbot.auth.requests.post", return_value=_signin_response()):
        with patch("src.exportbot.crosstab_probe.requests.get", return_value=crosstab_response):
            success, message = run_crosstab_probe(_config(), VIEW_ID)

    assert success is False
    assert "403" in message
    assert "crosstab/excel" in message
    assert USERNAME not in message
    assert PASSWORD not in message


def test_run_crosstab_probe_reports_sanitized_auth_failure():
    signin_failure = FakeResponse(status_code=401, text="Login Error")

    with patch("src.exportbot.auth.requests.post", return_value=signin_failure):
        success, message = run_crosstab_probe(_config(), VIEW_ID)

    assert success is False
    assert "401" in message
    assert USERNAME not in message
    assert PASSWORD not in message
