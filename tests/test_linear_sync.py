from __future__ import annotations

from typing import Any

import pytest

from kb.access import AccessStore, seat_dataset
from kb.config import CitadelConfig
from kb.linear_sync import (
    LinearAPIError,
    LinearClient,
    LinearIssue,
    LinearSyncer,
    format_issue_note,
    resolve_mirror_dataset,
    seat_email_index,
)
from kb.service import Citadel


class FakeLinearClient(LinearClient):
    def __init__(
        self, issues: list[dict[str, Any]], users: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(api_key="test-key")
        self._issues = issues
        self._users = users or []

    def fetch_issues(self, *, max_issues: int) -> list[LinearIssue]:
        parsed = [LinearIssue.from_node(item) for item in self._issues]
        return [item for item in parsed if item][:max_issues]

    def fetch_users(self, *, max_users: int = 250) -> list[dict[str, Any]]:
        return self._users


@pytest.fixture
def sample_issues() -> list[dict[str, Any]]:
    return [
        {
            "id": "issue-1",
            "identifier": "ENG-1",
            "title": "Ship Linear sync",
            "description": "Implement workspace sync.",
            "url": "https://linear.app/acme/issue/ENG-1",
            "priority": 2,
            "updatedAt": "2026-06-25T10:00:00Z",
            "state": {"name": "In Progress", "type": "started"},
            "team": {"key": "ENG", "name": "Engineering"},
            "assignee": {
                "id": "user-john",
                "name": "John Doe",
                "email": "john@example.com",
            },
        },
        {
            "id": "issue-2",
            "identifier": "ENG-2",
            "title": "Org-wide roadmap",
            "description": "Central only.",
            "url": "https://linear.app/acme/issue/ENG-2",
            "priority": 1,
            "updatedAt": "2026-06-25T09:00:00Z",
            "state": {"name": "Backlog", "type": "backlog"},
            "team": {"key": "ENG", "name": "Engineering"},
            "assignee": None,
        },
    ]


def test_format_issue_note(sample_issues: list[dict[str, Any]]) -> None:
    issue = LinearIssue.from_node(sample_issues[0])
    assert issue is not None
    note = format_issue_note(issue)
    assert "ENG-1" in note
    assert "John Doe" in note


def test_seat_email_index(tmp_path: Any) -> None:
    store = AccessStore(str(tmp_path / "access.json"))
    store.create_seat(name="John Doe", slug="john", email="john@example.com", issue_token=False)
    mapping = seat_email_index(store)
    assert mapping["john@example.com"] == seat_dataset("john")


def test_resolve_mirror_dataset(sample_issues: list[dict[str, Any]]) -> None:
    issue = LinearIssue.from_node(sample_issues[0])
    assert issue is not None
    dataset = resolve_mirror_dataset(
        issue,
        {"john@example.com": seat_dataset("john")},
    )
    assert dataset == seat_dataset("john")


@pytest.mark.asyncio
async def test_linear_sync_ingests_central_and_mirror(
    tmp_path: Any,
    sample_issues: list[dict[str, Any]],
    monkeypatch: Any,
) -> None:
    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "linear_state.json"),
        access_store_path=str(tmp_path / "access.json"),
    )
    citadel = Citadel(config)
    store = AccessStore(config.access_store_path)
    store.create_seat(name="John Doe", slug="john", email="john@example.com", issue_token=False)

    ingests: list[dict[str, Any]] = []

    async def fake_learn(
        self: Any,
        data: str,
        *,
        dataset: str | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
        operation: str = "ingest",
        run_improve: bool = False,
        detect_conflicts: bool = True,
        tier: str = "full",
        defer_cognify: bool = False,
    ) -> Any:
        ingests.append(
            {
                "dataset": dataset,
                "tags": tags or [],
                "operation": operation,
                "tier": tier,
                "data": data[:80],
                "defer_cognify": defer_cognify,
            }
        )

        class FakeResult:
            accepted = True

        class Outcome:
            ingest = FakeResult()

        return Outcome()

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", fake_learn)
    # #46: the resync must coalesce ONE cognify over the datasets it touched
    # (Central + mirrors) instead of one-per-issue — capture it here.
    scheduled: list[list[str]] = []
    monkeypatch.setattr(
        citadel.cognee, "schedule_cognify", lambda datasets: scheduled.append(list(datasets))
    )

    syncer = LinearSyncer(
        citadel,
        client=FakeLinearClient(sample_issues),
        access_store=store,
    )
    result = await syncer.run(force=True)
    assert result["ok"] is True
    assert result["issue_count"] == 2
    assert result["mirrored_count"] == 1
    assert any(item["dataset"] == "masumi-network" for item in ingests)
    assert any(item["dataset"] == seat_dataset("john") for item in ingests)
    # Every write is add-only (deferred), and exactly one coalesced cognify is
    # scheduled over Central + the seat mirror.
    assert all(item["defer_cognify"] is True for item in ingests)
    assert scheduled == [["masumi-network", seat_dataset("john")]]
    assert syncer.issues_for_scope(scope="my", seat_dataset_name=seat_dataset("john"))
    assert len(syncer.issues_for_scope(scope="org", seat_dataset_name=None)) == 2


