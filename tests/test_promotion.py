from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from kb.access import AccessStore, AccessIdentity, seat_dataset
from kb.config import CitadelConfig
from kb.learning import LearningOutcome
from kb.models import IngestResult
from kb.promotion import PromotionEngine, _coerce_classification
import kb.promotion as promotion

SEAT = seat_dataset("alice")
CENTRAL = "masumi-network"  # CitadelConfig.github_sync_dataset default

# A blocking-severity AWS access key (critical) for the secret-gate test.
# Assembled at runtime so no literal key is committed (GitHub push protection
# scans literals); the joined string still trips the scanner at test time.
SECRET_TEXT = "deploy creds " + "AKIA" + "ABCDEFGHIJKLMNOP" + " rotate me"


class FakeCitadel:
    def __init__(self, config: CitadelConfig, nodes: list[str], *, central_hits: bool = False) -> None:
        self.config = config
        self._nodes = nodes
        self.central_hits = central_hits

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        dataset = kwargs.get("dataset")
        if dataset == CENTRAL:
            if self.central_hits:
                return [{"text": f"Central knowledge matching {query[:40]}"}]
            return []
        return [{"text": node} for node in self._nodes]


class FakeLearning:
    """Records every learn() call so tests can assert on the write targets.

    Returns the real :class:`LearningOutcome` shape the engine reads, because a
    fake inventing its own shape is how a guard ships inert. ``central_reject_reason``
    makes Central writes come back ``accepted=False`` with that reason, the
    contract ``Citadel.ingest`` uses for rejections like duplicate_in_process.
    """

    def __init__(self, central_reject_reason: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.central_reject_reason = central_reject_reason

    async def learn(self, data: str, **kwargs: Any) -> LearningOutcome:
        dataset = kwargs.get("dataset") or CENTRAL
        tags = tuple(kwargs.get("tags") or ())
        self.calls.append({"data": data, "dataset": kwargs.get("dataset"), "tier": kwargs.get("tier"), "tags": kwargs.get("tags")})
        if self.central_reject_reason and dataset == CENTRAL:
            result = IngestResult(False, self.central_reject_reason, dataset, tags)
        else:
            result = IngestResult(True, "accepted", dataset, tags)
        return LearningOutcome(ingest=result, dataset=dataset)

    @property
    def central_writes(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["dataset"] == CENTRAL]


def _config(**overrides: Any) -> CitadelConfig:
    base: dict[str, Any] = {"promotion_enabled": True}
    base.update(overrides)
    return CitadelConfig(**base)


def _engine(
    tmp_path: Path,
    nodes: list[str],
    config: CitadelConfig | None = None,
    *,
    central_hits: bool = False,
) -> tuple[PromotionEngine, FakeLearning, AccessStore]:
    config = config or _config()
    learning = FakeLearning()
    store = AccessStore(str(tmp_path / "access.json"))
    engine = PromotionEngine(
        FakeCitadel(config, nodes, central_hits=central_hits),
        learning,
        store,
        config,
    )
    return engine, learning, store


def _org_note(extra: str = "roadmap") -> str:
    return (
        f"Org note about the product {extra} — "
        "https://github.com/masumi-network/Citadel-Archive"
    )


def _github_state(tmp_path: Path) -> CitadelConfig:
    state_path = tmp_path / "github-state.json"
    state_path.write_text(
        '{"repos": {"masumi-network/Citadel-Archive": {}}}',
        encoding="utf-8",
    )
    return _config(github_sync_state_path=str(state_path))


def _stub_llm(monkeypatch: pytest.MonkeyPatch, *, relevant: bool = True, sensitive: bool = False, score: float = 0.9, fail: bool = False) -> None:
    def fake_chat(*args: Any, **kwargs: Any) -> str | None:
        if fail:
            return None
        return json.dumps({"relevant": relevant, "sensitive": sensitive, "score": score, "reason": "stubbed"})

    monkeypatch.setattr(promotion, "openrouter_chat", fake_chat)


def test_coerce_classification_rejects_malformed() -> None:
    assert _coerce_classification({"relevant": True, "sensitive": False, "score": 0.8, "reason": "ok"}) is not None
    assert _coerce_classification({"relevant": "yes", "sensitive": False, "score": 0.8, "reason": "ok"}) is None
    assert _coerce_classification({"relevant": True, "sensitive": False, "score": 2, "reason": "ok"}) is None
    assert _coerce_classification({"relevant": True, "sensitive": False, "score": 0.8}) is None
    assert _coerce_classification("not a dict") is None


async def test_dry_run_proposes_but_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, learning, store = _engine(
        tmp_path,
        [_org_note()],
        _github_state(tmp_path),
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.9)

    result = await engine.run(SEAT, dry_run=True)

    assert result["dry_run"] is True
    assert result["proposed"] == 1
    assert result["promoted"] == 0
    assert any(p["decision"] == "promote" for p in result["proposals"])
    # The core safety invariant: a dry run performs NO writes at all.
    assert learning.calls == []
    assert store.recent_audit_events(action="promotion.promote") == []


async def test_relevant_clean_item_is_promoted_to_central_with_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, learning, store = _engine(
        tmp_path,
        [_org_note()],
        _github_state(tmp_path),
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.9)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 1
    # Promotion routed through the org-ready dual-write -> a real Central write.
    assert len(learning.central_writes) == 1
    assert "org-ready" in learning.central_writes[0]["tags"]
    assert "promotion-agent" in learning.central_writes[0]["tags"]
    assert "promotion-seat:alice" in learning.central_writes[0]["tags"]
    promote_events = store.recent_audit_events(action="promotion.promote")
    assert len(promote_events) == 1
    assert promote_events[0]["success"] is True
    assert promote_events[0]["dataset"] == CENTRAL
    assert promote_events[0]["detail"]["seat"] == SEAT


async def test_sensitive_item_is_not_promoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, learning, store = _engine(tmp_path, ["my personal salary and home address"])
    _stub_llm(monkeypatch, relevant=True, sensitive=True, score=0.95)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    assert learning.central_writes == []
    assert result["proposals"][0]["decision"] == "skip"
    assert result["proposals"][0]["reason"] == "sensitive"


async def test_secret_bearing_item_is_not_promoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, learning, _store = _engine(tmp_path, [SECRET_TEXT])
    # Even if the classifier would say "promote", the secret gate must win.
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.99)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    assert learning.calls == []
    assert result["proposals"][0]["decision"] == "skip"
    assert result["proposals"][0]["reason"] == "secret_content"
    assert result["proposals"][0]["secret_blocked"] is True


