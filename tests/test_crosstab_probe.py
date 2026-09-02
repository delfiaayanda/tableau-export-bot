"""Tests probe_crosstab_excel()'s handling of different HTTP responses
using mocked requests.get -- no real network call, no claim that this
matches the live server or the browser's actual "Download -> Crosstab"
behavior (see CLAUDE.md). Confirms the response body is never surfaced,
only metadata about it.
"""

import requests
from unittest.mock import patch

from src.exportbot.auth import TableauSession
from src.exportbot.crosstab_probe import probe_crosstab_excel


class FakeResponse:
    def __init__(self, status_code=200, content=b"", text="", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = text
        self.headers = headers or {}


def _session() -> TableauSession:
    return TableauSession(
        auth_token="fake-token",
        site_id="site-1",
        server_url="https://insight.example.com",
        api_version="3.29",
    )


def test_probe_crosstab_reports_metadata_and_filename_on_success():
    fake_bytes = b"pretend-xlsx-bytes-not-real-content" * 10
    fake = FakeResponse(
        status_code=200,
        content=fake_bytes,
        headers={
            "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "Content-Disposition": 'attachment; filename="Dashboard 1.xlsx"',
        },
    )

    with patch("src.exportbot.crosstab_probe.requests.get", return_value=fake) as mock_get:
        result = probe_crosstab_excel(_session(), "view-guid-1")

    assert result.status_code == 200
    assert result.filename == "Dashboard 1.xlsx"
    assert result.response_size_bytes == len(fake_bytes)
    assert "pretend-xlsx-bytes" not in str(result)  # body contents never surfaced

    call = mock_get.call_args
    assert call.args[0] == "https://insight.example.com/api/3.29/sites/site-1/views/view-guid-1/crosstab/excel"
    assert call.kwargs["headers"]["X-Tableau-Auth"] == "fake-token"
    assert "Accept" not in call.kwargs["headers"]


def test_probe_crosstab_extracts_rfc5987_filename():
    fake = FakeResponse(
        status_code=200,
        content=b"x",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''Return%20%26%20Rotate.xlsx"},
    )

    with patch("src.exportbot.crosstab_probe.requests.get", return_value=fake):
        result = probe_crosstab_excel(_session(), "view-guid-1")

    assert result.filename == "Return%20%26%20Rotate.xlsx"


def test_probe_crosstab_reports_error_body_on_failure_without_size():
    fake = FakeResponse(status_code=403, text="Forbidden: missing Export Data permission")

    with patch("src.exportbot.crosstab_probe.requests.get", return_value=fake):
        result = probe_crosstab_excel(_session(), "view-guid-1")

    assert result.status_code == 403
    assert "Forbidden" in result.error_body
    assert result.response_size_bytes is None


def test_probe_crosstab_distinguishes_timeout():
    with patch(
        "src.exportbot.crosstab_probe.requests.get",
        side_effect=requests.Timeout("timed out"),
    ):
        result = probe_crosstab_excel(_session(), "view-guid-1", timeout=10.0)

    assert result.status_code == 0
    assert "timeout" in result.error_body.lower()


def test_probe_crosstab_does_not_raise_on_network_error():
    with patch(
        "src.exportbot.crosstab_probe.requests.get",
        side_effect=requests.ConnectionError("boom"),
    ):
        result = probe_crosstab_excel(_session(), "view-guid-1")

    assert result.status_code == 0
    assert "network error" in result.error_body