@pytest.mark.asyncio
async def test_linear_sync_writes_each_issue_to_central(
    tmp_path: Any,
    sample_issues: list[dict[str, Any]],
    monkeypatch: Any,
) -> None:
    # #52: each issue's full text (not just the digest of titles) must reach
    # Central so linear_search returns real issues org-wide.
    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "s.json"),
        access_store_path=str(tmp_path / "a.json"),
    )
    citadel = Citadel(config)
    ingests: list[dict[str, Any]] = []

    async def fake_learn(self: Any, data: str, *, dataset: str | None = None, tags: list[str] | None = None, **_: Any) -> Any:
        ingests.append({"dataset": dataset, "tags": tags or [], "data": data})

        class Outcome:
            class ingest:
                accepted = True

        return Outcome()

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", fake_learn)
    monkeypatch.setattr(citadel.cognee, "schedule_cognify", lambda datasets: None)
    syncer = LinearSyncer(citadel, client=FakeLinearClient(sample_issues))

    result = await syncer.run(force=True)
    assert result["ok"] is True

    central_issue_writes = [
        i for i in ingests if i["dataset"] == "masumi-network" and "linear-issue" in i["tags"]
    ]
    id_tags = {tag for i in central_issue_writes for tag in i["tags"] if tag.startswith("linear:")}
    assert "linear:ENG-1" in id_tags
    assert "linear:ENG-2" in id_tags
    # The full description reaches Central, not just the title.
    assert any("Implement workspace sync." in i["data"] for i in central_issue_writes)
    # Each Central issue carries a structured team tag so issues are filterable by
    # team (both sample issues are on team ENG).
    team_tags = {tag for i in central_issue_writes for tag in i["tags"] if tag.startswith("team:")}
    assert team_tags == {"team:ENG"}


@pytest.mark.asyncio
async def test_linear_sync_auto_maps_assignee_by_member_email(
    tmp_path: Any, monkeypatch: Any
) -> None:
    # #46: when the issue payload omits assignee.email, resolve the mirror by
    # matching the assignee id against the Linear members list (id->email) vs seats.
    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "s.json"),
        access_store_path=str(tmp_path / "a.json"),
    )
    citadel = Citadel(config)
    store = AccessStore(config.access_store_path)
    store.create_seat(name="John Doe", slug="john", email="john@example.com", issue_token=False)

    issues = [
        {
            "id": "issue-1",
            "identifier": "ENG-1",
            "title": "x",
            "description": "d",
            "url": "u",
            "priority": 1,
            "updatedAt": "2026-06-25T10:00:00Z",
            "state": {"name": "In Progress", "type": "started"},
            "team": {"key": "ENG", "name": "Eng"},
            "assignee": {"id": "linear-user-john", "name": "John", "email": None},  # no email
        }
    ]
    members = [{"id": "linear-user-john", "name": "John Doe", "email": "john@example.com", "active": True}]

    async def fake_learn(self: Any, data: str, **_: Any) -> Any:
        class Outcome:
            class ingest:
                accepted = True

        return Outcome()

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", fake_learn)
    monkeypatch.setattr(citadel.cognee, "schedule_cognify", lambda datasets: None)
    syncer = LinearSyncer(
        citadel, client=FakeLinearClient(issues, users=members), access_store=store
    )

    result = await syncer.run(force=True)
    assert result["ok"] is True
    assert result["auto_mapped_assignees"] == 1
    assert result["mirrored_count"] == 1
    assert seat_dataset("john") in result["mirrors"]


