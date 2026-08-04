"""A surface must report what it OBSERVED, not what it REQUESTED.

Two families of the same defect live here.

**The approval signal.** Five MCP tools tell the calling agent to obtain the
user's explicit approval before writing. That instruction existed only as
prose in a docstring: nothing carried the answer back to the node, so the
audit trail could not tell a write the user confirmed from one the agent made
on its own. Both produced byte-identical rows. The fix records a tri-state
(``confirmed`` / ``not_confirmed`` / ``unknown``) the caller sets, and the
absent case is ``unknown``, never the optimistic value.

**The readiness canary.** ``/readyz`` reported a node with no canary and a
node whose canary passed identically, because ``canary is None`` was folded
into the ok expression and the payload carried a bare ``null``. A monitoring
consumer could not branch on the difference.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import kb.server as server_module
from kb.access import AccessStore
from kb.mcp_server import TOOL_POLICIES, CitadelHttpClient
from kb.promotion_queue import ReferenceAssessment, build_pending_item
from kb.security_scan import SecretContentError
from kb.server import app

MCP_TOOL_HEADER = "X-Citadel-MCP-Tool"
CONFIRM_HEADER = "X-Citadel-User-Confirmed"

# The tools whose docstrings instruct the agent to get the user's approval.
APPROVAL_REQUIRED_TOOLS = {
    "citadel_ingest",
    "citadel_share_session",
    "citadel_contribute",
    "citadel_promotion_approve",
    "citadel_promotion_reject",
}

_APPROVAL_PROSE = re.compile(
    r"(explicit user (approval|confirmation)"
    r"|user for explicit approval"
    r"|requires explicit user confirmation)",
    re.IGNORECASE,
)


def _audit_events() -> list[dict[str, Any]]:
    return app.state.access_store.snapshot()["audit_events"]


def _last_mcp_event() -> dict[str, Any]:
    events = [e for e in _audit_events() if str(e["action"]).startswith("mcp.")]
    assert events, f"no mcp.* audit event was recorded: {_audit_events()}"
    return events[-1]


# --------------------------------------------------------------------------
# A. The approval signal reaches the audit trail
# --------------------------------------------------------------------------


def test_mcp_ingest_records_an_explicit_user_confirmation(tmp_path: Path) -> None:
    from test_server import authed_client

    app.state.access_store = AccessStore(tmp_path / "access.json")
    writer = authed_client("test-writer")

    response = writer.post(
        "/ingest",
        json={"data": "Runbook: rotate the sync token before minting seats."},
        headers={MCP_TOOL_HEADER: "citadel_ingest", CONFIRM_HEADER: "true"},
    )

    assert response.status_code == 200
    assert _last_mcp_event()["detail"]["user_confirmation"] == "confirmed"


def test_mcp_ingest_without_the_header_records_unknown_not_confirmed(
    tmp_path: Path,
) -> None:
    """The absent case must not read as approval.

    This is the whole point: an older client that never sends the field, and a
    client whose user said yes, must not produce the same audit row.
    """
    from test_server import authed_client

    app.state.access_store = AccessStore(tmp_path / "access.json")
    writer = authed_client("test-writer")

    response = writer.post(
        "/ingest",
        json={"data": "Runbook: rotate the sync token before minting seats."},
        headers={MCP_TOOL_HEADER: "citadel_ingest"},
    )

    assert response.status_code == 200
    detail = _last_mcp_event()["detail"]
    assert detail["user_confirmation"] == "unknown"
    assert detail["user_confirmation"] != "confirmed"


def test_a_declined_confirmation_is_recorded_and_the_write_still_runs(
    tmp_path: Path,
) -> None:
    """Recorded, not enforced.

    The signal is asserted by the calling agent, so the node cannot verify it.
    Rejecting on it would teach every client to hardcode ``true`` and destroy
    the observability the field exists to provide, and would 422 every client
    built before the field existed. So the row says ``not_confirmed`` and the
    write proceeds, and the trail can answer the question later.
    """
    from test_server import authed_client

    app.state.access_store = AccessStore(tmp_path / "access.json")
    writer = authed_client("test-writer")

    response = writer.post(
        "/ingest",
        json={"data": "Runbook: rotate the sync token before minting seats."},
        headers={MCP_TOOL_HEADER: "citadel_ingest", CONFIRM_HEADER: "false"},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert _last_mcp_event()["detail"]["user_confirmation"] == "not_confirmed"


def test_a_read_only_tool_does_not_claim_a_confirmation(tmp_path: Path) -> None:
    """Only tools that ask for approval carry the field.

    A search has nothing to confirm, so a ``user_confirmation`` on its row
    would be a claim about a question nobody asked.
    """
    from test_server import authed_client

    app.state.access_store = AccessStore(tmp_path / "access.json")
    reader = authed_client("test-reader")

    response = reader.post(
        "/search",
        json={"query": "runbook"},
        headers={MCP_TOOL_HEADER: "citadel_search", CONFIRM_HEADER: "true"},
    )

    assert response.status_code == 200
    assert "user_confirmation" not in _last_mcp_event()["detail"]


def test_every_approval_instructing_tool_declares_the_signal() -> None:
    """The docstring and the policy cannot drift apart.

    The defect was a tool that *tells* the model to get approval while nothing
    downstream records the answer. Parsing the source keeps that from coming
    back through a newly added tool: prose demanding approval without
    ``requires_user_approval=True`` fails here.
    """
    source = Path(server_module.__file__).with_name("mcp_server.py").read_text()
    tree = ast.parse(source)

    instructing: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node) or ""
        if not _APPROVAL_PROSE.search(doc):
            continue
        for decorator in node.decorator_list:
            for sub in ast.walk(decorator):
                if (
                    isinstance(sub, ast.Subscript)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "TOOL_POLICIES"
                    and isinstance(sub.slice, ast.Constant)
                ):
                    instructing.add(str(sub.slice.value))

    assert instructing == APPROVAL_REQUIRED_TOOLS, (
        "tools whose docstring instructs user approval changed; update "
        "APPROVAL_REQUIRED_TOOLS and the policy table together"
    )
    declared = {
        name for name, policy in TOOL_POLICIES.items() if policy.requires_user_approval
    }
    assert declared == APPROVAL_REQUIRED_TOOLS


def test_the_mcp_client_puts_the_confirmation_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal the tool accepts but never transmits is the same defect again."""
    seen: list[dict[str, str]] = []

    class _Response:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Response:
        seen.append(dict(request.headers))
        return _Response()

    monkeypatch.setattr("kb.mcp_server.urlopen", fake_urlopen)
    client = CitadelHttpClient(base_url="https://node.example", access_token="t")

    client.post("/ingest", {"data": "x"}, tool_name="citadel_ingest", user_confirmed=True)
    client.post("/ingest", {"data": "x"}, tool_name="citadel_ingest", user_confirmed=False)
    client.post("/ingest", {"data": "x"}, tool_name="citadel_ingest")

    # urllib title-cases header names.
    assert seen[0]["X-citadel-user-confirmed"] == "true"
    assert seen[1]["X-citadel-user-confirmed"] == "false"
    assert "X-citadel-user-confirmed" not in seen[2]


