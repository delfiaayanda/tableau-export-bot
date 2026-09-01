import pandas as pd
import pytest

from src.exportbot.exceptions import ValidationError
from src.exportbot.validation import validate_dataframe


def test_validate_dataframe_accepts_valid_data():
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    validate_dataframe(df)  # should not raise


def test_validate_dataframe_rejects_none():
    with pytest.raises(ValidationError, match="No data"):
        validate_dataframe(None)


def test_validate_dataframe_rejects_empty():
    with pytest.raises(ValidationError, match="empty"):
        validate_dataframe(pd.DataFrame())


def test_validate_dataframe_rejects_all_null():
    df = pd.DataFrame({"a": [None, None], "b": [None, None]})
    with pytest.raises(ValidationError, match="null"):
        validate_dataframe(df)