@pytest.mark.asyncio
async def test_linear_sync_defers_coalesced_cognify_when_inline_suppressed(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any
) -> None:
    # #46/#47: in the evolve Phase-1 subprocess (CITADEL_SUPPRESS_INLINE_COGNIFY=true)
    # the resync is add-only and the web cognifies in Phase 2 as the sole Kuzu
    # writer — so the coalesced cognify must NOT be scheduled here.
    monkeypatch.setenv("CITADEL_SUPPRESS_INLINE_COGNIFY", "true")
    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "s.json"),
        access_store_path=str(tmp_path / "a.json"),
    )
    citadel = Citadel(config)

    async def fake_learn(self: Any, data: str, **_: Any) -> Any:
        class Outcome:
            class ingest:
                accepted = True

        return Outcome()

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", fake_learn)
    scheduled: list[list[str]] = []
    monkeypatch.setattr(
        citadel.cognee, "schedule_cognify", lambda datasets: scheduled.append(list(datasets))
    )
    syncer = LinearSyncer(citadel, client=FakeLinearClient(sample_issues))

    result = await syncer.run(force=True)
    assert result["ok"] is True
    assert scheduled == []  # suppressed: Phase 2 cognifies, not the subprocess


@pytest.mark.asyncio
async def test_linear_sync_awaits_coalesced_cognify_when_requested(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any
) -> None:
    # #46/standalone: CITADEL_RUN_MODE=linear-sync passes await_cognify=True so a
    # manual forced run AWAITS the single coalesced cognify (indexing the issues)
    # instead of scheduling a task that asyncio.run cancels on teardown.
    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "s.json"),
        access_store_path=str(tmp_path / "a.json"),
    )
    citadel = Citadel(config)

    async def fake_learn(self: Any, data: str, **_: Any) -> Any:
        class Outcome:
            class ingest:
                accepted = True

        return Outcome()

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", fake_learn)
    awaited: list[list[str]] = []
    scheduled: list[list[str]] = []

    async def fake_cognify(*, datasets: Any, force: bool = False) -> dict[str, Any]:
        awaited.append(list(datasets))
        return {"ok": True}

    monkeypatch.setattr(citadel.cognee, "cognify", fake_cognify)
    monkeypatch.setattr(
        citadel.cognee, "schedule_cognify", lambda datasets: scheduled.append(list(datasets))
    )
    syncer = LinearSyncer(citadel, client=FakeLinearClient(sample_issues))

    result = await syncer.run(force=True, await_cognify=True)
    assert result["ok"] is True
    assert awaited == [["masumi-network"]]  # awaited inline over Central (no mirrors)
    assert scheduled == []  # awaited, not backgrounded


@pytest.mark.asyncio
async def test_linear_sync_surfaces_api_error(tmp_path: Any) -> None:
    # #46: an API failure returns ok:False with a reason and is persisted so
    # status()/list_sources stop showing a stale green last_synced_at.
    from kb.linear_sync import LinearAPIError

    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "s.json"),
    )
    citadel = Citadel(config)

    class BrokenClient(LinearClient):
        def __init__(self) -> None:
            super().__init__(api_key="x")

        def fetch_issues(self, *, max_issues: int) -> list[LinearIssue]:
            raise LinearAPIError("401 Unauthorized")

    syncer = LinearSyncer(citadel, client=BrokenClient())

    result = await syncer.run(force=True)
    assert result["ok"] is False
    assert result["reason"] == "linear_api_error"
    assert "401" in result["error"]

    status = await syncer.status()
    assert status["last_error"] == "401 Unauthorized"
    assert status["last_attempt_at"]


def test_linear_sync_status_disabled(tmp_path: Any) -> None:
    config = CitadelConfig(
        linear_sync_state_path=str(tmp_path / "linear_state.json"),
    )
    syncer = LinearSyncer(Citadel(config))

    async def _status() -> dict[str, Any]:
        return await syncer.status()

    import asyncio

    status = asyncio.run(_status())
    assert status["enabled"] is False


