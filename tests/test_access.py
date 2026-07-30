from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from kb.access import SESSION_TRACES_DATASET, AccessStore, default_scopes, validate_role_scopes


def store(tmp_path: Path) -> AccessStore:
    return AccessStore(tmp_path / "access.json")


def iso_offset(**delta: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def rejection_events(access_store: AccessStore) -> list[dict[str, Any]]:
    return [
        event
        for event in access_store.snapshot()["audit_events"]
        if event["action"] == "access.token.rejected"
    ]


def test_valid_token_authenticates_and_updates_last_used(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="ci-bot",
        kind="service_account",
        role="reader",
    )

    session = access_store.authenticate_token(created.token)

    assert session is not None
    assert session.identity.role == "reader"
    assert session.identity.actor_name == "ci-bot"
    assert session.identity.token_id == created.api_token.id
    tokens = access_store.snapshot()["tokens"]
    assert tokens[0]["last_used_at"] is not None


def test_expired_token_is_rejected_with_audit_event(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="expired-bot",
        kind="service_account",
        role="reader",
        expires_at=iso_offset(hours=-1),
    )

    assert access_store.authenticate_token(created.token) is None

    events = rejection_events(access_store)
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["detail"]["reason"] == "expired"
    assert events[0]["detail"]["token_id"] == created.api_token.id


def test_future_expiry_still_authenticates(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="fresh-bot",
        kind="service_account",
        role="writer",
        expires_at=iso_offset(hours=1),
    )

    session = access_store.authenticate_token(created.token)

    assert session is not None
    assert rejection_events(access_store) == []


def test_revoked_token_is_rejected_with_audit_event(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="revoked-bot",
        kind="service_account",
        role="admin",
    )

    revoked = access_store.revoke_token(created.api_token.id)

    assert revoked is not None
    assert access_store.authenticate_token(created.token) is None
    events = rejection_events(access_store)
    assert events[0]["detail"]["reason"] == "revoked"


def test_token_session_lookup_enforces_expiry(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="cookie-bot",
        kind="service_account",
        role="reader",
        expires_at=iso_offset(seconds=-1),
    )

    assert access_store.token_session(created.api_token.id) is None
    assert rejection_events(access_store)[0]["detail"]["reason"] == "expired"


def test_unknown_token_returns_none_without_audit_noise(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    access_store.create_principal_token(name="real", kind="service_account", role="reader")

    assert access_store.authenticate_token("ctdl_not_a_real_token") is None
    assert rejection_events(access_store) == []


def test_scopes_cannot_exceed_role(tmp_path: Path) -> None:
    access_store = store(tmp_path)

    with pytest.raises(ValueError, match="exceed reader role"):
        access_store.create_principal_token(
            name="greedy-reader",
            kind="service_account",
            role="reader",
            scopes=["kb:read", "kb:ingest"],
        )


def test_scopes_can_reduce_within_role(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="narrow-writer",
        kind="service_account",
        role="writer",
        scopes=["kb:read", "kb:search"],
    )

    session = access_store.authenticate_token(created.token)

    assert session is not None
    assert session.identity.scopes == ("kb:read", "kb:search")


def test_token_role_downgrade_gets_downgraded_default_scopes(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    principal = access_store.create_principal(
        name="ops-admin",
        kind="user",
        role="admin",
    )

    created = access_store.create_token(
        principal_id=principal.id,
        name="read-only-key",
        role="reader",
    )

    assert created.api_token.role == "reader"
    assert created.api_token.scopes == default_scopes("reader")


def test_validate_role_scopes_rejects_unknown_role_and_dedupes() -> None:
    with pytest.raises(ValueError, match="Unsupported role"):
        validate_role_scopes("owner", ["kb:read"])

    assert validate_role_scopes("reader", ["kb:read", "kb:read", " kb:search "]) == (
        "kb:read",
        "kb:search",
    )


def test_has_tokens_ignores_revoked_tokens(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="only-token",
        kind="service_account",
        role="reader",
    )
    assert access_store.has_tokens() is True

    access_store.revoke_token(created.api_token.id)

    assert access_store.has_tokens() is False


def test_token_memory_scope_fields_persist_and_resolve(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="scoped-agent",
        kind="service_account",
        role="writer",
        default_dataset="personal",
        default_session="agent-session-1",
        allowed_datasets=["personal", "team-notes"],
    )

    session = access_store.authenticate_token(created.token)

    assert session is not None
    assert session.identity.default_dataset == "personal"
    assert session.identity.default_session == "agent-session-1"
    assert session.identity.allowed_datasets == ("personal", "team-notes")
    snapshot = access_store.snapshot()
    assert snapshot["tokens"][0]["default_dataset"] == "personal"
    assert snapshot["tokens"][0]["allowed_datasets"] == ("personal", "team-notes")


def test_token_inherits_principal_memory_defaults(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    principal = access_store.create_principal(
        name="team-member",
        kind="user",
        role="reader",
        default_dataset="personal",
        default_session="member-session",
    )
    created = access_store.create_token(
        principal_id=principal.id,
        name="member-key",
    )

    session = access_store.authenticate_token(created.token)

    assert session is not None
    assert session.identity.default_dataset == "personal"
    assert session.identity.default_session == "member-session"
    assert session.identity.allowed_datasets == ()


def test_token_overrides_principal_memory_defaults(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    principal = access_store.create_principal(
        name="team-member",
        kind="user",
        role="reader",
        default_dataset="personal",
        default_session="member-session",
    )
    created = access_store.create_token(
        principal_id=principal.id,
        name="override-key",
        default_dataset="team-notes",
        allowed_datasets=["team-notes"],
    )

    session = access_store.authenticate_token(created.token)

    assert session is not None
    assert session.identity.default_dataset == "team-notes"
    assert session.identity.default_session == "member-session"
    assert session.identity.allowed_datasets == ("team-notes",)


def test_legacy_tokens_without_memory_fields_authenticate(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="legacy-agent",
        kind="service_account",
        role="reader",
    )
    data = access_store._load()
    data["tokens"][0].pop("default_dataset", None)
    data["tokens"][0].pop("default_session", None)
    data["tokens"][0].pop("allowed_datasets", None)
    data["principals"][0].pop("default_dataset", None)
    data["principals"][0].pop("default_session", None)
    access_store._save(data)

    session = access_store.authenticate_token(created.token)

    assert session is not None
    assert session.identity.default_dataset is None
    assert session.identity.default_session is None
    assert session.identity.allowed_datasets == ()


def test_create_seat_provisions_principal_and_scoped_token(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_seat(
        name="Alice Smith",
        slug="alice",
        email="alice@example.com",
    )

    assert created.principal.seat_slug == "alice"
    assert created.principal.default_dataset == "seat:alice"
    assert created.principal.default_session == "seat-alice"
    assert created.principal.email == "alice@example.com"
    assert created.token is not None
    assert created.api_token is not None
    assert created.api_token.allowed_datasets == (
        "seat:alice",
        "masumi-network",
        SESSION_TRACES_DATASET,
    )

    session = access_store.authenticate_token(created.token)
    assert session is not None
    assert session.identity.default_dataset == "seat:alice"
    assert session.identity.allowed_datasets == (
        "seat:alice",
        "masumi-network",
        SESSION_TRACES_DATASET,
    )


def test_create_seat_rejects_duplicate_slug(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    access_store.create_seat(name="Alice", slug="alice")

    with pytest.raises(ValueError, match="already exists"):
        access_store.create_seat(name="Alice Two", slug="alice")


def test_create_seat_rejects_admin_role(tmp_path: Path) -> None:
    access_store = store(tmp_path)

    with pytest.raises(ValueError, match="admin role"):
        access_store.create_seat(name="Root", slug="root", role="admin")


def test_create_seat_uses_supplied_central_dataset(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    created = access_store.create_seat(
        name="Mallory",
        slug="mallory",
        central_dataset="org-vault",
    )

    assert created.api_token is not None
    assert created.api_token.allowed_datasets == (
        "seat:mallory",
        "org-vault",
        SESSION_TRACES_DATASET,
    )


def test_issue_seat_token_for_existing_seat(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    access_store.create_seat(name="Sarthi", slug="sarthi", central_dataset="masumi-network")

    issued = access_store.issue_seat_token(slug="sarthi", central_dataset="masumi-network")

    assert issued.token.startswith("ctdl_")
    assert issued.api_token.default_dataset == "seat:sarthi"  # routes to the seat
    assert issued.api_token.allowed_datasets == (
        "seat:sarthi",
        "masumi-network",
        SESSION_TRACES_DATASET,
    )
    assert issued.principal.seat_slug == "sarthi"  # linked to the existing seat principal


def test_issue_seat_token_unknown_seat_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        store(tmp_path).issue_seat_token(slug="ghost")


def test_validate_seat_slug_rejects_invalid_values() -> None:
    from kb.access import validate_seat_slug

    assert validate_seat_slug("alice-smith") == "alice-smith"
    with pytest.raises(ValueError, match="Seat slug"):
        validate_seat_slug("Bad Slug")
    with pytest.raises(ValueError, match="Seat slug"):
        validate_seat_slug("-bad")


def test_capture_policy_round_trip(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    access_store.create_seat(name="Alice", slug="alice")

    baseline = access_store.get_capture_policy("alice")
    assert baseline.deny_globs == ()

    updated = access_store.set_capture_policy(
        "alice",
        deny_globs=["private/*", "private/*"],
        actor_id="admin_1",
    )
    assert updated.deny_globs == ("private/*",)
    assert updated.updated_by == "admin_1"

    loaded = access_store.get_capture_policy("alice")
    assert loaded.deny_globs == ("private/*",)


def test_approved_capture_roots_round_trip(tmp_path: Path) -> None:
    access_store = store(tmp_path)
    access_store.create_seat(name="Alice", slug="alice")

    empty = access_store.get_approved_capture_roots("alice")
    assert empty.paths == ()

    updated = access_store.set_approved_capture_roots(
        "alice",
        paths=["/Users/alice/work", "/Users/alice/work"],
        actor_id="admin_1",
    )
    assert updated.paths == ("/Users/alice/work",)
    assert updated.updated_by == "admin_1"

    loaded = access_store.get_approved_capture_roots("alice")
    assert loaded.paths == ("/Users/alice/work",)