async def test_llm_failure_falls_back_to_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, learning, _store = _engine(
        tmp_path,
        [_org_note()],
        _github_state(tmp_path),
    )
    _stub_llm(monkeypatch, fail=True)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    assert learning.calls == []
    assert result["proposals"][0]["decision"] == "skip"
    assert result["proposals"][0]["reason"] == "llm_unavailable"


async def test_below_threshold_is_not_promoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, learning, _store = _engine(
        tmp_path,
        [_org_note("marginal")],
        _github_state(tmp_path),
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.5)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    assert learning.calls == []
    assert result["proposals"][0]["reason"] == "below_threshold"


async def test_disabled_returns_status_and_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, learning, _store = _engine(tmp_path, ["anything"], config=_config(promotion_enabled=False))
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.9)

    result = await engine.run(SEAT, dry_run=False)

    assert result["enabled"] is False
    assert result["reason"] == "disabled"
    assert learning.calls == []


async def test_non_seat_dataset_rejected(tmp_path: Path) -> None:
    engine, _learning, _store = _engine(tmp_path, ["anything"])
    with pytest.raises(ValueError):
        await engine.run(CENTRAL, dry_run=True)


async def test_personal_capture_tag_never_promotes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = (
        "# Capture summary: notes\n"
        "- Capture Root Tags: personal\n"
        "- Path: `/tmp/notes`\n"
    )
    engine, learning, _store = _engine(tmp_path, [summary])
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.99)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    assert learning.central_writes == []
    assert result["proposals"][0]["decision"] == "skip"
    assert result["proposals"][0]["reason"] == "capture_tag_personal"


async def test_new_org_project_queues_pending_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = (
        "# Capture summary: side project\n"
        "- Remote: `https://github.com/other-org/new-app.git`\n"
        "- Capture Root Tags: org-work\n"
    )
    state_path = tmp_path / "github-state.json"
    state_path.write_text('{"repos": {"masumi-network/Citadel-Archive": {}}}', encoding="utf-8")
    engine, learning, store = _engine(
        tmp_path,
        [summary],
        config=_config(github_sync_state_path=str(state_path)),
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.95)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    assert result["queued"] == 1
    assert learning.central_writes == []
    assert result["proposals"][0]["decision"] == "pending_approval"
    pending = store.list_promotion_pending(seat_slug="alice")
    assert len(pending) == 1


