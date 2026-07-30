import logging

import pytest

from kb.logging_utils import (
    VALID_LEVELS,
    configure_logging,
    resolve_log_level,
)


@pytest.fixture
def restore_root():
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    yield root
    root.setLevel(level)
    root.handlers = handlers


def test_resolve_defaults_to_info(monkeypatch):
    monkeypatch.delenv("CITADEL_LOG_LEVEL", raising=False)
    assert resolve_log_level() == "INFO"


def test_resolve_normalizes_case_and_whitespace(monkeypatch):
    monkeypatch.delenv("CITADEL_LOG_LEVEL", raising=False)
    assert resolve_log_level("  debug ") == "DEBUG"


def test_resolve_invalid_falls_back_to_info(monkeypatch):
    monkeypatch.delenv("CITADEL_LOG_LEVEL", raising=False)
    assert resolve_log_level("verbose") == "INFO"


def test_resolve_reads_env_when_no_argument(monkeypatch):
    monkeypatch.setenv("CITADEL_LOG_LEVEL", "warning")
    assert resolve_log_level() == "WARNING"


def test_explicit_argument_overrides_env(monkeypatch):
    monkeypatch.setenv("CITADEL_LOG_LEVEL", "ERROR")
    assert resolve_log_level("debug") == "DEBUG"


def test_every_valid_level_round_trips(monkeypatch):
    monkeypatch.delenv("CITADEL_LOG_LEVEL", raising=False)
    for level in VALID_LEVELS:
        assert resolve_log_level(level.lower()) == level


def test_configure_logging_sets_root_level(restore_root, monkeypatch):
    monkeypatch.delenv("CITADEL_LOG_LEVEL", raising=False)
    configure_logging("debug")
    assert restore_root.level == logging.DEBUG


def test_configure_logging_is_idempotent_on_handlers(restore_root, monkeypatch):
    monkeypatch.delenv("CITADEL_LOG_LEVEL", raising=False)
    restore_root.handlers = []
    configure_logging("info")
    configure_logging("info")  # second call must not add another handler
    assert len(restore_root.handlers) == 1
    assert restore_root.level == logging.INFO
