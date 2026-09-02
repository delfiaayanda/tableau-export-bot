"""Validation checks applied to extracted data before export."""

from __future__ import annotations

import pandas as pd

from .exceptions import ValidationError


def validate_dataframe(df: pd.DataFrame) -> None:
    """Raise ValidationError if the DataFrame is not fit to export.

    Checks are deliberately generic (not tied to any specific Tableau view
    schema) since no real Tableau response has been observed yet.
    """
    if df is None:
        raise ValidationError("No data returned.")

    if df.empty:
        raise ValidationError("Extracted data is empty.")

    if df.isna().all(axis=None):
        raise ValidationError("Extracted data contains only null values.")