async def test_unreferenced_note_skips_without_central_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, learning, _store = _engine(
        tmp_path,
        ["a useful org note about the product roadmap with no repo link"],
        _github_state(tmp_path),
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.9)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    assert result["proposals"][0]["decision"] == "skip"
    assert result["proposals"][0]["reason"] == "no_org_reference"
    assert learning.central_writes == []


async def test_unreferenced_note_promotes_on_central_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, learning, _store = _engine(
        tmp_path,
        ["shared runbook details with no repo link"],
        _github_state(tmp_path),
        central_hits=True,
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.9)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 1
    assert learning.central_writes


async def test_custom_capture_tag_never_auto_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = (
        "# Capture summary: side repo\n"
        "- Remote: `https://github.com/masumi-network/Citadel-Archive.git`\n"
        "- Capture Root Tags: custom-label\n"
    )
    engine, learning, _store = _engine(
        tmp_path,
        [summary],
        _github_state(tmp_path),
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.99)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    assert result["proposals"][0]["reason"] == "capture_tag_not_org_work"
    assert learning.central_writes == []


async def test_rejected_candidate_is_not_requeued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = (
        "# Capture summary: side project\n"
        "- Remote: `https://github.com/other-org/new-app.git`\n"
        "- Capture Root Tags: org-work\n"
    )
    engine, learning, store = _engine(
        tmp_path,
        [summary],
        _github_state(tmp_path),
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.95)

    first = await engine.run(SEAT, dry_run=False)
    assert first["queued"] == 1
    item = store.list_promotion_pending(seat_slug="alice")[0]
    actor = AccessIdentity(
        role="writer",
        actor_id="alice",
        actor_kind="user",
        actor_name="Alice",
        source="token",
        default_dataset=SEAT,
        seat_slug="alice",
    )
    await engine.reject_pending(item.id, actor)

    second = await engine.run(SEAT, dry_run=False)
    assert second["queued"] == 0
    assert second["proposals"][0]["reason"] == "previously_rejected"
    assert learning.central_writes == []


class _ExplodingCitadel:
    """Fails a chosen number of leading seed searches, succeeds on the rest."""

    def __init__(self, config: CitadelConfig, *, fail_first: int, exc: Exception) -> None:
        self.config = config
        self._fail_first = fail_first
        self._exc = exc
        self.calls = 0

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if self.calls <= self._fail_first:
            raise self._exc
        return [{"text": _org_note()}]


def _exploding_engine(tmp_path: Path, *, fail_first: int, exc: Exception) -> PromotionEngine:
    config = _config()
    return PromotionEngine(
        _ExplodingCitadel(config, fail_first=fail_first, exc=exc),
        FakeLearning(),
        AccessStore(str(tmp_path / "access.json")),
        config,
    )


async def test_enumerate_raises_when_every_seed_query_fails(tmp_path: Path) -> None:
    """A seat whose searches all die is a failure, not an empty seat.

    Production logged "seats=11 promoted=0 failures=0" for days while all 22
    searches were failing on the Kuzu lock and DatasetNotFoundError, because
    enumerate swallowed each one and returned [].
    """
    total = len(promotion.DEFAULT_SEED_QUERIES)
    engine = _exploding_engine(tmp_path, fail_first=total, exc=RuntimeError("kuzu locked"))

    with pytest.raises(promotion.PromotionEnumerationError) as caught:
        await engine.enumerate(SEAT, 20)

    assert "RuntimeError" in str(caught.value)
    assert SEAT in str(caught.value)


async def test_enumerate_stays_best_effort_when_one_query_fails(tmp_path: Path) -> None:
    """One flaky query must not fail the seat — only a total wipeout does."""
    if len(promotion.DEFAULT_SEED_QUERIES) < 2:  # pragma: no cover - guards the fixture
        pytest.skip("needs at least two seed queries")
    engine = _exploding_engine(tmp_path, fail_first=1, exc=RuntimeError("kuzu locked"))

    candidates = await engine.enumerate(SEAT, 20)

    assert candidates, "a surviving query's results must still be returned"


