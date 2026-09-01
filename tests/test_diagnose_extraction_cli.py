"""Tests the extraction-diagnosis CLI's reporting across multiple Accept
header variants, using mocked HTTP calls -- no real network call, no
claim that the real 406 has been diagnosed live. Confirms credentials,
the auth token, and row values never appear in any reported line.
"""

from unittest.mock import patch

import requests

from diagnose_extraction import run_diagnosis
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


def test_run_diagnosis_reports_one_line_per_variant_without_leaking_secrets():
    def fake_get(url, headers, timeout):
        # Reproduce the reported live pattern exactly: explicit Accept:
        # text/csv gets a fast 406, every other Accept variant hangs
        # until timeout -- so the diagnosis can surface that asymmetry
        # instead of assuming a simple content-negotiation fix.
        if headers.get("Accept") == "text/csv":
            return FakeResponse(status_code=406, text="Not Acceptable", headers={"Content-Type": "text/html"})
        raise requests.Timeout("timed out")

    with patch("src.exportbot.auth.requests.post", return_value=_signin_response()):
        with patch("src.exportbot.extraction_diagnostics.requests.get", side_effect=fake_get):
            success, lines = run_diagnosis(_config(), VIEW_ID)

    assert success is True

    # Both candidate endpoints are tried, not just "Query View Data".
    assert any("Query View Data" in line for line in lines)
    assert any("Query View Crosstab Excel" in line for line in lines)

    text_csv_line = next(line for line in lines if "Accept=text/csv" in line)
    assert "HTTP 406" in text_csv_line

    none_line = next(line for line in lines if "Accept=(none)" in line)
    assert "timeout" in none_line.lower()

    joined = "\n".join(lines)
    assert USERNAME not in joined
    assert PASSWORD not in joined
    assert FAKE_TOKEN not in joined
    assert "West" not in joined  # row values must never appear


def test_run_diagnosis_reports_sanitized_auth_failure():
    signin_failure = FakeResponse(status_code=401, text="Login Error")

    with patch("src.exportbot.auth.requests.post", return_value=signin_failure):
        success, lines = run_diagnosis(_config(), VIEW_ID)

    assert success is False
    assert any("401" in line for line in lines)
    assert not any(USERNAME in line or PASSWORD in line for line in lines)