# --- #148: corrupt state must fail loudly, not report "no issues" ------------


@pytest.mark.asyncio
async def test_corrupt_state_goes_red_in_status_and_raises_on_reads(tmp_path: Any) -> None:
    """#148: flattening corruption to empty told the user they had no assigned
    issues while /api/sources stayed green. Reads must raise; status must
    carry the error instead of 500ing."""
    from kb.state_io import StateFileError

    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "linear_state.json"),
    )
    from pathlib import Path

    Path(config.linear_sync_state_path).write_text('{"issues": [', encoding="utf-8")
    syncer = LinearSyncer(Citadel(config), client=FakeLinearClient([]))

    status = await syncer.status()
    assert "linear_state.json" in status["state_error"]
    assert status["issue_count"] == 0

    with pytest.raises(StateFileError):
        syncer.issues_for_scope(scope="org", seat_dataset_name=None)


@pytest.mark.asyncio
async def test_member_fetch_failure_is_carried_not_a_neutral_zero(
    tmp_path: Any,
    sample_issues: list[dict[str, Any]],
    monkeypatch: Any,
) -> None:
    """#148 (adjacent): a failed fetch_users left auto_mapped_assignees at 0,
    and the docs read that 0 as "the key cannot read member emails" — a wrong
    diagnosis. The error must ride in the payload and the persisted state."""
    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "linear_state.json"),
        access_store_path=str(tmp_path / "access.json"),
    )
    citadel = Citadel(config)
    store = AccessStore(config.access_store_path)
    store.create_seat(name="John Doe", slug="john", email="john@example.com", issue_token=False)

    async def fake_learn(self: Any, data: str, **kwargs: Any) -> Any:
        class FakeResult:
            accepted = True

        class Outcome:
            ingest = FakeResult()

        return Outcome()

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", fake_learn)
    monkeypatch.setattr(citadel.cognee, "schedule_cognify", lambda datasets: None)

    class MemberFetchFails(FakeLinearClient):
        def fetch_users(self, *, max_users: int = 250) -> list[dict[str, Any]]:
            raise LinearAPIError("Linear API request failed with HTTP 403")

    syncer = LinearSyncer(
        citadel,
        client=MemberFetchFails(sample_issues),
        access_store=store,
    )
    result = await syncer.run(force=True)

    assert result["ok"] is True
    assert result["auto_mapped_assignees"] == 0
    assert "403" in result["auto_map_error"]

    import json as _json
    from pathlib import Path

    state = _json.loads(Path(config.linear_sync_state_path).read_text(encoding="utf-8"))
    assert "403" in state["auto_map_error"]


# --- #90: force must observably differ from an unforced (incremental) run ----


def _capture_learn(monkeypatch: Any, ingests: list[dict[str, Any]]) -> None:
    async def fake_learn(
        self: Any,
        data: str,
        *,
        dataset: str | None = None,
        tags: list[str] | None = None,
        **_: Any,
    ) -> Any:
        ingests.append({"dataset": dataset, "tags": tags or [], "data": data})

        class Outcome:
            class ingest:
                accepted = True

        return Outcome()

    monkeypatch.setattr("kb.linear_sync.LearningProcess.learn", fake_learn)


def _incremental_syncer(
    tmp_path: Any,
    sample_issues: list[dict[str, Any]],
    monkeypatch: Any,
    *,
    with_seat: bool = True,
) -> tuple[LinearSyncer, list[dict[str, Any]], list[list[str]]]:
    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "linear_state.json"),
        access_store_path=str(tmp_path / "access.json"),
    )
    citadel = Citadel(config)
    store: AccessStore | None = None
    if with_seat:
        store = AccessStore(config.access_store_path)
        store.create_seat(
            name="John Doe", slug="john", email="john@example.com", issue_token=False
        )
    ingests: list[dict[str, Any]] = []
    _capture_learn(monkeypatch, ingests)
    scheduled: list[list[str]] = []
    monkeypatch.setattr(
        citadel.cognee, "schedule_cognify", lambda datasets: scheduled.append(list(datasets))
    )
    syncer = LinearSyncer(
        citadel, client=FakeLinearClient(sample_issues), access_store=store
    )
    return syncer, ingests, scheduled