@pytest.mark.asyncio
async def test_promotion_approve_records_the_confirmation_in_its_audit_detail(
    tmp_path: Path,
) -> None:
    """The Central-bound decision is the one that most needs the answer."""
    from test_promotion import CENTRAL, SEAT, FakeCitadel, FakeLearning, _config

    from kb.access import AccessIdentity
    from kb.promotion import PromotionEngine

    store = AccessStore(tmp_path / "access.json")
    config = _config(promotion_enabled=True)
    learning = FakeLearning()
    engine = PromotionEngine(FakeCitadel(config, []), learning, store, config)

    item = build_pending_item(
        seat_slug="alice",
        seat_dataset=SEAT,
        candidate_text="Central-worthy note about the release process.",
        assessment=ReferenceAssessment(status="new_org_project", reason="new"),
        created_at="2026-08-04T00:00:00+00:00",
    )
    store.add_promotion_pending(item)
    actor = AccessIdentity(
        role="admin",
        actor_id="admin",
        actor_kind="user",
        actor_name="Admin",
        source="token",
        default_dataset=CENTRAL,
        seat_slug=None,
    )

    await engine.approve_pending(item.id, actor, user_confirmation="confirmed")

    approvals = [
        e for e in store.snapshot()["audit_events"] if e["action"] == "promotion.approve"
    ]
    assert approvals and approvals[-1]["detail"]["user_confirmation"] == "confirmed"


