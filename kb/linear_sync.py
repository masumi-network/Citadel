"""Linear workspace sync: org-wide issues → Central, assignee **Seat-Scoped Mirrors** → Nodes.

Read-only from Citadel's perspective — no write-back to Linear. Uses the Linear
GraphQL API with ``CITADEL_LINEAR_API_KEY``.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from kb.secure_http import open_secure

from kb.access import CENTRAL_DATASET, SEAT_DATASET_PREFIX, AccessStore, seat_dataset
from kb.cognee_client import _suppress_inline_cognify
from kb.learning import LearningProcess
from kb.security_scan import SecretContentError
from kb.service import Citadel
from kb.state_io import StateFileError, load_state_file, save_state_file

logger = logging.getLogger(__name__)

LINEAR_API = "https://api.linear.app/graphql"
STATE_VERSION = 1

# Tolerance for ordinary clock skew between Linear's clock and ours before an
# updatedAt counts as future-dated and is excluded from cursor advancement.
_CURSOR_SKEW_TOLERANCE = timedelta(minutes=5)
# Consecutive incremental passes that wrote nothing before warning that the
# sync may be stalled rather than the workspace merely quiet.
_UNCHANGED_STREAK_WARN = 12


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    """Parse a Linear ISO-8601 timestamp; None when absent or malformed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _linear_state_path(configured: str | None) -> str:
    if configured:
        return configured
    root = Path("/data/.citadel" if Path("/data").exists() else ".citadel")
    return str(root / "linear_sync_state.json")


class LinearAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class LinearIssue:
    id: str
    identifier: str
    title: str
    description: str
    url: str
    priority: int
    updated_at: str
    state_name: str
    state_type: str
    team_key: str
    team_name: str
    assignee_id: str | None
    assignee_name: str | None
    assignee_email: str | None

    @classmethod
    def from_node(cls, node: dict[str, Any]) -> LinearIssue | None:
        if not isinstance(node, dict) or not node.get("id"):
            return None
        state = node.get("state") if isinstance(node.get("state"), dict) else {}
        team = node.get("team") if isinstance(node.get("team"), dict) else {}
        assignee = node.get("assignee") if isinstance(node.get("assignee"), dict) else {}
        identifier = str(node.get("identifier") or node.get("id") or "").strip()
        title = str(node.get("title") or identifier or "Untitled").strip()
        return cls(
            id=str(node["id"]),
            identifier=identifier,
            title=title,
            description=str(node.get("description") or "").strip(),
            url=str(node.get("url") or "").strip(),
            priority=int(node.get("priority") or 0),
            updated_at=str(node.get("updatedAt") or ""),
            state_name=str(state.get("name") or ""),
            state_type=str(state.get("type") or ""),
            team_key=str(team.get("key") or ""),
            team_name=str(team.get("name") or ""),
            assignee_id=str(assignee["id"]) if assignee.get("id") else None,
            assignee_name=str(assignee.get("name") or assignee.get("displayName") or "").strip()
            or None,
            assignee_email=str(assignee.get("email") or "").strip().lower() or None,
        )


ISSUES_QUERY = """
query Issues($first: Int!, $after: String) {
  issues(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      identifier
      title
      description
      url
      priority
      updatedAt
      state { name type }
      team { key name }
      assignee { id name email displayName }
    }
  }
}
"""


USERS_QUERY = """
query Users($first: Int!) {
  users(first: $first) {
    nodes { id name email active }
  }
}
"""