async def test_enumerate_names_an_unprovisioned_seat_distinctly(tmp_path: Path) -> None:
    """"This seat has no dataset" is a different problem from "search is broken" (#147).

    The driver in scripts/run_railway.py logs exc.__class__.__name__, so the two
    cases were indistinguishable in production even though one is an outage to
    investigate and the other is one backfill run. Six of eleven seats were the
    second kind and read as the first for weeks.
    """

    class DatasetNotFoundError(Exception):
        """Stands in for cognee's, which is matched by name, not by class."""

    total = len(promotion.DEFAULT_SEED_QUERIES)
    engine = _exploding_engine(
        tmp_path, fail_first=total, exc=DatasetNotFoundError("No datasets found.")
    )

    with pytest.raises(promotion.SeatDatasetMissingError) as caught:
        await engine.enumerate(SEAT, 20)

    assert SEAT in str(caught.value)
    assert "backfill" in str(caught.value)
    # Still a PromotionEnumerationError, so existing handlers keep catching it.
    assert isinstance(caught.value, promotion.PromotionEnumerationError)


def test_promotion_matches_cognees_real_exception_name() -> None:
    """Pin the literal kb/promotion.py matches to cognee's actual class (#147).

    The discriminator compares exc.__class__.__name__ to the string
    "DatasetNotFoundError" because no kb module imports cognee at module scope.
    Nothing else in the suite touches the real class, so a cognee rename would
    turn the discrimination off silently: every unprovisioned seat would go
    back to reading as a generic failure and no test would notice.

    Imported from cognee.modules.data.exceptions specifically. That is the one
    both search-reachable raise sites use (search.py:294, recall.py:595).
    cognee 1.2.2 also defines a same-named class in api/v1/exceptions that no
    search path raises.
    """
    from cognee.modules.data.exceptions import DatasetNotFoundError

    assert DatasetNotFoundError.__name__ == "DatasetNotFoundError"


async def test_enumerate_keeps_the_generic_error_for_mixed_failures(tmp_path: Path) -> None:
    """A seat that is BOTH unprovisioned and erroring is not a provisioning story.

    Only an unmixed run of DatasetNotFoundError means "just backfill it". If
    anything else is in the mix the seat needs looking at, so it must not be
    filed under the cheap remedy.
    """
    if len(promotion.DEFAULT_SEED_QUERIES) < 2:  # pragma: no cover - guards the fixture
        pytest.skip("needs at least two seed queries")

    class DatasetNotFoundError(Exception):
        pass

    class _MixedCitadel:
        """First query dies on the lock, every later one on a missing dataset."""

        def __init__(self, config: CitadelConfig) -> None:
            self.config = config
            self.calls = 0

        async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("kuzu locked")
            raise DatasetNotFoundError("No datasets found.")

    config = _config()
    engine = PromotionEngine(
        _MixedCitadel(config),
        FakeLearning(),
        AccessStore(str(tmp_path / "access.json")),
        config,
    )

    with pytest.raises(promotion.PromotionEnumerationError) as caught:
        await engine.enumerate(SEAT, 20)

    assert not isinstance(caught.value, promotion.SeatDatasetMissingError)
    assert "RuntimeError" in str(caught.value)
    assert "DatasetNotFoundError" in str(caught.value)


async def test_enumerate_returns_empty_without_raising_when_searches_succeed(
    tmp_path: Path,
) -> None:
    """A genuinely empty seat stays an empty list, not an error."""
    engine, _, _ = _engine(tmp_path, [])

    assert await engine.enumerate(SEAT, 20) == []


