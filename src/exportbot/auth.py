"""Tableau authentication.

Defines the authenticator interface used by the rest of the pipeline, plus
a real Tableau Server REST API implementation.

NOTE: TableauServerAuthenticator has NOT been exercised against a live
server. Network access to the target Tableau instance is blocked from this
development environment (see CLAUDE.md). The sign-in request is built to
match the documented Tableau REST API (`POST /api/{version}/auth/signin`),
but it must be validated from a machine that can actually reach the server
before being relied on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

from .config import Config
from .exceptions import AuthenticationError

DEFAULT_API_VERSION = "3.22"


@dataclass(frozen=True)
class TableauSession:
    """An authenticated Tableau session."""

    auth_token: str
    site_id: str
    server_url: str
    api_version: str


class Authenticator(ABC):
    """Interface for obtaining an authenticated Tableau session."""

    @abstractmethod
    def authenticate(self, config: Config) -> TableauSession:
        raise NotImplementedError


class TableauServerAuthenticator(Authenticator):
    """Signs in to Tableau Server/Online via the REST API.

    Untested against a real server from this environment (network to the
    target host is blocked). Verify against the real instance before
    production use.
    """

    def __init__(self, api_version: str = DEFAULT_API_VERSION, timeout: float = 30.0):
        self.api_version = api_version
        self.timeout = timeout

    def authenticate(self, config: Config) -> TableauSession:
        url = f"{config.tableau_server_url.rstrip('/')}/api/{self.api_version}/auth/signin"
        payload = {
            "credentials": {
                "name": config.tableau_username,
                "password": config.tableau_password,
                "site": {"contentUrl": config.tableau_site},
            }
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AuthenticationError(f"Could not reach Tableau server at {url}: {exc}") from exc

        if response.status_code != 200:
            raise AuthenticationError(
                f"Tableau sign-in failed with status {response.status_code}: {response.text[:500]}"
            )

        try:
            body = response.json()
            credentials = body["credentials"]
            auth_token = credentials["token"]
            site_id = credentials["site"]["id"]
        except (ValueError, KeyError) as exc:
            raise AuthenticationError(f"Unexpected sign-in response shape: {exc}") from exc

        return TableauSession(
            auth_token=auth_token,
            site_id=site_id,
            server_url=config.tableau_server_url,
            api_version=self.api_version,
        )