@pytest.mark.asyncio
async def test_promotion_reject_defaults_to_unknown_not_confirmed(
    tmp_path: Path,
) -> None:
    from test_promotion import CENTRAL, SEAT, FakeCitadel, FakeLearning, _config

    from kb.access import AccessIdentity
    from kb.promotion import PromotionEngine

    store = AccessStore(tmp_path / "access.json")
    config = _config(promotion_enabled=True)
    engine = PromotionEngine(FakeCitadel(config, []), FakeLearning(), store, config)

    item = build_pending_item(
        seat_slug="alice",
        seat_dataset=SEAT,
        candidate_text="A candidate a reviewer will decline.",
        assessment=ReferenceAssessment(status="new_org_project", reason="new"),
        created_at="2026-08-04T00:00:00+00:00",
    )
    store.add_promotion_pending(item)
    actor = AccessIdentity(
        role="admin",
        actor_id="admin",
        actor_kind="user",
        actor_name="Admin",
        source="token",
        default_dataset=CENTRAL,
        seat_slug=None,
    )

    await engine.reject_pending(item.id, actor)

    rejects = [
        e for e in store.snapshot()["audit_events"] if e["action"] == "promotion.reject"
    ]
    assert rejects and rejects[-1]["detail"]["user_confirmation"] == "unknown"


def test_the_approve_endpoint_forwards_the_header_to_the_engine(
    tmp_path: Path,
) -> None:
    """The header has to survive the HTTP hop, not just exist at both ends."""
    from test_server import authed_client

    store = AccessStore(tmp_path / "access.json")
    app.state.access_store = store
    client = authed_client()

    item = build_pending_item(
        seat_slug="alice",
        seat_dataset="seat:alice",
        candidate_text="Central-worthy note about the release process.",
        assessment=ReferenceAssessment(status="new_org_project", reason="new"),
        created_at="2026-08-04T00:00:00+00:00",
    )
    store.add_promotion_pending(item)

    captured: dict[str, Any] = {}

    class _Engine:
        async def approve_pending(
            self, item_id: str, actor: Any, *, delegate: bool = False, **kwargs: Any
        ) -> dict[str, Any]:
            captured.update(kwargs)
            return {"ok": True, "promoted": True}

    server_module.app.dependency_overrides = {}
    original = server_module.get_promotion_engine
    server_module.get_promotion_engine = lambda: _Engine()  # type: ignore[assignment]
    try:
        response = client.post(
            f"/api/promotion/pending/{item.id}/approve",
            json={},
            headers={
                MCP_TOOL_HEADER: "citadel_promotion_approve",
                CONFIRM_HEADER: "true",
            },
        )
    finally:
        server_module.get_promotion_engine = original  # type: ignore[assignment]

    assert response.status_code == 200
    assert captured.get("user_confirmation") == "confirmed"


# --------------------------------------------------------------------------
# B. /readyz tells "never ran" from "passed" from "failed"
# --------------------------------------------------------------------------


def _populated_client() -> TestClient:
    from test_server import FakeCitadel, authed_client

    class PopulatedCitadel(FakeCitadel):
        async def _graph_counts(self) -> dict[str, int]:
            return {"nodes": 280, "edges": 514}

    class BusySyncer:
        async def status(self) -> dict[str, Any]:
            return {"tracked_repositories": 50, "tracked_files": 0, "issue_count": 0}

    client = authed_client("test-reader")
    app.state.citadel = PopulatedCitadel()
    app.state.github_syncer = BusySyncer()
    app.state.repo_content_syncer = BusySyncer()
    app.state.linear_syncer = BusySyncer()
    return client


def test_readyz_says_never_ran_when_no_canary_has_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "_LAST_CANARY", None)
    client = _populated_client()

    ready = client.get("/readyz")

    assert ready.status_code == 200
    canary = ready.json()["canary"]
    assert canary["state"] == "never_ran"
    # None, not True: nothing observed a pass.
    assert canary["ok"] is None
    # The scheduler is off by default, which is *why* nothing ran.
    assert canary["scheduler_enabled"] is False


