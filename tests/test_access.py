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


def test_no_expiry_is_still_permanent(tmp_path: Path) -> None:
    """Omitting the field means permanent, and must stay that way.

    Tightening the unreadable-expiry path must not touch this case.
    """
    access_store = store(tmp_path)
    created = access_store.create_principal_token(
        name="permanent-bot",
        kind="service_account",
        role="reader",
        expires_at=None,
    )

    assert access_store.authenticate_token(created.token) is not None
    assert rejection_events(access_store) == []


def test_an_unreadable_expiry_is_refused_at_mint(tmp_path: Path) -> None:
    """The value was stored raw while role and scopes beside it were validated.

    An expiry the expiry check cannot parse is indistinguishable from no expiry,
    so the token lived forever while the dashboard labelled it "Expires".
    """
    access_store = store(tmp_path)

    with pytest.raises(ValueError, match="not an ISO 8601 timestamp"):
        access_store.create_principal_token(
            name="never-expires-bot",
            kind="service_account",
            role="reader",
            expires_at="next friday",
        )


def test_a_refused_expiry_leaves_no_orphan_principal(tmp_path: Path) -> None:
    """create_principal_token builds the principal before the token.

    Validating only inside create_token would strand a principal with no token
    attached every time an expiry is rejected.
    """
    access_store = store(tmp_path)
    before = len(access_store.snapshot()["principals"])

    with pytest.raises(ValueError):
        access_store.create_principal_token(
            name="orphan-bot",
            kind="service_account",
            role="reader",
            expires_at="whenever",
        )

    assert len(access_store.snapshot()["principals"]) == before


def test_an_unreadable_expiry_already_in_the_store_is_treated_as_expired(
    tmp_path: Path,
) -> None:
    """Covers what mint-time validation cannot: a value edited in by hand.

    Before, an unparseable stored expiry made _is_expired return False and the
    token authenticated forever.
    """
    import json

    path = tmp_path / "access.json"
    access_store = AccessStore(path)
    created = access_store.create_principal_token(
        name="hand-edited-bot",
        kind="service_account",
        role="reader",
        expires_at=iso_offset(hours=1),
    )
    assert access_store.authenticate_token(created.token) is not None

    raw = json.loads(path.read_text())
    for token in raw["tokens"]:
        token["expires_at"] = "sometime next quarter"
    path.write_text(json.dumps(raw))

    reopened = AccessStore(path)

    assert reopened.authenticate_token(created.token) is None
    events = rejection_events(reopened)
    assert events[-1]["detail"]["reason"] == "expired"


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


def test_capture_roots_store_refuses_more_than_the_api_accepts(tmp_path: Path) -> None:
    """The store must not hold a list its own PUT endpoint would reject.

    It did: CaptureRootsBody capped roots at 50 and the store enforced nothing,
    so GET could hand a client roots that PUT refused, and the client retried
    that rejected write on every sync. One shared constant now, so the two
    cannot drift again.
    """
    from kb.capture_config import MAX_APPROVED_CAPTURE_ROOTS

    access_store = store(tmp_path)
    access_store.create_seat(name="Sarthi", slug="sarthi", issue_token=False)

    at_limit = [f"/Users/sarthi/p{index}" for index in range(MAX_APPROVED_CAPTURE_ROOTS)]
    saved = access_store.set_approved_capture_roots("sarthi", paths=at_limit, actor_id="a")
    assert len(saved.paths) == MAX_APPROVED_CAPTURE_ROOTS

    with pytest.raises(ValueError, match="Too many capture roots"):
        access_store.set_approved_capture_roots(
            "sarthi", paths=[*at_limit, "/Users/sarthi/one-too-many"], actor_id="a"
        )


def test_concurrent_writers_do_not_lose_events(tmp_path: Path) -> None:
    """Two writers must not silently discard each other's edits.

    The web process and the Railway cron services all construct an AccessStore
    over the same file, and every mutation is load-modify-save. Without a lock
    spanning all three steps the second save writes back a snapshot taken before
    the first, and the first edit is gone.

    Measured before the fix: 60 of 120 events survived with two writers, 44 of
    160 with four, plus FileNotFoundError raised into callers because every
    writer shared one fixed `.tmp` path.
    """
    import threading

    path = tmp_path / "access.json"
    AccessStore(path).create_principal_token(
        name="seed", kind="service_account", role="reader"
    )

    def writer(tag: str) -> None:
        # A separate instance per thread, which is the cross-process shape.
        local = AccessStore(path)
        for index in range(40):
            local.record_event(
                action=f"probe.{tag}", actor=None, success=True, detail={"i": index}
            )

    threads = [threading.Thread(target=writer, args=(f"w{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = AccessStore(path).snapshot()["audit_events"]
    probes = [e for e in events if e["action"].startswith("probe.")]
    assert len(probes) == 160, f"{160 - len(probes)} events lost to concurrent writes"


def test_a_concurrent_write_cannot_resurrect_a_revoked_token(tmp_path: Path) -> None:
    """The lost update that matters: a vanished revocation fails OPEN.

    authenticate_token refreshes last_used_at. If that refresh writes back a
    snapshot taken before a revocation landed, the revocation is erased and the
    token works again. This is why the stamping path re-reads under the lock
    rather than reusing the snapshot it authenticated from.
    """
    import threading

    path = tmp_path / "access.json"
    store = AccessStore(path)
    created = store.create_principal_token(
        name="victim", kind="service_account", role="writer"
    )

    # Authenticate (loads a pre-revocation snapshot), revoke concurrently, then
    # let the stamp write land.
    barrier = threading.Barrier(2)

    def revoke() -> None:
        barrier.wait()
        AccessStore(path).revoke_token(created.api_token.id)

    def authenticate() -> None:
        other = AccessStore(path)
        barrier.wait()
        for _ in range(20):
            other.authenticate_token(created.token)

    threads = [threading.Thread(target=revoke), threading.Thread(target=authenticate)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert AccessStore(path).authenticate_token(created.token) is None, (
        "the revocation was overwritten by a concurrent last_used_at stamp"
    )
    assert AccessStore(path).has_tokens() is False


def test_save_uses_a_unique_temp_file(tmp_path: Path) -> None:
    """A shared temp path lets one writer truncate another's half-written file.

    open("w") truncates, so with a fixed `<name>.tmp` two writers could
    interleave into the same file and then rename the result into place. That
    produced an unparseable access.json in testing, which takes authentication
    down for everyone.
    """
    path = tmp_path / "access.json"
    store = AccessStore(path)
    store.create_principal_token(name="a", kind="service_account", role="reader")

    assert not (tmp_path / "access.json.tmp").exists(), "still using a fixed temp path"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_last_used_is_stamped_but_not_on_every_request(tmp_path: Path) -> None:
    """Stamping every request made each auth rewrite the whole store (#105).

    First use still stamps, so "when was this last used" stays answerable. A
    burst of subsequent authentications inside the interval writes nothing, and
    the file is left byte-identical.
    """
    path = tmp_path / "access.json"
    store = AccessStore(path)
    created = store.create_principal_token(
        name="hot", kind="service_account", role="reader"
    )

    assert store.authenticate_token(created.token) is not None
    stamped = store.snapshot()["tokens"][0]["last_used_at"]
    assert stamped is not None, "first use must record last_used_at"

    before = path.read_bytes()
    for _ in range(25):
        assert store.authenticate_token(created.token) is not None

    assert path.read_bytes() == before, "an authentication rewrote the store"
    assert store.snapshot()["tokens"][0]["last_used_at"] == stamped


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
