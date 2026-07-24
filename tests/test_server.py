from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import tempfile
from typing import Any

from fastapi.testclient import TestClient

import kb.server as server_module

from kb.access import AccessIdentity, AccessStore, SESSION_TRACES_DATASET
from kb.config import CitadelConfig
from kb.conflicts import KnowledgeConflictStore
from kb.knowledge_mesh import KnowledgeMesh
from kb.mesh import MeshState
from kb.models import FeedbackResult, IngestResult
from kb.obsidian_sync import ObsidianSyncStore
from kb.server import app


class FakeCitadel:
    config = CitadelConfig(
        tenant_id="test",
        default_dataset="notes",
        admin_key="test-admin",
        reader_keys=("test-reader",),
        writer_keys=("test-writer",),
        auto_improve=True,
        build_global_context_index=True,
    )

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        return IngestResult(True, "accepted", kwargs["dataset"] or "notes", tuple(kwargs["tags"]))

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"query": query, "dataset": kwargs["dataset"], "top_k": kwargs["top_k"]}]

    documents: dict[str, dict[str, Any]] = {}

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        return self.documents.get(document_id)

    async def cleanup_legacy_nodes(self, *, dry_run: bool = True) -> dict[str, Any]:
        return {
            "dry_run": dry_run,
            "counts_by_kind": {"marker": 1, "dataitem": 2},
            "candidates": [{"id": "g1", "kind": "marker", "preview": "x"}],
            "deleted": 0 if dry_run else 3,
        }

    async def feedback(self, request: Any) -> FeedbackResult:
        return FeedbackResult(recorded=bool(request.qa_id), improved=True)

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        return {"dataset": kwargs["dataset"], "session_ids": kwargs["session_ids"]}

    async def cognify_dataset(self, *, dataset: Any = None, verify: bool = False, force: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "dataset": dataset or self.config.default_dataset,
            "graph_before": {"nodes": 0, "edges": 0},
            "graph_after": {"nodes": 5, "edges": 7},
            "graph_grew": True,
            "verify": verify,
            "verification": (
                {"marker": "COGNIFY_TEST_MARKER_x", "search_hit": True, "graph_grew": True, "ok": True}
                if verify
                else None
            ),
        }


class FakeLinearSyncer:
    async def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "dataset": "masumi-network",
            "last_synced_at": "2026-05-21T00:00:00Z",
            "issue_count": 2,
            "mirror_count": 1,
            "state_path": "/tmp/linear-state.json",
        }

    async def run(self, *, force: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "enabled": True,
            "ingested": True,
            "issue_count": 2 if force else 0,
            "mirror_count": 1,
        }

    def issues_for_scope(
        self,
        *,
        scope: str,
        seat_dataset_name: str | None,
    ) -> list[dict[str, Any]]:
        if scope == "org":
            return [
                {"identifier": "MAS-1", "title": "Org issue"},
                {"identifier": "MAS-2", "title": "Another org issue"},
            ]
        if scope == "my" and seat_dataset_name:
            return [{"identifier": "MAS-1", "title": "My mirrored issue"}]
        return []


class FakeGitHubSyncer:
    async def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "org": "masumi-network",
            "source_url": "https://github.com/orgs/masumi-network/repositories",
            "dataset": "masumi-network",
            "session_id": "masumi-github-daily",
            "last_checked_at": "2026-05-21T00:00:00Z",
            "last_digest_at": "2026-05-21T00:00:00Z",
            "tracked_repositories": 3,
            "seen_events": 4,
            "tracked_commit_repositories": 2,
            "include_commits": True,
            "max_commits_per_repo": 5,
            "run_improve": True,
            "ingest_unchanged": True,
        }

    async def run(self, *, force: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "org": "masumi-network",
            "source_url": "https://github.com/orgs/masumi-network/repositories",
            "checked_at": "2026-05-21T00:00:00Z",
            "repos_scanned": 3,
            "changed_count": 1 if force else 0,
            "event_count": 2,
            "commit_count": 1,
            "changed_repositories": [
                {
                    "name": "agent",
                    "full_name": "masumi-network/agent",
                    "url": "https://github.com/masumi-network/agent",
                    "pushed_at": "2026-05-21T00:00:00Z",
                }
            ],
            "recent_commits": [
                {
                    "repo": "masumi-network/agent",
                    "sha": "abc123def456",
                    "message": "update docs",
                }
            ],
            "recent_events": [],
            "ingested": True,
            "improved": True,
        }


class FakeLearningAgent:
    async def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "agent": "citadel-learning-agent",
            "sources": {
                "github": await FakeGitHubSyncer().status(),
                "repo_content": {
                    "ok": True,
                    "source_type": "github_repo_content",
                    "enabled": True,
                    "tracked_files": 8,
                },
            },
            "capabilities": ["summarize_recent_commits", "sync_repo_content"],
        }

    async def run(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        post_to_chat: bool = False,
        include_digest_preview: bool = True,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "agent": "citadel-learning-agent",
            "sources": {
                "github": {
                    **(await FakeGitHubSyncer().run(force=force)),
                    "dry_run": dry_run,
                },
                "repo_content": {
                    "ok": True,
                    "enabled": True,
                    "files_ingested": 0 if dry_run else 4,
                    "files_skipped": 2,
                    "improved": not dry_run,
                    "dry_run": dry_run,
                },
            },
            "ingested": not dry_run,
            "improved": not dry_run,
            "dry_run": dry_run,
            "organization_digest": {
                "enabled": True,
                "meaningful": True,
                **({"preview": "Digest preview"} if include_digest_preview else {}),
            },
            "notifications": {
                "google_chat": {
                    "enabled": True,
                    "sent": post_to_chat,
                    "reason": None if post_to_chat else "preview_only",
                }
            },
        }

    async def test_google_chat_delivery(self, message: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "enabled": True,
            "sent": True,
            "gateway": "google_chat",
            "status_category": "success",
            "message_name": "spaces/AAA/messages/BBB",
            "thread_name": "spaces/AAA/threads/T",
        }

    async def test_gateway_delivery(
        self,
        gateway_name: str,
        *,
        message: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "enabled": True,
            "sent": True,
            "gateway": gateway_name,
            "status_category": "success",
            "message_name": f"{gateway_name}/messages/BBB",
        }


def authed_client(access_key: str = "test-admin") -> TestClient:
    app.state.citadel = FakeCitadel()
    app.state.mesh = MeshState()
    app.state.github_syncer = FakeGitHubSyncer()
    app.state.linear_syncer = FakeLinearSyncer()
    app.state.learning_agent = FakeLearningAgent()
    # Keep knowledge-conflict state out of the repo-local .citadel directory.
    app.state.conflict_store = KnowledgeConflictStore(
        Path(tempfile.mkdtemp()) / "conflicts.json"
    )
    client = TestClient(app, base_url="https://testserver")
    response = client.post("/admin/session", json={"access_key": access_key})
    assert response.status_code == 200
    return client


def test_healthz() -> None:
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "citadel"}


def test_security_headers_are_applied_to_http_responses() -> None:
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "camera=()" in response.headers["permissions-policy"]
    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_security_headers_do_not_force_hsts_on_plain_http() -> None:
    client = TestClient(app, base_url="http://testserver")

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "strict-transport-security" not in response.headers


def test_private_responses_are_no_store() -> None:
    client = authed_client()

    session = client.get("/api/session")
    search = client.post("/search", json={"query": "useful"})

    assert session.status_code == 200
    assert search.status_code == 200
    assert session.headers["cache-control"] == "no-store"
    assert session.headers["pragma"] == "no-cache"
    assert search.headers["cache-control"] == "no-store"
    assert search.headers["pragma"] == "no-cache"


def test_public_health_response_is_not_cacheable() -> None:
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_mcp_legacy_path_redirects_relative() -> None:
    client = TestClient(app, base_url="https://testserver")

    response = client.post("/mcp", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/mcp/"
    assert not response.headers["location"].startswith("http://")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_login_page_uses_static_script_for_csp() -> None:
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/login")

    assert response.status_code == 200
    assert '<script src="/static/login.js" type="module"></script>' in response.text
    assert "<script>\n" not in response.text


def test_api_uses_configured_citadel_service() -> None:
    client = authed_client()

    ready = client.get("/readyz")
    ingest = client.post("/ingest", json={"data": "A useful note", "tags": ["research"]})
    search = client.post("/search", json={"query": "useful", "top_k": 3})
    mesh = client.get("/api/mesh")
    indexes = client.get("/api/indexes")
    sync_status = client.get("/api/github-sync")
    sync_run = client.post("/api/github-sync/run", json={"force": True})
    learning_status = client.get("/api/learning-agent")
    learning_run = client.post("/api/learning-agent/run", json={"force": True})
    feedback = client.post("/feedback", json={"qa_id": "qa-1", "score": 1, "text": "useful"})
    upgrade = client.post("/api/self-upgrade", json={})
    updated_mesh = client.get("/api/mesh")

    assert ready.status_code == 200
    assert ready.json()["default_dataset"] == "notes"
    assert ingest.status_code == 200
    assert ingest.json()["tags"] == ["research"]
    assert search.status_code == 200
    search_hit = search.json()["results"][0]
    assert search_hit["top_k"] == 3
    assert search_hit["id"].startswith("chunk:")
    assert search_hit["_citadel"]["rank"] == 1
    assert search_hit["_citadel"]["dataset"] == "notes"
    assert search_hit["_citadel"]["result_id"] == search_hit["id"]
    assert len(search_hit["_citadel"]["content_sha256"]) == 64
    assert search_hit["_citadel"]["provenance"] == {}
    assert search_hit["_citadel"]["retrieval"] == {
        "untrusted_context": True,
        "citation_required": True,
        "document_drilldown_available": False,
    }
    assert mesh.status_code == 200
    assert mesh.json()["stats"]["documents"] == 1
    assert indexes.status_code == 200
    assert len(indexes.json()["indexes"]) == 4
    assert sync_status.status_code == 200
    assert sync_status.json()["tracked_repositories"] == 3
    assert sync_run.status_code == 200
    assert sync_run.json()["changed_count"] == 1
    assert learning_status.status_code == 200
    assert learning_status.json()["agent"] == "citadel-learning-agent"
    assert learning_run.status_code == 200
    assert learning_run.json()["sources"]["github"]["commit_count"] == 1
    assert feedback.status_code == 200
    assert feedback.json() == {"recorded": True, "improved": True, "ok": True, "reason": None}
    assert updated_mesh.status_code == 200
    # Search telemetry (implicit) + explicit /feedback both land in the feedback index.
    assert updated_mesh.json()["stats"]["feedback"] >= 2
    assert upgrade.status_code == 200


def test_linear_sync_api_endpoints() -> None:
    client = authed_client()

    status = client.get("/api/linear-sync")
    org_issues = client.get("/api/linear-sync/issues?scope=org")
    my_issues = client.get("/api/linear-sync/issues?scope=my")
    sync_run = client.post("/api/linear-sync/run", json={"force": True})
    sources = client.get("/api/sources?type=linear")

    assert status.status_code == 200
    assert status.json()["enabled"] is True
    assert status.json()["issue_count"] == 2
    assert org_issues.status_code == 200
    assert org_issues.json()["count"] == 2
    assert my_issues.status_code == 200
    assert my_issues.json()["count"] == 0
    assert sync_run.status_code == 200
    assert sync_run.json()["issue_count"] == 2
    assert sources.status_code == 200
    assert sources.json()["summary"]["linear_issues"] == 2
    linear_source = sources.json()["sources"][0]
    assert linear_source["source_type"] == "linear"
    assert linear_source["documents"] == 2


def test_admin_can_run_cognify_recovery_and_verification() -> None:
    client = authed_client()

    recover = client.post("/api/cognify/run", json={"dataset": "masumi-network"})
    verify = client.post("/api/cognify/run", json={"verify": True})

    assert recover.status_code == 200
    assert recover.json()["dataset"] == "masumi-network"
    assert recover.json()["graph_grew"] is True
    assert recover.json()["verification"] is None
    assert verify.status_code == 200
    assert verify.json()["verify"] is True
    assert verify.json()["verification"]["ok"] is True


def test_cognify_run_requires_admin() -> None:
    client = authed_client("test-writer")

    response = client.post("/api/cognify/run", json={})

    assert response.status_code == 403


def test_knowledge_events_api_returns_resumable_timeline() -> None:
    client = authed_client()

    ingest = client.post("/ingest", json={"data": "A useful note", "tags": ["research"]})
    search = client.post("/search", json={"query": "useful", "top_k": 3})
    timeline = client.get("/api/knowledge/events?limit=10")
    resumed = client.get("/api/knowledge/events?after_id=1&limit=1")
    chunked = client.get("/api/knowledge/events?kind=chunk_indexed")
    typed = client.get("/api/knowledge/events?type=search")
    invalid_limit = client.get("/api/knowledge/events?limit=0")
    invalid_after = client.get("/api/knowledge/events?after_id=-1")
    unauthenticated = TestClient(app, base_url="https://testserver").get(
        "/api/knowledge/events"
    )

    assert ingest.status_code == 200
    assert search.status_code == 200
    assert timeline.status_code == 200
    timeline_body = timeline.json()
    assert timeline_body["ok"] is True
    assert timeline_body["latest_event_id"] == timeline_body["stats"]["latest_event_id"]
    assert timeline_body["stats"]["indexed_chunks"] == 1
    # Implicit search telemetry also lands on the timeline, so locate the search
    # event by type rather than assuming it is the newest one.
    search_event = next(e for e in timeline_body["events"] if e["type"] == "search")
    assert search_event["timeline"] == {
        "kind": "retrieval_served",
        "status": "searched",
        "dataset": "notes",
        "source": "search",
        "metrics": {"results": 1},
    }
    assert resumed.status_code == 200
    # Resuming from id 1 yields the next event, newest first — assert the cursor
    # advances rather than pinning how many events one search now produces.
    resumed_ids = [event["id"] for event in resumed.json()["events"]]
    assert len(resumed_ids) == 1
    assert resumed_ids[0] > 1
    assert chunked.status_code == 200
    assert [event["id"] for event in chunked.json()["events"]] == [1]
    assert typed.status_code == 200
    assert [event["type"] for event in typed.json()["events"]] == ["search"]
    assert invalid_limit.status_code == 422
    assert invalid_after.status_code == 422
    assert unauthenticated.status_code == 401


def test_knowledge_events_scopes_timeline_to_the_caller(tmp_path: Any) -> None:
    # ADR-0009: the timeline carries Node content (event messages, dataset names,
    # error operations). It previously discarded the identity and returned every
    # seat's events to any reader token — visible in plain `citadel activity`
    # output, which printed other seats' ingests under the caller's own token.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    created = admin.post(
        "/api/access/tokens",
        json={
            "name": "scoped-reader",
            "role": "writer",
            "kind": "service_account",
            "default_dataset": "personal",
            "allowed_datasets": ["personal"],
        },
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")
    headers = {"Authorization": f"Bearer {token}"}

    # An event on a dataset the scoped token cannot see, and one it can.
    admin.post("/ingest", json={"data": "Another seat's note", "dataset": "masumi-network"})
    api_client.post("/ingest", json={"data": "My own note"}, headers=headers)

    scoped = api_client.get("/api/knowledge/events?limit=50", headers=headers)
    unscoped = admin.get("/api/knowledge/events?limit=50")

    assert scoped.status_code == 200
    scoped_datasets = {
        (event.get("details") or {}).get("dataset") for event in scoped.json()["events"]
    }
    assert "masumi-network" not in scoped_datasets
    assert "personal" in scoped_datasets
    # latest_event_id stays global so --watch resumption cannot loop forever.
    assert scoped.json()["latest_event_id"] == unscoped.json()["latest_event_id"]
    # An admin/bypass caller still sees everything.
    unscoped_datasets = {
        (event.get("details") or {}).get("dataset") for event in unscoped.json()["events"]
    }
    assert {"masumi-network", "personal"} <= unscoped_datasets


def test_reader_access_can_view_and_search_but_not_mutate() -> None:
    client = authed_client("test-reader")

    session = client.get("/api/session")
    mesh = client.get("/api/mesh")
    search = client.post("/search", json={"query": "useful"})
    ingest = client.post("/ingest", json={"data": "A useful note"})
    sync_run = client.post("/api/github-sync/run", json={"force": True})

    assert session.status_code == 200
    assert session.json()["role"] == "reader"
    assert session.json()["capabilities"] == {"read": True, "write": False, "admin": False}
    assert mesh.status_code == 200
    assert search.status_code == 200
    assert ingest.status_code == 403
    assert sync_run.status_code == 403


def test_ingest_inline_cognify_flag() -> None:
    client = authed_client()
    # Default: no inline cognify, no `cognified` in the response.
    plain = client.post("/ingest", json={"data": "note one", "tags": []})
    assert plain.status_code == 200
    assert "cognified" not in plain.json()
    # cognify=True → the Node cognifies inline (server-side) and reports it.
    with_cognify = client.post("/ingest", json={"data": "note two", "tags": [], "cognify": True})
    assert with_cognify.status_code == 200
    assert with_cognify.json()["cognified"] is True


def test_ingest_and_contribute_reject_oversized_payloads(monkeypatch: Any) -> None:
    monkeypatch.setenv("CITADEL_MCP_MAX_INGEST_BYTES", "16")
    client = authed_client()

    big = "x" * 50
    ingest = client.post("/ingest", json={"data": big})
    contribute = client.post("/api/contribute", json={"title": "T", "content": big})
    small = client.post("/ingest", json={"data": "ok"})

    assert ingest.status_code == 413
    assert "limit is 16 bytes" in ingest.json()["detail"]
    assert contribute.status_code == 413
    assert small.status_code == 200


def test_mcp_accept_shim_advertises_both_content_types() -> None:
    # #45: minimal MCP clients (json-only / no Accept / */*) must reach the
    # streamable-HTTP transport, which 406s unless Accept lists both types.
    from kb.server import _McpAcceptShim

    captured: dict[str, Any] = {}

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        captured["scope"] = scope

    shim = _McpAcceptShim(inner)

    def accept_after(headers: list[tuple[bytes, bytes]]) -> list[str]:
        captured.clear()
        asyncio.run(shim({"type": "http", "headers": headers}, None, None))
        return [
            v.decode("latin-1")
            for n, v in captured["scope"]["headers"]
            if n.lower() == b"accept"
        ]

    both = "application/json, text/event-stream"
    assert accept_after([]) == [both]
    assert accept_after([(b"accept", b"*/*")]) == [both]
    assert accept_after([(b"accept", b"application/json, text/event-stream")]) == [both]

    json_only = accept_after([(b"accept", b"application/json")])
    assert len(json_only) == 1
    assert "application/json" in json_only[0] and "text/event-stream" in json_only[0]

    sse_only = accept_after([(b"accept", b"text/event-stream")])
    assert "application/json" in sse_only[0] and "text/event-stream" in sse_only[0]

    # Non-http scopes pass through untouched.
    captured.clear()
    asyncio.run(shim({"type": "lifespan"}, None, None))
    assert captured["scope"]["type"] == "lifespan"


def test_obsidian_push_enforces_byte_cap_per_document(monkeypatch: Any, tmp_path: Any) -> None:
    # #51: the obsidian sync push path must honor the same byte cap as /ingest. An
    # oversized note is rejected individually without failing the rest of the sync.
    monkeypatch.setenv("CITADEL_MCP_MAX_INGEST_BYTES", "32")
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    client = authed_client("test-writer")

    vault_id = client.post("/api/obsidian/vaults", json={"vault_name": "V"}).json()["vault"]["id"]
    pushed = client.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "documents": [
                {"path": "small.md", "content": "tiny note"},
                {"path": "big.md", "content": "x" * 200},
            ],
        },
    )

    assert pushed.status_code == 200
    results = {r["document_id"]: r for r in pushed.json()["ingest_results"]}
    by_path = {a["path"]: a["document_id"] for a in pushed.json()["accepted"]}
    assert results[by_path["small.md"]]["accepted"] is True
    big = results[by_path["big.md"]]
    assert big["accepted"] is False
    assert "limit is 32 bytes" in big["reason"]


