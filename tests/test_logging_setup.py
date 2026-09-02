import logging

from src.exportbot.logging_setup import setup_logging


def _reset_exportbot_logger():
    logger = logging.getLogger("exportbot")
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)


def test_setup_logging_returns_named_logger():
    _reset_exportbot_logger()

    logger = setup_logging()

    assert logger.name == "exportbot"


def test_setup_logging_defaults_to_info_level():
    _reset_exportbot_logger()

    logger = setup_logging()

    assert logger.level == logging.INFO


def test_setup_logging_applies_custom_level():
    _reset_exportbot_logger()

    logger = setup_logging("DEBUG")

    assert logger.level == logging.DEBUG


def test_setup_logging_accepts_lowercase_level():
    _reset_exportbot_logger()

    logger = setup_logging("warning")

    assert logger.level == logging.WARNING


def test_setup_logging_adds_exactly_one_handler():
    _reset_exportbot_logger()

    logger = setup_logging()

    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)


def test_setup_logging_does_not_duplicate_handlers_on_repeat_calls():
    _reset_exportbot_logger()

    setup_logging()
    setup_logging()
    logger = setup_logging()

    assert len(logger.handlers) == 1