class LinearClient:
    def __init__(self, *, api_key: str, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.last_issue_fetch_complete = True

    def query(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        request = Request(  # noqa: S310
            LINEAR_API,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": self.api_key,
            },
        )
        try:
            with open_secure(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LinearAPIError(f"Linear HTTP {exc.code}: {detail[:240]}") from exc
        except URLError as exc:
            raise LinearAPIError(f"Linear request failed: {exc.reason}") from exc
        if body.get("errors"):
            raise LinearAPIError(str(body["errors"])[:400])
        data = body.get("data")
        if not isinstance(data, dict):
            raise LinearAPIError("Linear response missing data")
        return data

    def fetch_issues(self, *, max_issues: int) -> list[LinearIssue]:
        issues: list[LinearIssue] = []
        cursor: str | None = None
        max_issues = max(max_issues, 1)
        self.last_issue_fetch_complete = False
        while len(issues) < max_issues:
            page_size = min(max_issues - len(issues), 100)
            data = self.query(
                ISSUES_QUERY,
                {"first": page_size, "after": cursor},
            )
            block = data.get("issues")
            if not isinstance(block, dict):
                break
            nodes = block.get("nodes")
            if not isinstance(nodes, list):
                break
            for raw in nodes:
                parsed = LinearIssue.from_node(raw)
                if parsed:
                    issues.append(parsed)
                    if len(issues) >= max_issues:
                        break
            page_info = block.get("pageInfo") if isinstance(block.get("pageInfo"), dict) else {}
            if not page_info.get("hasNextPage"):
                self.last_issue_fetch_complete = True
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break
        return issues

    def fetch_users(self, *, max_users: int = 250) -> list[dict[str, Any]]:
        """List workspace members (id/name/email) for assignee→seat auto-mapping."""
        data = self.query(USERS_QUERY, {"first": min(max(max_users, 1), 250)})
        block = data.get("users") if isinstance(data.get("users"), dict) else {}
        nodes = block.get("nodes")
        if not isinstance(nodes, list):
            return []
        return [node for node in nodes if isinstance(node, dict) and node.get("id")]


def format_issue_note(issue: LinearIssue) -> str:
    lines = [
        f"# Linear {issue.identifier}: {issue.title}",
        "",
        f"- **State:** {issue.state_name or 'unknown'} ({issue.state_type or 'n/a'})",
        f"- **Team:** {issue.team_name or issue.team_key or 'unknown'}",
        f"- **Priority:** {issue.priority}",
        f"- **Updated:** {issue.updated_at or 'unknown'}",
    ]
    if issue.assignee_name:
        lines.append(f"- **Assignee:** {issue.assignee_name}")
    if issue.url:
        lines.append(f"- **URL:** {issue.url}")
    if issue.description:
        lines.append("")
        lines.append(issue.description[:4000])
    return "\n".join(lines).strip()


def format_workspace_digest(issues: list[LinearIssue]) -> str:
    lines = ["# Linear workspace sync", "", f"Synced {len(issues)} issues.", ""]
    for issue in issues[:120]:
        assignee = issue.assignee_name or "unassigned"
        lines.append(
            f"- **{issue.identifier}** [{issue.state_name}] {issue.title} — {assignee}"
        )
    if len(issues) > 120:
        lines.append(f"- … and {len(issues) - 120} more")
    return "\n".join(lines).strip()


def seat_email_index(access_store: AccessStore) -> dict[str, str]:
    """Map assignee email (lowercase) → seat node dataset ``seat:{slug}``."""
    mapping: dict[str, str] = {}
    for principal in access_store.snapshot().get("principals", []):
        if not isinstance(principal, dict):
            continue
        slug = principal.get("seat_slug")
        email = principal.get("email")
        if slug and email:
            mapping[str(email).strip().lower()] = seat_dataset(str(slug))
    return mapping


def resolve_mirror_dataset(
    issue: LinearIssue,
    email_index: dict[str, str],
    *,
    linear_user_map: dict[str, str] | None = None,
) -> str | None:
    if issue.assignee_email and issue.assignee_email in email_index:
        return email_index[issue.assignee_email]
    if linear_user_map and issue.assignee_id and issue.assignee_id in linear_user_map:
        slug = linear_user_map[issue.assignee_id]
        return seat_dataset(slug)
    return None


class LinearSyncer:
    def __init__(
        self,
        citadel: Citadel,
        *,
        client: LinearClient | None = None,
        access_store: AccessStore | None = None,
    ) -> None:
        self.citadel = citadel
        self.config = citadel.config
        self.client = client
        self.access_store = access_store
        self.state_path = Path(_linear_state_path(self.config.linear_sync_state_path))

    def _client(self) -> LinearClient:
        if self.client:
            return self.client
        api_key = self.config.linear_api_key
        if not api_key:
            raise LinearAPIError("CITADEL_LINEAR_API_KEY is not configured")
        return LinearClient(api_key=api_key)

    def _load_state(self) -> dict[str, Any]:
        # Absent file = genuine first run. A corrupt file raises instead of
        # flattening to empty: an empty state reports "you have no assigned
        # issues" with a green source status (#148).
        payload = load_state_file(self.state_path)
        if payload is None:
            return {"version": STATE_VERSION, "issues": [], "mirrors": {}}
        return payload

    def _save_state(self, payload: dict[str, Any]) -> None:
        # Atomic (temp file + rename) so a restart mid-write cannot leave the
        # truncated file _load_state would refuse (#148).
        save_state_file(self.state_path, payload)

    async def status(self) -> dict[str, Any]:
        # A corrupt state file must show as a red source, not a 500 (#148).
        state_error: str | None = None
        try:
            state = self._load_state()
        except StateFileError as exc:
            state = {}
            state_error = str(exc)
        issues = state.get("issues") if isinstance(state.get("issues"), list) else []
        mirrors = state.get("mirrors") if isinstance(state.get("mirrors"), dict) else {}
        mirror_count = sum(len(v) for v in mirrors.values() if isinstance(v, list))
        return {
            "state_error": state_error,
            "enabled": bool(self.config.linear_api_key),
            "dataset": self.config.linear_sync_dataset,
            "last_synced_at": state.get("last_synced_at"),
            # Surface the last failure + when it was attempted so a broken sync is
            # visible instead of a stale green last_synced_at (#46).
            "last_error": state.get("last_error"),
            "last_attempt_at": state.get("last_attempt_at"),
            "issue_count": len(issues),
            "listing_complete": state.get("listing_complete"),
            "mirror_count": mirror_count,
            "auto_map_members_fetched": state.get("auto_map_members_fetched", 0),
            "auto_mapped_assignees": state.get("auto_mapped_assignees", 0),
            "unresolved_assignee_count": state.get("unresolved_assignee_count", 0),
            "auto_map_error": state.get("auto_map_error"),
            "state_path": str(self.state_path),
        }

    def issues_for_scope(
        self,
        *,
        scope: str,
        seat_dataset_name: str | None,
    ) -> list[dict[str, Any]]:
        state = self._load_state()
        issues = state.get("issues") if isinstance(state.get("issues"), list) else []
        if scope == "org":
            return [item for item in issues if isinstance(item, dict)]
        if scope == "my" and seat_dataset_name:
            mirrors = state.get("mirrors") if isinstance(state.get("mirrors"), dict) else {}
            ids = mirrors.get(seat_dataset_name)
            if not isinstance(ids, list):
                return []
            wanted = {str(item) for item in ids}
            return [
                item
                for item in issues
                if isinstance(item, dict) and str(item.get("identifier")) in wanted
            ]
        return []

    async def run(self, *, force: bool = False, await_cognify: bool = False) -> dict[str, Any]:
        """Sync Linear issues into Central (+ assignee seat mirrors).

        Incremental by default (#90): an issue is rewritten only when its
        ``updatedAt`` is newer than the stored cursor, when it is new to the
        local state, or when its seat mirror has never recorded it (a seat
        created after the issue last changed). ``force=True`` rewrites every
        fetched issue regardless — the pre-#90 unconditional behaviour, and
        what ``CITADEL_RUN_MODE=linear-sync`` uses.
        """
        if not self.config.linear_api_key:
            return {"ok": False, "enabled": False, "reason": "linear_api_key_missing"}

        try:
            client = self._client()
            issues = await asyncio.to_thread(
                client.fetch_issues,
                max_issues=self.config.linear_sync_max_issues,
            )
            listing_complete = client.last_issue_fetch_complete
        except LinearAPIError as exc:
            # Persist the failure so status()/list_sources surface a reason instead
            # of a stale green last_synced_at, and the evolve stage logs it (#46).
            state = self._load_state()
            state["last_error"] = str(exc)
            state["last_attempt_at"] = utc_now()
            self._save_state(state)
            logger.error("Linear sync failed: %s", exc)
            return {"ok": False, "enabled": True, "reason": "linear_api_error", "error": str(exc)}
        sync_started_at = utc_now()
        email_index = (
            seat_email_index(self.access_store)
            if self.access_store
            else {}
        )
        # Auto-resolve assignee_id -> seat by matching Linear members' emails to
        # seat emails, using the node's own Linear key (#46). This populates seat
        # mirrors without a manual CITADEL_LINEAR_USER_MAP, and works even when the
        # per-issue assignee.email is null (a common non-admin-key limitation) —
        # the id is matched instead. Explicit config map entries always win.
        user_map = dict(self.config.linear_user_map)
        auto_mapped = 0
        auto_map_members_fetched = 0
        auto_map_error: str | None = None
        members: list[dict[str, Any]] = []
        if self.access_store:
            email_to_slug = {
                email: dataset.removeprefix(SEAT_DATASET_PREFIX)
                for email, dataset in email_index.items()
            }
            try:
                members = await asyncio.to_thread(self._client().fetch_users)
            except LinearAPIError as exc:
                # Carry the failure into the payload and persisted state so a
                # 0 auto-map count is not misread as "the key cannot read
                # member emails" when the fetch itself failed (#148).
                auto_map_error = str(exc)
                logger.warning(
                    "Linear member fetch failed; mirrors fall back to assignee email/config map: %s",
                    exc,
                )
            auto_map_members_fetched = len(members)
            for member in members:
                member_id = member.get("id")
                member_email = (member.get("email") or "").strip().lower()
                if member_id and member_email and member_email in email_to_slug:
                    if member_id not in user_map:
                        user_map[member_id] = email_to_slug[member_email]
                        auto_mapped += 1

        learning = LearningProcess(self.citadel)
        central_dataset = self.config.linear_sync_dataset or CENTRAL_DATASET
        session_id = self.config.linear_sync_session

        # Incremental sync (#90): rewrite an issue only when Linear's updatedAt is
        # newer than the cursor the previous run stored. The cursor is the max
        # updatedAt SEEN — Linear's clock, not ours — so an issue updated while a
        # sync is in flight still sorts after the cursor and is caught next pass;
        # comparing against our own wall clock would skip exactly those writes.
        # An issue absent from the prior state is always written (new issue, or
        # the max_issues window shifted onto it). force=True rewrites everything.
        prior_state = self._load_state()
        # Anything past this horizon is future-dated, not merely skewed. A cursor
        # ahead of real time makes `updated > prior_cursor` False for EVERY issue
        # forever — a permanent stall that still reports ok:true with a fresh
        # last_synced_at — so future-dated values are barred from the cursor on
        # both the read side (below) and the write side (cursor advancement).
        horizon = datetime.now(UTC) + _CURSOR_SKEW_TOLERANCE
        prior_cursor = None if force else _parse_iso(prior_state.get("last_seen_updated_at"))
        if prior_cursor is not None and prior_cursor > horizon:
            # Self-heal state poisoned before the clamp existed (or edited by
            # hand): ignore the stored cursor and run this pass as a full sync.
            logger.warning(
                "Stored Linear sync cursor %s is future-dated; ignoring it and running a full pass",
                prior_state.get("last_seen_updated_at"),
            )
            prior_cursor = None
        prior_issues = prior_state.get("issues")
        prior_issue_by_id = {
            str(item.get("id")): item
            for item in (prior_issues if isinstance(prior_issues, list) else [])
            if isinstance(item, dict) and item.get("id")
        }
        prior_ids = {
            str(item.get("id"))
            for item in (prior_issues if isinstance(prior_issues, list) else [])
            if isinstance(item, dict) and item.get("id")
        }
        prior_mirrors = (
            prior_state.get("mirrors") if isinstance(prior_state.get("mirrors"), dict) else {}
        )

        def _changed(issue: LinearIssue) -> bool:
            if prior_cursor is None or issue.id not in prior_ids:
                return True
            updated = _parse_iso(issue.updated_at)
            # A malformed timestamp fails OPEN into a write: a redundant write is
            # visible, a silently skipped issue is not.
            return updated is None or updated > prior_cursor

        changed_ids = {issue.id for issue in issues if _changed(issue)}
        # A deletion changes the digest (the issue drops out of the listing)
        # without moving any surviving issue's updatedAt, so it refreshes the
        # digest too.
        removed_ids = (
            prior_ids - {issue.id for issue in issues}
            if listing_complete
            else set()
        )
        tombstoned_count = 0
        tombstoned_datasets: set[str] = set()

        async def _tombstone_linear_issue(
            *,
            dataset: str,
            issue_id: str,
            reason: str,
        ) -> None:
            nonlocal tombstoned_count
            if self.citadel.lifecycle_store is None:
                return
            tombstones = await self.citadel.tombstone_source(
                dataset=dataset,
                source_key=f"linear:issue:{issue_id}",
                reason=reason,
                capture_actor_id="linear-sync",
                capture_run_id=sync_started_at,
            )
            tombstoned_count += len(tombstones)
            if tombstones:
                tombstoned_datasets.add(dataset)

        for removed_id in sorted(removed_ids):
            await _tombstone_linear_issue(
                dataset=central_dataset,
                issue_id=removed_id,
                reason="Linear issue removed from synchronized scope",
            )
            prior_issue = prior_issue_by_id.get(removed_id, {})
            identifier = str(prior_issue.get("identifier") or "")
            if identifier:
                for mirror_dataset, identifiers in prior_mirrors.items():
                    if isinstance(identifiers, list) and identifier in {
                        str(item) for item in identifiers
                    }:
                        await _tombstone_linear_issue(
                            dataset=str(mirror_dataset),
                            issue_id=removed_id,
                            reason="Linear issue removed from synchronized scope",
                        )

        # Coalesce cognify (#46/#52): a full resync writes the digest + ~200 issues +
        # seat mirrors. Each write used to schedule its OWN background cognify, so
        # the on-demand POST /api/linear-sync/run fired ~200 Kuzu-writing cognifies
        # that stormed the writer lock and starved the request into a timeout. Write
        # ADD-ONLY here (defer_cognify=True) and schedule ONE cognify over every
        # dataset touched after the loop instead.
        # Secret-scan containment (#117): learning.learn scans every document
        # (ADR-0005) and raises SecretContentError on a blocking finding. The
        # sibling syncers (github_sync, repo_content_sync) record a block and
        # keep going; here ONE poisoned issue used to kill the entire sync —
        # one refused entry out of 200 zeroing the whole Linear surface.
        # Blocked items are recorded by identifier only, never content, and
        # simply retried whenever the issue next changes.
        blocked: list[str] = []

        central_outcome = None
        if force or changed_ids or removed_ids:
            digest = format_workspace_digest(issues)
            try:
                central_outcome = await learning.learn(
                    digest,
                    dataset=central_dataset,
                    tags=["linear-workspace", "linear-sync"],
                    session_id=session_id,
                    source_key="linear:workspace-digest",
                    media_type="text/markdown",
                    capture_actor_id="linear-sync",
                    capture_run_id=sync_started_at,
                    operation="linear_sync",
                    run_improve=self.config.linear_sync_run_improve,
                    tier="full",
                    defer_cognify=True,
                )
            except SecretContentError as exc:
                blocked.append("workspace-digest")
                logger.warning(
                    "Linear workspace digest blocked by the secret scanner: %s",
                    exc.public_message,
                )

        mirrored = 0
        skipped_unchanged = 0
        mirrors: dict[str, list[str]] = {}
        written_mirror_datasets: list[str] = []
        unresolved_assignee_count = 0
        # Tracked independently of central_outcome (the workspace digest's own
        # result): the digest and each issue's Central note are separate
        # learning.learn() calls, so the digest can be blocked while an
        # unrelated issue's Central write still lands. touched_datasets must
        # reflect that real write, not just the digest's fate.
        central_issue_written = False
        for issue in issues:
            changed = issue.id in changed_ids
            mirror_dataset = resolve_mirror_dataset(issue, email_index, linear_user_map=user_map)
            prior_mirror_ids = prior_mirrors.get(mirror_dataset) if mirror_dataset else None
            mirror_has_note = mirror_dataset is not None and (
                isinstance(prior_mirror_ids, list)
                and issue.identifier in {str(item) for item in prior_mirror_ids}
            )

            central_blocked = False
            if changed:
                # Write each issue's full text (title + description) to Central so
                # linear_search returns real issues org-wide — the digest only carried
                # titles, leaving the 200 synced issues invisible to search (#52).
                try:
                    issue_outcome = await learning.learn(
                        format_issue_note(issue),
                        dataset=central_dataset,
                        tags=[
                            "linear-issue",
                            "linear-sync",
                            f"linear:{issue.identifier}",
                            # Team as a structured, filterable metadata tag so Central issues
                            # are discoverable by team (e.g. "what is the marketing team
                            # working on?"). The human team NAME also rides in the note body
                            # (format_issue_note) for semantic search.
                            f"team:{issue.team_key}" if issue.team_key else "linear",
                        ],
                        session_id=session_id,
                        source_key=f"linear:issue:{issue.id}",
                        source_locator=issue.url or None,
                        media_type="text/markdown",
                        capture_actor_id="linear-sync",
                        capture_run_id=sync_started_at,
                        operation="linear_sync",
                        run_improve=False,
                        tier="light",
                        defer_cognify=True,
                    )
                    if issue_outcome.ingest.accepted:
                        # Only an ACCEPTED add is a write; a filter rejection or
                        # in-process duplicate returns accepted=False without
                        # raising and stores nothing.
                        central_issue_written = True
                except SecretContentError as exc:
                    central_blocked = True
                    blocked.append(issue.identifier)
                    logger.warning(
                        "Linear issue %s blocked by the secret scanner; its Central "
                        "write and mirror are withheld this pass (content not "
                        "stored): %s",
                        issue.identifier,
                        exc.public_message,
                    )
            else:
                skipped_unchanged += 1

            if not mirror_dataset:
                if issue.assignee_id:
                    unresolved_assignee_count += 1
                continue

            if central_blocked:
                # Refused content must not reach a seat mirror either. A note
                # that already landed on a PRIOR (unblocked) pass stays listed
                # — it is not overwritten with the now-refused text, it just
                # goes stale until the issue changes again and passes clean.
                if mirror_has_note:
                    mirrors.setdefault(mirror_dataset, []).append(issue.identifier)
                continue

            # Backfill a mirror the state has never recorded this issue in even
            # when the issue itself is unchanged — a seat created (or mapped)
            # AFTER the issue last changed would otherwise never receive it
            # until the issue next updates.
            if not changed and mirror_has_note:
                mirrors.setdefault(mirror_dataset, []).append(issue.identifier)
                continue

            note = format_issue_note(issue)
            try:
                await learning.learn(
                    note,
                    dataset=mirror_dataset,
                    tags=[
                        "linear-assignee",
                        "linear-issue",
                        f"linear:{issue.identifier}",
                        f"team:{issue.team_key}" if issue.team_key else "linear",
                    ],
                    session_id=f"linear-{mirror_dataset.removeprefix(SEAT_DATASET_PREFIX)}",
                    source_key=f"linear:issue:{issue.id}",
                    source_locator=issue.url or None,
                    media_type="text/markdown",
                    capture_actor_id="linear-sync",
                    capture_run_id=sync_started_at,
                    operation="linear_mirror",
                    run_improve=False,
                    tier="light",
                    defer_cognify=True,
                )
            except SecretContentError as exc:
                blocked.append(issue.identifier)
                logger.warning(
                    "Linear issue %s mirror to %s blocked by the secret scanner "
                    "(content not stored): %s",
                    issue.identifier,
                    mirror_dataset,
                    exc.public_message,
                )
                if mirror_has_note:
                    mirrors.setdefault(mirror_dataset, []).append(issue.identifier)
                continue

            # The state mapping covers every fetched issue whose mirror note is
            # actually present — this pass or a prior one — since
            # issues_for_scope reads it directly.
            mirrors.setdefault(mirror_dataset, []).append(issue.identifier)
            if mirror_dataset not in written_mirror_datasets:
                written_mirror_datasets.append(mirror_dataset)
            mirrored += 1

        current_issue_by_identifier = {issue.identifier: issue.id for issue in issues}
        for mirror_dataset, prior_identifiers in prior_mirrors.items():
            if not isinstance(prior_identifiers, list):
                continue
            current_identifiers = {
                str(item) for item in mirrors.get(str(mirror_dataset), [])
            }
            for identifier in sorted(
                {str(item) for item in prior_identifiers} - current_identifiers
            ):
                issue_id = current_issue_by_identifier.get(identifier)
                if issue_id is not None:
                    await _tombstone_linear_issue(
                        dataset=str(mirror_dataset),
                        issue_id=issue_id,
                        reason="Linear issue mirror assignment removed",
                    )
                elif not listing_complete:
                    mirrors.setdefault(str(mirror_dataset), []).append(identifier)

        # One coalesced cognify over Central + every seat mirror we wrote — unless
        # nothing was written (a fully-unchanged incremental pass has nothing to
        # fold in) or inline cognify is suppressed (the evolve Phase-1 subprocess
        # is add-only and the web cognifies in Phase 2 as the sole Kuzu writer, #47).
        touched_datasets: list[str] = []
        if central_outcome is not None or central_issue_written:
            touched_datasets.append(central_dataset)
        touched_datasets.extend(written_mirror_datasets)
        touched_datasets.extend(sorted(tombstoned_datasets))
        touched_datasets = list(dict.fromkeys(touched_datasets))
        # What this pass OBSERVED about the coalesced graph write, reported as
        # `central_ingested` below. Only the awaited branch sees the cognify
        # finish (or fail); the scheduled branch has merely REQUESTED one, and
        # must say so instead of implying completion.
        cognify_observed: str | None = None
        if touched_datasets and self.citadel.lifecycle_store is not None:
            if await_cognify:
                try:
                    await self.citadel.wait_for_lifecycle_idle()
                except Exception:  # noqa: BLE001 - retained work remains retryable
                    logger.exception("Linear lifecycle projection failed")
                    self.citadel.resume_lifecycle_queue()
                    cognify_observed = "cognify_failed"
                else:
                    cognify_observed = "cognified"
            else:
                cognify_observed = "queued_not_confirmed"
        elif touched_datasets and not _suppress_inline_cognify():
            cognify_datasets = list(dict.fromkeys(touched_datasets))
            if await_cognify:
                # Standalone CITADEL_RUN_MODE=linear-sync: AWAIT the single coalesced
                # cognify so a manual forced run actually indexes the issues, instead
                # of scheduling a task that asyncio.run cancels on teardown. Best-effort
                # — the writes already landed in Postgres, so a cognify failure (e.g. a
                # cross-process Kuzu lock if the web is writing, #47) is logged, not
                # raised; the next evolve pass folds the data into the graph.
                try:
                    await self.citadel.cognee.cognify(datasets=cognify_datasets)
                except Exception:  # noqa: BLE001 - writes succeeded; cognify is a follow-on
                    logger.exception("Linear sync coalesced cognify failed")
                    self.citadel.cognee.schedule_cognify(cognify_datasets)
                    cognify_observed = "cognify_failed"
                else:
                    cognify_observed = "cognified"
            else:
                # On-demand endpoint / evolve: background it so the request returns
                # without waiting on the graph write.
                queued = self.citadel.cognee.schedule_cognify(cognify_datasets)
                cognify_observed = "queued_not_confirmed" if queued else "not_scheduled"
        elif touched_datasets:
            # Evolve Phase-1 subprocess (CITADEL_SUPPRESS_INLINE_COGNIFY): add-only
            # by design; the web cognifies in Phase 2 as the sole Kuzu writer.
            cognify_observed = "suppressed"

        # Advance the cursor to the newest updatedAt seen (keep the prior one
        # when a pass sees nothing newer, e.g. an empty or truncated fetch),
        # clamped to the present: one future-dated updatedAt (Linear-side clock
        # trouble, a bad import, a migration stamping the wrong year) must never
        # pin the cursor ahead of real time and stall every later pass. The
        # future-dated issue itself keeps being rewritten each pass (`updated >
        # cursor` stays true — fail open, same rule as malformed timestamps)
        # and is named in the warning, so the anomaly stays visible.
        new_cursor = None
        best: datetime | None = None
        stored_raw = prior_state.get("last_seen_updated_at")
        stored = _parse_iso(stored_raw)
        if stored is not None and stored <= horizon:
            best = stored
            new_cursor = stored_raw
        for issue in issues:
            parsed = _parse_iso(issue.updated_at)
            if parsed is None:
                continue
            if parsed > horizon:
                logger.warning(
                    "Linear issue %s has a future-dated updatedAt (%s); "
                    "not advancing the sync cursor past it",
                    issue.identifier,
                    issue.updated_at,
                )
                continue
            if best is None or parsed > best:
                best = parsed
                new_cursor = issue.updated_at

        # A long run of write-less passes is either a genuinely quiet workspace
        # or a stalled cursor — the logs are otherwise identical, so say so and
        # name the disambiguator. A force=True pass writes (resetting the
        # streak) and rebuilds the cursor from scratch.
        streak_raw = prior_state.get("unchanged_pass_streak")
        streak = (streak_raw if isinstance(streak_raw, int) and streak_raw >= 0 else 0) + 1
        if touched_datasets:
            streak = 0
        elif streak >= _UNCHANGED_STREAK_WARN:
            logger.warning(
                "Linear sync has written nothing for %s consecutive passes "
                "(%s issues fetched each time). Either the workspace is quiet or "
                "the incremental cursor is stalled — a force=True run "
                "(POST /api/linear-sync/run or CITADEL_RUN_MODE=linear-sync) "
                "distinguishes the two.",
                streak,
                len(issues),
            )

        persisted_issues = [
            asdict(issue) for issue in issues if issue.identifier not in blocked
        ]
        if not listing_complete:
            fetched_ids = {issue.id for issue in issues}
            persisted_issues.extend(
                item
                for item in (prior_issues if isinstance(prior_issues, list) else [])
                if isinstance(item, dict) and str(item.get("id")) not in fetched_ids
            )
        payload = {
            "version": STATE_VERSION,
            "last_synced_at": utc_now(),
            "last_seen_updated_at": new_cursor,
            "unchanged_pass_streak": streak,
            "last_error": None,  # clear any prior failure on a successful sync
            "last_attempt_at": utc_now(),
            "auto_map_error": auto_map_error,
            "auto_map_members_fetched": auto_map_members_fetched,
            "auto_mapped_assignees": auto_mapped,
            "unresolved_assignee_count": unresolved_assignee_count,
            "listing_complete": listing_complete,
            # Scanner-blocked issues are excluded here, not just from Central/
            # mirror writes: issues_for_scope() reads this list directly, so a
            # blocked issue's title/description landing here would re-serve
            # refused content through Linear search — identifier only (via
            # `blocked` above), never content.
            "issues": persisted_issues,
            "mirrors": mirrors,
        }
        self._save_state(payload)

        return {
            "ok": True,
            "enabled": True,
            "issue_count": len(issues),
            "listing_complete": listing_complete,
            # Incrementality diagnostics (#90): how many issues this pass actually
            # rewrote vs skipped as unchanged since the stored cursor.
            "written_count": len(changed_ids),
            "skipped_unchanged": skipped_unchanged,
            "mirrored_count": mirrored,
            "tombstoned_count": tombstoned_count,
            # Diagnostics for #46: how many assignees were auto-mapped to seats by
            # email. Only read 0 as "the Linear key cannot read member emails —
            # set CITADEL_LINEAR_USER_MAP" when auto_map_error is None; a failed
            # member fetch also leaves this at 0 (#148).
            "auto_mapped_assignees": auto_mapped,
            "auto_map_members_fetched": auto_map_members_fetched,
            "auto_map_error": auto_map_error,
            "unresolved_assignee_count": unresolved_assignee_count,
            # Fate of this pass's Central writes as OBSERVED, never assumed.
            # This used to report cognee.add() acceptance as True, but add()
            # only QUEUES the graph write (cognify never runs synchronously),
            # so a pass whose graph write later died was byte-identical to a
            # working one. States a caller can branch on:
            #   "cognified"             awaited coalesced cognify completed
            #   "cognify_failed"        awaited coalesced cognify raised
            #   "queued_not_confirmed"  background cognify scheduled; outcome
            #                           not observed by this pass
            #   "not_scheduled"        durable background queue rejected the work
            #   "suppressed"            add-only mode; evolve Phase 2 cognifies
            #   None                    no accepted Central write this pass
            "central_ingested": (
                cognify_observed
                if (
                    (central_outcome is not None and central_outcome.ingest.accepted)
                    or central_issue_written
                )
                else None
            ),
            "mirrors": mirrors,
            # Issues (or the workspace digest) the secret scanner refused this
            # pass (#117): identifiers only, never content. A blocked write is
            # contained, not silently dropped.
            "blocked": blocked,
            "blocked_count": len(blocked),
            "last_synced_at": payload["last_synced_at"],
        }