def test_writer_access_can_ingest_and_feedback_but_not_admin_actions() -> None:
    client = authed_client("test-writer")

    session = client.get("/api/session")
    ingest = client.post("/ingest", json={"data": "A useful note"})
    feedback = client.post("/feedback", json={"qa_id": "qa-1", "score": 1})
    upgrade = client.post("/api/self-upgrade", json={})

    assert session.status_code == 200
    assert session.json()["role"] == "writer"
    assert session.json()["capabilities"] == {"read": True, "write": True, "admin": False}
    assert ingest.status_code == 200
    assert feedback.status_code == 200
    assert upgrade.status_code == 403


def test_ui_requires_admin_key() -> None:
    app.state.citadel = FakeCitadel()
    client = TestClient(app)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_ui_shell_is_served_after_login() -> None:
    client = authed_client()

    response = client.get("/")

    assert response.status_code == 200
    assert "Citadel Archive" in response.text
    assert "CITADEL" in response.text
    assert "Knowledge Mesh" in response.text
    assert 'id="graphCanvas"' in response.text
    assert "Source Sync" in response.text
    assert "Run learning agent" in response.text
    assert "Send Google Chat test" in response.text
    assert "Obsidian Vaults" in response.text
    assert "mcp-remote" in response.text
    assert "Audit event filter" in response.text


