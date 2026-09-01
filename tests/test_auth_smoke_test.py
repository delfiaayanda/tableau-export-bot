"""Tests the auth smoke test's success/failure reporting using mocked HTTP
responses -- no real network call, no claim that real sign-in works. Also
guards the "never print secrets" requirement: asserts the configured
username/password/token never appear in the returned message.
"""

from unittest.mock import patch

from auth_smoke_test import run_smoke_test
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


def _make_config() -> Config:
    return Config(
        tableau_server_url="https://insight.example.com",
        tableau_site="",
        tableau_username=USERNAME,
        tableau_password=PASSWORD,
        tableau_api_version="3.29",
        view_id="view-1",
        export_dir="exports",
        log_level="INFO",
    )


def test_run_smoke_test_reports_success_without_leaking_token():
    fake = FakeResponse(
        status_code=200,
        json_body={"credentials": {"token": FAKE_TOKEN, "site": {"id": "site-1"}}},
    )

    with patch("src.exportbot.auth.requests.post", return_value=fake):
        success, message = run_smoke_test(_make_config())

    assert success is True
    assert "3.29" in message
    assert FAKE_TOKEN not in message
    assert USERNAME not in message
    assert PASSWORD not in message


def test_run_smoke_test_reports_sanitized_failure():
    fake = FakeResponse(status_code=401, text="Login Error: Invalid username or password")

    with patch("src.exportbot.auth.requests.post", return_value=fake):
        success, message = run_smoke_test(_make_config())

    assert success is False
    assert "401" in message
    assert USERNAME not in message
    assert PASSWORD not in message


def test_run_smoke_test_redacts_username_if_server_echoes_it():
    fake = FakeResponse(status_code=401, text=f"Login Error for user {USERNAME}")

    with patch("src.exportbot.auth.requests.post", return_value=fake):
        success, message = run_smoke_test(_make_config())

    assert success is False
    assert USERNAME not in message
    assert "[REDACTED]" in message


def test_run_smoke_test_does_not_call_authenticate_when_version_discovery_fails():
    config = Config(
        tableau_server_url="https://insight.example.com",
        tableau_site="",
        tableau_username=USERNAME,
        tableau_password=PASSWORD,
        tableau_api_version="",  # force auto-detection
        view_id="view-1",
        export_dir="exports",
        log_level="INFO",
    )
    fake = FakeResponse(status_code=503, text="Service Unavailable")

    with patch("src.exportbot.serverinfo.requests.get", return_value=fake):
        with patch("src.exportbot.auth.requests.post") as mock_post:
            success, message = run_smoke_test(config)

    assert success is False
    assert "503" in message
    mock_post.assert_not_called()
