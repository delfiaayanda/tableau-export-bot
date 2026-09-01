"""Shared exception types for the export pipeline."""


class ExportBotError(Exception):
    """Base class for all exportbot errors."""


class AuthenticationError(ExportBotError):
    """Raised when Tableau authentication fails."""


class ExtractionError(ExportBotError):
    """Raised when retrieving data from Tableau fails."""


class ValidationError(ExportBotError):
    """Raised when exported data fails validation checks."""
