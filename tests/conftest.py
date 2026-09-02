import pytest


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch):
    """Prevent config.load_config()'s `load_dotenv(override=False)` from
    reading this machine's real .env file during tests.

    Without this, a test that does `monkeypatch.delenv("TABLEAU_USERNAME")`
    to exercise the "missing required variable" error path gets that
    variable silently refilled from the real .env sitting in the repo
    root (which holds real credentials) -- so the test's outcome depends
    on whether the machine running it happens to have a real .env file,
    not on the code under test. Every test in this suite already sets
    the environment variables it needs explicitly via monkeypatch, so
    the real .env is never actually needed here.
    """
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
