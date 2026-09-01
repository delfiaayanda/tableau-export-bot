"""Discover the Tableau REST API version from the unauthenticated
`serverinfo` endpoint, so callers don't have to hardcode or guess it.

No sign-in is required for this endpoint. It also does not require the
version in the request path to match the server's actual version --
confirmed against the real server: requesting `/api/3.22/serverinfo`
still returned the server's real restApiVersion (3.29).
"""

from __future__ import annotations

import requests

from .exceptions import ExtractionError

# Any accepted path version works; the server reports its real version
# regardless (see module docstring). Kept low as it's the most widely
# supported value across Tableau Server releases.
PROBE_API_VERSION = "2.4"


def discover_api_version(server_url: str, timeout: float = 10.0) -> str:
    """Return the server's real REST API version (e.g. "3.29")."""
    url = f"{server_url.rstrip('/')}/api/{PROBE_API_VERSION}/serverinfo"

    try:
        response = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout)
    except requests.RequestException as exc:
        raise ExtractionError(f"Could not reach Tableau server at {url}: {exc}") from exc

    if response.status_code != 200:
        raise ExtractionError(
            f"serverinfo request failed with status {response.status_code}: {response.text[:500]}"
        )

    try:
        rest_api_version = response.json()["serverInfo"]["restApiVersion"]
    except (ValueError, KeyError) as exc:
        raise ExtractionError(f"Unexpected serverinfo response shape: {exc}") from exc

    if not rest_api_version:
        raise ExtractionError("serverinfo response did not include a restApiVersion")

    return rest_api_version