def test_readyz_never_ran_and_passed_are_not_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect in one assertion.

    Before the fix both nodes answered ``"canary": null`` with ``ok: true``.
    """
    client = _populated_client()

    monkeypatch.setattr(server_module, "_LAST_CANARY", None)
    never_ran = client.get("/readyz").json()["canary"]

    monkeypatch.setattr(
        server_module,
        "_LAST_CANARY",
        {"ok": True, "search_hit": True, "graph_grew": True, "marker": "m"},
    )
    passed = client.get("/readyz").json()["canary"]

    assert never_ran != passed
    assert never_ran["state"] == "never_ran"
    assert passed["state"] == "passed"
    assert passed["ok"] is True


def test_readyz_is_red_when_the_canary_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        server_module,
        "_LAST_CANARY",
        {"ok": False, "search_hit": False, "graph_grew": False, "marker": "m"},
    )
    client = _populated_client()

    ready = client.get("/readyz")

    assert ready.status_code == 503
    assert ready.json()["ok"] is False
    assert ready.json()["canary"]["state"] == "failed"


def test_readyz_does_not_assume_a_pass_when_the_verdict_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded canary with no verdict has not observed a pass."""
    monkeypatch.setattr(server_module, "_LAST_CANARY", {"marker": "m"})
    client = _populated_client()

    ready = client.get("/readyz")

    assert ready.json()["canary"]["state"] == "failed"
    assert ready.status_code == 503


def test_check_corpus_reports_the_canary_state_to_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`citadel status` carried the same optimistic default as /readyz."""
    import kb.status as status_module

    payload = {
        "ok": True,
        "corpus": {"ok": True, "tracked_sources": 50, "indexed_docs": 280},
        "canary": {"state": "never_ran", "ok": None, "scheduler_enabled": False},
    }
    monkeypatch.setattr(status_module, "_request", lambda *a, **k: payload)

    check = status_module.check_corpus("https://node.example", "token")

    assert check is not None
    # Never-ran is not a failure, but it is not a pass either. Say which.
    assert check.ok is True
    assert "never ran" in check.detail
    assert check.data["canary"]["state"] == "never_ran"


def test_check_corpus_is_red_when_the_canary_state_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kb.status as status_module

    payload = {
        "ok": False,
        "corpus": {"ok": True, "tracked_sources": 50, "indexed_docs": 280},
        "canary": {"state": "failed", "ok": False, "scheduler_enabled": True},
    }
    monkeypatch.setattr(status_module, "_request", lambda *a, **k: payload)

    check = status_module.check_corpus("https://node.example", "token")

    assert check is not None
    assert check.ok is False


# --------------------------------------------------------------------------
# C. /api/promote/run's secret block reaches the MCP audit with its reason
# --------------------------------------------------------------------------


def test_promote_run_secret_block_records_the_reason_in_the_mcp_audit(
    tmp_path: Path,
) -> None:
    """Parity with /ingest.

    The generic middleware backstop already wrote an ``mcp.*`` row for this
    request, but it carried only ``status_code: 422``, so the row could not say
    the write was stopped by the secret gate, or how severe the finding was.
    /ingest records both facts on the same failure.
    """
    from test_server import authed_client

    store = AccessStore(tmp_path / "access.json")
    app.state.access_store = store
    client = authed_client()

    class _Engine:
        async def run(self, dataset: str, **kwargs: Any) -> dict[str, Any]:
            raise SecretContentError(
                dataset=dataset,
                highest_severity="critical",
                block_severity="high",
                findings=[{"rule_id": "aws-access-key", "severity": "critical"}],
            )

    original = server_module.get_promotion_engine
    server_module.get_promotion_engine = lambda: _Engine()  # type: ignore[assignment]
    try:
        response = client.post(
            "/api/promote/run",
            json={"dataset": "seat:alice", "dry_run": True},
            headers={MCP_TOOL_HEADER: "citadel_ingest"},
        )
    finally:
        server_module.get_promotion_engine = original  # type: ignore[assignment]

    assert response.status_code == 422
    mcp_rows = [e for e in store.snapshot()["audit_events"] if str(e["action"]).startswith("mcp.")]
    assert mcp_rows, "the MCP surface recorded nothing for a blocked promotion run"
    detail = mcp_rows[-1]["detail"]
    assert detail["blocked"] == "secret_content"
    assert detail["highest_severity"] == "critical"
    assert mcp_rows[-1]["success"] is False