def test_admin_can_create_and_use_role_based_access_token(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    created = client.post(
        "/api/access/tokens",
        json={"name": "research-agent", "role": "reader", "kind": "service_account"},
    )

    assert created.status_code == 200
    payload = created.json()
    assert payload["token"].startswith("ctdl_")
    assert "token_hash" not in payload["api_token"]

    access = client.get("/api/access")
    assert access.status_code == 200
    assert access.json()["principals"][0]["name"] == "research-agent"
    assert access.json()["tokens"][0]["prefix"] == payload["token"][:12]
    assert "token_hash" not in access.text

    token_client = authed_client(payload["token"])
    session = token_client.get("/api/session")
    search = token_client.post("/search", json={"query": "useful"})
    ingest = token_client.post("/ingest", json={"data": "A useful note"})
    admin_access = token_client.get("/api/access")

    assert session.status_code == 200
    assert session.json()["role"] == "reader"
    assert session.json()["actor"]["name"] == "research-agent"
    assert search.status_code == 200
    assert ingest.status_code == 403
    assert admin_access.status_code == 403


def test_bearer_tokens_can_access_api_without_cookie(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={"name": "writer-agent", "role": "writer", "kind": "service_account"},
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    session = api_client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
    ingest = api_client.post(
        "/ingest",
        json={"data": "A useful note"},
        headers={"Authorization": f"Bearer {token}"},
    )
    admin_run = api_client.post(
        "/api/learning-agent/run",
        json={"force": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert session.status_code == 200
    assert session.json()["role"] == "writer"
    assert ingest.status_code == 200
    assert admin_run.status_code == 403


def test_token_default_dataset_is_used_when_search_omits_dataset(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={
            "name": "personal-reader",
            "role": "reader",
            "kind": "service_account",
            "default_dataset": "personal",
        },
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    session = api_client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
    search = api_client.post(
        "/search",
        json={"query": "notes"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert session.status_code == 200
    assert session.json()["default_dataset"] == "personal"
    assert session.json()["default_session"] is None
    assert session.json()["allowed_datasets"] is None
    assert search.status_code == 200
    assert search.json()["dataset"] == "personal"
    assert search.json()["results"][0]["dataset"] == "personal"


def test_token_allowed_datasets_rejects_other_dataset(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={
            "name": "scoped-writer",
            "role": "writer",
            "kind": "service_account",
            "default_dataset": "personal",
            "allowed_datasets": ["personal"],
        },
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    allowed = api_client.post(
        "/ingest",
        json={"data": "Scoped note"},
        headers={"Authorization": f"Bearer {token}"},
    )
    denied = api_client.post(
        "/search",
        json={"query": "notes", "dataset": "masumi-network"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert allowed.status_code == 200
    assert allowed.json()["dataset"] == "personal"
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Dataset not allowed: masumi-network."


def test_feedback_rejects_dataset_outside_token_allowlist(tmp_path: Any) -> None:
    # /feedback is a durable write on a cache miss. Before this was resolved, a
    # writer token could name any dataset — including another seat's node, which
    # enforce_dataset_allowlist is default-deny for — and have the write and its
    # mesh event attributed there.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={
            "name": "scoped-feedback-writer",
            "role": "writer",
            "kind": "service_account",
            "default_dataset": "personal",
            "allowed_datasets": ["personal"],
        },
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    denied = api_client.post(
        "/feedback",
        json={"qa_id": "qa-1", "score": 1, "text": "useful", "dataset": "seat:someone-else"},
        headers={"Authorization": f"Bearer {token}"},
    )
    allowed = api_client.post(
        "/feedback",
        json={"qa_id": "qa-1", "score": 1, "text": "useful"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"] == "Dataset not allowed: seat:someone-else."
    assert allowed.status_code == 200


def test_feedback_rejects_oversized_text(tmp_path: Any) -> None:
    # FeedbackBody.text carries no max_length; the durable-write path needs the
    # same byte cap as /ingest.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={"name": "feedback-writer", "role": "writer", "kind": "service_account"},
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    oversized = api_client.post(
        "/feedback",
        json={"qa_id": "qa-1", "score": 1, "text": "x" * (200 * 1024 + 1)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert oversized.status_code == 413


def test_tokens_without_memory_fields_use_config_defaults(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={"name": "legacy-reader", "role": "reader", "kind": "service_account"},
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    session = api_client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
    search = api_client.post(
        "/search",
        json={"query": "notes"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert session.status_code == 200
    assert session.json()["default_dataset"] == "notes"
    assert search.status_code == 200
    assert search.json()["dataset"] == "notes"


def test_admin_token_bypasses_allowed_datasets(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={
            "name": "admin-scoped",
            "role": "admin",
            "kind": "service_account",
            "allowed_datasets": ["personal"],
        },
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    search = api_client.post(
        "/search",
        json={"query": "notes", "dataset": "masumi-network"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert search.status_code == 200
    assert search.json()["dataset"] == "masumi-network"


def test_google_chat_test_delivery_is_admin_only_and_redacted(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    reader = authed_client("test-reader")

    denied = reader.post("/api/learning-agent/google-chat/test", json={})
    response = admin.post(
        "/api/learning-agent/google-chat/test",
        json={"message": "custom rollout smoke test"},
    )

    assert denied.status_code == 403
    assert response.status_code == 200
    assert response.json()["sent"] is True
    events = app.state.access_store.snapshot()["audit_events"]
    event = events[-1]
    serialized = str(event)
    assert event["action"] == "learning_agent.google_chat_test"
    assert event["success"] is True
    assert event["detail"]["status_category"] == "success"
    assert event["detail"]["message_name"] == "spaces/AAA/messages/BBB"
    assert "custom rollout smoke test" not in serialized


def test_gateway_test_delivery_is_admin_only_and_redacted(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    reader = authed_client("test-reader")

    denied = reader.post("/api/learning-agent/gateways/google_chat/test", json={})
    response = admin.post(
        "/api/learning-agent/gateways/google_chat/test",
        json={"message": "gateway rollout smoke test"},
    )

    assert denied.status_code == 403
    assert response.status_code == 200
    assert response.json()["sent"] is True
    events = app.state.access_store.snapshot()["audit_events"]
    event = events[-1]
    serialized = str(event)
    assert event["action"] == "learning_agent.gateway_test"
    assert event["success"] is True
    assert event["detail"]["gateway"] == "google_chat"
    assert event["detail"]["status_category"] == "success"
    assert "gateway rollout smoke test" not in serialized


def test_custom_token_scopes_are_enforced(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={
            "name": "ingest-only-agent",
            "role": "writer",
            "kind": "service_account",
            "scopes": ["kb:ingest"],
        },
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    login = api_client.post("/admin/session", json={"access_key": token})
    ingest = api_client.post(
        "/ingest",
        json={"data": "A scoped note"},
        headers={"Authorization": f"Bearer {token}"},
    )
    feedback = api_client.post(
        "/feedback",
        json={"qa_id": "qa-1", "score": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    search = api_client.post(
        "/search",
        json={"query": "note"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert created.status_code == 200
    assert created.json()["api_token"]["scopes"] == ["kb:ingest"]
    assert login.status_code == 200
    assert login.json()["capabilities"] == {"read": False, "write": True, "admin": False}
    assert ingest.status_code == 200
    assert feedback.status_code == 403
    assert feedback.json()["detail"] == "Scope required: kb:feedback."
    assert search.status_code == 403
    assert search.json()["detail"] == "Scope required: kb:search."


def test_custom_token_scopes_cannot_exceed_role(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    reader_with_write_scope = client.post(
        "/api/access/tokens",
        json={
            "name": "bad-reader",
            "role": "reader",
            "kind": "service_account",
            "scopes": ["kb:read", "kb:ingest"],
        },
    )
    writer_with_admin_scope = client.post(
        "/api/access/tokens",
        json={
            "name": "bad-writer",
            "role": "writer",
            "kind": "service_account",
            "scopes": ["kb:read", "sources:sync"],
        },
    )

    assert reader_with_write_scope.status_code == 422
    assert "Scopes exceed reader role: kb:ingest" in reader_with_write_scope.json()["detail"]
    assert writer_with_admin_scope.status_code == 422
    assert "Scopes exceed writer role: sources:sync" in writer_with_admin_scope.json()["detail"]


def test_admin_token_scopes_are_enforced_for_management_surfaces(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={
            "name": "access-manager",
            "role": "admin",
            "kind": "service_account",
            "scopes": ["access:manage"],
        },
    )
    token = created.json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    access = api_client.get("/api/access", headers={"Authorization": f"Bearer {token}"})
    audit = api_client.get("/api/audit", headers={"Authorization": f"Bearer {token}"})
    learning_run = api_client.post(
        "/api/learning-agent/run",
        json={"dry_run": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert created.status_code == 200
    assert access.status_code == 200
    assert audit.status_code == 403
    assert audit.json()["detail"] == "Scope required: audit:read."
    assert learning_run.status_code == 403
    assert learning_run.json()["detail"] == "Scope required: sources:sync."


def test_mcp_search_calls_are_attributable_and_redacted(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client("test-reader")

    response = client.post(
        "/search",
        json={
            "query": "sensitive roadmap question",
            "dataset": "masumi-network",
            "top_k": 2,
        },
        headers={"X-Citadel-MCP-Tool": "citadel_search"},
    )

    assert response.status_code == 200
    events = app.state.access_store.snapshot()["audit_events"]
    event = events[-1]
    serialized = str(event)

    assert event["action"] == "mcp.citadel_search"
    assert event["actor_id"] == "bootstrap:reader"
    assert event["role"] == "reader"
    assert event["success"] is True
    assert event["dataset"] == "masumi-network"
    assert event["detail"]["surface"] == "mcp"
    assert event["detail"]["tool"] == "citadel_search"
    assert event["detail"]["required_role"] == "reader"
    assert event["detail"]["required_scope"] == "kb:search"
    assert event["detail"]["result_count"] == 1
    assert event["detail"]["top_k"] == 2
    assert event["detail"]["query_length"] == len("sensitive roadmap question")
    assert "query_sha256" in event["detail"]
    assert "sensitive roadmap question" not in serialized


def test_failed_mcp_auth_is_audited_without_actor(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.citadel = FakeCitadel()
    app.state.mesh = MeshState()
    client = TestClient(app, base_url="https://testserver")

    response = client.post(
        "/search",
        json={"query": "anything"},
        headers={"X-Citadel-MCP-Tool": "citadel_search"},
    )

    assert response.status_code == 401
    events = app.state.access_store.snapshot()["audit_events"]
    event = events[-1]

    assert event["action"] == "mcp.citadel_search"
    assert event["actor_id"] is None
    assert event["role"] is None
    assert event["success"] is False
    assert event["dataset"] is None
    assert event["detail"]["surface"] == "mcp"
    assert event["detail"]["tool"] == "citadel_search"
    assert event["detail"]["path"] == "/search"
    assert event["detail"]["status_code"] == 401


def test_audit_api_filters_summarizes_and_limits_events(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    created = admin.post(
        "/api/access/tokens",
        json={"name": "reader-agent", "role": "reader", "kind": "service_account"},
    )
    reader = authed_client("test-reader")
    search = reader.post(
        "/search",
        json={"query": "sensitive project question", "dataset": "masumi-network"},
        headers={"X-Citadel-MCP-Tool": "citadel_search"},
    )
    session = reader.get("/api/session", headers={"X-Citadel-MCP-Tool": "citadel_session"})
    unauthenticated = TestClient(app, base_url="https://testserver")
    failed = unauthenticated.post(
        "/search",
        json={"query": "do not log this"},
        headers={"X-Citadel-MCP-Tool": "citadel_search"},
    )

    all_events = admin.get("/api/audit")
    mcp_events = admin.get("/api/audit?view=mcp&limit=1")
    access_events = admin.get("/api/audit?view=access")
    failure_events = admin.get("/api/audit?view=failures")
    invalid_view = admin.get("/api/audit?view=unknown")
    invalid_limit = admin.get("/api/audit?limit=0")

    assert created.status_code == 200
    assert search.status_code == 200
    assert session.status_code == 200
    assert failed.status_code == 401

    assert all_events.status_code == 200
    assert all_events.json()["view"] == "all"
    assert all_events.json()["summary"] == {
        "total_events": 4,
        "returned_events": 4,
        "mcp_events": 3,
        "access_events": 1,
        "failure_events": 1,
        "mcp_failures": 1,
        "mcp_actors": 1,
    }

    assert mcp_events.status_code == 200
    assert mcp_events.json()["view"] == "mcp"
    assert mcp_events.json()["summary"]["returned_events"] == 1
    assert len(mcp_events.json()["audit_events"]) == 1
    assert mcp_events.json()["audit_events"][0]["action"] == "mcp.citadel_search"
    assert mcp_events.json()["audit_events"][0]["success"] is False
    assert "do not log this" not in str(mcp_events.json())

    assert access_events.status_code == 200
    assert [event["action"] for event in access_events.json()["audit_events"]] == [
        "access.token.create"
    ]

    assert failure_events.status_code == 200
    assert [event["id"] for event in failure_events.json()["audit_events"]] == [
        mcp_events.json()["audit_events"][0]["id"]
    ]
    assert invalid_view.status_code == 422
    assert invalid_limit.status_code == 422


def test_backup_mirror_status_and_run_are_admin_scoped(tmp_path: Any) -> None:
    config = CitadelConfig(
        admin_key="test-admin",
        reader_keys=("test-reader",),
        writer_keys=("test-writer",),
        access_store_path=str(tmp_path / "access.json"),
        obsidian_sync_state_path=str(tmp_path / "obsidian.json"),
        github_sync_state_path=str(tmp_path / "github.json"),
        backup_mirror_root_path=str(tmp_path / "mirror"),
        backup_mirror_enabled=False,
    )
    (tmp_path / "github.json").write_text(
        '{"last_checked_at":"2026-06-03T00:00:00Z"}',
        encoding="utf-8",
    )
    citadel = FakeCitadel()
    citadel.config = config
    app.state.citadel = citadel
    app.state.mesh = MeshState()
    app.state.access_store = AccessStore(config.access_store_path)
    admin = TestClient(app, base_url="https://testserver")
    reader = TestClient(app, base_url="https://testserver")
    assert admin.post("/admin/session", json={"access_key": "test-admin"}).status_code == 200
    assert reader.post("/admin/session", json={"access_key": "test-reader"}).status_code == 200

    status = admin.get("/api/backup-mirror")
    dry_run = admin.post("/api/backup-mirror/run", json={"dry_run": True})
    disabled_run = admin.post("/api/backup-mirror/run", json={"dry_run": False})
    reader_status = reader.get("/api/backup-mirror")

    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert status.json()["summary"]["available_files"] == 1
    assert dry_run.status_code == 200
    assert dry_run.json()["written"] is False
    assert dry_run.json()["manifest"]["summary"]["available_files"] == 1
    assert disabled_run.status_code == 409
    assert reader_status.status_code == 403
    events = app.state.access_store.snapshot()["audit_events"]
    assert [event["action"] for event in events] == ["backup_mirror.run", "backup_mirror.run"]
    assert events[0]["success"] is True
    assert events[1]["success"] is False


def test_backup_mirror_push_errors_are_audited(tmp_path: Any) -> None:
    config = CitadelConfig(
        admin_key="test-admin",
        access_store_path=str(tmp_path / "access.json"),
        obsidian_sync_state_path=str(tmp_path / "obsidian.json"),
        github_sync_state_path=str(tmp_path / "github.json"),
        backup_mirror_root_path=str(tmp_path / "mirror"),
        backup_mirror_enabled=True,
        backup_mirror_push_enabled=True,
        backup_mirror_token=None,
    )
    citadel = FakeCitadel()
    citadel.config = config
    app.state.citadel = citadel
    app.state.mesh = MeshState()
    app.state.access_store = AccessStore(config.access_store_path)
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/admin/session", json={"access_key": "test-admin"}).status_code == 200

    response = client.post("/api/backup-mirror/run", json={"dry_run": False})

    assert response.status_code == 502
    assert response.json()["detail"] == "Vault Backup Mirror push token is not configured."
    events = app.state.access_store.snapshot()["audit_events"]
    assert events[-1]["action"] == "backup_mirror.run"
    assert events[-1]["success"] is False
    assert events[-1]["detail"]["reason"] == "publish_failed"


def test_obsidian_vault_is_not_readable_or_writable_by_another_actor(tmp_path: Any) -> None:
    # owner_actor_id was recorded at registration and read nowhere, so any token
    # holding the obsidian scopes could address another seat's vault by id — and
    # ids are disclosed via /api/sources. Reads returned full note bodies and
    # revision history. 404 (not 403) everywhere: no existence oracle.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    admin = authed_client()

    owner_token = admin.post(
        "/api/access/tokens",
        json={"name": "vault-owner", "role": "writer", "kind": "service_account"},
    ).json()["token"]
    intruder_token = admin.post(
        "/api/access/tokens",
        json={"name": "vault-intruder", "role": "writer", "kind": "service_account"},
    ).json()["token"]

    api = TestClient(app, base_url="https://testserver")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    intruder_headers = {"Authorization": f"Bearer {intruder_token}"}

    vault_id = api.post(
        "/api/obsidian/vaults", json={"vault_name": "Private Vault"}, headers=owner_headers
    ).json()["vault"]["id"]
    api.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "documents": [{"path": "Secrets.md", "content": "private body", "base_rev": None}],
        },
        headers=owner_headers,
    )

    owner_manifest = api.get(f"/api/obsidian/manifest?vault_id={vault_id}", headers=owner_headers)
    document_id = owner_manifest.json()["documents"][0]["id"]

    intruder_manifest = api.get(
        f"/api/obsidian/manifest?vault_id={vault_id}", headers=intruder_headers
    )
    intruder_pull = api.get(
        f"/api/obsidian/sync/pull?vault_id={vault_id}&cursor=0", headers=intruder_headers
    )
    intruder_document = api.get(f"/api/documents/{document_id}", headers=intruder_headers)
    intruder_push = api.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "documents": [{"path": "Secrets.md", "content": "overwritten", "base_rev": None}],
        },
        headers=intruder_headers,
    )

    assert owner_manifest.status_code == 200
    assert intruder_manifest.status_code == 404
    assert intruder_pull.status_code == 404
    assert intruder_document.status_code == 404
    assert intruder_push.status_code == 404
    # The owner still has full access, and an admin/bypass token is unaffected.
    assert api.get(f"/api/documents/{document_id}", headers=owner_headers).status_code == 200
    assert admin.get(f"/api/obsidian/manifest?vault_id={vault_id}").status_code == 200


def test_obsidian_vault_sync_registers_pushes_pulls_and_lists_sources(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    client = authed_client("test-writer")

    registered = client.post(
        "/api/obsidian/vaults",
        json={"vault_name": "Team Vault", "team_id": "masumi", "plugin_version": "0.1.0"},
    )
    assert registered.status_code == 200
    vault_id = registered.json()["vault"]["id"]

    manifest = client.get(f"/api/obsidian/manifest?vault_id={vault_id}")
    pushed = client.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "dataset": "notes",
            "tags": ["team"],
            "documents": [
                {
                    "path": "Team/Architecture.md",
                    "content": "Architecture decision: Citadel syncs explicit Obsidian notes.",
                }
            ],
        },
    )

    assert manifest.status_code == 200
    assert manifest.json()["documents"] == []
    assert pushed.status_code == 200
    payload = pushed.json()
    assert payload["accepted"][0]["path"] == "Team/Architecture.md"
    assert payload["accepted"][0]["rev"] == 1
    assert payload["ingest_results"][0]["accepted"] is True
    document_id = payload["accepted"][0]["document_id"]

    document = client.get(f"/api/documents/{document_id}")
    pulled = client.get(f"/api/obsidian/sync/pull?vault_id={vault_id}&cursor=0")
    sources = client.get("/api/sources?type=obsidian_vault")

    assert document.status_code == 200
    assert document.json()["document"]["body"].startswith("Architecture decision")
    assert pulled.status_code == 200
    assert pulled.json()["documents"][0]["normalized_path"] == "Team/Architecture.md"
    assert sources.status_code == 200
    assert sources.json()["summary"]["obsidian_vaults"] == 1
    assert sources.json()["summary"]["obsidian_documents"] == 1

    reader = authed_client("test-reader")
    rejected = reader.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "documents": [{"path": "Team/Denied.md", "content": "Reader cannot push."}],
        },
    )
    assert rejected.status_code == 403


def test_obsidian_vault_sync_detects_and_resolves_conflicts(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    app.state.conflict_store = KnowledgeConflictStore(tmp_path / "conflicts.json")
    client = authed_client("test-writer")
    vault_id = client.post("/api/obsidian/vaults", json={"vault_name": "Team Vault"}).json()[
        "vault"
    ]["id"]
    first_push = client.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "documents": [{"path": "Team/Roadmap.md", "content": "Remote revision one."}],
        },
    )
    stale_push = client.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "documents": [
                {
                    "path": "Team/Roadmap.md",
                    "content": "Local edit from stale base.",
                    "base_rev": 0,
                }
            ],
        },
    )

    assert first_push.status_code == 200
    assert stale_push.status_code == 200
    conflict = stale_push.json()["conflicts"][0]
    assert conflict["reason"] == "base_revision_mismatch"

    resolved = client.post(
        f"/api/obsidian/conflicts/{conflict['id']}/resolve",
        json={"resolution": "manual", "body": "Merged roadmap note."},
    )
    manifest = client.get(f"/api/obsidian/manifest?vault_id={vault_id}")
    document_id = first_push.json()["accepted"][0]["document_id"]
    document = client.get(f"/api/documents/{document_id}")

    assert resolved.status_code == 200
    assert resolved.json()["conflict"]["status"] == "resolved_manual"
    assert manifest.json()["documents"][0]["current_rev"] == 2
    assert document.json()["document"]["body"] == "Merged roadmap note."


def test_access_tokens_are_hashed_and_revocable(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/tokens",
        json={"name": "writer", "role": "writer", "kind": "user"},
    )
    token = created.json()["token"]
    token_id = created.json()["api_token"]["id"]

    raw_store = (tmp_path / "access.json").read_text()
    assert token not in raw_store
    assert "token_hash" in raw_store

    revoke = client.post(f"/api/access/tokens/{token_id}/revoke", json={})
    assert revoke.status_code == 200

    rejected = TestClient(app, base_url="https://testserver").post(
        "/admin/session",
        json={"access_key": token},
    )
    assert rejected.status_code == 401


def test_expired_bearer_tokens_are_rejected_with_audit_event(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    created = admin.post(
        "/api/access/tokens",
        json={
            "name": "short-lived",
            "role": "reader",
            "kind": "service_account",
            "expires_at": "2020-01-01T00:00:00+00:00",
        },
    )
    token = created.json()["token"]
    token_id = created.json()["api_token"]["id"]

    rejected = TestClient(app, base_url="https://testserver").get(
        "/api/session",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rejected.status_code == 401

    audit = admin.get("/api/audit?view=failures")
    assert audit.status_code == 200
    rejections = [
        event
        for event in audit.json()["audit_events"]
        if event["action"] == "access.token.rejected"
    ]
    assert rejections[0]["detail"]["reason"] == "expired"
    assert rejections[0]["detail"]["token_id"] == token_id


class EmptyCitadel(FakeCitadel):
    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class ProvenanceCitadel(FakeCitadel):
    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": "ghsync:abc123",
                "source": "github_sync_state",
                "dataset": kwargs["dataset"],
                "session_id": "masumi-github-daily",
                "title": "Recent commits",
                "content": "teach the archive about commits",
                "metadata": {
                    "source_url": "https://github.com/orgs/masumi-network/repositories",
                    "checked_at": "2026-06-01T00:00:00Z",
                },
            }
        ]


def test_search_without_dataset_hints_known_datasets() -> None:
    client = authed_client("test-reader")
    app.state.citadel = EmptyCitadel()

    response = client.post("/search", json={"query": "anything"})

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert "note" in body
    assert "masumi-network" in body["known_datasets"]


def test_search_with_explicit_empty_dataset_omits_hint() -> None:
    client = authed_client("test-reader")
    app.state.citadel = EmptyCitadel()

    response = client.post("/search", json={"query": "anything", "dataset": "notes"})

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert "note" not in body


def test_search_results_include_source_provenance_envelope() -> None:
    client = authed_client("test-reader")
    app.state.citadel = ProvenanceCitadel()

    response = client.post(
        "/search",
        json={"query": "commits", "dataset": "masumi-network"},
    )

    assert response.status_code == 200
    hit = response.json()["results"][0]
    assert hit["id"] == "ghsync:abc123"
    assert hit["_citadel"]["rank"] == 1
    assert hit["_citadel"]["dataset"] == "masumi-network"
    assert hit["_citadel"]["result_id"] == "ghsync:abc123"
    assert hit["_citadel"]["document_endpoint"] == "/api/documents/ghsync:abc123"
    assert hit["_citadel"]["provenance"] == {
        "source": "github_sync_state",
        "source_url": "https://github.com/orgs/masumi-network/repositories",
        "title": "Recent commits",
        "session_id": "masumi-github-daily",
    }
    assert hit["_citadel"]["retrieval"]["untrusted_context"] is True
    assert hit["_citadel"]["retrieval"]["citation_required"] is True
    assert hit["_citadel"]["retrieval"]["document_drilldown_available"] is True


def test_github_digest_search_hit_drills_down_to_document(tmp_path: Any) -> None:
    import json as _json

    state_path = tmp_path / "github_state.json"
    state_path.write_text(
        _json.dumps(
            {
                "org": "masumi-network",
                "last_checked_at": "2026-06-01T00:00:00Z",
                "last_digest_at": "2026-06-01T00:00:00Z",
                "last_digest": "# masumi-network GitHub daily update\n\n"
                "## Recent commits\n- abc: teach the archive about commits.\n",
            }
        ),
        encoding="utf-8",
    )
    citadel = FakeCitadel()
    citadel.config = CitadelConfig(
        admin_key="test-admin",
        reader_keys=("test-reader",),
        writer_keys=("test-writer",),
        github_sync_dataset="masumi-network",
        github_sync_state_path=str(state_path),
    )
    client = authed_client("test-reader")
    app.state.citadel = citadel

    from kb.source_search import search_github_sync_state

    hit = search_github_sync_state("commits", citadel.config, top_k=1)[0]
    document = client.get(f"/api/documents/{hit['id']}")

    assert document.status_code == 200
    assert document.json()["document"]["title"] == hit["title"]
    assert "teach the archive about commits" in document.json()["document"]["body"]


def test_graph_cleanup_admin_only_and_dry_run_default(tmp_path: Any) -> None:
    # #15: destructive cleanup is admin-only and dry-run by default.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()

    dry = admin.post("/api/admin/graph/cleanup", json={})
    assert dry.status_code == 200
    assert dry.json()["dry_run"] is True
    assert dry.json()["deleted"] == 0
    assert dry.json()["candidates"]

    wet = admin.post("/api/admin/graph/cleanup", json={"dry_run": False})
    assert wet.status_code == 200
    assert wet.json()["deleted"] == 3

    reader = authed_client("test-reader")
    assert reader.post("/api/admin/graph/cleanup", json={}).status_code in (401, 403)
    writer = authed_client("test-writer")
    assert writer.post("/api/admin/graph/cleanup", json={}).status_code in (401, 403)


def test_readyz_reports_503_when_data_plane_empty() -> None:
    # #27: many sources tracked but an empty graph → /readyz is RED (503), so an
    # always-on probe and `citadel status` stop reporting green over a broken plane.
    class EmptyGraphCitadel(FakeCitadel):
        async def _graph_counts(self) -> dict[str, int]:
            return {"nodes": 0, "edges": 0}

    class BusySyncer:
        async def status(self) -> dict[str, Any]:
            return {"tracked_repositories": 50, "tracked_files": 0, "issue_count": 0}

    client = authed_client("test-reader")
    app.state.citadel = EmptyGraphCitadel()
    app.state.github_syncer = BusySyncer()
    app.state.repo_content_syncer = BusySyncer()
    app.state.linear_syncer = BusySyncer()

    ready = client.get("/readyz")
    assert ready.status_code == 503
    body = ready.json()
    assert body["ok"] is False
    assert body["corpus"]["ok"] is False
    assert body["corpus"]["indexed_docs"] == 0
    assert body["corpus"]["tracked_sources"] == 50


def test_readyz_ok_when_graph_populated() -> None:
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

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["corpus"]["ok"] is True


def test_search_across_datasets_runs_concurrently() -> None:
    # #50: per-dataset recalls run concurrently, not serially.
    import asyncio as aio

    from kb.server import search_across_datasets

    order: list[tuple[str, str]] = []

    class ConcurrentCitadel:
        config = FakeCitadel.config

        async def search(self, query: str, *, dataset: str, session_id: Any, top_k: int) -> list[Any]:
            order.append(("start", dataset))
            await aio.sleep(0.05)
            order.append(("end", dataset))
            return [{"id": dataset}]

    merged = aio.run(
        search_across_datasets(
            ConcurrentCitadel(), query="q", datasets=["a", "b"], sessions={}, top_k=10
        )
    )
    # Concurrent: both datasets start before either finishes.
    assert order[0][0] == "start" and order[1][0] == "start"
    assert {d for d, _ in merged} == {"a", "b"}


def test_search_returns_429_when_at_capacity(monkeypatch: Any) -> None:
    # #50: at capacity the Node returns a 429 + Retry-After backpressure contract.
    client = authed_client("test-reader")
    monkeypatch.setattr(server_module, "_search_inflight", 9999)

    r = client.post("/search", json={"query": "q", "top_k": 3})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "1"
    assert r.headers["X-RateLimit-Remaining"] == "0"


def test_mesh_graph_and_search_have_independent_budgets(
    monkeypatch: Any, tmp_path: Any
) -> None:
    # CHANGE 2 (#50): the mesh graph read and /search hold SEPARATE concurrency
    # budgets, so a ~15-seat login burst on the default Knowledge Mesh view can't
    # 429 /search — and vice versa. This is a WIRING test (distinct in-flight
    # counters via the real endpoints); real concurrency under the single-loop
    # TestClient would be flaky, so we saturate one budget and assert the other
    # still serves.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client("test-reader")
    app.state.knowledge_mesh = None  # fallback graph → cheap, deterministic 200

    # Search saturated, mesh free → mesh serves (own budget, not search's).
    monkeypatch.setattr(server_module, "_search_inflight", 9999)
    monkeypatch.setattr(server_module, "_mesh_graph_inflight", 0)
    assert client.get("/api/mesh/graph").status_code == 200

    # Mesh saturated → mesh 429s with the same contract, but /search stays free.
    monkeypatch.setattr(server_module, "_search_inflight", 0)
    monkeypatch.setattr(server_module, "_mesh_graph_inflight", 9999)
    mesh = client.get("/api/mesh/graph")
    assert mesh.status_code == 429
    assert mesh.headers["Retry-After"] == "1"
    assert mesh.headers["X-RateLimit-Limit"] == str(
        FakeCitadel.config.mesh_graph_max_concurrency
    )
    assert mesh.headers["X-RateLimit-Remaining"] == "0"
    assert client.post("/search", json={"query": "q", "top_k": 3}).status_code == 200


def test_search_sets_ratelimit_headers_when_served() -> None:
    client = authed_client("test-reader")
    r = client.post("/search", json={"query": "q", "top_k": 3})
    assert r.status_code == 200
    assert int(r.headers["X-RateLimit-Limit"]) >= 1
    assert "X-RateLimit-Remaining" in r.headers


def test_search_degrades_to_empty_on_timeout_budget() -> None:
    # #44: a recall slower than the budget degrades to empty-fast with a note,
    # instead of hanging for 100s+.
    import asyncio as aio
    import dataclasses

    class SlowCitadel(FakeCitadel):
        config = dataclasses.replace(FakeCitadel.config, search_timeout_seconds=0.01)

        async def search(self, query: str, **kwargs: Any) -> list[Any]:
            await aio.sleep(0.3)
            return [{"id": "x"}]

    client = authed_client("test-reader")
    app.state.citadel = SlowCitadel()

    r = client.post("/search", json={"query": "q", "top_k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == []
    assert body.get("timed_out") is True
    assert body.get("truncated") is True
    assert "budget" in body["note"]


def test_document_endpoint_for_result_covers_real_ids_only() -> None:
    # #28: ghsync/doc_/cognee-UUID ids are drillable; synthetic chunk: ids are not.
    from kb.server import document_endpoint_for_result

    uuid = "9dbe579d-eccb-51b6-9bba-13982cbaf69f"
    assert document_endpoint_for_result("ghsync:abc") == "/api/documents/ghsync:abc"
    assert document_endpoint_for_result("doc_123") == "/api/documents/doc_123"
    assert document_endpoint_for_result(uuid) == f"/api/documents/{uuid}"
    assert document_endpoint_for_result("chunk:deadbeef") is None
    assert document_endpoint_for_result("") is None


def test_cognee_search_hit_drills_down_to_document() -> None:
    # #28: a cognee search hit (UUID id) advertises drilldown and resolves to a
    # readable document, instead of advertising false and 404ing.
    class DrilldownCitadel(FakeCitadel):
        async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"id": "node-uuid-1", "text": "the answer is 42"}]

        async def get_document(self, document_id: str) -> dict[str, Any] | None:
            if document_id == "node-uuid-1":
                return {
                    "id": "node-uuid-1",
                    "source_type": "cognee",
                    "title": "Answer",
                    "body": "the answer is 42",
                    "metadata": {},
                }
            return None

    client = authed_client("test-reader")
    app.state.citadel = DrilldownCitadel()  # after authed_client, which resets citadel

    search = client.post("/search", json={"query": "answer", "top_k": 1})
    assert search.status_code == 200
    hit = search.json()["results"][0]
    assert hit["_citadel"]["retrieval"]["document_drilldown_available"] is True
    endpoint = hit["_citadel"]["document_endpoint"]
    assert endpoint == "/api/documents/node-uuid-1"

    doc = client.get(endpoint)
    assert doc.status_code == 200
    assert doc.json()["document"]["body"] == "the answer is 42"
    assert doc.json()["document"]["source_type"] == "cognee"

    # An unknown cognee id still 404s cleanly.
    assert client.get("/api/documents/does-not-exist").status_code == 404


def test_knowledge_conflict_listing_and_resolution_are_role_gated(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    writer = authed_client("test-writer")
    app.state.conflict_store = KnowledgeConflictStore(tmp_path / "conflicts.json")
    vault_id = writer.post("/api/obsidian/vaults", json={"vault_name": "Team Vault"}).json()[
        "vault"
    ]["id"]
    writer.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "documents": [{"path": "Team/Roadmap.md", "content": "Server copy."}],
        },
    )
    writer.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "documents": [
                {"path": "Team/Roadmap.md", "content": "Stale local edit.", "base_rev": 0}
            ],
        },
    )

    listed = writer.get("/api/conflicts?status=open")
    assert listed.status_code == 200
    conflicts = listed.json()["conflicts"]
    assert listed.json()["open_count"] == 1
    assert conflicts[0]["kind"] == "obsidian_push"
    assert conflicts[0]["side_a"]["excerpt"] == "Stale local edit."
    assert conflicts[0]["side_b"]["excerpt"] == "Server copy."
    conflict_id = conflicts[0]["id"]

    mesh = writer.get("/api/mesh")
    conflict_events = [
        event for event in mesh.json()["events"] if event["type"] == "conflict"
    ]
    assert conflict_events[0]["details"]["conflict_id"] == conflict_id

    reader_store = app.state.conflict_store
    reader = authed_client("test-reader")
    app.state.conflict_store = reader_store
    reader_list = reader.get("/api/conflicts")
    reader_resolve = reader.post(
        f"/api/conflicts/{conflict_id}/resolve",
        json={"resolution_note": "reader cannot resolve"},
    )
    assert reader_list.status_code == 200
    assert reader_resolve.status_code == 403

    writer_store = app.state.conflict_store
    writer = authed_client("test-writer")
    app.state.conflict_store = writer_store
    resolved = writer.post(
        f"/api/conflicts/{conflict_id}/resolve",
        json={"resolution_note": "Kept the newer server revision."},
    )
    assert resolved.status_code == 200
    assert resolved.json()["conflict"]["status"] == "resolved"
    assert resolved.json()["conflict"]["resolution_note"] == "Kept the newer server revision."
    assert writer.get("/api/conflicts?status=open").json()["conflicts"] == []

    invalid_status = writer.get("/api/conflicts?status=bogus")
    missing = writer.post(
        "/api/conflicts/kconflict_missing/resolve",
        json={"resolution_note": "nothing here"},
    )
    assert invalid_status.status_code == 422
    assert missing.status_code == 404

    actions = [event["action"] for event in app.state.access_store.snapshot()["audit_events"]]
    assert "conflicts.list" in actions
    assert "conflicts.resolve" in actions


def test_knowledge_conflicts_require_authentication() -> None:
    client = TestClient(app, base_url="https://testserver")

    assert client.get("/api/conflicts").status_code == 401
    assert (
        client.post(
            "/api/conflicts/kconflict_x/resolve",
            json={"resolution_note": "no"},
        ).status_code
        == 401
    )


def test_mesh_graph_returns_fallback_without_cognee_graph_access(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client("test-reader")
    app.state.knowledge_mesh = None  # force rebuild from FakeCitadel (no gateway)

    response = client.get("/api/mesh/graph")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["fallback"] is True
    assert body["fallback_reason"] == "graph_access_unavailable"
    # ADR-0009: presence hubs render even on fallback payloads — Central is
    # always there (no seats exist in this fresh access store).
    assert [node["id"] for node in body["nodes"]] == ["dataset:masumi-network"]
    assert body["nodes"][0]["presence"] == {"documents": 0}
    assert body["edges"] == []
    assert body["total_nodes"] == 0
    assert body["limit"] == 200


class BrokenSnapshotAccessStore:
    """Delegates to a real store except snapshot(), which raises."""

    def __init__(self, inner: AccessStore) -> None:
        self._inner = inner

    def snapshot(self) -> dict[str, Any]:
        raise RuntimeError("access store offline")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_mesh_graph_survives_access_store_read_failure(tmp_path: Any) -> None:
    # A broken access-store read degrades presence to Central-only; the graph
    # endpoint must keep returning 200 with the Central hub present.
    class FakeGraphGateway:
        async def graph_data(self) -> tuple[list[Any], list[Any]]:
            return ([("n1", {"name": "Citadel", "type": "Entity"})], [])

    store = AccessStore(tmp_path / "access.json")
    app.state.access_store = store
    client = authed_client("test-reader")
    app.state.knowledge_mesh = KnowledgeMesh(FakeGraphGateway())
    app.state.access_store = BrokenSnapshotAccessStore(store)
    try:
        response = client.get("/api/mesh/graph")
    finally:
        app.state.knowledge_mesh = None
        app.state.access_store = store

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert any(node["id"] == "dataset:masumi-network" for node in body["nodes"])
    assert any(node["id"] == "n1" for node in body["nodes"])


def test_mesh_graph_serves_real_graph_through_injected_gateway(tmp_path: Any) -> None:
    class FakeGraphGateway:
        async def graph_data(self) -> tuple[list[Any], list[Any]]:
            return (
                [
                    ("n1", {"name": "Citadel", "type": "Entity"}),
                    ("n2", {"name": "Cognee", "type": "Tool"}),
                    ("n3", {"name": "Kuzu", "type": "Tool"}),
                ],
                [("n1", "n2", "uses", {}), ("n2", "n3", "embeds", {})],
            )

    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client("test-reader")
    app.state.knowledge_mesh = KnowledgeMesh(FakeGraphGateway())
    try:
        full = client.get("/api/mesh/graph")
        capped = client.get("/api/mesh/graph?limit=2")
        invalid = client.get("/api/mesh/graph?limit=0")
        unauthenticated = TestClient(app, base_url="https://testserver").get("/api/mesh/graph")
    finally:
        app.state.knowledge_mesh = None

    def content_nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [node for node in payload["nodes"] if node["type"] != "dataset"]

    assert full.status_code == 200
    assert full.json()["fallback"] is False
    assert [node["id"] for node in content_nodes(full.json())] == ["n1", "n2", "n3"]
    # Universal presence (ADR-0009): the Central hub rides along for every
    # caller, independent of content.
    assert any(node["id"] == "dataset:masumi-network" for node in full.json()["nodes"])
    assert {
        "source": "n2",
        "target": "n3",
        "relationship": "embeds",
    } in full.json()["edges"]
    assert capped.status_code == 200
    assert capped.json()["truncated"] is True
    assert len(content_nodes(capped.json())) == 2
    assert capped.json()["limit"] == 2
    assert invalid.status_code == 422
    assert unauthenticated.status_code == 401


def test_mesh_graph_hides_seat_attribution_from_plain_readers(tmp_path: Any) -> None:
    # Seat datasets are default-deny private memory (enforce_dataset_allowlist):
    # graph attribution must not let any kb:search reader durably enumerate
    # which seat contributed which document. Non-seat datasets stay open for
    # unscoped tokens, mirroring the allowlist rules exactly.
    class FakeDatasetGateway:
        async def graph_data(self) -> tuple[list[Any], list[Any]]:
            return (
                [
                    ("doc-1", {"name": "Seat doc", "type": "TextDocument"}),
                    ("doc-2", {"name": "Org doc", "type": "TextDocument"}),
                ],
                [],
            )

        async def node_dataset_map(self) -> dict[str, list[str]]:
            return {"doc-1": ["seat:alice"], "doc-2": ["masumi-network"]}

    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    app.state.knowledge_mesh = KnowledgeMesh(FakeDatasetGateway())
    token = admin.post(
        "/api/access/tokens",
        json={"name": "plain-reader", "role": "reader", "kind": "service_account"},
    ).json()["token"]
    try:
        admin_view = admin.get("/api/mesh/graph").json()
        reader_view = (
            TestClient(app, base_url="https://testserver")
            .get("/api/mesh/graph", headers={"Authorization": f"Bearer {token}"})
            .json()
        )
    finally:
        app.state.knowledge_mesh = None

    # Bypassing callers (admin) keep full attribution, seat hubs included.
    assert any(node["id"] == "dataset:seat:alice" for node in admin_view["nodes"])
    # Plain readers: the seat name appears nowhere in the payload...
    assert "seat:alice" not in json.dumps(reader_view)
    # ...but non-seat attribution is retained.
    assert any(node["id"] == "dataset:masumi-network" for node in reader_view["nodes"])
    doc = next(node for node in reader_view["nodes"] if node["id"] == "doc-2")
    assert doc["dataset"] == "masumi-network"


def test_mesh_projection_hides_seat_content_from_plain_readers(tmp_path: Any) -> None:
    # ADR-0009 blocker: /api/mesh (its SSE twin /events, and the citadel_get_mesh
    # MCP tool that proxies it) is a runtime-activity projection that recorded
    # each seat's document first line and raw search-query text as node labels
    # keyed by dataset. A plain reader/agent token must not receive another
    # seat's content; the seat's presence (dataset hub) stays universal; bypass
    # callers (admin) still see everything.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    alice_token = admin.post(
        "/api/access/seats",
        json={"name": "Alice Example", "slug": "alice", "email": "alice@example.com"},
    ).json()["token"]
    reader_token = admin.post(
        "/api/access/tokens",
        json={"name": "plain-reader", "role": "reader", "kind": "service_account"},
    ).json()["token"]

    config = server_module.get_citadel().config
    mesh = server_module.get_mesh()
    secret = "SECRET_ACQUISITION_TARGET is BetaCorp at $50M valuation"
    org_doc = "Public roadmap milestone for the org"
    secret_query = "alice private acquisition query"

    async def _populate() -> None:
        await mesh.record_ingest(
            config,
            IngestResult(accepted=True, reason="stored", dataset="seat:alice", tags=()),
            data=secret,
            dataset="seat:alice",
            tags=[],
        )
        await mesh.record_search(
            config, query=secret_query, dataset="seat:alice", result_count=1
        )
        await mesh.record_ingest(
            config,
            IngestResult(
                accepted=True, reason="stored", dataset="masumi-network", tags=()
            ),
            data=org_doc,
            dataset="masumi-network",
            tags=[],
        )

    asyncio.run(_populate())

    api = TestClient(app, base_url="https://testserver")
    reader_view = api.get(
        "/api/mesh", headers={"Authorization": f"Bearer {reader_token}"}
    ).json()
    alice_view = api.get(
        "/api/mesh", headers={"Authorization": f"Bearer {alice_token}"}
    ).json()
    admin_view = admin.get("/api/mesh").json()

    reader_blob = json.dumps(reader_view)
    # The leak: neither the seat's document first line nor its query text may
    # reach a plain reader.
    assert secret not in reader_blob
    assert secret_query not in reader_blob
    # Org (non-seat) content stays visible to every reader.
    assert org_doc in reader_blob
    # Seat presence stays universal: the seat's dataset hub is still there.
    assert any(
        node.get("metadata", {}).get("dataset") == "seat:alice"
        and node["type"] == "dataset"
        for node in reader_view["nodes"]
    )
    # No content-bearing seat node survives for the reader.
    assert not any(
        node.get("metadata", {}).get("dataset") == "seat:alice"
        and node["type"] != "dataset"
        for node in reader_view["nodes"]
    )
    # Owner and admin both retain their own/all content.
    assert secret in json.dumps(alice_view)
    assert secret in json.dumps(admin_view)


class IsolationDatasetGateway:
    """Three docs across two seats and Central, with a controllable map."""

    def __init__(self, *, map_error: bool = False, empty_map: bool = False) -> None:
        self.map_error = map_error
        self.empty_map = empty_map

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        return (
            [
                ("doc-a", {"name": "Alice doc", "type": "TextDocument"}),
                ("doc-b", {"name": "Bob doc", "type": "TextDocument"}),
                ("doc-c", {"name": "Org doc", "type": "TextDocument"}),
            ],
            [],
        )

    async def node_dataset_map(self) -> dict[str, list[str]]:
        if self.map_error:
            raise RuntimeError("relational store offline")
        if self.empty_map:
            return {}
        return {
            "doc-a": ["seat:alice"],
            "doc-b": ["seat:bob"],
            "doc-c": ["masumi-network"],
        }


def test_mesh_graph_isolates_content_per_caller_but_presence_is_universal(
    tmp_path: Any,
) -> None:
    # ADR-0009: content follows the caller's search scope (own Node + Central),
    # while every seat always appears as a presence hub — slug only.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    admin.post(
        "/api/access/seats",
        json={"name": "Alice Example", "slug": "alice", "email": "alice@example.com"},
    )
    bob_token = admin.post(
        "/api/access/seats",
        json={"name": "Bob Example", "slug": "bob", "email": "bob@example.com"},
    ).json()["token"]
    reader_token = admin.post(
        "/api/access/tokens",
        json={"name": "plain-reader", "role": "reader", "kind": "service_account"},
    ).json()["token"]
    app.state.knowledge_mesh = KnowledgeMesh(IsolationDatasetGateway())
    api = TestClient(app, base_url="https://testserver")
    try:
        reader_view = api.get(
            "/api/mesh/graph", headers={"Authorization": f"Bearer {reader_token}"}
        ).json()
        bob_view = api.get(
            "/api/mesh/graph", headers={"Authorization": f"Bearer {bob_token}"}
        ).json()
        admin_view = admin.get("/api/mesh/graph").json()
    finally:
        app.state.knowledge_mesh = None

    def content_ids(view: dict[str, Any]) -> set[str]:
        return {node["id"] for node in view["nodes"] if node["type"] != "dataset"}

    def hub_ids(view: dict[str, Any]) -> set[str]:
        return {node["id"] for node in view["nodes"] if node["type"] == "dataset"}

    all_hubs = {"dataset:masumi-network", "dataset:seat:alice", "dataset:seat:bob"}

    # Plain reader: Central content only, yet every seat hub is present.
    assert content_ids(reader_view) == {"doc-c"}
    assert hub_ids(reader_view) == all_hubs
    assert reader_view["visible_nodes"] == 1
    assert reader_view["total_nodes"] == 3

    # Seat holder: own Node + Central, never the other seat's content.
    assert content_ids(bob_view) == {"doc-b", "doc-c"}
    assert hub_ids(bob_view) == all_hubs
    assert bob_view["visible_nodes"] == 2

    # Presence metadata (contribution counts) is visible to scoped callers.
    alice_hub = next(n for n in bob_view["nodes"] if n["id"] == "dataset:seat:alice")
    assert alice_hub["presence"] == {"documents": 1}
    # Seat hubs anchor to the Central hub instead of floating.
    assert {
        "source": "dataset:seat:bob",
        "target": "dataset:masumi-network",
        "relationship": "presence",
    } in reader_view["edges"]

    # Admin bypass: all content, all hubs, no caller-scope field.
    assert content_ids(admin_view) == {"doc-a", "doc-b", "doc-c"}
    assert hub_ids(admin_view) == all_hubs
    assert "visible_nodes" not in admin_view

    # Presence never carries member names or emails (ADR-0009).
    for view in (reader_view, bob_view, admin_view):
        payload = json.dumps(view)
        assert "alice@example.com" not in payload
        assert "bob@example.com" not in payload
        assert "Alice Example" not in payload
        assert "Bob Example" not in payload
        assert "@" not in payload


def test_mesh_graph_map_failure_fails_closed_for_scoped_callers(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    app.state.knowledge_mesh = KnowledgeMesh(IsolationDatasetGateway(map_error=True))
    try:
        bob_view = (
            TestClient(app, base_url="https://testserver")
            .get("/api/mesh/graph", headers={"Authorization": f"Bearer {bob_token}"})
            .json()
        )
        admin_view = admin.get("/api/mesh/graph").json()
    finally:
        app.state.knowledge_mesh = None

    # Without attribution nothing is provably in scope: content is withheld,
    # presence hubs still render the org.
    assert [n for n in bob_view["nodes"] if n["type"] != "dataset"] == []
    assert {n["id"] for n in bob_view["nodes"] if n["type"] == "dataset"} == {
        "dataset:masumi-network",
        "dataset:seat:bob",
    }
    assert bob_view["visible_nodes"] == 0
    # Bypass callers keep the unfiltered graph even when attribution fails.
    assert {n["id"] for n in admin_view["nodes"] if n["type"] != "dataset"} == {
        "doc-a",
        "doc-b",
        "doc-c",
    }


class DrilldownIsolationCitadel(FakeCitadel):
    cognee_documents: dict[str, dict[str, Any]] = {
        "doc-a": {
            "id": "doc-a",
            "source_type": "cognee",
            "title": "Alice doc",
            "body": "alice text",
            "metadata": {},
            "dataset_node_ids": ["doc-a"],
        },
        "doc-b": {
            "id": "doc-b",
            "source_type": "cognee",
            "title": "Bob doc",
            "body": "bob text",
            "metadata": {},
            "dataset_node_ids": ["doc-b"],
        },
        # A chunk id resolves through its is_part_of-linked document's map entry.
        "chunk-b": {
            "id": "chunk-b",
            "source_type": "cognee",
            "title": None,
            "body": "bob chunk text",
            "metadata": {},
            "dataset_node_ids": ["chunk-b", "doc-b"],
        },
        "doc-c": {
            "id": "doc-c",
            "source_type": "cognee",
            "title": "Org doc",
            "body": "org text",
            "metadata": {},
            "dataset_node_ids": ["doc-c"],
        },
    }

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        return self.cognee_documents.get(document_id)


def test_document_drilldown_enforces_read_scope_without_existence_oracle(
    tmp_path: Any,
) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    app.state.citadel = DrilldownIsolationCitadel()  # authed_client resets citadel
    app.state.knowledge_mesh = KnowledgeMesh(IsolationDatasetGateway())
    api = TestClient(app, base_url="https://testserver")
    bob = {"Authorization": f"Bearer {bob_token}"}
    try:
        missing = api.get("/api/documents/does-not-exist", headers=bob)
        foreign = api.get("/api/documents/doc-a", headers=bob)
        own = api.get("/api/documents/doc-b", headers=bob)
        own_chunk = api.get("/api/documents/chunk-b", headers=bob)
        central = api.get("/api/documents/doc-c", headers=bob)
        admin_foreign = admin.get("/api/documents/doc-a")
    finally:
        app.state.knowledge_mesh = None

    # A foreign seat's document is byte-identical to a nonexistent id: same
    # status, same body — no existence oracle.
    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert foreign.content == missing.content

    # Search drill-down (#28) keeps working for the caller's own Node and
    # Central, chunk ids included.
    assert own.status_code == 200
    assert own.json()["document"]["body"] == "bob text"
    assert own_chunk.status_code == 200
    assert own_chunk.json()["document"]["body"] == "bob chunk text"
    assert central.status_code == 200
    # Internal attribution plumbing never leaks into the response.
    assert "dataset_node_ids" not in own.json()["document"]

    # Admin bypass: support/audit access to any seat's document.
    assert admin_foreign.status_code == 200
    assert admin_foreign.json()["document"]["body"] == "alice text"
    assert "dataset_node_ids" not in admin_foreign.json()["document"]


def test_document_drilldown_map_failure_fails_closed_for_scoped_callers(
    tmp_path: Any,
) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    app.state.citadel = DrilldownIsolationCitadel()
    api = TestClient(app, base_url="https://testserver")
    bob = {"Authorization": f"Bearer {bob_token}"}

    for gateway in (
        IsolationDatasetGateway(map_error=True),
        IsolationDatasetGateway(empty_map=True),
    ):
        app.state.knowledge_mesh = KnowledgeMesh(gateway)
        try:
            own = api.get("/api/documents/doc-b", headers=bob)
            admin_doc = admin.get("/api/documents/doc-a")
        finally:
            app.state.knowledge_mesh = None

        # Even the caller's own document denies when attribution cannot be
        # resolved (fail-closed) — while bypass callers are unaffected.
        assert own.status_code == 404
        assert own.json()["detail"] == "Document not found."
        assert admin_doc.status_code == 200


def test_document_drilldown_denies_when_gateway_lacks_node_dataset_map(
    tmp_path: Any,
) -> None:
    # A gateway that exposes no node_dataset_map must fail closed for scoped
    # callers (any future gateway MUST expose it), while admins bypass.
    class _NoMapGateway:
        async def graph_data(self) -> tuple[list[Any], list[Any]]:
            return ([], [])

    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    app.state.citadel = DrilldownIsolationCitadel()
    app.state.knowledge_mesh = KnowledgeMesh(_NoMapGateway())
    api = TestClient(app, base_url="https://testserver")
    try:
        own = api.get(
            "/api/documents/doc-b", headers={"Authorization": f"Bearer {bob_token}"}
        )
        admin_doc = admin.get("/api/documents/doc-a")
    finally:
        app.state.knowledge_mesh = None

    assert own.status_code == 404
    assert own.json()["detail"] == "Document not found."
    assert admin_doc.status_code == 200


class DrilldownSearchCitadel(DrilldownIsolationCitadel):
    """Search returns native cognee ids whose drill-down status varies by caller:
    a Central doc (readable), a foreign seat's doc (404 for a scoped reader), a
    CHUNKS hit on the caller's own doc (whose datasets live on its is_part_of
    parent, not the chunk node), and a textless entity (404 for anyone).
    ``get_document`` (inherited) resolves the docs+chunk and returns None for the
    entity, so /api/documents status is real."""

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        # Same set for every queried dataset; /search dedups by text.
        return [
            {"id": "doc-c", "text": "org text"},
            {"id": "doc-a", "text": "alice text"},
            {"id": "chunk-b", "text": "bob chunk text"},
            {"id": "entity-x", "text": "an entity"},
        ]


def _search_hint(body: dict[str, Any]) -> dict[str, bool]:
    return {
        hit["_citadel"]["result_id"]: hit["_citadel"]["retrieval"][
            "document_drilldown_available"
        ]
        for hit in body["results"]
    }


def test_search_drilldown_hint_matches_document_endpoint_per_caller(
    tmp_path: Any,
) -> None:
    # ADR-0009: document_drilldown_available is TRUE only when /api/documents
    # would return 200 for THIS caller — asserted against the actual endpoint
    # status for the same id+caller (the whole point of the honest hint).
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    app.state.citadel = DrilldownSearchCitadel()  # authed_client resets citadel
    app.state.knowledge_mesh = KnowledgeMesh(IsolationDatasetGateway())
    api = TestClient(app, base_url="https://testserver")
    bob = {"Authorization": f"Bearer {bob_token}"}
    ids = ("doc-c", "doc-a", "chunk-b", "entity-x")
    try:
        bob_body = api.post("/search", json={"query": "x"}, headers=bob).json()
        admin_body = admin.post("/search", json={"query": "x"}).json()
        bob_status = {
            doc: api.get(f"/api/documents/{doc}", headers=bob).status_code
            for doc in ids
        }
        # Admin's textless entity 404s too (get_document is None), which the
        # bypass hint cannot cheaply foresee; assert consistency on resolvable ids.
        admin_status = {
            doc: admin.get(f"/api/documents/{doc}").status_code
            for doc in ("doc-c", "doc-a", "chunk-b")
        }
    finally:
        app.state.knowledge_mesh = None

    bob_hint = _search_hint(bob_body)
    admin_hint = _search_hint(admin_body)
    bob_meta = {h["_citadel"]["result_id"]: h["_citadel"] for h in bob_body["results"]}

    # Scoped reader: Central doc AND own-doc chunk drillable (the chunk resolves
    # through its is_part_of parent doc-b -> seat:bob); foreign-seat doc and
    # textless entity not. The chunk case is exactly what a raw-id map lookup got
    # wrong: hint False while the endpoint served 200.
    assert bob_hint == {
        "doc-c": True,
        "doc-a": False,
        "chunk-b": True,
        "entity-x": False,
    }
    assert bob_status == {"doc-c": 200, "doc-a": 404, "chunk-b": 200, "entity-x": 404}
    # The flag never disagrees with the endpoint it points at.
    for doc in ids:
        assert bob_hint[doc] is (bob_status[doc] == 200)
    # An unavailable hint also withholds the URL — no agent is handed a 404 link.
    assert "document_endpoint" in bob_meta["doc-c"]
    assert "document_endpoint" in bob_meta["chunk-b"]
    assert "document_endpoint" not in bob_meta["doc-a"]
    assert "document_endpoint" not in bob_meta["entity-x"]

    # Admin bypass: every resolvable id is drillable, consistent with the endpoint.
    assert admin_hint == {
        "doc-c": True,
        "doc-a": True,
        "chunk-b": True,
        "entity-x": True,
    }
    assert admin_status == {"doc-c": 200, "doc-a": 200, "chunk-b": 200}


def test_search_drilldown_hint_fails_closed_on_cold_map(tmp_path: Any) -> None:
    # A cold/empty node_dataset_map denies the hint for scoped callers (better a
    # false "unavailable" than a promised 404) while admin bypass is unaffected —
    # matching the /api/documents fail-closed behavior for the same ids.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    app.state.citadel = DrilldownSearchCitadel()
    app.state.knowledge_mesh = KnowledgeMesh(IsolationDatasetGateway(empty_map=True))
    api = TestClient(app, base_url="https://testserver")
    bob = {"Authorization": f"Bearer {bob_token}"}
    try:
        bob_body = api.post("/search", json={"query": "x"}, headers=bob).json()
        admin_body = admin.post("/search", json={"query": "x"}).json()
        # Even the Central doc the reader normally reads 404s under a cold map.
        bob_central = api.get("/api/documents/doc-c", headers=bob).status_code
        admin_central = admin.get("/api/documents/doc-c").status_code
    finally:
        app.state.knowledge_mesh = None

    assert _search_hint(bob_body) == {
        "doc-c": False,
        "doc-a": False,
        "chunk-b": False,
        "entity-x": False,
    }
    assert bob_central == 404  # hint False is consistent with the endpoint
    assert _search_hint(admin_body) == {
        "doc-c": True,
        "doc-a": True,
        "chunk-b": True,
        "entity-x": True,
    }
    assert admin_central == 200


class KnowledgeCitadel(FakeCitadel):
    async def search(self, query: str, **kwargs: Any) -> list[Any]:
        return [
            {
                "text": "Rotate keys quarterly",
                "source_url": "https://example.com/runbook",
                "score": 0.92,
                "metadata": {"citadel_tags": ["ops"]},
            },
            "bare string result",
        ]


def test_repo_content_sync_status_and_run(tmp_path: Any) -> None:
    class FakeRepoContentSyncer:
        async def status(self) -> dict[str, Any]:
            return {
                "ok": True,
                "enabled": True,
                "source_type": "github_repo_content",
                "tracked_files": 14,
            }

        async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
            return {
                "ok": True,
                "enabled": True,
                "files_ingested": 2 if not dry_run else 0,
                "files_skipped": 1,
                "dry_run": dry_run,
                "improved": not dry_run,
            }

    app.state.repo_content_syncer = FakeRepoContentSyncer()
    reader = authed_client("test-reader")
    admin = authed_client("test-admin")

    status = reader.get("/api/repo-content-sync")
    assert status.status_code == 200
    assert status.json()["source_type"] == "github_repo_content"

    denied = reader.post("/api/repo-content-sync/run", json={"dry_run": True})
    assert denied.status_code == 403

    run = admin.post("/api/repo-content-sync/run", json={"force": True, "dry_run": True})
    assert run.status_code == 200
    assert run.json()["dry_run"] is True


def test_recent_contributions_lists_audit_events(tmp_path: Any) -> None:
    store = AccessStore(str(tmp_path / "access.json"))
    app.state.access_store = store
    writer = authed_client("test-writer")

    response = writer.post(
        "/api/contribute",
        json={"title": "WIP: MCP docs", "content": "OAuth uses hosted endpoint.", "tags": ["wip"]},
    )
    assert response.status_code == 200

    recent = writer.get("/api/contributions/recent?limit=5")
    assert recent.status_code == 200
    payload = recent.json()
    assert payload["ok"] is True
    assert len(payload["contributions"]) == 1
    assert payload["contributions"][0]["action"] == "contribute"
    assert payload["contributions"][0]["detail"]["title"] == "WIP: MCP docs"


def test_contribute_routes_through_learning_process_and_audits(tmp_path: Any) -> None:
    client = authed_client("test-writer")
    store = AccessStore(str(tmp_path / "access.json"))
    app.state.access_store = store

    response = client.post(
        "/api/contribute",
        json={
            "title": "Decision: adopt deepseek for enrichment",
            "content": "We standardized on deepseek/deepseek-v4-flash via OpenRouter.",
            "tags": ["decision", "llm"],
            "source_url": "https://github.com/masumi-network/Citadel-Archive",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["accepted"] is True
    assert payload["chunks"] == 1
    assert payload["conflict"] is None
    assert payload["dataset"] == "masumi-network"

    events = store.snapshot()["audit_events"]
    contribute_events = [event for event in events if event["action"] == "contribute"]
    assert len(contribute_events) == 1
    assert contribute_events[0]["success"] is True
    assert contribute_events[0]["detail"]["chunks"] == 1
    assert contribute_events[0]["detail"]["tag_count"] == 4
    assert contribute_events[0]["detail"]["tags"] == [
        "decision",
        "llm",
        "vault-contribution",
        "author:writer-bootstrap-key",
    ]
    # Raw contribution content never lands in the audit trail.
    assert "deepseek-v4-flash" not in json.dumps(contribute_events[0])


def test_contribute_requires_writer_role() -> None:
    reader = authed_client("test-reader")
    body = {"title": "Note", "content": "Body"}

    assert reader.post("/api/contribute", json=body).status_code == 403
    assert (
        TestClient(app, base_url="https://testserver")
        .post("/api/contribute", json=body)
        .status_code
        == 401
    )


def test_contribute_validates_payload() -> None:
    client = authed_client("test-writer")

    assert client.post("/api/contribute", json={"title": "", "content": "x"}).status_code == 422
    assert client.post("/api/contribute", json={"content": "x"}).status_code == 422
    assert client.post("/api/contribute", json={"title": "t"}).status_code == 422


def test_knowledge_alias_returns_flat_agent_friendly_results() -> None:
    client = authed_client("test-reader")
    app.state.citadel = KnowledgeCitadel()

    response = client.get("/api/knowledge", params={"q": "rotate keys", "limit": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["query"] == "rotate keys"
    assert payload["results"][0] == {
        "text": "Rotate keys quarterly",
        "source": "https://example.com/runbook",
        "score": 0.92,
        "tags": ["ops"],
    }
    assert payload["results"][1]["text"] == "bare string result"
    assert payload["results"][1]["source"] is None


def test_knowledge_alias_validates_query_and_limit() -> None:
    client = authed_client("test-reader")

    assert client.get("/api/knowledge", params={"q": "   "}).status_code == 422
    assert client.get("/api/knowledge", params={"q": "x", "limit": 0}).status_code == 422
    assert client.get("/api/knowledge", params={"q": "x", "limit": 999}).status_code == 422
    assert (
        TestClient(app, base_url="https://testserver")
        .get("/api/knowledge", params={"q": "x"})
        .status_code
        == 401
    )


def test_optimize_endpoint_is_admin_only_bounded_and_audited(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    writer = authed_client("test-writer")
    assert writer.post("/api/learning-agent/optimize", json={}).status_code == 403

    client = authed_client()
    store = AccessStore(str(tmp_path / "access.json"))
    app.state.access_store = store

    response = client.post(
        "/api/learning-agent/optimize",
        json={"dry_run": True, "max_items": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["max_items"] == 5
    assert payload["optimized"] == 0  # no LLM key -> deterministic no-op fallback
    assert payload["llm_used"] is False

    events = store.snapshot()["audit_events"]
    optimize_events = [
        event for event in events if event["action"] == "learning_agent.optimize"
    ]
    assert len(optimize_events) == 1
    assert optimize_events[0]["success"] is True
    assert optimize_events[0]["detail"]["dry_run"] is True


class MultiSearchCitadel(FakeCitadel):
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.session_calls: dict[str, str | None] = {}
        self.cognee = type(
            "_Cognee",
            (),
            {"scheduled": [], "schedule_cognify": lambda self, datasets: self.scheduled.append(list(datasets))},
        )()

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        dataset = kwargs["dataset"]
        self.search_calls.append(dataset)
        self.session_calls[dataset] = kwargs.get("session_id")
        return [
            {
                "query": query,
                "dataset": dataset,
                "text": f"{query} in {dataset}",
                "top_k": kwargs["top_k"],
            }
        ]


class TrackingCitadel(FakeCitadel):
    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        dataset = kwargs.get("dataset") or "notes"
        return IngestResult(True, "accepted", dataset, tuple(kwargs.get("tags") or ()))


def test_create_seat_api_provisions_node_and_writer_token(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    response = client.post(
        "/api/access/seats",
        json={"name": "Alice Smith", "slug": "alice", "email": "alice@example.com"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["principal"]["seat_slug"] == "alice"
    assert payload["principal"]["default_dataset"] == "seat:alice"
    assert payload["principal"]["default_session"] == "seat-alice"
    assert payload["api_token"]["default_dataset"] == "seat:alice"
    assert payload["api_token"]["allowed_datasets"] == [
        "seat:alice",
        "masumi-network",
        SESSION_TRACES_DATASET,
    ]
    assert payload["token"].startswith("ctdl_")

    duplicate = client.post(
        "/api/access/seats",
        json={"name": "Alice Two", "slug": "alice"},
    )
    assert duplicate.status_code == 422
    assert "already exists" in duplicate.json()["detail"]

    invalid = client.post(
        "/api/access/seats",
        json={"name": "Bad", "slug": "Bad Slug"},
    )
    assert invalid.status_code == 422


def test_seat_token_searches_node_and_central(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    created = admin.post(
        "/api/access/seats",
        json={"name": "Bob", "slug": "bob"},
    )
    token = created.json()["token"]
    app.state.citadel = MultiSearchCitadel()
    api_client = TestClient(app, base_url="https://testserver")

    search = api_client.post(
        "/search",
        json={"query": "architecture"},
        headers={"Authorization": f"Bearer {token}"},
    )
    knowledge = api_client.get(
        "/api/knowledge",
        params={"q": "architecture", "limit": 5},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert search.status_code == 200
    payload = search.json()
    assert payload["dataset"] == "seat:bob"
    assert payload["datasets"] == ["seat:bob", "masumi-network", SESSION_TRACES_DATASET]
    assert len(payload["results"]) == 3
    assert payload["results"][0]["dataset"] == "seat:bob"
    assert payload["results"][1]["dataset"] == "masumi-network"
    assert payload["results"][2]["dataset"] == SESSION_TRACES_DATASET
    assert set(app.state.citadel.search_calls) == {
        "seat:bob",
        "masumi-network",
        SESSION_TRACES_DATASET,
    }
    # The seat session scopes the private node only; Central must stay
    # dataset-wide (session_id None) or org-wide hits get hidden.
    assert app.state.citadel.session_calls == {
        "seat:bob": "seat-bob",
        "masumi-network": None,
        SESSION_TRACES_DATASET: None,
    }

    assert knowledge.status_code == 200
    assert knowledge.json()["datasets"] == [
        "seat:bob",
        "masumi-network",
        SESSION_TRACES_DATASET,
    ]
    assert "sections" in payload
    assert payload["sections"]["session_traces"]


def test_seat_cannot_recall_another_seats_session(tmp_path: Any) -> None:
    # Session-scoped recall ignores the dataset allowlist, and seat sessions are
    # derived from a guessable slug, so a seat naming another seat's session would
    # read its private node. A non-bypass caller may only name its own session.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"})
    token = admin.post("/api/access/seats", json={"name": "Bob", "slug": "bob"}).json()["token"]
    app.state.citadel = MultiSearchCitadel()
    api_client = TestClient(app, base_url="https://testserver")

    foreign = api_client.post(
        "/search",
        json={"query": "secrets", "session_id": "seat-alice"},
        headers={"Authorization": f"Bearer {token}"},
    )
    own = api_client.post(
        "/search",
        json={"query": "notes", "session_id": "seat-bob"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert foreign.status_code == 403
    assert foreign.json()["detail"] == "Session not allowed."
    assert own.status_code == 200
    # Even an explicit own session scopes the node only; Central stays wide.
    assert app.state.citadel.session_calls == {
        "seat:bob": "seat-bob",
        "masumi-network": None,
        SESSION_TRACES_DATASET: None,
    }
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post(
        "/api/access/tokens",
        json={"name": "ops", "role": "admin", "kind": "service_account"},
    ).json()["token"]
    app.state.citadel = MultiSearchCitadel()
    api_client = TestClient(app, base_url="https://testserver")

    search = api_client.post(
        "/search",
        json={"query": "anything", "dataset": "masumi-network", "session_id": "masumi-github-daily"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert search.status_code == 200
    assert app.state.citadel.session_calls == {"masumi-network": "masumi-github-daily"}


def test_seat_search_deduplicates_preferring_node(tmp_path: Any) -> None:
    class DuplicateCitadel(FakeCitadel):
        async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"text": "shared hit", "dataset": kwargs["dataset"]}]

    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Carol", "slug": "carol"}).json()["token"]
    app.state.citadel = DuplicateCitadel()
    api_client = TestClient(app, base_url="https://testserver")

    search = api_client.post(
        "/search",
        json={"query": "shared", "top_k": 5},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert search.status_code == 200
    assert len(search.json()["results"]) == 1
    assert search.json()["results"][0]["dataset"] == "seat:carol"


def test_seat_ingest_defaults_to_node_light_path(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Dana", "slug": "dana"}).json()["token"]
    tracking = TrackingCitadel()
    app.state.citadel = tracking
    app.state.mesh = MeshState()
    api_client = TestClient(app, base_url="https://testserver")

    response = api_client.post(
        "/ingest",
        json={"data": "Working memory note"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["dataset"] == "seat:dana"
    assert len(tracking.ingest_calls) == 1
    assert tracking.ingest_calls[0]["dataset"] == "seat:dana"


def test_seat_org_tag_ingest_stays_on_node(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Eve", "slug": "eve"}).json()["token"]
    tracking = TrackingCitadel()
    app.state.citadel = tracking
    app.state.mesh = MeshState()
    api_client = TestClient(app, base_url="https://testserver")

    tagged = api_client.post(
        "/ingest",
        json={"data": "Org policy", "tags": ["repo-content"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert tagged.status_code == 403
    assert "personal node" in tagged.json()["detail"].lower()
    assert tracking.ingest_calls == []


def test_promotion_tag_dual_writes_node_and_central(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Frank", "slug": "frank"}).json()["token"]
    tracking = TrackingCitadel()
    app.state.citadel = tracking
    app.state.mesh = MeshState()
    api_client = TestClient(app, base_url="https://testserver")

    response = api_client.post(
        "/ingest",
        json={"data": "Curated share", "tags": ["org-ready"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert "promotion" in response.json()["detail"].lower()
    assert tracking.ingest_calls == []


def test_seat_token_cannot_reach_another_seat_node(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    admin.post("/api/access/seats", json={"name": "Grace", "slug": "grace"})
    token = admin.post("/api/access/seats", json={"name": "Heidi", "slug": "heidi"}).json()["token"]
    app.state.citadel = TrackingCitadel()
    app.state.mesh = MeshState()
    api_client = TestClient(app, base_url="https://testserver")

    read = api_client.post(
        "/search",
        json={"query": "secrets", "dataset": "seat:grace"},
        headers={"Authorization": f"Bearer {token}"},
    )
    write = api_client.post(
        "/ingest",
        json={"data": "cross-seat write", "dataset": "seat:grace"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert read.status_code == 403
    assert read.json()["detail"] == "Dataset not allowed: seat:grace."
    assert write.status_code == 403
    assert "seat:heidi" in write.json()["detail"]
    assert app.state.citadel.ingest_calls == []


def test_unscoped_token_cannot_reach_a_seat_node(tmp_path: Any) -> None:
    # A writer token with no allowlist stays open for ordinary datasets but must
    # never be able to name a private seat node and reach it.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    admin.post("/api/access/seats", json={"name": "Ivan", "slug": "ivan"})
    token = admin.post(
        "/api/access/tokens",
        json={"name": "open-writer", "role": "writer", "kind": "service_account"},
    ).json()["token"]
    app.state.citadel = TrackingCitadel()
    app.state.mesh = MeshState()
    api_client = TestClient(app, base_url="https://testserver")

    seat_write = api_client.post(
        "/ingest",
        json={"data": "poke", "dataset": "seat:ivan"},
        headers={"Authorization": f"Bearer {token}"},
    )
    ordinary_write = api_client.post(
        "/ingest",
        json={"data": "fine", "dataset": "team-notes"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert seat_write.status_code == 403
    assert seat_write.json()["detail"] == "Dataset not allowed: seat:ivan."
    assert ordinary_write.status_code == 200
    assert ordinary_write.json()["dataset"] == "team-notes"


def test_seat_direct_central_write_blocked(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Karl", "slug": "karl"}).json()["token"]
    tracking = TrackingCitadel()
    app.state.citadel = tracking
    app.state.mesh = MeshState()
    api_client = TestClient(app, base_url="https://testserver")

    untagged = api_client.post(
        "/ingest",
        json={"data": "raw drop", "dataset": "masumi-network"},
        headers={"Authorization": f"Bearer {token}"},
    )
    tagged = api_client.post(
        "/ingest",
        json={"data": "curated", "dataset": "masumi-network", "tags": ["org-ready"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    node_default = api_client.post(
        "/ingest",
        json={"data": "working note"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert untagged.status_code == 403
    assert tagged.status_code == 403
    assert node_default.status_code == 200
    assert node_default.json()["dataset"] == "seat:karl"
    assert [call["dataset"] for call in tracking.ingest_calls] == ["seat:karl"]


def test_seat_token_defaulting_to_central_still_gated(tmp_path: Any) -> None:
    # A seat-scoped token whose default_dataset is Central must not slip the
    # curation gate: the seat node in its allowlist is the authoritative signal,
    # so raw drops into Central (explicit or via the default target) are blocked.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post(
        "/api/access/tokens",
        json={
            "name": "seat-defaulting-central",
            "role": "writer",
            "kind": "user",
            "default_dataset": "masumi-network",
            "allowed_datasets": ["seat:leo", "masumi-network"],
        },
    ).json()["token"]
    tracking = TrackingCitadel()
    app.state.citadel = tracking
    app.state.mesh = MeshState()
    api_client = TestClient(app, base_url="https://testserver")

    explicit = api_client.post(
        "/ingest",
        json={"data": "raw drop", "dataset": "masumi-network"},
        headers={"Authorization": f"Bearer {token}"},
    )
    defaulted = api_client.post(
        "/ingest",
        json={"data": "working note"},
        headers={"Authorization": f"Bearer {token}"},
    )
    tagged = api_client.post(
        "/ingest",
        json={"data": "curated", "dataset": "masumi-network", "tags": ["org-ready"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert explicit.status_code == 403
    assert defaulted.status_code == 200
    assert defaulted.json()["dataset"] == "seat:leo"
    assert tagged.status_code == 403
    assert [call["dataset"] for call in tracking.ingest_calls] == ["seat:leo"]


def test_obsidian_push_keeps_seat_org_tagged_docs_on_node(tmp_path: Any) -> None:
    # ADR-0007: seat Obsidian pushes stay on the Node even when tagged org-ready.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    app.state.mesh = MeshState()
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Mia", "slug": "mia"}).json()["token"]
    tracking = TrackingCitadel()
    app.state.citadel = tracking
    api_client = TestClient(app, base_url="https://testserver")
    headers = {"Authorization": f"Bearer {token}"}

    vault_id = api_client.post(
        "/api/obsidian/vaults",
        json={"vault_name": "Mia Vault"},
        headers=headers,
    ).json()["vault"]["id"]
    pushed = api_client.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "tags": ["org-ready"],
            "documents": [{"path": "Notes/Share.md", "content": "Org-ready note."}],
        },
        headers=headers,
    )

    assert pushed.status_code == 200
    datasets = [call["dataset"] for call in tracking.ingest_calls]
    assert datasets == ["seat:mia"]
    assert "org-ready" not in tracking.ingest_calls[0]["tags"]
    events = app.state.access_store.snapshot()["audit_events"]
    push_event = next(e for e in events if e["action"] == "obsidian.sync.push")
    assert push_event["detail"]["written_datasets"] == ["seat:mia"]


def test_create_seat_api_rejects_admin_role(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    response = client.post(
        "/api/access/seats",
        json={"name": "Judy", "slug": "judy", "role": "admin"},
    )

    assert response.status_code == 422
    assert "admin role" in response.json()["detail"]


def test_seat_session_reports_own_seat_slug_and_node(tmp_path: Any) -> None:
    # A seat's self-describing scope must reflect only the authenticated caller:
    # its own seat_slug and a friendly label for its private node dataset.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post(
        "/api/access/seats",
        json={"name": "Nora", "slug": "nora"},
    ).json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    session = api_client.get(
        "/api/session", headers={"Authorization": f"Bearer {token}"}
    )

    assert session.status_code == 200
    payload = session.json()
    assert payload["seat_slug"] == "nora"
    assert payload["default_dataset"] == "seat:nora"
    assert payload["node_label"] == "nora's private Node"
    assert payload["search_datasets"] == [
        "seat:nora",
        "masumi-network",
        SESSION_TRACES_DATASET,
    ]


def test_me_summary_for_seat_reports_node_scope(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post(
        "/api/access/seats",
        json={"name": "Nora", "slug": "nora"},
    ).json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    summary = api_client.get(
        "/api/me/summary", headers={"Authorization": f"Bearer {token}"}
    )

    assert summary.status_code == 200
    payload = summary.json()
    assert payload["ok"] is True
    assert payload["seat_slug"] == "nora"
    assert payload["node_dataset"] == "seat:nora"
    assert payload["node_label"] == "nora's private Node"
    assert payload["document_count"] == 0
    assert payload["pending_promotions"] == 0
    assert payload["empty"] is True
    assert payload["search_datasets"][0] == "seat:nora"
    assert any(item["id"] == "capture" for item in payload["checklist"])


def test_me_summary_uses_audit_when_mesh_empty(tmp_path: Any) -> None:
    # After a process restart the runtime mesh is empty; durable audit must still
    # keep Seat home from claiming an empty Node when ingests are on record.
    # Documents count stays mesh-backed (0 here); empty/checklist use audit presence.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.mesh = MeshState()
    admin = authed_client()
    token = admin.post(
        "/api/access/seats",
        json={"name": "Nora", "slug": "nora"},
    ).json()["token"]
    app.state.access_store.record_event(
        action="ingest",
        actor=AccessIdentity(
            role="writer",
            actor_id="seat:nora",
            actor_kind="user",
            actor_name="Nora",
            source="token",
            seat_slug="nora",
        ),
        success=True,
        dataset="seat:nora",
        detail={"surface": "test"},
    )
    api_client = TestClient(app, base_url="https://testserver")

    summary = api_client.get(
        "/api/me/summary", headers={"Authorization": f"Bearer {token}"}
    )

    assert summary.status_code == 200
    payload = summary.json()
    assert payload["document_count"] == 0
    assert payload["empty"] is False
    assert payload["last_ingest_at"] is not None
    assert payload["recent_activity"]
    assert all(item["dataset"] == "seat:nora" for item in payload["recent_activity"])
    assert any(item["id"] == "capture" and item["done"] for item in payload["checklist"])


def test_me_summary_keeps_node_activity_when_central_is_busy(tmp_path: Any) -> None:
    # Seat home must still surface Node timeline rows when Central/shared traffic
    # fills a mixed visible page — filter to the seat Node before the limit slice.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.mesh = MeshState()
    admin = authed_client()
    token = admin.post(
        "/api/access/seats",
        json={"name": "Nora", "slug": "nora"},
    ).json()["token"]
    config = server_module.get_citadel().config
    mesh = server_module.get_mesh()

    async def _populate() -> None:
        await mesh.record_ingest(
            config,
            IngestResult(accepted=True, reason="stored", dataset="seat:nora", tags=()),
            data="nora private note",
            dataset="seat:nora",
            tags=[],
        )
        for i in range(20):
            await mesh.record_ingest(
                config,
                IngestResult(
                    accepted=True,
                    reason="stored",
                    dataset="masumi-network",
                    tags=(),
                ),
                data=f"central noise {i}",
                dataset="masumi-network",
                tags=[],
            )

    asyncio.run(_populate())
    api_client = TestClient(app, base_url="https://testserver")

    summary = api_client.get(
        "/api/me/summary", headers={"Authorization": f"Bearer {token}"}
    )

    assert summary.status_code == 200
    payload = summary.json()
    assert payload["document_count"] >= 1
    assert payload["recent_activity"], "expected Node activity despite busy Central"
    assert all(item["dataset"] == "seat:nora" for item in payload["recent_activity"])
    assert payload["last_ingest_at"] is not None


def test_me_summary_non_seat_has_null_seat(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    token = client.post(
        "/api/access/tokens",
        json={
            "name": "plain-reader",
            "role": "reader",
            "kind": "service_account",
            "default_dataset": "personal",
        },
    ).json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    summary = api_client.get(
        "/api/me/summary", headers={"Authorization": f"Bearer {token}"}
    )

    assert summary.status_code == 200
    payload = summary.json()
    assert payload["seat_slug"] is None
    assert payload["node_dataset"] is None
    assert payload["checklist"] == []
    assert payload["empty"] is False


def test_non_seat_token_session_nulls_seat_slug(tmp_path: Any) -> None:
    # A plain (non-seat) token carries no seat marker; the self-describing scope
    # must null seat_slug and node_label rather than invent one.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    token = client.post(
        "/api/access/tokens",
        json={
            "name": "plain-reader",
            "role": "reader",
            "kind": "service_account",
            "default_dataset": "personal",
        },
    ).json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    session = api_client.get(
        "/api/session", headers={"Authorization": f"Bearer {token}"}
    )

    assert session.status_code == 200
    payload = session.json()
    assert payload["seat_slug"] is None
    assert payload["node_label"] is None


def test_list_seats_returns_active_seats_with_token_counts(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post(
        "/api/access/seats",
        json={"name": "Olive", "slug": "olive", "email": "olive@example.com"},
    ).json()
    # A non-seat principal/token must never appear in the seat inventory.
    client.post(
        "/api/access/tokens",
        json={"name": "plain-agent", "role": "reader", "kind": "service_account"},
    )

    listing = client.get("/api/access/seats")

    assert listing.status_code == 200
    seats = listing.json()["seats"]
    assert len(seats) == 1
    seat = seats[0]
    assert seat["seat_slug"] == "olive"
    assert seat["node_dataset"] == "seat:olive"
    assert seat["email"] == "olive@example.com"
    assert seat["active_token_count"] == 1
    assert seat["token_count"] == 1
    assert seat["tokens"][0]["id"] == created["api_token"]["id"]
    assert seat["tokens"][0]["prefix"] == created["api_token"]["prefix"]
    assert seat["tokens"][0]["revoked"] is False
    # The seat list is a redacted aggregation: no token hash leaks through.
    assert "token_hash" not in seat["tokens"][0]

    # Revoking the seat's token drops the active count to zero but keeps the seat.
    revoked = client.post(
        f"/api/access/tokens/{created['api_token']['id']}/revoke"
    )
    assert revoked.status_code == 200
    after = client.get("/api/access/seats").json()["seats"]
    assert after[0]["active_token_count"] == 0
    assert after[0]["token_count"] == 1
    assert after[0]["tokens"][0]["revoked"] is True


def test_list_seats_requires_admin(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    writer_token = admin.post(
        "/api/access/tokens",
        json={"name": "writer-agent", "role": "writer", "kind": "service_account"},
    ).json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    forbidden = api_client.get(
        "/api/access/seats", headers={"Authorization": f"Bearer {writer_token}"}
    )

    assert forbidden.status_code == 403


def test_admin_scope_override_is_audited(tmp_path: Any) -> None:
    # An admin-role token that carries its own allowlist but reaches outside it
    # bypasses enforcement by design; the bypass must be flagged in the audit log.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post(
        "/api/access/tokens",
        json={
            "name": "scoped-admin",
            "role": "admin",
            "kind": "service_account",
            "allowed_datasets": ["personal"],
        },
    ).json()["token"]
    app.state.citadel = MultiSearchCitadel()
    api_client = TestClient(app, base_url="https://testserver")

    search = api_client.post(
        "/search",
        json={"query": "anything", "dataset": "masumi-network"},
        headers={
            "Authorization": f"Bearer {token}",
            "x-citadel-mcp-tool": "citadel_search",
        },
    )

    assert search.status_code == 200
    events = app.state.access_store.snapshot()["audit_events"]
    search_events = [event for event in events if event["detail"].get("operation") == "search"]
    assert search_events
    assert search_events[-1]["detail"]["scope_override"] is True


class GateCognee:
    """Minimal Cognee stub so a real Citadel gate runs through the server stack."""

    def __init__(self) -> None:
        self.remembered: list[str] = []

    async def remember(self, data: str, **kwargs: Any) -> dict[str, Any]:
        self.remembered.append(data)
        return {"ok": True}

    async def recall(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def add_feedback(self, **kwargs: Any) -> bool:
        return True

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def cognify(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def graph_data(self) -> tuple[list[Any], list[Any]]:
        return ([], [])


def secret_gate_client(tmp_path: Any) -> tuple[TestClient, GateCognee]:
    """Authed client whose Citadel is real (so the secret gate actually fires)."""
    from kb.service import Citadel

    cognee = GateCognee()
    app.state.citadel = Citadel(
        CitadelConfig(
            tenant_id="test",
            default_dataset="notes",
            admin_key="test-admin",
            writer_keys=("test-writer",),
            content_scan_enabled=True,
        ),
        cognee=cognee,
    )
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.mesh = MeshState()
    app.state.conflict_store = KnowledgeConflictStore(tmp_path / "conflicts.json")
    client = TestClient(app, base_url="https://testserver")
    assert client.post("/admin/session", json={"access_key": "test-admin"}).status_code == 200
    return client, cognee


def test_ingest_blocks_high_severity_secret(tmp_path: Any) -> None:
    client, cognee = secret_gate_client(tmp_path)

    response = client.post(
        "/ingest",
        json={"data": "deploy key AKIAIOSFODNN7EXAMPLE do not share"},
    )

    assert response.status_code == 422
    # The raw secret is never echoed back to the caller.
    assert "AKIAIOSFODNN7EXAMPLE" not in response.text
    # Nothing reached the vault.
    assert cognee.remembered == []
    # The block is audited via the access store.
    events = app.state.access_store.snapshot()["audit_events"]
    blocked = [e for e in events if e["detail"].get("blocked") == "secret_content"]
    assert blocked
    assert blocked[-1]["action"] == "ingest"
    assert blocked[-1]["success"] is False


def test_ingest_allows_clean_content(tmp_path: Any) -> None:
    client, cognee = secret_gate_client(tmp_path)

    response = client.post(
        "/ingest",
        json={"data": "A clean engineering note about cache invalidation strategy."},
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert cognee.remembered  # stored


def test_contribute_blocks_high_severity_secret(tmp_path: Any) -> None:
    client, cognee = secret_gate_client(tmp_path)

    # Build the key at runtime so the literal is not committed (GitHub push
    # protection scans literals); the assembled string still trips the scanner.
    stripe_key = "sk_" + "live_" + "abcdEFGHijklMNOPqrstUVwx"
    response = client.post(
        "/api/contribute",
        json={
            "title": "Onboarding secret",
            "content": f"Use this Stripe key {stripe_key} to test.",
        },
    )

    assert response.status_code == 422
    assert stripe_key not in response.text
    assert cognee.remembered == []
    events = app.state.access_store.snapshot()["audit_events"]
    blocked = [e for e in events if e["detail"].get("blocked") == "secret_content"]
    assert blocked
    assert blocked[-1]["action"] == "contribute"
    assert blocked[-1]["success"] is False


def test_promotion_status_requires_admin_and_reports_config() -> None:
    client = authed_client()

    response = client.get("/api/promote")

    assert response.status_code == 200
    body = response.json()
    # FakeCitadel config leaves promotion opt-in (disabled) by default.
    assert body["enabled"] is False
    assert body["dry_run_default"] is True
    assert body["promotion_tag"] == "org-ready"
    assert body["max_items"] == 20


def test_promotion_run_rejects_non_seat_dataset() -> None:
    client = authed_client()
    app.state.access_store = AccessStore(Path(tempfile.mkdtemp()) / "access.json")

    response = client.post(
        "/api/promote/run",
        json={"dataset": "masumi-network", "dry_run": True},
    )

    assert response.status_code == 400
    assert "seat" in response.json()["detail"].lower()


def test_promotion_run_disabled_returns_status_and_audits() -> None:
    client = authed_client()
    app.state.access_store = AccessStore(Path(tempfile.mkdtemp()) / "access.json")

    response = client.post(
        "/api/promote/run",
        json={"dataset": "seat:alice", "dry_run": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["reason"] == "disabled"
    assert body["promoted"] == 0
    events = app.state.access_store.snapshot()["audit_events"]
    runs = [e for e in events if e["action"] == "promotion.run"]
    assert runs and runs[-1]["success"] is True


def test_seat_writer_can_run_own_promotion(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    created = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"})
    token = created.json()["token"]
    seat_client = TestClient(app, base_url="https://testserver")

    response = seat_client.post(
        "/api/promote/run",
        json={"dataset": "seat:alice", "dry_run": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["dataset"] == "seat:alice"


def test_seat_writer_cannot_run_other_seat_promotion(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"})
    bob = admin.post("/api/access/seats", json={"name": "Bob", "slug": "bob"})
    bob_token = bob.json()["token"]
    bob_client = TestClient(app, base_url="https://testserver")

    response = bob_client.post(
        "/api/promote/run",
        json={"dataset": "seat:alice", "dry_run": True},
        headers={"Authorization": f"Bearer {bob_token}"},
    )

    assert response.status_code == 403


def _seat_mcp_client(tmp_path: Any, slug: str) -> tuple[TestClient, str, TrackingCitadel]:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post(
        "/api/access/seats",
        json={"name": slug.title(), "slug": slug},
    ).json()["token"]
    tracking = TrackingCitadel()
    app.state.citadel = tracking
    app.state.mesh = MeshState()
    return TestClient(app, base_url="https://testserver"), token, tracking


def test_mcp_seat_ingest_blocks_central_dataset(tmp_path: Any) -> None:
    client, token, tracking = _seat_mcp_client(tmp_path, "mcp-alice")

    response = client.post(
        "/ingest",
        json={"data": "try central", "dataset": "masumi-network"},
        headers={
            "Authorization": f"Bearer {token}",
            "x-citadel-mcp-tool": "citadel_ingest",
        },
    )

    assert response.status_code == 403
    assert "personal node" in response.json()["detail"].lower()
    assert tracking.ingest_calls == []


def test_mcp_seat_ingest_blocks_org_tags(tmp_path: Any) -> None:
    client, token, tracking = _seat_mcp_client(tmp_path, "mcp-bob")

    response = client.post(
        "/ingest",
        json={"data": "org note", "tags": ["org-ready"]},
        headers={
            "Authorization": f"Bearer {token}",
            "x-citadel-mcp-tool": "citadel_ingest",
        },
    )

    assert response.status_code == 403
    assert "central" in response.json()["detail"].lower()
    assert tracking.ingest_calls == []


def test_mcp_seat_ingest_allows_personal_node(tmp_path: Any) -> None:
    client, token, tracking = _seat_mcp_client(tmp_path, "mcp-carol")

    response = client.post(
        "/ingest",
        json={"data": "personal note"},
        headers={
            "Authorization": f"Bearer {token}",
            "x-citadel-mcp-tool": "citadel_ingest",
        },
    )

    assert response.status_code == 200
    assert response.json()["dataset"] == "seat:mcp-carol"
    assert tracking.ingest_calls[0]["dataset"] == "seat:mcp-carol"


def test_mcp_seat_contribute_forbidden(tmp_path: Any) -> None:
    client, token, tracking = _seat_mcp_client(tmp_path, "mcp-dana")

    response = client.post(
        "/api/contribute",
        json={"title": "Title", "content": "Body"},
        headers={
            "Authorization": f"Bearer {token}",
            "x-citadel-mcp-tool": "citadel_contribute",
        },
    )

    assert response.status_code == 403
    assert "central" in response.json()["detail"].lower()
    assert tracking.ingest_calls == []


def test_seat_contribute_forbidden(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Mcp Dana", "slug": "mcp-dana"}).json()["token"]
    api_client = TestClient(app, base_url="https://testserver")

    response = api_client.post(
        "/api/contribute",
        json={"title": "Title", "content": "Body"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert "promotion" in response.json()["detail"].lower()


def test_lifespan_rehydrates_mesh_from_source_state(tmp_path: Path) -> None:
    github = tmp_path / "github_sync_state.json"
    github.write_text(
        json.dumps(
            {
                "org": "acme",
                "last_checked_at": "2026-06-22T00:00:00Z",
                "repos": {"acme/one": {}, "acme/two": {}, "acme/three": {}},
            }
        ),
        encoding="utf-8",
    )
    citadel = FakeCitadel()
    citadel.config = CitadelConfig(
        tenant_id="test",
        default_dataset="notes",
        github_sync_state_path=str(github),
        repo_content_sync_state_path=str(tmp_path / "absent_repo_state.json"),
        linear_sync_state_path=str(tmp_path / "absent_linear_state.json"),
    )
    app.state.citadel = citadel

    # Entering the TestClient context triggers the lifespan, which builds and seeds
    # the mesh exactly once before serving requests.
    with TestClient(app):
        mesh = app.state.mesh

    assert mesh._rehydrated is True
    # Counters are not seeded (avoids double-counting live github/repo re-ingests);
    # last_indexed_at + the source graph projection carry the persistent state.
    assert mesh.documents == 0
    assert mesh.indexed_chunks == 0
    assert mesh.last_indexed_at == "2026-06-22T00:00:00Z"
    assert any(node["type"] == "source" for node in mesh.nodes.values())


# --- GitHub PR-merge webhook (ADR-0005 step 3) -----------------------------


class _WebhookSyncer:
    """Tracks whether the heavy org re-ingest was actually awaited inline."""

    def __init__(self) -> None:
        self.ran = False

    async def run(self, *, force: bool = False) -> dict[str, Any]:
        self.ran = True
        return {"ok": True, "force": force}


def _webhook_citadel(secret: str, *, enabled: bool = True) -> FakeCitadel:
    citadel = FakeCitadel()
    # Instance attribute shadows the class-level config for this test only.
    citadel.config = CitadelConfig(
        tenant_id="test",
        default_dataset="notes",
        github_sync_dataset="masumi-network",
        github_webhook_enabled=enabled,
        github_webhook_secret=secret,
    )
    return citadel


def test_webhook_reingest_records_mesh_error_on_failure(tmp_path: Path) -> None:
    app.state.citadel = _webhook_citadel(secrets.token_hex(16))
    mesh = MeshState()
    app.state.mesh = mesh

    class _FailingSyncer:
        async def run(self, *, force: bool = False) -> dict[str, Any]:
            raise RuntimeError("GitHub API returned 403: rate limit exceeded")

    asyncio.run(server_module._run_webhook_reingest(_FailingSyncer()))

    # The fire-and-forget webhook re-ingest used to swallow failures to a log
    # line; it now records the error on the mesh so a 403 is visible.
    assert mesh.errors == 1


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _setup_webhook(tmp_path: Path, secret: str, *, enabled: bool = True) -> _WebhookSyncer:
    app.state.citadel = _webhook_citadel(secret, enabled=enabled)
    app.state.mesh = MeshState()
    syncer = _WebhookSyncer()
    app.state.github_syncer = syncer
    app.state.access_store = AccessStore(tmp_path / "access.json")
    return syncer


def _merge_payload() -> bytes:
    return json.dumps(
        {
            "action": "closed",
            "pull_request": {"merged": True, "number": 42},
            "repository": {"full_name": "masumi-network/agent"},
        }
    ).encode("utf-8")


def test_github_webhook_merged_pr_returns_202_nonblocking_and_audits(
    tmp_path: Path, monkeypatch: Any
) -> None:
    secret = secrets.token_hex(16)  # built at runtime; no committed secret literal.
    syncer = _setup_webhook(tmp_path, secret)

    # Capture the re-ingest at scheduling time without running the heavy sync, so
    # the assertions are deterministic and prove the handler does not block on it.
    triggered: list[Any] = []

    def fake_reingest(passed: Any) -> Any:
        triggered.append(passed)

        async def _noop() -> None:
            return None

        return _noop()

    monkeypatch.setattr(server_module, "_run_webhook_reingest", fake_reingest)

    body = _merge_payload()
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(secret, body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    # Non-blocking: the ~26min org sync is scheduled, never awaited in-request.
    assert triggered == [syncer]
    assert syncer.ran is False

    events = app.state.access_store.snapshot()["audit_events"]
    merge_events = [e for e in events if e["action"] == "github_webhook.merge"]
    assert len(merge_events) == 1
    assert merge_events[0]["success"] is True
    assert merge_events[0]["detail"]["merged"] is True
    assert merge_events[0]["detail"]["pr_number"] == 42
    assert merge_events[0]["detail"]["repository"] == "masumi-network/agent"
    assert merge_events[0]["detail"]["triggered"] == "github_sync"


def test_github_webhook_invalid_signature_returns_401(
    tmp_path: Path, monkeypatch: Any
) -> None:
    secret = secrets.token_hex(16)
    syncer = _setup_webhook(tmp_path, secret)
    triggered: list[Any] = []
    monkeypatch.setattr(
        server_module, "_run_webhook_reingest", lambda s: triggered.append(s)
    )

    body = _merge_payload()
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            # Signature computed with the WRONG secret -> must be rejected.
            "X-Hub-Signature-256": _sign(secrets.token_hex(16), body),
        },
    )

    assert response.status_code == 401
    assert triggered == []
    assert syncer.ran is False
    events = app.state.access_store.snapshot()["audit_events"]
    assert [e for e in events if e["action"] == "github_webhook.merge"] == []


def test_github_webhook_missing_signature_returns_401(tmp_path: Path) -> None:
    secret = secrets.token_hex(16)
    syncer = _setup_webhook(tmp_path, secret)

    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/api/webhooks/github",
        content=_merge_payload(),
        headers={"X-GitHub-Event": "pull_request"},
    )

    assert response.status_code == 401
    assert syncer.ran is False


def test_github_webhook_disabled_returns_404(tmp_path: Path) -> None:
    secret = secrets.token_hex(16)
    syncer = _setup_webhook(tmp_path, secret, enabled=False)

    body = _merge_payload()
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/api/webhooks/github",
        content=body,
        # Even a valid signature must 404 while the webhook is disabled.
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(secret, body),
        },
    )

    assert response.status_code == 404
    assert syncer.ran is False


def test_github_webhook_non_merge_close_returns_204(tmp_path: Path) -> None:
    secret = secrets.token_hex(16)
    syncer = _setup_webhook(tmp_path, secret)

    body = json.dumps(
        {"action": "closed", "pull_request": {"merged": False, "number": 7}}
    ).encode("utf-8")
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(secret, body),
        },
    )

    assert response.status_code == 204
    assert syncer.ran is False
    events = app.state.access_store.snapshot()["audit_events"]
    assert [e for e in events if e["action"] == "github_webhook.merge"] == []


def test_github_webhook_other_event_returns_204(tmp_path: Path) -> None:
    secret = secrets.token_hex(16)
    syncer = _setup_webhook(tmp_path, secret)

    body = json.dumps({"zen": "ping", "hook_id": 1}).encode("utf-8")
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/api/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": _sign(secret, body),
        },
    )

    assert response.status_code == 204
    assert syncer.ran is False


def test_org_capture_baseline_requires_admin(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    reader = authed_client("test-reader")

    response = reader.get("/api/access/capture-baseline")

    assert response.status_code == 403


def test_org_capture_baseline_merges_env_and_defaults(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.citadel = FakeCitadel()
    admin = authed_client()

    response = admin.get("/api/access/capture-baseline")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert ".git/*" in payload["env_exclude_patterns"]
    assert ".env" in payload["effective_deny_globs"]


def test_seat_capture_policy_admin_crud(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.citadel = FakeCitadel()
    admin = authed_client()
    admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"})

    empty = admin.get("/api/access/seats/alice/capture-policy")
    assert empty.status_code == 200
    assert empty.json()["baseline"]["deny_globs"] == []

    updated = admin.put(
        "/api/access/seats/alice/capture-policy",
        json={"deny_globs": ["team-private/*", ".env.local"]},
    )
    assert updated.status_code == 200
    payload = updated.json()
    assert payload["baseline"]["deny_globs"] == ["team-private/*", ".env.local"]
    assert "team-private/*" in payload["effective_deny_globs"]
    assert ".env.local" in payload["effective_deny_globs"]

    audit = admin.get("/api/audit")
    assert any(
        event["action"] == "access.capture_policy.update"
        for event in audit.json()["audit_events"]
    )


def test_seat_capture_policy_readable_by_seat_token(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.citadel = FakeCitadel()
    admin = authed_client()
    created = admin.post("/api/access/seats", json={"name": "Bob", "slug": "bob"})
    token = created.json()["token"]
    admin.put(
        "/api/access/seats/bob/capture-policy",
        json={"deny_globs": ["private-notes/*"]},
    )
    seat_client = TestClient(app, base_url="https://testserver")

    response = seat_client.get(
        "/api/access/seats/bob/capture-policy",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert "private-notes/*" in response.json()["effective_deny_globs"]


def test_seat_capture_policy_put_requires_admin(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.citadel = FakeCitadel()
    admin = authed_client()
    created = admin.post("/api/access/seats", json={"name": "Carol", "slug": "carol"})
    token = created.json()["token"]
    seat_client = TestClient(app, base_url="https://testserver")

    response = seat_client.put(
        "/api/access/seats/carol/capture-policy",
        json={"deny_globs": ["blocked/*"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_promotion_pending_list_redacts_body(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"})
    created = admin.post("/api/access/seats", json={"name": "Bob", "slug": "bob"})
    bob_token = created.json()["token"]
    bob_client = TestClient(app, base_url="https://testserver")

    from kb.access import now_iso
    from kb.promotion_queue import build_pending_item
    from kb.promotion_refs import ReferenceAssessment

    item = build_pending_item(
        seat_slug="alice",
        seat_dataset="seat:alice",
        candidate_text="secret candidate body should not leak",
        assessment=ReferenceAssessment(status="new_org_project", reason="no_org_or_central_match"),
        created_at=now_iso(),
    )
    app.state.access_store.add_promotion_pending(item)

    own = admin.get("/api/promotion/pending")
    assert own.status_code == 200
    assert own.json()["count"] == 1
    assert "candidate_text" not in own.json()["items"][0]

    other = bob_client.get(
        "/api/promotion/pending",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert other.status_code == 200
    assert other.json()["count"] == 0


def test_promotion_approve_reject_require_admin_not_seat_writer(tmp_path: Any) -> None:
    # #48: a seat-writer must NOT be able to approve/reject a promotion into Central.
    # The admin gate must 403 BEFORE the item lookup, so even a real own-seat item
    # cannot be self-promoted, and a bogus id never reaches a 404 for a seat.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    created = admin.post("/api/access/seats", json={"name": "Bob", "slug": "bob"})
    bob_token = created.json()["token"]
    bob = TestClient(app, base_url="https://testserver")
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    from kb.access import now_iso
    from kb.promotion_queue import build_pending_item
    from kb.promotion_refs import ReferenceAssessment

    item = build_pending_item(
        seat_slug="bob",
        seat_dataset="seat:bob",
        candidate_text="bob's own candidate",
        assessment=ReferenceAssessment(status="new_org_project", reason="no_org_or_central_match"),
        created_at=now_iso(),
    )
    app.state.access_store.add_promotion_pending(item)

    for verb in ("approve", "reject"):
        # Seat-writer is 403'd for both its own real item and a bogus id (authz first).
        own = bob.post(f"/api/promotion/pending/{item.id}/{verb}", headers=bob_headers, json={})
        bogus = bob.post(f"/api/promotion/pending/does-not-exist/{verb}", headers=bob_headers, json={})
        assert own.status_code == 403, verb
        assert bogus.status_code == 403, verb
        # Admin passes the authz gate (proven by reaching the 404 id lookup).
        admin_bogus = admin.post(f"/api/promotion/pending/does-not-exist/{verb}", json={})
        assert admin_bogus.status_code == 404, verb

    # The seat's own pending item is still queued (never approved/rejected/promoted).
    assert app.state.access_store.get_promotion_pending(item.id) is not None


def register_seat_capture_roots(client: TestClient, slug: str, roots: list[str]) -> None:
    response = client.put(
        f"/api/access/seats/{slug}/capture-roots",
        json={"roots": roots},
    )
    assert response.status_code == 200
    stored = response.json()["roots"]
    assert stored == [str(Path(root).resolve()) for root in roots]


class ShareCitadel(FakeCitadel):
    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []
        self.cognee = type("_FakeCognee", (), {})()
        self.cognee.scheduled = []
        self.cognee.schedule_cognify = lambda datasets: self.cognee.scheduled.append(list(datasets))

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        dataset = kwargs.get("dataset") or "notes"
        return IngestResult(True, "accepted", dataset, tuple(kwargs.get("tags") or ()))


class CrossSeatTraceCitadel(FakeCitadel):
    """Stateful fake: shared traces ingested by one seat are searchable by another."""

    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []
        self.cognee = type("_FakeCognee", (), {})()
        self.cognee.scheduled: list[list[str]] = []
        self.cognee.schedule_cognify = lambda datasets: self.cognee.scheduled.append(list(datasets))

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        dataset = kwargs.get("dataset") or "notes"
        if dataset == SESSION_TRACES_DATASET:
            self.traces.append({"text": data, "dataset": dataset})
        return IngestResult(True, "accepted", dataset, tuple(kwargs.get("tags") or ()))

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        dataset = kwargs["dataset"]
        if dataset == SESSION_TRACES_DATASET:
            needle = query.lower()
            return [
                {"text": trace["text"], "dataset": dataset}
                for trace in self.traces
                if needle in trace["text"].lower()
            ]
        if dataset.startswith("seat:"):
            return []
        return [{"query": query, "dataset": dataset, "text": f"{query} in {dataset}"}]


def test_share_session_dual_writes_and_schedules_cognify(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()["token"]
    app.state.citadel = ShareCitadel()
    client = TestClient(app, base_url="https://testserver")
    root = str(tmp_path)
    register_seat_capture_roots(admin, "alice", [root])
    data = (
        "# Shared Session Trace\nAuthor-Seat: alice\nCreated-At: 2026-07-20T12:00:00Z\n\n"
        "Task: fix lock\nApproach: in-process cognify"
    )

    response = client.post(
        "/api/share-session",
        json={"data": data, "cwd": root, "capture_roots": [root], "has_tool_errors": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["cognify"] == "deferred"
    datasets = [call["dataset"] for call in app.state.citadel.ingest_calls]
    assert datasets == ["seat:alice", SESSION_TRACES_DATASET]
    assert app.state.citadel.cognee.scheduled == [["seat:alice", SESSION_TRACES_DATASET]]


def test_share_session_trace_visible_to_other_seat(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    alice_token = admin.post(
        "/api/access/seats",
        json={"name": "Alice", "slug": "alice"},
    ).json()["token"]
    bob_token = admin.post(
        "/api/access/seats",
        json={"name": "Bob", "slug": "bob"},
    ).json()["token"]
    citadel = CrossSeatTraceCitadel()
    app.state.citadel = citadel
    client = TestClient(app, base_url="https://testserver")
    root = str(tmp_path)
    register_seat_capture_roots(admin, "alice", [root])
    marker = "kuzu-lock-dead-end-cross-seat-marker"
    data = (
        "# Shared Session Trace\nAuthor-Seat: alice\nCreated-At: 2026-07-20T12:00:00Z\n\n"
        f"Task: fix lock\nApproach: in-process cognify\nDead ends: {marker}"
    )

    share = client.post(
        "/api/share-session",
        json={"data": data, "cwd": root, "capture_roots": [root], "has_tool_errors": True},
        headers={"Authorization": f"Bearer {alice_token}"},
    )
    assert share.status_code == 200
    assert [call["dataset"] for call in citadel.ingest_calls] == [
        "seat:alice",
        SESSION_TRACES_DATASET,
    ]

    search = client.post(
        "/search",
        json={"query": marker},
        headers={"Authorization": f"Bearer {bob_token}"},
    )

    assert search.status_code == 200
    payload = search.json()
    trace_hits = payload["sections"]["session_traces"]
    assert trace_hits
    assert trace_hits[0]["_citadel"]["trust"] == "reference-only"
    assert trace_hits[0]["_citadel"]["doc_type"] == "session-trace"
    assert trace_hits[0]["_citadel"]["trust_tier"] == "reference-only"
    assert trace_hits[0]["_citadel"]["author_seat"] == "alice"
    assert payload["sections"]["node"] == []
    assert "seat:alice" not in {hit.get("dataset") for hit in payload["results"]}


def test_share_session_trace_not_visible_without_share(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats",
        json={"name": "Bob", "slug": "bob"},
    ).json()["token"]
    app.state.citadel = CrossSeatTraceCitadel()
    client = TestClient(app, base_url="https://testserver")

    search = client.post(
        "/search",
        json={"query": "kuzu-lock-dead-end-cross-seat-marker"},
        headers={"Authorization": f"Bearer {bob_token}"},
    )

    assert search.status_code == 200
    assert search.json()["sections"]["session_traces"] == []


def test_share_session_refuses_outside_capture_root(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()["token"]
    app.state.citadel = ShareCitadel()
    client = TestClient(app, base_url="https://testserver")
    register_seat_capture_roots(admin, "alice", [str(tmp_path)])

    response = client.post(
        "/api/share-session",
        json={
            "data": "Task: secret path",
            "cwd": "/tmp/outside",
            "capture_roots": [str(tmp_path)],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert "server-approved Capture Root" in response.json()["detail"]


def test_share_session_rejects_spoofed_client_capture_roots(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()["token"]
    app.state.citadel = ShareCitadel()
    client = TestClient(app, base_url="https://testserver")
    approved = str(tmp_path / "approved")
    approved_path = Path(approved)
    approved_path.mkdir()
    register_seat_capture_roots(admin, "alice", [approved])

    response = client.post(
        "/api/share-session",
        json={
            "data": "Task: spoofed root bypass attempt",
            "cwd": "/tmp/spoofed",
            "capture_roots": ["/tmp/spoofed"],
            "has_tool_errors": True,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert app.state.citadel.ingest_calls == []


def test_share_session_blocks_secret_before_llm(tmp_path: Any, monkeypatch: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()["token"]
    app.state.citadel = ShareCitadel()
    client = TestClient(app, base_url="https://testserver")
    root = str(tmp_path)
    register_seat_capture_roots(admin, "alice", [root])

    def explode(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("flagged share payload must never reach OpenRouter")

    monkeypatch.setattr("kb.session_trace.enrichment_enabled", lambda: True)
    monkeypatch.setattr("kb.session_trace.openrouter_chat", explode)
    data = (
        "# Shared Session Trace\nAuthor-Seat: alice\nCreated-At: 2026-07-20T12:00:00Z\n\n"
        "Task: deploy key AKIAIOSFODNN7EXAMPLE do not share\nApproach: retry auth"
    )

    response = client.post(
        "/api/share-session",
        json={"data": data, "cwd": root, "capture_roots": [root], "has_tool_errors": True},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
    assert "AKIAIOSFODNN7EXAMPLE" not in response.text
    assert app.state.citadel.ingest_calls == []
    events = app.state.access_store.snapshot()["audit_events"]
    blocked = [event for event in events if event["detail"].get("blocked") == "secret_content"]
    assert blocked
    assert blocked[-1]["action"] == "share_session"


def test_share_session_overwrites_spoofed_author_seat(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()["token"]
    app.state.citadel = ShareCitadel()
    client = TestClient(app, base_url="https://testserver")
    root = str(tmp_path)
    register_seat_capture_roots(admin, "alice", [root])
    data = (
        "# Shared Session Trace\nAuthor-Seat: bob\nCreated-At: 2026-07-20T12:00:00Z\n\n"
        "Task: spoof author attribution\nApproach: claim bob wrote this"
    )

    response = client.post(
        "/api/share-session",
        json={"data": data, "cwd": root, "capture_roots": [root], "has_tool_errors": False},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    ingested = app.state.citadel.ingest_calls[-1]["data"]
    assert "Author-Seat: alice" in ingested
    assert "Author-Seat: bob" not in ingested


class PartialSessionTraceWriteCitadel(FakeCitadel):
    """Accepts seat-node writes but rejects session-traces ingest."""

    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []
        self.cognee = type("_FakeCognee", (), {})()
        self.cognee.scheduled: list[list[str]] = []
        self.cognee.schedule_cognify = lambda datasets: self.cognee.scheduled.append(list(datasets))

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        dataset = kwargs.get("dataset") or "notes"
        if dataset == SESSION_TRACES_DATASET:
            return IngestResult(False, "rejected", dataset, tuple(kwargs.get("tags") or ()))
        return IngestResult(True, "accepted", dataset, tuple(kwargs.get("tags") or ()))


class FlakySessionTraceWriteCitadel(FakeCitadel):
    """Rejects session-traces on first attempt, accepts on retry."""

    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []
        self.session_traces_attempts = 0
        self.cognee = type("_FakeCognee", (), {})()
        self.cognee.scheduled: list[list[str]] = []
        self.cognee.schedule_cognify = lambda datasets: self.cognee.scheduled.append(list(datasets))

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        dataset = kwargs.get("dataset") or "notes"
        if dataset == SESSION_TRACES_DATASET:
            self.session_traces_attempts += 1
            if self.session_traces_attempts == 1:
                return IngestResult(
                    False,
                    "rejected",
                    dataset,
                    tuple(kwargs.get("tags") or ()),
                )
        return IngestResult(True, "accepted", dataset, tuple(kwargs.get("tags") or ()))


def test_share_session_retries_session_traces_then_succeeds(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()["token"]
    citadel = FlakySessionTraceWriteCitadel()
    app.state.citadel = citadel
    client = TestClient(app, base_url="https://testserver")
    root = str(tmp_path)
    register_seat_capture_roots(admin, "alice", [root])
    data = (
        "# Shared Session Trace\nAuthor-Seat: alice\nCreated-At: 2026-07-20T12:00:00Z\n\n"
        "Task: flaky session-traces write\nApproach: retry once before success"
    )

    response = client.post(
        "/api/share-session",
        json={"data": data, "cwd": root, "capture_roots": [root], "has_tool_errors": False},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["cognify"] == "deferred"
    assert [call["dataset"] for call in citadel.ingest_calls] == [
        "seat:alice",
        SESSION_TRACES_DATASET,
        SESSION_TRACES_DATASET,
    ]
    assert citadel.session_traces_attempts == 2
    assert citadel.cognee.scheduled == [["seat:alice", SESSION_TRACES_DATASET]]


def test_share_session_fails_when_session_traces_write_rejected(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()["token"]
    app.state.citadel = PartialSessionTraceWriteCitadel()
    client = TestClient(app, base_url="https://testserver")
    root = str(tmp_path)
    register_seat_capture_roots(admin, "alice", [root])
    data = (
        "# Shared Session Trace\nAuthor-Seat: alice\nCreated-At: 2026-07-20T12:00:00Z\n\n"
        "Task: partial dual-write failure\nApproach: session-traces ingest rejected"
    )

    response = client.post(
        "/api/share-session",
        json={"data": data, "cwd": root, "capture_roots": [root], "has_tool_errors": False},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error_type"] == "partial_write_failure"
    assert detail["retried"] is True
    assert SESSION_TRACES_DATASET in detail["failed_targets"]
    assert [call["dataset"] for call in app.state.citadel.ingest_calls] == [
        "seat:alice",
        SESSION_TRACES_DATASET,
        SESSION_TRACES_DATASET,
    ]
    assert app.state.citadel.cognee.scheduled == []
    events = app.state.access_store.snapshot()["audit_events"]
    failed = [event for event in events if event["action"] == "share_session" and not event["success"]]
    assert failed
    assert failed[-1]["detail"]["error_type"] == "partial_write_failure"
    assert failed[-1]["detail"]["retried"] is True


def test_search_marks_session_trace_hits_reference_only(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Bob", "slug": "bob"}).json()["token"]
    app.state.citadel = MultiSearchCitadel()
    client = TestClient(app, base_url="https://testserver")

    response = client.post(
        "/search",
        json={"query": "dead end"},
        headers={"Authorization": f"Bearer {token}"},
    )

    trace_hits = [
        item
        for item in response.json()["results"]
        if item.get("_citadel", {}).get("dataset") == SESSION_TRACES_DATASET
    ]
    assert trace_hits
    assert trace_hits[0]["_citadel"]["trust"] == "reference-only"
    assert trace_hits[0]["_citadel"]["doc_type"] == "session-trace"
    assert trace_hits[0]["_citadel"]["trust_tier"] == "reference-only"
