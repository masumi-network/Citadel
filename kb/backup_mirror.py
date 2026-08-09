from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

from kb.secure_http import open_secure

from kb.config import CitadelConfig
from kb.retry import run_with_retries
from kb.security_scan import redact_secrets

logger = logging.getLogger(__name__)

MIRROR_VERSION = 1
GITHUB_API = "https://api.github.com"


class BackupMirrorDisabled(RuntimeError):
    pass


class BackupMirrorPublishError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def snapshot_id(exported_at: str) -> str:
    return exported_at.replace(":", "").replace("-", "").replace("Z", "Z").replace("T", "-")


def json_payload_bytes(payload: dict[str, Any]) -> bytes:
    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n".encode("utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_updated_at(path: Path) -> str:
    return (
        datetime.fromtimestamp(path.stat().st_mtime, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class GitHubMirrorPublisher:
    def __init__(
        self,
        *,
        repo: str,
        branch: str,
        token: str,
        api_base: str = GITHUB_API,
        timeout: float = 20.0,
    ) -> None:
        owner, name = self._parse_repo(repo)
        self.owner = owner
        self.name = name
        self.branch = branch
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def publish_manifest(self, *, snapshot: str, manifest: dict[str, Any]) -> dict[str, Any]:
        payload = json_payload_bytes(manifest)
        date_prefix = snapshot.split("-", 1)[0]
        remote_paths = [
            f"snapshots/{date_prefix}/{snapshot}/manifest.json",
            "manifests/latest.json",
        ]
        published = [
            self._put_file(
                path=path,
                content=payload,
                message=f"chore: mirror citadel manifest {snapshot}",
            )
            for path in remote_paths
        ]
        return {
            "repo": f"{self.owner}/{self.name}",
            "branch": self.branch,
            "remote_paths": [item["path"] for item in published],
            "commits": [item["commit_sha"] for item in published if item.get("commit_sha")],
        }

    def _put_file(self, *, path: str, content: bytes, message: str) -> dict[str, Any]:
        existing_sha = self._existing_sha(path)
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.branch,
        }
        if existing_sha:
            body["sha"] = existing_sha
        payload = self._request_json("PUT", self._contents_path(path), body=body)
        commit = payload.get("commit") if isinstance(payload, dict) else {}
        return {
            "path": path,
            "commit_sha": commit.get("sha") if isinstance(commit, dict) else None,
        }

    def _existing_sha(self, path: str) -> str | None:
        try:
            payload = self._request_json(
                "GET",
                f"{self._contents_path(path)}?ref={quote(self.branch, safe='')}",
            )
        except BackupMirrorPublishError as exc:
            if "GitHub API returned 404" in str(exc):
                return None
            raise
        if not isinstance(payload, dict):
            return None
        value = payload.get("sha")
        return str(value) if value else None

    def _contents_path(self, path: str) -> str:
        safe_path = quote(path.strip("/"), safe="/")
        return f"/repos/{quote(self.owner)}/{quote(self.name)}/contents/{safe_path}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> Any:
        request = Request(
            f"{self.api_base}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "citadel-backup-mirror",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method=method,
        )

        def send() -> Any:
            with open_secure(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")

        try:
            return run_with_retries(send, operation=f"backup_mirror.{method.lower()} {path}")
        except HTTPError as exc:
            detail = redact_secrets(exc.read().decode("utf-8", errors="replace")[:300], self.token)
            if exc.code != 404:
                logger.error("Backup mirror GitHub request %s failed: HTTPError %s", path, exc.code)
            raise BackupMirrorPublishError(f"GitHub API returned {exc.code}: {detail}") from exc
        except URLError as exc:
            logger.error(
                "Backup mirror GitHub request %s failed: %s: %s",
                path,
                exc.__class__.__name__,
                redact_secrets(str(exc.reason), self.token),
            )
            raise BackupMirrorPublishError(f"Could not reach GitHub API: {exc.reason}") from exc

    def _parse_repo(self, repo: str) -> tuple[str, str]:
        owner, separator, name = repo.strip().partition("/")
        if separator != "/" or not owner or not name or "/" in name:
            raise BackupMirrorPublishError(
                "CITADEL_BACKUP_MIRROR_REPO must use owner/repository format."
            )
        return owner, name


class BackupMirror:
    """Manifest-only export for the private Vault Backup Mirror.

    The first implementation tracks source state files by hash and size. It does
    not copy raw state files, token stores, embeddings, vector indexes, or graph
    databases into the mirror.
    """

    def __init__(
        self,
        config: CitadelConfig,
        *,
        publisher: GitHubMirrorPublisher | None = None,
    ) -> None:
        self.config = config
        self.root_path = Path(config.backup_mirror_root_path)
        self.publisher = publisher

    @property
    def latest_manifest_path(self) -> Path:
        return self.root_path / "manifests" / "latest.json"

    def status(self) -> dict[str, Any]:
        latest = self._load_latest_manifest()
        tracked_files = self.tracked_files()
        return {
            "ok": True,
            "enabled": self.config.backup_mirror_enabled,
            "repo": self.config.backup_mirror_repo,
            "branch": self.config.backup_mirror_branch,
            "root_path": str(self.root_path),
            "latest_manifest_path": str(self.latest_manifest_path),
            "latest_export": latest,
            "tracked_files": tracked_files,
            "summary": self._summary(tracked_files),
            "push": {
                "enabled": self.config.backup_mirror_push_enabled,
                "token_configured": bool(self.config.backup_mirror_token),
                "repo": self.config.backup_mirror_repo,
                "branch": self.config.backup_mirror_branch,
            },
        }

    def run(self, *, dry_run: bool = True) -> dict[str, Any]:
        if not dry_run and not self.config.backup_mirror_enabled:
            raise BackupMirrorDisabled("Vault Backup Mirror export is disabled.")
        if not dry_run and self.config.backup_mirror_push_enabled and not self._publisher():
            raise BackupMirrorPublishError("Vault Backup Mirror push token is not configured.")

        exported_at = utc_now()
        manifest = self._manifest(exported_at=exported_at, dry_run=dry_run)
        result: dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "enabled": self.config.backup_mirror_enabled,
            "snapshot_id": manifest["snapshot_id"],
            "manifest": manifest,
            "latest_manifest_path": str(self.latest_manifest_path),
            "snapshot_manifest_path": None,
            "written": False,
            "published": False,
            "publish": {
                "enabled": self.config.backup_mirror_push_enabled,
                "attempted": False,
                "remote_paths": [],
                "commits": [],
            },
        }
        if dry_run:
            return result

        snapshot_path = self._snapshot_manifest_path(manifest["snapshot_id"])
        self._write_json(snapshot_path, manifest)
        self._write_json(self.latest_manifest_path, manifest)
        result["snapshot_manifest_path"] = str(snapshot_path)
        result["written"] = True
        logger.info(
            "Backup mirror manifest %s written to %s", manifest["snapshot_id"], snapshot_path
        )
        publisher = self._publisher()
        if self.config.backup_mirror_push_enabled and publisher:
            publish_result = publisher.publish_manifest(
                snapshot=manifest["snapshot_id"],
                manifest=manifest,
            )
            result["published"] = True
            result["publish"] = {
                "enabled": True,
                "attempted": True,
                **publish_result,
            }
            logger.info(
                "Backup mirror manifest %s published to %s@%s",
                manifest["snapshot_id"],
                publish_result.get("repo"),
                publish_result.get("branch"),
            )
        return result

    def tracked_files(self) -> list[dict[str, Any]]:
        return [
            self._file_record("github_sync_state", Path(self.config.github_sync_state_path)),
            self._file_record("obsidian_sync_state", Path(self.config.obsidian_sync_state_path)),
            self._file_record("access_store", Path(self.config.access_store_path)),
        ]

    def _manifest(self, *, exported_at: str, dry_run: bool) -> dict[str, Any]:
        tracked_files = self.tracked_files()
        return {
            "version": MIRROR_VERSION,
            "snapshot_id": snapshot_id(exported_at),
            "exported_at": exported_at,
            "dry_run": dry_run,
            "repo": self.config.backup_mirror_repo,
            "branch": self.config.backup_mirror_branch,
            "summary": self._summary(tracked_files),
            "tracked_files": tracked_files,
            "policy": {
                "mode": "manifest_only",
                "include": [
                    "state file path labels",
                    "file sizes",
                    "sha256 hashes",
                    "updated timestamps",
                ],
                "exclude": [
                    "raw tokens",
                    "secret values",
                    "source file bodies",
                    "embeddings",
                    "vector indexes",
                    "graph database files",
                    "large binaries",
                ],
            },
        }

    def _file_record(self, name: str, path: Path) -> dict[str, Any]:
        if not path.exists() or not path.is_file():
            return {
                "name": name,
                "path": str(path),
                "exists": False,
                "size_bytes": 0,
                "sha256": None,
                "updated_at": None,
            }
        return {
            "name": name,
            "path": str(path),
            "exists": True,
            "size_bytes": path.stat().st_size,
            "sha256": file_sha256(path),
            "updated_at": file_updated_at(path),
        }

    def _summary(self, tracked_files: list[dict[str, Any]]) -> dict[str, int]:
        available = [file for file in tracked_files if file.get("exists")]
        total_bytes = sum(int(file.get("size_bytes") or 0) for file in available)
        return {
            "tracked_files": len(tracked_files),
            "available_files": len(available),
            "missing_files": len(tracked_files) - len(available),
            "total_bytes": total_bytes,
        }

    def _snapshot_manifest_path(self, snapshot: str) -> Path:
        date_prefix = snapshot.split("-", 1)[0]
        return self.root_path / "snapshots" / date_prefix / snapshot / "manifest.json"

    def _load_latest_manifest(self) -> dict[str, Any] | None:
        if not self.latest_manifest_path.exists():
            return None
        try:
            with self.latest_manifest_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        with temp_path.open("wb") as file:
            file.write(json_payload_bytes(payload))
        temp_path.replace(path)

    def _publisher(self) -> GitHubMirrorPublisher | None:
        if self.publisher is not None:
            return self.publisher
        if not self.config.backup_mirror_token:
            return None
        return GitHubMirrorPublisher(
            repo=self.config.backup_mirror_repo,
            branch=self.config.backup_mirror_branch,
            token=self.config.backup_mirror_token,
        )