async def test_slow_classifier_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other coroutines must keep running while the classifier is in flight.

    The classifier is a synchronous HTTP call with a 60s timeout per retry
    attempt. Awaited inline it parks the whole event loop: nine consecutive
    evolve passes spent 16-27 minutes in promotion and every route on the node
    queued behind the blocked loop for the duration (search 22.9s, /mcp 41.9s,
    an ingest abandoned at 120s). This asserts the observable behaviour (the
    loop schedules this test's coroutine while classify is mid-call), not the
    presence of any particular dispatch mechanism.
    """
    started = threading.Event()
    release = threading.Event()

    def stalled_chat(*args: Any, **kwargs: Any) -> str | None:
        started.set()
        if not release.wait(timeout=2.0):
            # The loop never freed us: degrade to the llm_unavailable skip so
            # the red direction fails on the liveness assert, not a hang.
            return None
        return json.dumps(
            {"relevant": True, "sensitive": False, "score": 0.9, "reason": "stubbed"}
        )

    monkeypatch.setattr(promotion, "openrouter_chat", stalled_chat)
    engine, _learning, _store = _engine(tmp_path, [_org_note()], _github_state(tmp_path))

    run_task = asyncio.create_task(engine.run(SEAT, dry_run=False))
    alive_during_classify = False
    for _ in range(400):
        await asyncio.sleep(0.005)
        if started.is_set():
            # The classifier has started and is still held open (release is
            # unset), yet this coroutine is running: the loop is alive. When
            # classify runs ON the loop this line is unreachable until the
            # call gives up, by which point run_task has already finished.
            alive_during_classify = not run_task.done()
            break
    release.set()
    result = await run_task

    assert started.is_set(), "the classifier was never invoked"
    assert alive_during_classify, (
        "the event loop made no progress while the classifier was in flight"
    )
    assert result["promoted"] == 1


async def test_central_rejection_is_not_recorded_as_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write Central refuses must not be counted or audited as delivered.

    One production pass logged promoted=169 while ~150 of those Central writes
    came back duplicate_in_process; each was recorded success=True,
    accepted=True because the outcome of execute_learning_writes was dropped.
    """
    config = _github_state(tmp_path)
    learning = FakeLearning(central_reject_reason="duplicate_in_process")
    store = AccessStore(str(tmp_path / "access.json"))
    engine = PromotionEngine(FakeCitadel(config, [_org_note()]), learning, store, config)
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.9)

    result = await engine.run(SEAT, dry_run=False)

    assert result["promoted"] == 0
    proposal = result["proposals"][0]
    assert proposal["decision"] == "skip"
    assert proposal["reason"] == "duplicate_in_process"
    assert proposal["promoted"] is False
    assert proposal["secret_blocked"] is False, "a duplicate is not a secret block"
    events = store.recent_audit_events(action="promotion.promote")
    assert len(events) == 1
    assert events[0]["success"] is False
    assert events[0]["detail"]["accepted"] is False
    assert events[0]["detail"]["write_reason"] == "duplicate_in_process"


async def test_duplicate_across_seats_skips_the_classifier_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content already landed in Central this pass is not re-classified.

    The evolve stage runs one engine across every seat, so identical content
    surfacing from a second seat used to cost a full classifier call and a
    Central write that the ingest dedupe then rejected: ~150 wasted LLM calls
    in one measured pass. Once this engine has delivered the exact text, the
    next identical candidate settles as duplicate_in_process before classify.
    """
    config = _github_state(tmp_path)
    learning = FakeLearning()
    store = AccessStore(str(tmp_path / "access.json"))
    engine = PromotionEngine(FakeCitadel(config, [_org_note()]), learning, store, config)
    classifier_calls = {"count": 0}

    def counting_chat(*args: Any, **kwargs: Any) -> str:
        classifier_calls["count"] += 1
        return json.dumps(
            {"relevant": True, "sensitive": False, "score": 0.9, "reason": "stubbed"}
        )

    monkeypatch.setattr(promotion, "openrouter_chat", counting_chat)

    first = await engine.run(SEAT, dry_run=False)
    assert first["promoted"] == 1
    assert classifier_calls["count"] == 1
    assert len(learning.central_writes) == 1

    second = await engine.run(seat_dataset("bob"), dry_run=False)
    assert second["promoted"] == 0
    assert second["proposals"][0]["decision"] == "skip"
    assert second["proposals"][0]["reason"] == "duplicate_in_process"
    assert classifier_calls["count"] == 1, (
        "identical content must not cost a second classifier call"
    )
    assert len(learning.central_writes) == 1, (
        "identical content must not be re-submitted to Central"
    )


async def test_case_divergent_duplicate_still_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivered-set key must match the ingest dedupe key exactly.

    Citadel.ingest dedupes on sha256 of the RAW text (kb/service.py), while
    candidate_hash() strips and lowercases first. Keying the delivered set on
    the normalized hash is coarser than the guard it models: a case-variant of
    promoted content hashes differently at ingest (Central would accept the
    write) but identically under normalization (the engine would suppress it).
    Two case-divergent candidates must cost two classifier calls and two
    Central writes, exactly as on the base branch.
    """
    config = _github_state(tmp_path)
    learning = FakeLearning()
    store = AccessStore(str(tmp_path / "access.json"))
    citadel = FakeCitadel(config, [_org_note()])
    engine = PromotionEngine(citadel, learning, store, config)
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.9)

    first = await engine.run(SEAT, dry_run=False)
    assert first["promoted"] == 1

    variant = _org_note().replace("Org note", "ORG NOTE")
    assert variant != _org_note()
    assert variant.lower() == _org_note().lower()
    citadel._nodes = [variant]

    second = await engine.run(seat_dataset("bob"), dry_run=False)

    assert second["promoted"] == 1, (
        "a case-variant hashes differently at ingest, so Central would accept "
        "it; the engine must not suppress the write"
    )
    assert second["proposals"][0]["decision"] == "promote"
    assert len(learning.central_writes) == 2


