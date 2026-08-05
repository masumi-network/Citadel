"""Knowledge Conflict records and cheap, deterministic conflict detection.

Per CONTEXT.md, a Knowledge Conflict is a visible disagreement between pieces
of Structured Knowledge or their supporting Source Snapshots. Citadel prefers
the newer source-linked repository truth but never silently overwrites: every
detected disagreement is kept visible until a writer or admin resolves it.

Detection limitations (Phase 1): only cheap local signals are consulted —
Obsidian push conflicts (base-revision mismatches) and title matches against
the GitHub sync state digest or Obsidian sync state documents. A general Vault
Contribution with no title/path overlap against those states has no cheap
deterministic signal yet, so it is not conflict-checked.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha1, sha256
import json
import logging
from pathlib import Path
from typing import Any

from kb.config import CitadelConfig
from kb.security_scan import redact_secrets
from kb.source_search import _digest_sections, _load_state

logger = logging.getLogger(__name__)

EXCERPT_MAX_CHARS = 240
OPEN = "open"
RESOLVED = "resolved"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_conflict_id(basis: str) -> str:
    return f"kconflict_{sha1(basis.encode('utf-8')).hexdigest()[:16]}"


def clip_excerpt(text: str) -> str:
    clean = redact_secrets(" ".join(str(text or "").split()))
    if len(clean) <= EXCERPT_MAX_CHARS:
        return clean
    return f"{clean[: EXCERPT_MAX_CHARS - 1]}…"


@dataclass(frozen=True)
class ConflictSide:
    source: str
    excerpt: str
    timestamp: str | None = None


@dataclass(frozen=True)
class ConflictRecord:
    id: str
    detected_at: str
    kind: str
    summary: str
    side_a: ConflictSide
    side_b: ConflictSide
    dataset: str | None = None
    status: str = OPEN
    resolution_note: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None


@dataclass(frozen=True)
class ConflictCandidate:
    kind: str
    summary: str
    side_a: ConflictSide
    side_b: ConflictSide
    dedupe_key: str
    dataset: str | None = None


class KnowledgeConflictStore:
    """Bounded persistent store for Knowledge Conflicts (JSON state file)."""

    def __init__(self, path: Path | str, *, max_records: int = 500) -> None:
        self.path = Path(path)
        self.max_records = max(1, max_records)

    def record(self, candidate: ConflictCandidate) -> dict[str, Any]:
        data = self._load()
        dedupe_key = f"{candidate.kind}:{candidate.dedupe_key}"
        if candidate.dataset:
            dedupe_key = f"{candidate.dataset}:{dedupe_key}"
        conflict_id = stable_conflict_id(dedupe_key)
        existing = data["conflicts"].get(conflict_id)
        if existing and existing.get("status") == OPEN:
            return existing

        record = ConflictRecord(
            id=conflict_id,
            detected_at=now_iso(),
            kind=candidate.kind,
            summary=candidate.summary,
            side_a=candidate.side_a,
            side_b=candidate.side_b,
            dataset=candidate.dataset,
        )
        data["conflicts"][conflict_id] = asdict(record)
        self._prune(data)
        self._save(data)
        logger.warning(
            "Knowledge conflict recorded: %s (%s)", conflict_id, candidate.kind
        )
        return data["conflicts"][conflict_id]

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        conflicts = list(self._load()["conflicts"].values())
        if status:
            conflicts = [item for item in conflicts if item.get("status") == status]
        conflicts.sort(key=lambda item: str(item.get("detected_at") or ""), reverse=True)
        return conflicts

    def get(self, conflict_id: str) -> dict[str, Any]:
        conflict = self._load()["conflicts"].get(conflict_id)
        if not conflict:
            raise KeyError(conflict_id)
        return conflict

    def resolve(
        self,
        conflict_id: str,
        *,
        resolution_note: str,
        resolved_by: str,
    ) -> dict[str, Any]:
        data = self._load()
        conflict = data["conflicts"].get(conflict_id)
        if not conflict:
            raise KeyError(conflict_id)
        if conflict.get("status") == RESOLVED:
            return conflict
        updated = {
            **conflict,
            "status": RESOLVED,
            "resolution_note": clip_excerpt(resolution_note),
            "resolved_at": now_iso(),
            "resolved_by": resolved_by,
        }
        data["conflicts"][conflict_id] = updated
        self._save(data)
        logger.info("Knowledge conflict %s resolved", conflict_id)
        return updated

    def open_count(self) -> int:
        return len(self.list(status=OPEN))

    def _prune(self, data: dict[str, Any]) -> None:
        conflicts = list(data["conflicts"].values())
        if len(conflicts) <= self.max_records:
            return
        # Keep open conflicts visible for as long as possible: drop resolved
        # records first, then the oldest records.
        conflicts.sort(
            key=lambda item: (
                item.get("status") == OPEN,
                str(item.get("detected_at") or ""),
            )
        )
        for stale in conflicts[: len(conflicts) - self.max_records]:
            data["conflicts"].pop(stale["id"], None)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "conflicts": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "conflicts": {}}
        if not isinstance(data, dict):
            return {"version": 1, "conflicts": {}}
        data.setdefault("version", 1)
        data.setdefault("conflicts", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        temp_path.replace(self.path)


def obsidian_push_conflict_candidate(
    sync_conflict: dict[str, Any],
    *,
    vault_name: str | None = None,
    dataset: str | None = None,
) -> ConflictCandidate:
    """Build a Knowledge Conflict candidate from an Obsidian push conflict."""
    path = str(sync_conflict.get("path") or "unknown note")
    vault = vault_name or sync_conflict.get("source_id") or "Obsidian vault"
    return ConflictCandidate(
        kind="obsidian_push",
        summary=(
            f"Obsidian note '{path}' was pushed from a stale base revision; the "
            "newer server revision is kept and the disagreement stays visible."
        ),
        side_a=ConflictSide(
            source=f"obsidian:{vault}:{path} (incoming push)",
            excerpt=clip_excerpt(str(sync_conflict.get("local_body") or "")),
            timestamp=sync_conflict.get("created_at"),
        ),
        side_b=ConflictSide(
            source=f"obsidian:{vault}:{path} (server revision {sync_conflict.get('remote_rev')})",
            excerpt=clip_excerpt(str(sync_conflict.get("remote_body") or "")),
            timestamp=sync_conflict.get("updated_at"),
        ),
        dedupe_key=str(sync_conflict.get("id") or f"{vault}:{path}"),
        dataset=dataset,
    )


def detect_contribution_conflict(
    data: str,
    *,
    config: CitadelConfig,
    dataset: str | None = None,
) -> ConflictCandidate | None:
    """Cheap ingest-time check: does a contribution's title match existing
    indexed content with a different content hash?

    Consults the GitHub sync state digest sections and the Obsidian sync state
    documents only — both are local JSON files, so detection stays cheap and
    deterministic. Returns ``None`` when no overlap exists.
    """
    title = _contribution_title(data)
    if not title or len(title) < 4:
        return None
    contribution_hash = sha256(data.encode("utf-8")).hexdigest()
    normalized_title = title.casefold()

    candidate = _github_digest_conflict(
        normalized_title,
        contribution_hash,
        data=data,
        config=config,
        dataset=dataset,
    )
    if candidate:
        return candidate
    return _obsidian_document_conflict(
        normalized_title,
        contribution_hash,
        data=data,
        config=config,
        dataset=dataset,
    )


def _contribution_title(data: str) -> str | None:
    for line in data.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return None


def _github_digest_conflict(
    normalized_title: str,
    contribution_hash: str,
    *,
    data: str,
    config: CitadelConfig,
    dataset: str | None,
) -> ConflictCandidate | None:
    state = _load_state(Path(config.github_sync_state_path))
    digest = str(state.get("last_digest") or "").strip()
    if not digest:
        return None
    org = str(state.get("org") or config.github_org)
    for section_title, section_content in _digest_sections(digest):
        if section_title.casefold() != normalized_title:
            continue
        section_hash = sha256(section_content.encode("utf-8")).hexdigest()
        if section_hash == contribution_hash:
            return None
        return ConflictCandidate(
            kind="contribution_vs_repository_update",
            summary=(
                f"A Vault Contribution titled '{section_title}' disagrees with the "
                f"latest {org} Repository Daily Update section. Prefer the newer "
                "source-linked repository truth; the conflict stays visible."
            ),
            side_a=ConflictSide(
                source="vault_contribution",
                excerpt=clip_excerpt(data),
                timestamp=now_iso(),
            ),
            side_b=ConflictSide(
                source=f"github:{org} digest section '{section_title}'",
                excerpt=clip_excerpt(section_content),
                timestamp=state.get("last_digest_at") or state.get("last_checked_at"),
            ),
            dedupe_key=f"{org}:{section_title.casefold()}:{contribution_hash[:16]}",
            dataset=dataset,
        )
    return None


def _obsidian_document_conflict(
    normalized_title: str,
    contribution_hash: str,
    *,
    data: str,
    config: CitadelConfig,
    dataset: str | None,
) -> ConflictCandidate | None:
    state = _load_state(Path(config.obsidian_sync_state_path))
    documents = state.get("documents")
    if not isinstance(documents, dict):
        return None
    for document in documents.values():
        if not isinstance(document, dict) or document.get("deleted_at"):
            continue
        document_path = str(document.get("normalized_path") or document.get("path") or "")
        if not document_path:
            continue
        stem = Path(document_path).stem.casefold()
        if stem != normalized_title and document_path.casefold() != normalized_title:
            continue
        existing_hash = str(document.get("content_hash") or "")
        if not existing_hash or existing_hash == contribution_hash:
            return None
        return ConflictCandidate(
            kind="contribution_vs_obsidian_note",
            summary=(
                f"A Vault Contribution titled '{normalized_title}' matches the synced "
                f"Obsidian note '{document_path}' but carries different content. "
                "Both versions stay visible until resolved."
            ),
            side_a=ConflictSide(
                source="vault_contribution",
                excerpt=clip_excerpt(data),
                timestamp=now_iso(),
            ),
            side_b=ConflictSide(
                source=f"obsidian:{document.get('source_id')}:{document_path}",
                excerpt=f"content_hash {existing_hash[:16]}… (rev {document.get('current_rev')})",
                timestamp=document.get("updated_at"),
            ),
            dedupe_key=f"{document.get('id')}:{contribution_hash[:16]}",
            dataset=dataset,
        )
    return None
