"""Orchestrates auth -> extraction -> validation -> export.

Kept separate from main.py so it can be exercised in tests with fake
Authenticator/TableauClient implementations, without any network access.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .auth import Authenticator
from .config import Config, require_view_id
from .exporter import export_to_excel
from .tableau_client import TableauClient
from .validation import validate_dataframe


def run_pipeline(
    config: Config,
    authenticator: Authenticator,
    client_factory,
    logger: logging.Logger,
) -> Path:
    """Run the full export pipeline.

    client_factory: callable(TableauSession) -> TableauClient, since the
    client depends on the session produced by authentication.
    """
    require_view_id(config)

    logger.info("Authenticating with Tableau...")
    session = authenticator.authenticate(config)

    logger.info("Retrieving view data (view_id=%s)...", config.view_id)
    client: TableauClient = client_factory(session)
    df = client.get_view_data(config.view_id)

    logger.info("Validating extracted data (%d rows, %d columns)...", *df.shape)
    validate_dataframe(df)

    logger.info("Exporting to Excel...")
    output_path = export_to_excel(df, config.export_dir)
    logger.info("Export complete: %s", output_path)

    return output_path
