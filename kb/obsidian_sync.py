from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from hashlib import sha1, sha256
import json
import logging
from pathlib import Path
from typing import Any, Callable

from kb.access import AccessIdentity

logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{sha1(value.encode('utf-8')).hexdigest()[:16]}"


def content_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip().lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Path must stay inside the vault.")
    return "/".join(parts)


@dataclass(frozen=True)
class ObsidianVault:
    id: str
    name: str
    source_type: str
    team_id: str | None
    owner_actor_id: str
    plugin_version: str | None
    created_at: str
    updated_at: str
    last_push_at: str | None = None


@dataclass(frozen=True)
class SourceDocument:
    id: str
    source_id: str
    path: str
    normalized_path: str
    current_rev: int
    content_hash: str
    size: int
    created_at: str
    updated_at: str
    dataset: str | None = None
    deleted_at: str | None = None


@dataclass(frozen=True)
class SourceRevision:
    id: str
    document_id: str
    rev: int
    base_rev: int | None
    actor_id: str
    origin: str
    body_hash: str
    body: str
    path: str
    created_at: str
    deleted: bool = False


@dataclass(frozen=True)
class SyncConflict:
    id: str
    document_id: str
    source_id: str
    path: str
    local_rev: int | None
    remote_rev: int
    base_rev: int | None
    local_hash: str
    remote_hash: str
    local_body: str
    remote_body: str
    status: str
    reason: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SyncPushDocument:
    path: str
    content: str = ""
    base_rev: int | None = None
    deleted: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
    dataset: str | None = None


class ObsidianSyncStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def register_vault(
        self,
        *,
        name: str,
        actor: AccessIdentity,
        team_id: str | None = None,
        plugin_version: str | None = None,
    ) -> ObsidianVault:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Vault name is required.")
        data = self._load()
        vault_id = stable_id("vault", f"{team_id or 'default'}:{actor.actor_id}:{clean_name}")
        created_at = data.get("vaults", {}).get(vault_id, {}).get("created_at") or now_iso()
        vault = ObsidianVault(
            id=vault_id,
            name=clean_name,
            source_type="obsidian_vault",
            team_id=team_id,
            owner_actor_id=actor.actor_id,
            plugin_version=plugin_version,
            created_at=created_at,
            updated_at=now_iso(),
            last_push_at=data.get("vaults", {}).get(vault_id, {}).get("last_push_at"),
        )
        data.setdefault("vaults", {})[vault_id] = asdict(vault)
        self._save(data)
        return vault

    def source_status(
        self,
        *,
        source_type: str | None = None,
        visible_owner: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        data = self._load()
        vaults = list(data.get("vaults", {}).values())
        if source_type and source_type != "obsidian_vault":
            vaults = []
        if visible_owner is not None:
            vaults = [vault for vault in vaults if visible_owner(vault)]
        visible_vault_ids = {vault.get("id") for vault in vaults}
        documents = [
            document
            for document in data.get("documents", {}).values()
            if document.get("source_id") in visible_vault_ids
        ]
        conflicts = [
            conflict
            for conflict in data.get("conflicts", {}).values()
            if conflict.get("status") == "open"
            and conflict.get("source_id") in visible_vault_ids
        ]
        by_source: dict[str, dict[str, Any]] = {
            vault["id"]: {
                **vault,
                "documents": 0,
                "open_conflicts": 0,
            }
            for vault in vaults
        }
        for document in documents:
            source = by_source.get(document.get("source_id"))
            if source:
                source["documents"] += 1
        for conflict in conflicts:
            source = by_source.get(conflict.get("source_id"))
            if source:
                source["open_conflicts"] += 1
        return {
            "sources": list(by_source.values()),
            "source_type": source_type or "all",
            "summary": {
                "obsidian_vaults": len(vaults),
                "obsidian_documents": sum(source["documents"] for source in by_source.values()),
                "open_conflicts": len(conflicts),
                "sequence": data.get("sequence", 0),
            },
        }

    def manifest(self, *, vault_id: str, cursor: int | None = None) -> dict[str, Any]:
        data = self._load()
        vault = self._vault(data, vault_id)
        documents = [
            document
            for document in data.get("documents", {}).values()
            if document.get("source_id") == vault_id
        ]
        documents.sort(key=lambda item: item.get("normalized_path", ""))
        return {
            "vault": vault,
            "documents": documents,
            "cursor": cursor,
            "next_cursor": data.get("sequence", 0),
            "open_conflicts": [
                conflict
                for conflict in data.get("conflicts", {}).values()
                if conflict.get("source_id") == vault_id and conflict.get("status") == "open"
            ],
        }

    def push(
        self,
        *,
        vault_id: str,
        actor: AccessIdentity,
        documents: list[SyncPushDocument],
        dataset: str | None = None,
        origin: str = "obsidian_plugin",
    ) -> dict[str, Any]:
        data = self._load()
        vault = self._vault(data, vault_id)
        accepted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        now = now_iso()

        for incoming in documents:
            normalized_path = normalize_path(incoming.path)
            body = "" if incoming.deleted else incoming.content
            incoming_hash = content_hash(body)
            document_id = stable_id("doc", f"{vault_id}:{normalized_path}")
            current = data.get("documents", {}).get(document_id)
            current_rev = int(current.get("current_rev", 0)) if current else 0
            remote_body = self._latest_body(data, document_id)
            remote_hash = current.get("content_hash", "") if current else ""

            if current and incoming.base_rev != current_rev:
                conflict = self._create_conflict(
                    data,
                    document_id=document_id,
                    source_id=vault_id,
                    path=normalized_path,
                    local_rev=incoming.base_rev,
                    remote_rev=current_rev,
                    local_hash=incoming_hash,
                    remote_hash=remote_hash,
                    local_body=body,
                    remote_body=remote_body,
                    reason="base_revision_mismatch",
                )
                conflicts.append(conflict)
                continue

            if current and incoming_hash == remote_hash and bool(current.get("deleted_at")) == incoming.deleted:
                skipped.append(
                    {
                        "document_id": document_id,
                        "path": normalized_path,
                        "rev": current_rev,
                        "content_hash": incoming_hash,
                        "reason": "unchanged",
                    }
                )
                continue

            next_rev = current_rev + 1
            document = SourceDocument(
                id=document_id,
                source_id=vault_id,
                path=incoming.path,
                normalized_path=normalized_path,
                current_rev=next_rev,
                content_hash=incoming_hash,
                size=len(body),
                created_at=current.get("created_at", now) if current else now,
                updated_at=now,
                dataset=incoming.dataset or dataset,
                deleted_at=now if incoming.deleted else None,
            )
            revision = SourceRevision(
                id=f"{document_id}:rev:{next_rev}",
                document_id=document_id,
                rev=next_rev,
                base_rev=incoming.base_rev,
                actor_id=actor.actor_id,
                origin=origin,
                body_hash=incoming_hash,
                body=body,
                path=normalized_path,
                created_at=now,
                deleted=incoming.deleted,
            )
            data.setdefault("documents", {})[document_id] = asdict(document)
            data.setdefault("revisions", {})[revision.id] = asdict(revision)
            sequence = self._next_sequence(data)
            data.setdefault("changes", []).append(
                {
                    "sequence": sequence,
                    "document_id": document_id,
                    "source_id": vault_id,
                    "rev": next_rev,
                    "path": normalized_path,
                    "deleted": incoming.deleted,
                    "created_at": now,
                }
            )
            accepted.append(
                {
                    "document_id": document_id,
                    "path": normalized_path,
                    "rev": next_rev,
                    "content_hash": incoming_hash,
                    "deleted": incoming.deleted,
                    "sequence": sequence,
                }
            )

        data["vaults"][vault_id] = {
            **vault,
            "updated_at": now,
            "last_push_at": now if accepted or conflicts or skipped else vault.get("last_push_at"),
        }
        self._save(data)
        if conflicts:
            logger.warning(
                "Obsidian push to vault %s produced %d conflict(s)", vault_id, len(conflicts)
            )
        logger.info(
            "Obsidian push to vault %s finished: %d accepted, %d skipped, %d conflicts",
            vault_id,
            len(accepted),
            len(skipped),
            len(conflicts),
        )
        return {
            "vault_id": vault_id,
            "accepted": accepted,
            "skipped": skipped,
            "conflicts": conflicts,
            "next_cursor": data.get("sequence", 0),
        }

    def pull(self, *, vault_id: str, cursor: int | None = None) -> dict[str, Any]:
        data = self._load()
        self._vault(data, vault_id)
        resolved_cursor = cursor or 0
        changes = [
            change
            for change in data.get("changes", [])
            if change.get("source_id") == vault_id and int(change.get("sequence", 0)) > resolved_cursor
        ]
        documents: list[dict[str, Any]] = []
        for change in changes:
            document = data.get("documents", {}).get(change["document_id"])
            if not document:
                continue
            latest_body = self._latest_body(data, change["document_id"])
            documents.append({**document, "body": latest_body, "sequence": change["sequence"]})
        return {
            "vault_id": vault_id,
            "cursor": cursor,
            "next_cursor": data.get("sequence", 0),
            "documents": documents,
        }

    def document(self, document_id: str) -> dict[str, Any]:
        data = self._load()
        document = data.get("documents", {}).get(document_id)
        if not document:
            raise KeyError(document_id)
        revisions = [
            revision
            for revision in data.get("revisions", {}).values()
            if revision.get("document_id") == document_id
        ]
        revisions.sort(key=lambda item: int(item.get("rev", 0)), reverse=True)
        return {**document, "body": revisions[0]["body"] if revisions else "", "revisions": revisions}

    def resolve_conflict(
        self,
        *,
        conflict_id: str,
        actor: AccessIdentity,
        resolution: str,
        body: str | None = None,
    ) -> dict[str, Any]:
        data = self._load()
        conflict = data.get("conflicts", {}).get(conflict_id)
        if not conflict:
            raise KeyError(conflict_id)
        if conflict.get("status") != "open":
            return conflict

        if resolution == "accept_remote":
            updated = {**conflict, "status": "resolved_remote", "updated_at": now_iso()}
            data["conflicts"][conflict_id] = updated
            self._save(data)
            return updated

        if resolution == "accept_local":
            resolved_body = conflict["local_body"]
            path = conflict["path"]
        elif resolution == "manual":
            if body is None:
                raise ValueError("Manual conflict resolution requires a body.")
            resolved_body = body
            path = conflict["path"]
        elif resolution == "save_both":
            resolved_body = conflict["local_body"]
            path = self._conflict_copy_path(conflict["path"], conflict_id)
        else:
            raise ValueError(f"Unsupported conflict resolution: {resolution}")

        push_result = self.push(
            vault_id=conflict["source_id"],
            actor=actor,
            documents=[
                SyncPushDocument(
                    path=path,
                    content=resolved_body,
                    base_rev=conflict["remote_rev"] if resolution != "save_both" else None,
                )
            ],
            origin="conflict_resolution",
        )
        updated = {
            **conflict,
            "status": f"resolved_{resolution}",
            "updated_at": now_iso(),
            "resolution_document": push_result["accepted"][0] if push_result["accepted"] else None,
        }
        data = self._load()
        data.setdefault("conflicts", {})[conflict_id] = updated
        self._save(data)
        logger.info("Obsidian conflict %s resolved with %s", conflict_id, resolution)
        return updated

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": 1,
                "sequence": 0,
                "vaults": {},
                "documents": {},
                "revisions": {},
                "conflicts": {},
                "changes": [],
            }
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("version", 1)
        data.setdefault("sequence", 0)
        data.setdefault("vaults", {})
        data.setdefault("documents", {})
        data.setdefault("revisions", {})
        data.setdefault("conflicts", {})
        data.setdefault("changes", [])
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        tmp_path.replace(self.path)

    def _vault(self, data: dict[str, Any], vault_id: str) -> dict[str, Any]:
        vault = data.get("vaults", {}).get(vault_id)
        if not vault:
            raise KeyError(vault_id)
        return vault

    def vault_owner(self, vault_id: str) -> str | None:
        """Return the vault's ``owner_actor_id``; raise KeyError if unknown.

        Callers enforce ownership at the HTTP boundary — this only exposes the
        field, which was recorded at registration and previously read nowhere.
        """
        return self._vault(self._load(), vault_id).get("owner_actor_id")

    def document_vault_id(self, document_id: str) -> str | None:
        """Return the id of the vault a document belongs to (its ``source_id``)."""
        data = self._load()
        document = data.get("documents", {}).get(document_id)
        if not document:
            raise KeyError(document_id)
        return document.get("source_id")

    def conflict_vault_id(self, conflict_id: str) -> str | None:
        """Return the id of the vault a conflict belongs to (its ``source_id``)."""
        data = self._load()
        conflict = data.get("conflicts", {}).get(conflict_id)
        if not conflict:
            raise KeyError(conflict_id)
        return conflict.get("source_id")

    def _next_sequence(self, data: dict[str, Any]) -> int:
        data["sequence"] = int(data.get("sequence", 0)) + 1
        return data["sequence"]

    def _latest_body(self, data: dict[str, Any], document_id: str) -> str:
        revisions = [
            revision
            for revision in data.get("revisions", {}).values()
            if revision.get("document_id") == document_id
        ]
        if not revisions:
            return ""
        revisions.sort(key=lambda item: int(item.get("rev", 0)), reverse=True)
        return revisions[0].get("body", "")

    def _create_conflict(
        self,
        data: dict[str, Any],
        *,
        document_id: str,
        source_id: str,
        path: str,
        local_rev: int | None,
        remote_rev: int,
        local_hash: str,
        remote_hash: str,
        local_body: str,
        remote_body: str,
        reason: str,
    ) -> dict[str, Any]:
        now = now_iso()
        conflict_id = stable_id(
            "conflict",
            f"{document_id}:{local_rev}:{remote_rev}:{local_hash}:{remote_hash}",
        )
        conflict = SyncConflict(
            id=conflict_id,
            document_id=document_id,
            source_id=source_id,
            path=path,
            local_rev=local_rev,
            remote_rev=remote_rev,
            base_rev=local_rev,
            local_hash=local_hash,
            remote_hash=remote_hash,
            local_body=local_body,
            remote_body=remote_body,
            status="open",
            reason=reason,
            created_at=data.get("conflicts", {}).get(conflict_id, {}).get("created_at") or now,
            updated_at=now,
        )
        data.setdefault("conflicts", {})[conflict_id] = asdict(conflict)
        return asdict(conflict)

    def _conflict_copy_path(self, path: str, conflict_id: str) -> str:
        suffix = conflict_id.removeprefix("conflict_")[:8]
        if "." not in Path(path).name:
            return f"{path}.conflict-{suffix}"
        parent = Path(path).parent
        stem = Path(path).stem
        ext = Path(path).suffix
        filename = f"{stem}.conflict-{suffix}{ext}"
        return filename if str(parent) == "." else f"{parent.as_posix()}/{filename}"