@pytest.mark.asyncio
async def test_linear_sync_incremental_skips_unchanged_issues(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any
) -> None:
    # #90: `force` was accepted but never read, so every pass (including the
    # hourly evolve stage's force=False) was a full resync. A second pass over
    # an unchanged workspace must write nothing and schedule no cognify.
    syncer, ingests, scheduled = _incremental_syncer(tmp_path, sample_issues, monkeypatch)

    first = await syncer.run(force=False)  # no prior state: everything is new
    assert first["ok"] is True
    assert first["written_count"] == 2
    assert first["skipped_unchanged"] == 0
    ingests.clear()
    scheduled.clear()

    second = await syncer.run(force=False)
    assert second["ok"] is True
    assert ingests == []  # no digest, no issue notes, no mirror notes
    assert scheduled == []  # nothing written, nothing to cognify
    assert second["written_count"] == 0
    assert second["skipped_unchanged"] == 2
    assert second["mirrored_count"] == 0
    assert second["central_ingested"] is None
    # The pass still refreshes state: issues_for_scope stays complete.
    assert second["last_synced_at"]
    assert syncer.issues_for_scope(scope="my", seat_dataset_name=seat_dataset("john"))
    assert len(syncer.issues_for_scope(scope="org", seat_dataset_name=None)) == 2


@pytest.mark.asyncio
async def test_linear_sync_incremental_rewrites_only_updated_issue(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any
) -> None:
    # #90: only the issue whose updatedAt moved past the stored cursor is
    # rewritten; the digest refreshes (its listing changed) and the coalesced
    # cognify covers exactly Central + the one touched mirror.
    syncer, ingests, scheduled = _incremental_syncer(tmp_path, sample_issues, monkeypatch)
    assert (await syncer.run(force=False))["ok"] is True
    ingests.clear()
    scheduled.clear()

    sample_issues[0]["updatedAt"] = "2026-06-25T12:00:00Z"
    sample_issues[0]["title"] = "Ship Linear sync v2"

    second = await syncer.run(force=False)
    assert second["ok"] is True
    assert second["written_count"] == 1
    assert second["skipped_unchanged"] == 1
    issue_writes = [item for item in ingests if "linear-issue" in item["tags"]]
    id_tags = {tag for item in issue_writes for tag in item["tags"] if tag.startswith("linear:")}
    assert id_tags == {"linear:ENG-1"}  # ENG-2 untouched
    assert any("linear-workspace" in item["tags"] for item in ingests)  # digest refreshed
    assert scheduled == [["masumi-network", seat_dataset("john")]]


@pytest.mark.asyncio
async def test_linear_sync_force_rewrites_unchanged_issues(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any
) -> None:
    # #90: force=True (POST body / CITADEL_RUN_MODE=linear-sync) must actually
    # force — every issue is rewritten even when nothing changed.
    syncer, ingests, scheduled = _incremental_syncer(tmp_path, sample_issues, monkeypatch)
    assert (await syncer.run(force=False))["ok"] is True
    ingests.clear()
    scheduled.clear()

    second = await syncer.run(force=True)
    assert second["ok"] is True
    assert second["written_count"] == 2
    assert second["skipped_unchanged"] == 0
    assert second["mirrored_count"] == 1
    central_issue_writes = [
        item
        for item in ingests
        if item["dataset"] == "masumi-network" and "linear-issue" in item["tags"]
    ]
    id_tags = {
        tag for item in central_issue_writes for tag in item["tags"] if tag.startswith("linear:")
    }
    assert id_tags == {"linear:ENG-1", "linear:ENG-2"}


