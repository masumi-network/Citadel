from __future__ import annotations

import pytest
from fastapi import HTTPException

from kb.access import AccessIdentity
from kb.visibility import (
    conflict_visible_to,
    dataset_visible_to,
    enforce_dataset_allowlist,
    event_visible_to,
    owner_visible_to,
)


def seat_identity(slug: str, *, actor_id: str | None = None) -> AccessIdentity:
    return AccessIdentity(
        role="writer",
        actor_id=actor_id or f"principal_{slug}",
        actor_kind="user",
        actor_name=slug.title(),
        source="api_token",
        scopes=("kb:read", "kb:ingest"),
        allowed_datasets=(f"seat:{slug}",),
        seat_slug=slug,
    )


def test_seat_dataset_allowlist_is_shared_by_read_and_write_checks() -> None:
    alice = seat_identity("alice")

    enforce_dataset_allowlist(alice, "seat:alice")

    assert dataset_visible_to(alice, "seat:alice") is True
    assert dataset_visible_to(alice, "seat:bob") is False
    with pytest.raises(HTTPException) as error:
        enforce_dataset_allowlist(alice, "seat:bob")
    assert error.value.status_code == 403


def test_projection_visibility_fails_closed_for_unscoped_conflicts() -> None:
    alice = seat_identity("alice")
    alice_conflict = {"dataset": "seat:alice"}
    legacy_conflict = {"summary": "missing dataset"}

    assert conflict_visible_to(alice, alice_conflict) is True
    assert conflict_visible_to(alice, {"dataset": "seat:bob"}) is False
    assert conflict_visible_to(alice, legacy_conflict) is False
    assert event_visible_to(
        alice,
        {"type": "conflict", "details": {"dataset": "seat:alice"}},
    ) is True
    assert event_visible_to(alice, {"type": "conflict", "details": {}}) is False
    assert event_visible_to(alice, {"type": "error", "details": {}}) is False
    assert event_visible_to(
        alice,
        {"type": "error", "details": {"dataset": "seat:alice"}},
    ) is True
    assert event_visible_to(alice, {"action": "contribute", "dataset": "seat:alice"}) is True


def test_owner_visibility_accepts_actor_or_seat_identity() -> None:
    alice = seat_identity("alice", actor_id="actor-a")

    assert owner_visible_to(alice, "actor-a") is True
    assert owner_visible_to(alice, "other-actor", "alice") is True
    assert owner_visible_to(alice, "other-actor", "bob") is False
