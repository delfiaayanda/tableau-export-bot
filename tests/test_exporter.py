from datetime import datetime

import pandas as pd

from src.exportbot.exporter import export_to_excel


def test_export_to_excel_creates_date_folder_and_timestamped_file(tmp_path):
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    fixed_now = datetime(2026, 9, 1, 14, 30, 45)

    output_path = export_to_excel(df, str(tmp_path), now=fixed_now)

    assert output_path.parent.name == "2026-09-01"
    assert output_path.name == "export_20260901_143045.xlsx"
    assert output_path.exists()


def test_export_to_excel_writes_readable_data(tmp_path):
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})

    output_path = export_to_excel(df, str(tmp_path), now=datetime(2026, 9, 1, 0, 0, 0))
    result = pd.read_excel(output_path)

    pd.testing.assert_frame_equal(result, df)
