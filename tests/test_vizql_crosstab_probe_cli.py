"""Tests the vizql-crosstab-probe CLI's reporting using mocked HTTP calls
-- no real network call, no claim that live reproduction has been
verified. Confirms the session id, export result key, and file contents
never appear in the reported output, and that no network call is made at
all when the required ephemeral context isn't provided.
"""

import json
from unittest.mock import patch

from vizql_crosstab_probe import run_probe

FAKE_SESSION_ID = "test-session-id-not-real"
FAKE_RESULT_KEY = "8888888888"
SERVER_URL = "https://insight.example.com"


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._json_body


def _set_context_env(monkeypatch):
    monkeypatch.setenv("TABLEAU_VIZQL_SESSION_ID", FAKE_SESSION_ID)
    monkeypatch.setenv("TABLEAU_VIZQL_VISUAL_ID_PRES_MODEL", "fake-visual-id")
    monkeypatch.setenv("TABLEAU_VIZQL_COLUMNS_JSON", json.dumps(["ColA", "ColB"]))


def _clear_context_env(monkeypatch):
    for key in (
        "TABLEAU_VIZQL_SESSION_ID",
        "TABLEAU_VIZQL_VISUAL_ID_PRES_MODEL",
        "TABLEAU_VIZQL_COLUMNS_JSON",
    ):
        monkeypatch.delenv(key, raising=False)


def _export_body(result_key: str) -> dict:
    return {
        "vqlCmdResponse": {
            "layoutStatus": {
                "applicationPresModel": {
                    "presentationLayerNotification": [
                        {
                            "presModelHolder": {
                                "genExportFilePresModel": {
                                    "resultKey": result_key,
                                    "fileName": "Persentase Nasional_data.csv",
                                    "mimeType": "text/csv",
                                }
                            }
                        }
                    ]
                }
            }
        }
    }


def test_run_probe_reports_no_session_without_any_network_call_when_context_missing(monkeypatch):
    _clear_context_env(monkeypatch)

    with patch("src.exportbot.vizql_crosstab_probe.requests.post") as mock_post:
        with patch("src.exportbot.vizql_crosstab_probe.requests.get") as mock_get:
            success, lines = run_probe(SERVER_URL)

    assert success is False
    assert any("session established: no" in line for line in lines)
    mock_post.assert_not_called()
    mock_get.assert_not_called()


def test_run_probe_reports_full_success_without_leaking_session_or_key(monkeypatch):
    _set_context_env(monkeypatch)

    check_response = FakeResponse(status_code=200)
    export_response = FakeResponse(status_code=200, json_body=_export_body(FAKE_RESULT_KEY))
    tempfile_response = FakeResponse(
        status_code=200,
        content=b"col_a,col_b\nsecretvalue,other\n",
        headers={
            "Content-Type": "text/csv",
            "Content-Disposition": 'attachment; filename="Persentase Nasional_data.csv"',
        },
    )

    with patch(
        "src.exportbot.vizql_crosstab_probe.requests.post",
        side_effect=[check_response, export_response],
    ):
        with patch("src.exportbot.vizql_crosstab_probe.requests.get", return_value=tempfile_response):
            success, lines = run_probe(SERVER_URL)

    assert success is True
    joined = "\n".join(lines)

    assert "session established: yes" in joined
    assert "export request: HTTP 200" in joined
    assert "tempfile request: HTTP 200" in joined
    assert "Persentase Nasional_data.csv" in joined

    # Never leaked, under any circumstance:
    assert FAKE_SESSION_ID not in joined
    assert FAKE_RESULT_KEY not in joined
    assert "secretvalue" not in joined


def test_run_probe_stops_after_failed_session_check(monkeypatch):
    _set_context_env(monkeypatch)
    check_response = FakeResponse(status_code=440, text="Session expired")

    with patch("src.exportbot.vizql_crosstab_probe.requests.post", return_value=check_response) as mock_post:
        success, lines = run_probe(SERVER_URL)

    assert success is False
    assert any("session established: no" in line for line in lines)
    assert mock_post.call_count == 1  # never attempted the export command


def test_run_probe_stops_after_export_failure_without_calling_tempfile(monkeypatch):
    _set_context_env(monkeypatch)
    check_response = FakeResponse(status_code=200)
    export_response = FakeResponse(status_code=403, text="Forbidden")

    with patch(
        "src.exportbot.vizql_crosstab_probe.requests.post",
        side_effect=[check_response, export_response],
    ):
        with patch("src.exportbot.vizql_crosstab_probe.requests.get") as mock_get:
            success, lines = run_probe(SERVER_URL)

    assert success is False
    assert any("export request: HTTP 403" in line for line in lines)
    mock_get.assert_not_called()