@pytest.mark.asyncio
async def test_linear_sync_backfills_mirror_for_seat_created_after_issue_update(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any
) -> None:
    # #90: a seat created AFTER its issues last changed must still receive its
    # mirror notes on the next incremental pass — the issues are unchanged, but
    # the state has never recorded them in that mirror.
    syncer, ingests, scheduled = _incremental_syncer(
        tmp_path, sample_issues, monkeypatch, with_seat=False
    )
    first = await syncer.run(force=False)  # no seats yet: no mirrors resolve
    assert first["ok"] is True
    assert first["mirrored_count"] == 0
    ingests.clear()
    scheduled.clear()

    store = AccessStore(str(tmp_path / "access.json"))
    store.create_seat(name="John Doe", slug="john", email="john@example.com", issue_token=False)
    syncer.access_store = store

    second = await syncer.run(force=False)
    assert second["ok"] is True
    assert second["written_count"] == 0  # no Central rewrites
    assert second["mirrored_count"] == 1  # ENG-1 backfilled into seat:john
    assert [item["dataset"] for item in ingests] == [seat_dataset("john")]
    assert scheduled == [[seat_dataset("john")]]  # Central untouched


@pytest.mark.asyncio
async def test_linear_sync_future_dated_updated_at_cannot_stall_cursor(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any, caplog: Any
) -> None:
    # Review of #90: one future-dated updatedAt (Linear clock trouble, a bad
    # import) must not pin last_seen_updated_at ahead of real time — that would
    # make _changed() False for every issue forever, a permanent silent stall
    # that still reports ok:true with a fresh last_synced_at.
    import json
    import logging

    syncer, ingests, scheduled = _incremental_syncer(tmp_path, sample_issues, monkeypatch)
    sample_issues[1]["updatedAt"] = "2099-01-01T00:00:00Z"  # poisoned

    with caplog.at_level(logging.WARNING, logger="kb.linear_sync"):
        first = await syncer.run(force=False)
    assert first["ok"] is True
    # The cursor stopped at the newest VALID timestamp, not 2099, and the
    # future-dated issue was named in a warning.
    state = json.loads((tmp_path / "linear_state.json").read_text(encoding="utf-8"))
    assert state["last_seen_updated_at"] == "2026-06-25T10:00:00Z"
    assert any("ENG-2" in record.getMessage() for record in caplog.records)
    ingests.clear()

    # The assertion that matters: a normal issue updated AFTER the poisoned pass
    # is still written on the next incremental pass — the stall cannot happen.
    from kb.linear_sync import utc_now

    sample_issues[0]["updatedAt"] = utc_now()
    second = await syncer.run(force=False)
    assert second["ok"] is True
    id_tags = {
        tag
        for item in ingests
        if "linear-issue" in item["tags"]
        for tag in item["tags"]
        if tag.startswith("linear:")
    }
    assert "linear:ENG-1" in id_tags  # the real update landed
    assert "linear:ENG-2" in id_tags  # the poisoned issue fails open into rewrites


@pytest.mark.asyncio
async def test_linear_sync_recovers_from_future_dated_stored_cursor(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any
) -> None:
    # State persisted before the clamp existed (or edited by hand) can already
    # hold a future cursor. It must be ignored (full pass) and replaced by the
    # newest valid updatedAt, not preserved forever.
    import json

    syncer, ingests, scheduled = _incremental_syncer(tmp_path, sample_issues, monkeypatch)
    assert (await syncer.run(force=False))["ok"] is True
    ingests.clear()

    state_path = tmp_path / "linear_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["last_seen_updated_at"] = "2099-01-01T00:00:00Z"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    second = await syncer.run(force=False)
    assert second["ok"] is True
    assert second["written_count"] == 2  # full pass, nothing silently skipped
    healed = json.loads(state_path.read_text(encoding="utf-8"))
    assert healed["last_seen_updated_at"] == "2026-06-25T10:00:00Z"


# --- #117: a scanner-blocked issue must not kill the rest of the pass -------


