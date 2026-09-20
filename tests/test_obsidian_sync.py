from __future__ import annotations

from pathlib import Path

import pytest

from kb.access import AccessIdentity
from kb.obsidian_sync import ObsidianSyncStore, SyncPushDocument, normalize_path

ACTOR = AccessIdentity(
    role="writer",
    actor_id="principal_test",
    actor_kind="service_account",
    actor_name="Sync bot",
    source="api_token",
    scopes=("obsidian:sync:push", "obsidian:sync:pull"),
    token_id="token_test",
)


def vault_store(tmp_path: Path) -> tuple[ObsidianSyncStore, str]:
    sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    vault = sync.register_vault(name="Team Vault", actor=ACTOR)
    return sync, vault.id


def push_one(
    sync: ObsidianSyncStore,
    vault_id: str,
    *,
    path: str = "notes/decision.md",
    content: str = "Decision body",
    base_rev: int | None = None,
    deleted: bool = False,
) -> dict[str, object]:
    return sync.push(
        vault_id=vault_id,
        actor=ACTOR,
        documents=[
            SyncPushDocument(path=path, content=content, base_rev=base_rev, deleted=deleted)
        ],
    )


def test_normalize_path_rejects_escapes_and_cleans_separators() -> None:
    assert normalize_path("\\notes\\Decision.md") == "notes/Decision.md"
    assert normalize_path("./notes/a.md") == "notes/a.md"

    with pytest.raises(ValueError):
        normalize_path("../outside.md")
    with pytest.raises(ValueError):
        normalize_path("notes/../../outside.md")


def test_first_push_accepts_document_at_rev_one(tmp_path: Path) -> None:
    sync, vault_id = vault_store(tmp_path)

    result = push_one(sync, vault_id)

    assert len(result["accepted"]) == 1
    accepted = result["accepted"][0]
    assert accepted["rev"] == 1
    assert accepted["path"] == "notes/decision.md"
    assert result["conflicts"] == []


def test_stale_base_rev_creates_conflict_instead_of_overwriting(tmp_path: Path) -> None:
    sync, vault_id = vault_store(tmp_path)
    push_one(sync, vault_id, content="Server copy", base_rev=None)
    push_one(sync, vault_id, content="Second server write", base_rev=1)

    result = push_one(sync, vault_id, content="Stale local edit", base_rev=1)

    assert result["accepted"] == []
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["reason"] == "base_revision_mismatch"
    assert conflict["remote_rev"] == 2
    assert conflict["local_body"] == "Stale local edit"
    assert conflict["remote_body"] == "Second server write"
    # The server copy is untouched.
    document = sync.document(conflict["document_id"])
    assert document["body"] == "Second server write"


def test_unchanged_content_is_skipped(tmp_path: Path) -> None:
    sync, vault_id = vault_store(tmp_path)
    push_one(sync, vault_id, content="Same body")

    result = push_one(sync, vault_id, content="Same body", base_rev=1)

    assert result["accepted"] == []
    assert result["skipped"][0]["reason"] == "unchanged"


def test_conflict_resolution_accept_local_writes_new_revision(tmp_path: Path) -> None:
    sync, vault_id = vault_store(tmp_path)
    push_one(sync, vault_id, content="Server copy")
    push_one(sync, vault_id, content="Server second write", base_rev=1)
    conflict = push_one(sync, vault_id, content="Local edit", base_rev=1)["conflicts"][0]

    resolved = sync.resolve_conflict(
        conflict_id=conflict["id"],
        actor=ACTOR,
        resolution="accept_local",
    )

    assert resolved["status"] == "resolved_accept_local"
    document = sync.document(conflict["document_id"])
    assert document["body"] == "Local edit"
    assert document["current_rev"] == 3


def test_conflict_resolution_save_both_creates_conflict_copy(tmp_path: Path) -> None:
    sync, vault_id = vault_store(tmp_path)
    push_one(sync, vault_id, content="Server copy")
    push_one(sync, vault_id, content="Server second write", base_rev=1)
    conflict = push_one(sync, vault_id, content="Local edit", base_rev=1)["conflicts"][0]

    resolved = sync.resolve_conflict(
        conflict_id=conflict["id"],
        actor=ACTOR,
        resolution="save_both",
    )

    copy_path = resolved["resolution_document"]["path"]
    assert copy_path != "notes/decision.md"
    assert ".conflict-" in copy_path
    # Original document keeps the server copy.
    assert sync.document(conflict["document_id"])["body"] == "Server second write"


def test_manifest_and_pull_expose_changes_after_cursor(tmp_path: Path) -> None:
    sync, vault_id = vault_store(tmp_path)
    push_one(sync, vault_id, path="a.md", content="A")
    cursor = sync.manifest(vault_id=vault_id)["next_cursor"]
    push_one(sync, vault_id, path="b.md", content="B")

    pulled = sync.pull(vault_id=vault_id, cursor=cursor)

    assert [document["normalized_path"] for document in pulled["documents"]] == ["b.md"]
    assert pulled["documents"][0]["body"] == "B"


def test_unknown_vault_raises_key_error(tmp_path: Path) -> None:
    sync = ObsidianSyncStore(tmp_path / "obsidian.json")

    with pytest.raises(KeyError):
        sync.manifest(vault_id="vault_missing")


def test_duplicate_normalized_paths_rejected_without_mutation(tmp_path: Path) -> None:
    sync, vault_id = vault_store(tmp_path)
    # Seed one document so a rejected batch has an existing manifest to preserve.
    push_one(sync, vault_id, path="notes/a.md", content="Original")
    before = sync.manifest(vault_id=vault_id)

    # ``notes/a.md`` and ``./notes/a.md`` collapse to the same normalized path.
    with pytest.raises(ValueError, match="Duplicate normalized path"):
        sync.push(
            vault_id=vault_id,
            actor=ACTOR,
            documents=[
                SyncPushDocument(path="notes/a.md", content="First", base_rev=1),
                SyncPushDocument(path="./notes/a.md", content="Second", base_rev=1),
            ],
        )

    after = sync.manifest(vault_id=vault_id)
    assert after["documents"] == before["documents"]
    assert after["next_cursor"] == before["next_cursor"]
