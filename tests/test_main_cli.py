"""Tests main.py's delegation to the browser-automated export -- the only
extraction method confirmed working against the real server (see
CLAUDE.md). Mocks run_browser_crosstab_main entirely -- no real browser,
no real network call.
"""

from unittest.mock import patch

from main import main


def test_main_delegates_to_browser_crosstab_main():
    with patch("main.run_browser_crosstab_main", return_value=0) as mock_run:
        exit_code = main()

    mock_run.assert_called_once_with()
    assert exit_code == 0


def test_main_propagates_the_browser_crosstab_exit_code():
    with patch("main.run_browser_crosstab_main", return_value=1):
        exit_code = main()

    assert exit_code == 1