class FakeCogneeForScanTest:
    """Minimal real-scan-path fake: records every write so a blocked write's
    text can be asserted absent, without touching a real Cognee backend."""

    def __init__(self) -> None:
        self.remember_calls: list[dict[str, Any]] = []

    async def remember(self, data: Any, **kwargs: Any) -> dict[str, Any]:
        self.remember_calls.append({"data": data, **kwargs})
        return {"ok": True}

    async def cognify(self, **kwargs: Any) -> dict[str, Any]:
        return {"cognified": True}

    def schedule_cognify(self, datasets: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_linear_sync_contains_scanner_blocked_issue(tmp_path: Any) -> None:
    """#117: LinearSyncer must route through the same ingest gate as its
    sibling syncers AND contain a block the way they do — a scanner-blocked
    issue must be skipped (never stored), while the rest of the pass still
    lands. Uses the REAL LearningProcess/security_scan (no monkeypatch of
    .learn) so this proves the gate actually runs, not that a mock was called.
    """
    config = CitadelConfig(
        linear_api_key="lin_test",
        linear_sync_state_path=str(tmp_path / "linear_state.json"),
        access_store_path=str(tmp_path / "access.json"),
    )
    fake_cognee = FakeCogneeForScanTest()
    citadel = Citadel(config, cognee=fake_cognee)
    store = AccessStore(config.access_store_path)
    store.create_seat(name="John Doe", slug="john", email="john@example.com", issue_token=False)

    # AWS's own published example key (docs.aws.amazon.com) — shape-valid,
    # never a real credential (memory: never use real credentials as fixtures).
    planted_secret = "AKIAIOSFODNN7EXAMPLE"
    issues = [
        {
            "id": "issue-1",
            "identifier": "ENG-1",
            "title": "Ordinary issue",
            "description": "Nothing sensitive here.",
            "url": "https://linear.app/acme/issue/ENG-1",
            "priority": 2,
            "updatedAt": "2026-06-25T10:00:00Z",
            "state": {"name": "In Progress", "type": "started"},
            "team": {"key": "ENG", "name": "Engineering"},
            "assignee": None,
        },
        {
            "id": "issue-2",
            "identifier": "ENG-2",
            "title": "Leaked credential",
            "description": f"AWS key {planted_secret} leaked in a log paste.",
            "url": "https://linear.app/acme/issue/ENG-2",
            "priority": 1,
            "updatedAt": "2026-06-25T09:00:00Z",
            "state": {"name": "Backlog", "type": "backlog"},
            "team": {"key": "ENG", "name": "Engineering"},
            "assignee": {"id": "user-john", "name": "John Doe", "email": "john@example.com"},
        },
    ]

    syncer = LinearSyncer(citadel, client=FakeLinearClient(issues), access_store=store)
    result = await syncer.run(force=True)

    # The pass as a whole must not abort: the unaffected issue still lands.
    assert result["ok"] is True
    assert "ENG-2" in result.get("blocked", [])
    assert result.get("blocked_count", 0) >= 1

    # The planted secret must never reach the store, in ANY write (digest,
    # Central issue note, or seat mirror note).
    stored_texts = [str(call.get("data", "")) for call in fake_cognee.remember_calls]
    assert not any(planted_secret in text for text in stored_texts)

    # The unaffected issue's Central write still happened.
    central_datasets = [
        call.get("dataset_name")
        for call in fake_cognee.remember_calls
        if "ENG-1" in str(call.get("data", ""))
    ]
    assert "masumi-network" in central_datasets

    # The blocked issue must not be recorded as mirrored to john — the mirror
    # note was never actually written for it.
    assert "ENG-2" not in result["mirrors"].get(seat_dataset("john"), [])


@pytest.mark.asyncio
async def test_linear_sync_warns_after_prolonged_write_less_streak(
    tmp_path: Any, sample_issues: list[dict[str, Any]], monkeypatch: Any, caplog: Any
) -> None:
    # A permanently-stalled incremental sync and a genuinely quiet workspace
    # produce identical logs; after enough write-less passes, say so and name
    # force=True as the disambiguator.
    import logging

    monkeypatch.setattr("kb.linear_sync._UNCHANGED_STREAK_WARN", 2)
    syncer, ingests, scheduled = _incremental_syncer(tmp_path, sample_issues, monkeypatch)
    assert (await syncer.run(force=False))["ok"] is True  # writes; streak 0

    with caplog.at_level(logging.WARNING, logger="kb.linear_sync"):
        assert (await syncer.run(force=False))["ok"] is True  # streak 1: quiet
        assert not any("written nothing" in record.message for record in caplog.records)
        assert (await syncer.run(force=False))["ok"] is True  # streak 2: warn
    assert any("written nothing" in record.message for record in caplog.records)
