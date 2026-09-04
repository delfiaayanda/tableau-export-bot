"""Tests check_session()/export_view_data_summary_to_csv()/fetch_tempfile()
using mocked HTTP calls -- no real network call, no claim that this
reproduces the live server's behavior (see CLAUDE.md). Confirms the
export result key and any session/token value are never surfaced.
"""

import json
from unittest.mock import patch

import requests

from src.exportbot.vizql_crosstab_probe import (
    VizqlSessionContext,
    check_session,
    export_view_data_summary_to_csv,
    fetch_tempfile,
)

FAKE_SESSION_ID = "test-session-id-not-real"
FAKE_RESULT_KEY = "9999999999"  # distinctive synthetic value, never a real one


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", content=b"", headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


def _ctx() -> VizqlSessionContext:
    return VizqlSessionContext(
        server_url="https://insight.example.com",
        workbook_content_url="ReturnRotate",
        view_content_url="Dashboard1",
        session_id=FAKE_SESSION_ID,
        visual_id_pres_model="fake-visual-id",
        columns_json=json.dumps(["ColA", "ColB"]),
    )


def _export_response_body(result_key: str) -> dict:
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


def test_check_session_reports_success_and_uses_multipart_fields():
    fake = FakeResponse(status_code=200)

    with patch("src.exportbot.vizql_crosstab_probe.requests.post", return_value=fake) as mock_post:
        result = check_session(_ctx())

    assert result.status_code == 200
    call = mock_post.call_args
    assert FAKE_SESSION_ID in call.args[0]
    assert "get-view-data-csvexport-settings-server" in call.args[0]
    assert "telemetryCommandId" in call.kwargs["files"]


def test_check_session_reports_non_200():
    fake = FakeResponse(status_code=440, text="Session expired")

    with patch("src.exportbot.vizql_crosstab_probe.requests.post", return_value=fake):
        result = check_session(_ctx())

    assert result.status_code == 440
    assert "Session expired" in result.error_body


def test_export_view_data_extracts_result_key_without_exposing_it_elsewhere():
    fake = FakeResponse(status_code=200, json_body=_export_response_body(FAKE_RESULT_KEY))

    with patch("src.exportbot.vizql_crosstab_probe.requests.post", return_value=fake) as mock_post:
        result, result_key, filename, mime_type = export_view_data_summary_to_csv(_ctx())

    assert result.status_code == 200
    assert result_key == FAKE_RESULT_KEY  # returned internally for the caller to use
    assert filename == "Persentase Nasional_data.csv"
    assert mime_type == "text/csv"
    assert FAKE_RESULT_KEY not in str(result)  # never embedded in the safe result object

    call = mock_post.call_args
    fields = call.kwargs["files"]
    assert set(fields.keys()) == {
        "visualIdPresModel", "showAliases", "columns",
        "listSeparatorValue", "unicodeEncodingValue", "telemetryCommandId",
    }


def test_export_view_data_handles_malformed_response_shape():
    fake = FakeResponse(status_code=200, json_body={"unexpected": "shape"})

    with patch("src.exportbot.vizql_crosstab_probe.requests.post", return_value=fake):
        result, result_key, filename, mime_type = export_view_data_summary_to_csv(_ctx())

    assert result.status_code == 200
    assert result_key is None
    assert filename is None
    assert mime_type is None


def test_export_view_data_does_not_raise_on_network_error():
    with patch(
        "src.exportbot.vizql_crosstab_probe.requests.post",
        side_effect=requests.ConnectionError("boom"),
    ):
        result, result_key, filename, mime_type = export_view_data_summary_to_csv(_ctx())

    assert result.status_code == 0
    assert result_key is None


def test_fetch_tempfile_reports_metadata_and_uses_result_key_as_query_param():
    fake = FakeResponse(
        status_code=200,
        content=b"col_a,col_b\nvalue1,value2\n",
        headers={
            "Content-Type": "text/csv",
            "Content-Disposition": "attachment; filename*=UTF-8''Persentase%20Nasional_data.csv",
        },
    )

    with patch("src.exportbot.vizql_crosstab_probe.requests.get", return_value=fake) as mock_get:
        result = fetch_tempfile(_ctx(), FAKE_RESULT_KEY)

    assert result.status_code == 200
    # RFC 5987 `filename*=` is percent-encoded; it should be decoded.
    assert result.filename == "Persentase Nasional_data.csv"
    assert result.response_size_bytes == len(b"col_a,col_b\nvalue1,value2\n")
    assert "value1" not in str(result)  # row values never surfaced

    call = mock_get.call_args
    assert call.kwargs["params"]["key"] == FAKE_RESULT_KEY
    assert call.kwargs["params"]["keepfile"] == "yes"
    assert call.kwargs["params"]["attachment"] == "yes"
    assert FAKE_SESSION_ID in call.args[0]


def test_fetch_tempfile_distinguishes_network_error():
    with patch(
        "src.exportbot.vizql_crosstab_probe.requests.get",
        side_effect=requests.ConnectionError("boom"),
    ):
        result = fetch_tempfile(_ctx(), FAKE_RESULT_KEY)

    assert result.status_code == 0
    assert "network error" in result.error_body
