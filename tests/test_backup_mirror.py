from __future__ import annotations

import json
from typing import Any

import pytest

from kb.backup_mirror import (
    BackupMirror,
    BackupMirrorDisabled,
    BackupMirrorPublishError,
    GitHubMirrorPublisher,
)
from kb.config import CitadelConfig


class FakePublisher:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish_manifest(self, *, snapshot: str, manifest: dict[str, Any]) -> dict[str, Any]:
        self.published.append({"snapshot": snapshot, "manifest": manifest})
        return {
            "repo": "masumi-network/Vault-Backup-Mirror",
            "branch": "main",
            "remote_paths": [
                f"snapshots/{snapshot.split('-', 1)[0]}/{snapshot}/manifest.json",
                "manifests/latest.json",
            ],
            "commits": ["commit-one", "commit-two"],
        }


class RecordingGitHubPublisher(GitHubMirrorPublisher):
    def __init__(self) -> None:
        super().__init__(
            repo="masumi-network/Vault-Backup-Mirror",
            branch="main",
            token="github-token",
        )
        self.calls: list[dict[str, Any]] = []

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "body": body})
        if method == "GET" and "manifests/latest.json" in path:
            return {"sha": "existing-latest-sha"}
        if method == "GET":
            raise BackupMirrorPublishError("GitHub API returned 404: not found")
        return {"commit": {"sha": f"commit-{len(self.calls)}"}}


def mirror_config(
    tmp_path: Any,
    *,
    enabled: bool = False,
    push_enabled: bool = False,
    token: str | None = None,
) -> CitadelConfig:
    return CitadelConfig(
        access_store_path=str(tmp_path / "access.json"),
        obsidian_sync_state_path=str(tmp_path / "obsidian.json"),
        github_sync_state_path=str(tmp_path / "github.json"),
        backup_mirror_root_path=str(tmp_path / "mirror"),
        backup_mirror_enabled=enabled,
        backup_mirror_push_enabled=push_enabled,
        backup_mirror_token=token,
    )


def test_backup_mirror_dry_run_tracks_file_hashes_without_copying_content(tmp_path: Any) -> None:
    config = mirror_config(tmp_path)
    (tmp_path / "github.json").write_text('{"last_digest":"super-secret-body"}', encoding="utf-8")
    (tmp_path / "access.json").write_text('{"audit_events":[]}', encoding="utf-8")
    mirror = BackupMirror(config)

    result = mirror.run(dry_run=True)
    serialized = json.dumps(result)

    assert result["ok"] is True
    assert result["written"] is False
    assert result["manifest"]["summary"] == {
        "tracked_files": 3,
        "available_files": 2,
        "missing_files": 1,
        "total_bytes": (tmp_path / "github.json").stat().st_size
        + (tmp_path / "access.json").stat().st_size,
    }
    assert result["manifest"]["tracked_files"][0]["sha256"]
    assert "super-secret-body" not in serialized
    assert not (tmp_path / "mirror" / "manifests" / "latest.json").exists()
    assert mirror.status()["push"]["token_configured"] is False


def test_backup_mirror_write_requires_enabled_flag(tmp_path: Any) -> None:
    mirror = BackupMirror(mirror_config(tmp_path, enabled=False))

    with pytest.raises(BackupMirrorDisabled):
        mirror.run(dry_run=False)


def test_backup_mirror_writes_latest_manifest_when_enabled(tmp_path: Any) -> None:
    config = mirror_config(tmp_path, enabled=True)
    (tmp_path / "github.json").write_text(
        '{"last_checked_at":"2026-06-03T00:00:00Z"}', encoding="utf-8"
    )
    mirror = BackupMirror(config)

    result = mirror.run(dry_run=False)
    latest = tmp_path / "mirror" / "manifests" / "latest.json"

    assert result["written"] is True
    assert latest.exists()
    assert result["snapshot_manifest_path"]
    assert json.loads(latest.read_text(encoding="utf-8"))["snapshot_id"] == result["snapshot_id"]
    assert mirror.status()["latest_export"]["snapshot_id"] == result["snapshot_id"]


def test_backup_mirror_push_requires_explicit_token(tmp_path: Any) -> None:
    mirror = BackupMirror(mirror_config(tmp_path, enabled=True, push_enabled=True))

    with pytest.raises(BackupMirrorPublishError):
        mirror.run(dry_run=False)

    assert not (tmp_path / "mirror" / "manifests" / "latest.json").exists()


def test_backup_mirror_pushes_manifest_tree_when_enabled(tmp_path: Any) -> None:
    config = mirror_config(
        tmp_path,
        enabled=True,
        push_enabled=True,
        token="ghp_not_written_to_manifest",
    )
    (tmp_path / "github.json").write_text(
        '{"last_checked_at":"2026-06-03T00:00:00Z"}', encoding="utf-8"
    )
    publisher = FakePublisher()
    mirror = BackupMirror(config, publisher=publisher)

    result = mirror.run(dry_run=False)
    serialized = json.dumps(result)

    assert result["written"] is True
    assert result["published"] is True
    assert result["publish"]["remote_paths"] == [
        f"snapshots/{result['snapshot_id'].split('-', 1)[0]}/{result['snapshot_id']}/manifest.json",
        "manifests/latest.json",
    ]
    assert publisher.published[0]["snapshot"] == result["snapshot_id"]
    assert "ghp_not_written_to_manifest" not in serialized
    assert "ghp_not_written_to_manifest" not in json.dumps(publisher.published[0]["manifest"])


def test_github_publisher_upserts_snapshot_and_latest_manifest() -> None:
    publisher = RecordingGitHubPublisher()

    result = publisher.publish_manifest(
        snapshot="20260603-120000Z",
        manifest={"snapshot_id": "20260603-120000Z", "tracked_files": []},
    )
    put_calls = [call for call in publisher.calls if call["method"] == "PUT"]

    assert result["remote_paths"] == [
        "snapshots/20260603/20260603-120000Z/manifest.json",
        "manifests/latest.json",
    ]
    assert len(put_calls) == 2
    assert "sha" not in put_calls[0]["body"]
    assert put_calls[1]["body"]["sha"] == "existing-latest-sha"
    assert "github-token" not in json.dumps(put_calls)