async def test_approve_pending_surfaces_the_write_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approver must be able to tell a failed write from already-delivered.

    approve_pending consumes the item either way (pre-existing contract), and
    with honest write accounting a duplicate bounce returns ok=False. Without
    the server's reason in the response, an admin approving two identical
    pending items from different seats sees the second as a failure when
    nothing failed. The write_reason must reach the caller, not only the
    audit log.
    """
    summary = (
        "# Capture summary: side project\n"
        "- Remote: `https://github.com/other-org/new-app.git`\n"
        "- Capture Root Tags: org-work\n"
    )
    state_path = tmp_path / "github-state.json"
    state_path.write_text(
        '{"repos": {"masumi-network/Citadel-Archive": {}}}', encoding="utf-8"
    )
    config = _config(github_sync_state_path=str(state_path))
    learning = FakeLearning(central_reject_reason="duplicate_in_process")
    store = AccessStore(str(tmp_path / "access.json"))
    engine = PromotionEngine(FakeCitadel(config, [summary]), learning, store, config)
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.95)

    queued = await engine.run(SEAT, dry_run=False)
    assert queued["queued"] == 1
    item = store.list_promotion_pending(seat_slug="alice")[0]
    actor = AccessIdentity(
        role="writer",
        actor_id="alice",
        actor_kind="user",
        actor_name="Alice",
        source="token",
        default_dataset=SEAT,
        seat_slug="alice",
    )

    result = await engine.approve_pending(item.id, actor)

    assert result["promoted"] is False
    assert result["ok"] is False
    assert result["write_reason"] == "duplicate_in_process"
    # The item is consumed regardless, matching the contract on main.
    assert store.get_promotion_pending(item.id).status != "pending"


async def test_decision_audit_states_the_user_approval_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """promotion.approve / promotion.reject events must state what the engine
    was handed about user approval: the caller's signal, or the explicit
    not_recorded marker. Never silence, because an absent field reads the same
    as an event from before the field existed."""
    summary_a = (
        "# Capture summary: side project\n"
        "- Remote: `https://github.com/other-org/new-app.git`\n"
        "- Capture Root Tags: org-work\n"
    )
    summary_b = (
        "# Capture summary: second project\n"
        "- Remote: `https://github.com/other-org/second-app.git`\n"
        "- Capture Root Tags: org-work\n"
    )
    engine, _learning, store = _engine(
        tmp_path,
        [summary_a, summary_b],
        _github_state(tmp_path),
    )
    _stub_llm(monkeypatch, relevant=True, sensitive=False, score=0.95)

    queued = await engine.run(SEAT, dry_run=False)
    assert queued["queued"] == 2
    items = store.list_promotion_pending(seat_slug="alice")
    actor = AccessIdentity(
        role="admin",
        actor_id="root",
        actor_kind="user",
        actor_name="Root",
        source="token",
        default_dataset=CENTRAL,
        seat_slug=None,
    )

    await engine.approve_pending(items[0].id, actor, user_approval="yes, promote it")
    await engine.reject_pending(items[1].id, actor)

    approve_event = store.recent_audit_events(action="promotion.approve")[0]
    reject_event = store.recent_audit_events(action="promotion.reject")[0]
    assert approve_event["detail"]["user_approval"] == "yes, promote it"
    assert reject_event["detail"]["user_approval"] == promotion.USER_APPROVAL_NOT_RECORDED
