import logging

import pytest

from kb.logging_utils import (
    VALID_LEVELS,
    configure_logging,
    resolve_log_level,
    safe_log_value,
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


# --------------------------------------------------------------------------
# safe_log_value: untrusted text that has to appear in a log line
# --------------------------------------------------------------------------


def test_safe_log_value_escapes_a_line_break_instead_of_emitting_it():
    """A value carrying a newline would otherwise write a second log line.

    This project reads its own logs as evidence, so a caller-supplied value that
    can start a line can put a sentence of its choosing into that evidence.
    """
    forged = "notes\n2026-08-04 INFO kb.service Ingest accepted for dataset central"
    out = safe_log_value(forged)

    assert "\n" not in out
    assert "\\n" in out
    # Escaped, not deleted: two different inputs must not collapse to one string.
    assert "Ingest accepted" in out
    assert safe_log_value("a\nb") != safe_log_value("ab")


def test_safe_log_value_escapes_carriage_returns_tabs_and_other_control_bytes():
    assert safe_log_value("a\rb\tc\x00d\x1be") == "a\\rb\\tc\\x00d\\x1be"


def test_safe_log_value_bounds_what_it_emits_and_says_how_much_it_dropped():
    out = safe_log_value("x" * 5000, limit=32)

    assert out.startswith("x" * 32)
    assert len(out) < 80
    assert "4968" in out, "a truncated value must say how much of it is missing"


def test_safe_log_value_leaves_an_ordinary_value_untouched():
    assert safe_log_value("seat:sarthi") == "seat:sarthi"
    assert safe_log_value(None) == "None"
    assert safe_log_value(256) == "256"
