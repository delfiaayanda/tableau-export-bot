"""Excel export: date-based folder, timestamped filename."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd


def export_to_excel(df: pd.DataFrame, export_dir: str, now: datetime | None = None) -> Path:
    """Write df to <export_dir>/<YYYY-MM-DD>/export_<YYYYMMDD_HHMMSS>.xlsx.

    Returns the path written. `now` is injectable for deterministic tests.
    """
    now = now or datetime.now()

    day_folder = Path(export_dir) / now.strftime("%Y-%m-%d")
    day_folder.mkdir(parents=True, exist_ok=True)

    filename = f"export_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
    output_path = day_folder / filename

    df.to_excel(output_path, index=False)
    return output_path
