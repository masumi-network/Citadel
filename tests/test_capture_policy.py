from __future__ import annotations

from kb.capture_policy import (
    DEFAULT_ORG_CAPTURE_DENY_GLOBS,
    SeatCapturePolicy,
    capture_policy_payload,
    merged_deny_globs,
    normalize_deny_globs,
)


def test_normalize_deny_globs_dedupes_and_strips() -> None:
    assert normalize_deny_globs([" .env ", ".env", "", "*.pem"]) == (".env", "*.pem")


def test_merged_deny_globs_includes_env_and_defaults() -> None:
    effective = merged_deny_globs(
        env_exclude_patterns=(".git/*", "node_modules/*"),
        seat_deny_globs=("custom-secret/*",),
    )

    assert effective[0] == ".git/*"
    assert effective[1] == "node_modules/*"
    assert ".env" in effective
    assert "custom-secret/*" in effective


def test_merged_deny_globs_can_skip_default_org_denies() -> None:
    effective = merged_deny_globs(
        env_exclude_patterns=(".git/*",),
        include_default_org_denies=False,
    )

    assert effective == (".git/*",)
    assert ".env" not in effective


def test_capture_policy_payload_includes_effective_list() -> None:
    payload = capture_policy_payload(
        seat_slug="alice",
        baseline=SeatCapturePolicy(deny_globs=("team-private/*",)),
        env_exclude_patterns=(".git/*",),
    )

    assert payload["seat_slug"] == "alice"
    assert payload["baseline"]["deny_globs"] == ["team-private/*"]
    assert ".git/*" in payload["effective_deny_globs"]
    assert "team-private/*" in payload["effective_deny_globs"]
    assert payload["default_org_deny_globs"] == list(DEFAULT_ORG_CAPTURE_DENY_GLOBS)


def test_capture_policy_payload_without_seat_slug() -> None:
    payload = capture_policy_payload(
        seat_slug=None,
        baseline=SeatCapturePolicy(),
        env_exclude_patterns=(".git/*",),
    )

    assert "seat_slug" not in payload
    assert ".env" in payload["effective_deny_globs"]
