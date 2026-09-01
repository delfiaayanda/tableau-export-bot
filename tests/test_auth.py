from src.exportbot.auth import DEFAULT_API_VERSION, TableauServerAuthenticator


def test_default_api_version_matches_confirmed_server_version():
    assert DEFAULT_API_VERSION == "3.29"


def test_api_version_is_configurable_not_fixed():
    authenticator = TableauServerAuthenticator(api_version="9.9")
    assert authenticator.api_version == "9.9"
