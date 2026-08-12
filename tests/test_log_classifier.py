from __future__ import annotations

from scripts.classify_docker_logs import classify_logs


def test_classifier_ignores_200() -> None:
    result = classify_logs(phase="seed-search", app_lines=["POST /search 200"], qdrant_lines=[])

    assert result["ok"] is True


def test_classifier_catches_503_degraded_and_traceback() -> None:
    result = classify_logs(
        phase="outage",
        app_lines=["GET /search 503", "service degraded", "Traceback (most recent call last)"],
        qdrant_lines=[],
    )

    assert result["ok"] is False
    assert [finding["reason"] for finding in result["unexpected"]] == [
        "non_2xx",
        "severe",
        "severe",
    ]


def test_classifier_allows_only_declared_phase_pattern() -> None:
    result = classify_logs(
        phase="injected-outage",
        app_lines=["ERROR expected outage HTTP 503"],
        qdrant_lines=["WARN unrelated cache miss"],
        expected_fatal_patterns={"app": [r"expected outage HTTP 503"]},
    )

    assert result["expected"][0]["expected"] is True
    assert result["ok"] is False
    assert result["unexpected"][0]["source"] == "qdrant"


def test_fatal_token_cannot_be_excused_by_a_warning_allowlist() -> None:
    result = classify_logs(
        phase="mixed",
        app_lines=["WARNING TLS disabled; ERROR database unavailable"],
        qdrant_lines=[],
        expected_patterns={"app": [r"TLS disabled"]},
    )
    assert result["ok"] is False
    assert len(result["unexpected"]) == 1


def test_died_and_critical_are_detected() -> None:
    result = classify_logs(
        phase="fatal",
        app_lines=["Background task evolve-scheduler died: boom"],
        qdrant_lines=["CRITICAL storage checksum"],
    )
    assert result["ok"] is False
    assert len(result["unexpected"]) == 2


def test_pure_warning_stays_excusable() -> None:
    result = classify_logs(
        phase="warn",
        app_lines=["WARNING TLS disabled for loopback"],
        qdrant_lines=[],
        expected_patterns={"app": [r"TLS disabled"]},
    )
    assert result["ok"] is True


def test_plain_pattern_does_not_excuse_a_fatal_line() -> None:
    result = classify_logs(
        phase="injected-outage",
        app_lines=["ERROR expected outage HTTP 503"],
        qdrant_lines=[],
        expected_patterns={"app": [r"expected outage HTTP 503"]},
    )
    assert result["ok"] is False
    assert result["unexpected"][0]["source"] == "app"
