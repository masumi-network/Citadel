from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import logging
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest
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

    async def tombstone_source(self, **kwargs: Any) -> tuple[IngestResult, ...]:
        calls = getattr(self, "tombstone_calls", None)
        if calls is None:
            calls = []
            self.tombstone_calls = calls
        calls.append(kwargs)
        return (
            IngestResult(
                True,
                "queued_not_confirmed",
                kwargs["dataset"],
                (),
                source_revision_id="tombstone-source",
                projection_job_id="tombstone-job",
                projection_state="pending",
            ),
        )

    def lifecycle_operation(self, projection_job_id: str) -> dict[str, Any]:
        if projection_job_id not in {"job-1", "job-seat"}:
            raise KeyError(projection_job_id)
        return {
            "schema_version": 1,
            "projection_job_id": projection_job_id,
            "source_revision_id": "source-1",
            "dataset": "seat:alice" if projection_job_id == "job-seat" else "notes",
            "state": "searchable",
            "receipts": [
                {"backend": "relational", "provider": "sqlite", "state": "searchable"},
                {"backend": "vector", "provider": "qdrant", "state": "searchable"},
                {"backend": "graph", "provider": "ladybug", "state": "searchable"},
            ],
        }

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"query": query, "dataset": kwargs["dataset"], "top_k": kwargs["top_k"]}]

    documents: dict[str, dict[str, Any]] = {}

    async def _graph_counts(self) -> dict[str, int]:
        return {"nodes": 5, "edges": 7}

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

    async def reconcile_zero_chunk_documents(
        self,
        *,
        dataset: Any = None,
        apply: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "dataset": dataset,
            "apply": apply,
            "force": force,
            "reason": "no_zero_chunk_documents",
            "before": {
                "ok": True,
                "zero_chunk_count": 0,
                "unassigned_zero_chunk_count": 0,
                "repair_datasets": [],
            },
            "after": None,
        }

    async def reconcile_corpus(
        self,
        *,
        dataset: Any = None,
        apply: bool = False,
        force: bool = False,
        recover: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "dataset": dataset,
            "apply": apply,
            "force": force,
            "reason": "no_repair_required",
            "before": {
                "ok": True,
                "census_complete": True,
                "zero_chunk_count": 0,
                "oversized_document_count": 0,
                "oversized_chunk_count": 0,
            },
            "after": None,
        }

    async def reconcile_oversized_chunks(
        self,
        *,
        dataset: Any = None,
        apply: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "dataset": dataset,
            "apply": apply,
            "force": force,
            "reason": "no_oversized_chunks",
            "before": {
                "ok": True,
                "oversized_document_count": 0,
                "oversized_documents_truncated": False,
                "unassigned_oversized_document_count": 0,
                "repair_document_ids": [],
                "repair_datasets": [],
            },
            "after": None,
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


LEGACY_PUBLIC_SOURCES = {
    "/": "landing.html",
    "/info": "info.html",
    "/use-cases": "use-cases.html",
    "/contact": "contact.html",
}


def legacy_public_markup(path: str) -> str:
    if path == "/login":
        return server_module.LOGIN_HTML
    return (server_module.STATIC_DIR / LEGACY_PUBLIC_SOURCES[path]).read_text(
        encoding="utf-8"
    )


def test_healthz() -> None:
    client = TestClient(app)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "citadel"}


def test_public_state_uses_the_package_source_version() -> None:
    client = authed_client("test-reader")
    response = client.get("/api/state")
    assert response.status_code == 200
    assert response.json()["version"] == server_module._SERVICE_VERSION


def test_public_state_and_readyz_report_lifecycle_census(
    monkeypatch: Any,
) -> None:
    class LifecycleStateCitadel(FakeCitadel):
        config = CitadelConfig(
            tenant_id="test",
            default_dataset="notes",
            admin_key="test-admin",
            reader_keys=("test-reader",),
            lifecycle_enabled=True,
        )

        def lifecycle_census(self) -> dict[str, Any]:
            return {
                "source_revisions": 4,
                "current_sources": 3,
                "retained_bytes": 120,
                "projection_jobs": 4,
                "projection_receipts": 12,
                "job_states": {"completed": 3, "pending": 1},
                "receipt_states": {"searchable": 9, "pending": 3},
                "current_generation": {
                    "generation_id": "generation-1",
                    "projection_version": "projection-v1",
                    "current_sources": 3,
                    "current_projection_jobs": 3,
                    "current_projection_receipts": 9,
                    "current_receipts_by_backend": {
                        "graph": 3,
                        "relational": 3,
                        "vector": 3,
                    },
                    "current_searchable_by_backend": {
                        "graph": 2,
                        "relational": 3,
                        "vector": 2,
                    },
                },
            }

    async def healthy_corpus() -> dict[str, Any]:
        return {"ok": True}

    client = authed_client("test-reader")
    app.state.citadel = LifecycleStateCitadel()
    monkeypatch.setattr(server_module, "_corpus_health", healthy_corpus)

    state = client.get("/api/state")
    ready = client.get("/readyz")

    assert state.status_code == 200
    assert state.json()["lifecycle"]["source_revisions"] == 4
    assert state.json()["lifecycle"]["ok"] is True
    assert ready.status_code == 200
    assert ready.json()["lifecycle"]["projection_receipts"] == 12


def test_lifecycle_health_rejects_incomplete_current_generation() -> None:
    class IncompleteGenerationCitadel(FakeCitadel):
        config = CitadelConfig(lifecycle_enabled=True)

        def lifecycle_census(self) -> dict[str, Any]:
            return {
                "source_revisions": 2,
                "current_sources": 2,
                "retained_bytes": 20,
                "projection_jobs": 2,
                "projection_receipts": 6,
                "job_states": {"completed": 2},
                "receipt_states": {"searchable": 6},
                "current_generation": {
                    "generation_id": "generation-2",
                    "projection_version": "projection-v1",
                    "current_sources": 2,
                    "current_projection_jobs": 1,
                    "current_projection_receipts": 3,
                    "current_receipts_by_backend": {
                        "graph": 1,
                        "relational": 1,
                        "vector": 1,
                    },
                    "current_searchable_by_backend": {
                        "graph": 1,
                        "relational": 1,
                        "vector": 1,
                    },
                },
            }

    health = server_module.lifecycle_health(IncompleteGenerationCitadel())

    assert health["ok"] is False
    assert "current_generation_job_census_mismatch" in health["invariant_errors"]


def test_projection_operation_status_returns_bounded_backend_receipts() -> None:
    client = authed_client("test-reader")

    response = client.get("/api/operations/job-1")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "projection_job_id": "job-1",
        "source_revision_id": "source-1",
        "dataset": "notes",
        "state": "searchable",
        "receipts": [
            {"backend": "relational", "provider": "sqlite", "state": "searchable"},
            {"backend": "vector", "provider": "qdrant", "state": "searchable"},
            {"backend": "graph", "provider": "ladybug", "state": "searchable"},
        ],
    }


def test_projection_operation_status_enforces_private_dataset_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = authed_client("test-reader")
    monkeypatch.setattr(
        server_module,
        "require_access",
        lambda *_args, **_kwargs: AccessIdentity(
            role="reader",
            actor_id="reader-1",
            actor_kind="service",
            actor_name="reader",
            source="store",
            scopes=("kb:read",),
            allowed_datasets=("notes",),
        ),
    )
    assert reader.get("/api/operations/job-seat").status_code == 404


def test_public_state_and_discovery_share_deployment_identity() -> None:
    client = authed_client("test-reader")
    state = client.get("/api/state").json()
    discovery = client.get("/.well-known/citadel.json").json()

    assert state["version"] == discovery["service"]["version"] == server_module._SERVICE_VERSION
    assert state["build_id"] == discovery["service"]["build_id"] == server_module._BUILD_ID
    assert (
        state["deployment_id"]
        == discovery["service"]["deployment_id"]
        == server_module._DEPLOYMENT_ID
    )


def test_public_state_reports_the_captured_build_id(monkeypatch) -> None:
    monkeypatch.setattr(server_module, "_BUILD_ID", "a" * 40)

    response = authed_client("test-reader").get("/api/state")

    assert response.status_code == 200
    assert response.json()["build_id"] == "a" * 40


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


def test_no_path_relaxes_style_src() -> None:
    """Every route, / included, is served under the strict style-src.

    / renders a React Flow diagram that positions nodes by writing inline
    `transform` styles, and for a while that bought it the site's one
    'unsafe-inline' exemption. The exemption protected nothing: React writes
    those transforms through the CSSOM (element.style), which style-src does
    not govern. The directive covers <style> elements and style attributes
    arriving in markup. With the exemption removed, the diagram rendered
    identically and no securitypolicyviolation fired (measured in Chrome; the
    CSSOM carve-out is spec behaviour). So / is pinned strict here alongside
    everything else, and most deliberately of all: it is the one path that
    would drift back.
    """
    client = authed_client()

    strict = "style-src 'self';"

    for path in (
        "/",
        "/login",
        "/app",
        "/info",
        "/use-cases",
        "/contact",
        "/healthz",
        "/api/state",
    ):
        policy = client.get(path).headers["content-security-policy"]
        assert strict in policy, f"{path} lost the strict style-src"
        assert "'unsafe-inline'" not in policy, f"{path} carries an inline-style relaxation"
        # Script execution stays restricted to same-origin files everywhere.
        assert "script-src 'self';" in policy, path

    assert server_module.CSP_INLINE_STYLE_PATHS == frozenset(), (
        "Adding a path here is a security decision. Update this test "
        "deliberately, and check the page really renders something style-src "
        "governs: inline styles written through the CSSOM are not it."
    )


def test_csp_relaxation_is_opt_in_and_cannot_widen_by_accident() -> None:
    """The mechanism is exact-path, not prefix, and strict is the default.

    Kept even though the opt-in set is empty, because this is the property that
    matters when someone re-enables it: a prefix match would hand the relaxation
    to every child route, and defaulting to relaxed would invert the failure
    mode from "the flow looks broken" to "every page is exposed".
    """
    assert server_module.content_security_policy_for("/anything") == (
        server_module.CONTENT_SECURITY_POLICY
    )
    assert "'unsafe-inline'" not in server_module.CONTENT_SECURITY_POLICY
    assert "'unsafe-inline'" in server_module.CONTENT_SECURITY_POLICY_INLINE_STYLE
    # Script execution is never part of the bargain, in either policy.
    assert "script-src 'self';" in server_module.CONTENT_SECURITY_POLICY_INLINE_STYLE


def test_landing_diagram_ships_as_markup_and_upgrades_to_the_bundle() -> None:
    """Two forms of one diagram, and the served HTML must carry the simple one.

    The React Flow bundle is ~330 KB and is fetched only when the diagram
    scrolls into view. Someone who never scrolls that far, or who has
    JavaScript off, has to get a correct picture from the HTML alone, so the
    four-step spine ships visible and the React container ships empty.
    """
    home = legacy_public_markup("/")

    # The plain diagram ships visible, and it is four steps, not the fourteen
    # element two-lane version it replaced.
    assert '<div class="spine" id="spine-static"' in home
    for step in ("Capture", "Your Node", "Promotion", "Central"):
        assert f'<span class="t">{step}</span>' in home, step
    # The guarantee sentence survives every simplification of this diagram, and
    # stays on screen in both forms.
    assert "never another seat's" in home

    # The React container ships hidden and empty.
    assert '<div class="flowmount" id="flow-root" hidden></div>' in home
    # The bundle is never referenced from the HTML: landing.js injects it only
    # after the IntersectionObserver fires, so it costs nothing until reached.
    assert "flow.js" not in home
    assert "flow.css" not in home
    landing_js = (server_module.STATIC_DIR / "landing.js").read_text(encoding="utf-8")
    assert "/static/vendor/flow.js" in landing_js
    assert "IntersectionObserver" in landing_js


def test_landing_flow_bundle_is_committed_and_served() -> None:
    """The React Flow bundle is a build artefact that ships in the repo.

    There is no Node in CI or on the deploy host, so `npm run build:flow` runs
    on a developer machine and the output is committed. A source change landing
    without its rebuilt bundle is the failure this catches.
    """
    client = TestClient(app, base_url="https://testserver")

    script = client.get("/static/vendor/flow.js")
    styles = client.get("/static/vendor/flow.css")

    assert script.status_code == 200
    assert styles.status_code == 200
    # esbuild may prepend a strict-mode directive, but globalName must expose
    # CitadelFlow; landing.js calls window.CitadelFlow.mount() after loading.
    assert script.text.startswith(("var CitadelFlow=", '"use strict";var CitadelFlow='))
    assert ".react-flow" in styles.text


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
    page = legacy_public_markup("/login")

    assert '<script src="/static/login.js" type="module"></script>' in page
    assert "<script>\n" not in page
    assert "<style>" not in page
    assert 'style="' not in page


def test_login_page_uses_the_public_design_system() -> None:
    """/login is a public page, so it wears info.css — not the dashboard theme.

    A visitor arriving from /info must not be handed to a visibly different
    product at the door: same stylesheet, same nav, same theme toggle.
    """
    page = legacy_public_markup("/login")

    assert '<link rel="stylesheet" href="/static/info.css">' in page
    assert "/static/styles.css" not in page
    assert 'class="topnav"' in page
    assert 'id="themebtn"' in page

def test_use_cases_page_is_public_and_csp_clean() -> None:
    page = legacy_public_markup("/use-cases")
    # No token, no session — a coordinator or a curious engineer must be able
    # to open this. Team use cases lead; the partnering profile follows.
    assert "Four things teams run it for" in page
    assert "Draft work package" in page
    # Reuses the /info stylesheet + script rather than shipping its own.
    assert '<link rel="stylesheet" href="/static/info.css">' in page
    assert '<script src="/static/info.js" defer></script>' in page
    # Strict CSP: nothing inline.
    assert "<script>\n" not in page
    assert "<style>" not in page
    assert 'style="' not in page


def test_partners_url_still_resolves() -> None:
    """/partners was the first home of this page and is quoted outside the repo.

    It has been sent to coordinators and referenced in docs and commits, so the
    rename to /use-cases keeps the old URL alive rather than 404ing it.
    """
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/partners", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "/use-cases"


def test_contact_page_is_public_and_csp_clean() -> None:
    """The contact form is its own page, not a section at the bottom of another."""
    page = legacy_public_markup("/contact")

    assert 'id="contactForm"' in page
    assert '<script src="/static/contact.js" defer></script>' in page
    # The form only lives here now — /use-cases points at it instead.
    assert 'id="contactForm"' not in legacy_public_markup("/use-cases")
    assert '<link rel="stylesheet" href="/static/info.css">' in page
    assert "<script>\n" not in page
    assert "<style>" not in page
    assert 'style="' not in page


def test_public_pages_share_one_top_nav() -> None:
    """Every public page carries the same nav, each marking its own entry.

    The nav is what stops the public pages from being orphans reachable only by
    direct link.
    """
    pages = {
        path: legacy_public_markup(path)
        for path in ("/", "/info", "/use-cases", "/contact", "/login")
    }

    for path, page in pages.items():
        assert 'class="topnav"' in page, path
        for link in ("/", "/info", "/use-cases", "/contact", "/login"):
            assert f'<a href="{link}"' in page, f"{path} is missing the {link} nav link"
        assert "Sign in</a>" in page, path
        # The lockup lives in the nav now, so the page header must not repeat it.
        assert 'class="brandrow"' not in page, path
        assert f'<a href="{path}" aria-current="page">' in page, path


def test_public_pages_share_the_band_layout() -> None:
    """/info, /use-cases and /contact are built from the same bands as /.

    All three were a single 860px .wrap column left over from before the home
    page was rebuilt into alternating full-bleed bands. Side by side they read
    as two different websites, which one shared nav does not fix. The band
    rhythm and the sticky section index are what make the five pages one site,
    so they are pinned here rather than left to the next redesign to notice.
    """
    for path in ("/info", "/use-cases", "/contact"):
        page = legacy_public_markup(path)

        # The hero band carries the nav and the glow, exactly as / does.
        assert '<div class="band band-hero">' in page, path
        assert 'class="hero-glow' in page, path
        # Content sits in bands, never in the old single column.
        assert page.count('<div class="band-in">') >= 2, path
        assert 'class="wrap"' not in page, path
        # One sticky index per page, and the jump-chip nav it replaced is gone.
        assert '<nav class="index" aria-label="Sections">' in page, path
        assert 'class="toc"' not in page, path
        # The index is only a bar of dead links without the observer that
        # tracks the section in view.
        assert '<script src="/static/landing.js" defer></script>' in page, path
        # The live pill rides in the index bar, where / carries it.
        index = page[page.index('<nav class="index"'):page.index("</nav>", page.index('<nav class="index"'))]
        assert 'id="pill-health"' in index, path


def test_every_internal_link_on_the_public_pages_resolves() -> None:
    """Every internal href and every anchor on the public pages hits something.

    Two things make this rot quietly. Anchors are the first: the sticky index
    bars are hand-written lists of ids, so renaming a section silently turns
    its index entry into a no-op that scrolls nowhere and fails no test. The
    second is /partners, which is now a 301 to /use-cases. That redirect exists
    for links we have already sent to coordinators, not for our own pages, and
    a self-inflicted redirect costs a round trip and loses the fragment.

    Checked here rather than by eye because the fragments are the part nobody
    re-reads after a rename.
    """
    client = TestClient(app, base_url="https://testserver")

    pages = ("/", "/info", "/use-cases", "/contact", "/login")
    bodies = {path: client.get(path).text for path in pages}
    ids = {
        path: set(re.findall(r'id="([^"]+)"', body)) for path, body in bodies.items()
    }

    checked = 0
    for page, body in bodies.items():
        for href in re.findall(r'href="([^"]+)"', body):
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            checked += 1
            path, _, fragment = href.partition("#")

            if path.startswith("/static/"):
                assert client.get(path).status_code == 200, f"{page} -> {href}"
                continue

            assert path != "/partners", (
                f"{page} links to /partners. That route is a 301 kept alive for "
                "links already quoted outside the repo; inside our own pages, "
                "link to /use-cases directly."
            )
            if path:
                response = client.get(path, follow_redirects=False)
                assert response.status_code == 200, (
                    f"{page} -> {href} returned {response.status_code}"
                )

            if fragment:
                target = path or page
                assert target in ids, f"{page} -> {href} targets an unaudited page"
                assert fragment in ids[target], (
                    f"{page} -> {href} points at #{fragment}, which does not "
                    f"exist on {target}"
                )

    # A regex that stopped matching would pass every assertion above.
    assert checked > 30, f"only {checked} internal links found; the scan broke"


def test_public_pages_avoid_info_report_element_ids() -> None:
    """Pages sharing info.js with /info must not reuse report-only ids.

    info.js unconditionally writes the State-of-the-Vault text into these ids
    wherever it finds them. The partnering page reusing id="foot-note" silently
    replaced its own footer with "State-of-the-vault report ... window v0.2.0 ->
    v0.4.0" at runtime, which no server-side test would have caught.

    Each page keeps only the ids it genuinely wants hydrated: the health pill
    and the brand mark.
    """
    report_only_ids = ("foot-note", "state-updated", "m-version", "m-docs", "m-docs-sub")
    for path in ("/use-cases", "/contact"):
        page = legacy_public_markup(path)
        for element_id in report_only_ids:
            assert f'id="{element_id}"' not in page, (
                f'{path} declares id="{element_id}", which info.js overwrites '
                "with /info report copy. Use a different id or no id."
            )
        assert 'id="pill-health-text"' in page, path
        assert 'id="mark"' in page, path


def test_contact_page_has_no_unfilled_placeholders() -> None:
    """Contact details must be filled in before this page is deployed.

    /contact is public and outward-facing: it is sent to consortium
    coordinators and pasted into EU partner-search forms. Shipping it with a
    literal NAME/EMAIL/ADDRESS placeholder is worse than not shipping it, so
    the guard is a test rather than a review comment.

    This asserts on the placeholder **text**, not on the `todo` class it used to
    carry. The Next.js port reproduced the placeholders faithfully but styled
    them with Tailwind utilities, so a class-based check passed on a page that
    still said REGISTERED ADDRESS. A guard that the content can outlive is not a
    guard; keying on the words is what makes it survive a restyle or a rewrite.
    """
    client = TestClient(app, base_url="https://testserver")

    surfaces = {"/contact (hand-written)": client.get("/contact").text}
    ported = server_module.WEBUI_DIR / "contact.html"
    if ported.exists():
        surfaces["kb/webui/contact.html (Next.js port)"] = ported.read_text(encoding="utf-8")

    for where, markup in surfaces.items():
        for placeholder in (">NAME<", ">EMAIL<", "REGISTERED ADDRESS"):
            assert placeholder not in markup, (
                f"{where} still contains the unfilled placeholder {placeholder!r}. "
                "Fill the contact name, email and registered address, in every "
                "surface that renders them."
            )


def test_each_public_page_owns_its_subject() -> None:
    """One subject, one page. The public pages used to repeat each other.

    /info explained the Seat/Node/Central model and how to install, both of
    which / already covered in full, so the same paragraphs were maintained in
    two places and drifted. Home owns what-it-is and how-to-start; /info owns
    the live numbers and the roadmap.
    """
    home = legacy_public_markup("/")
    info = legacy_public_markup("/info")

    # The pipeline diagram and the install commands live on / only.
    assert 'class="spine"' in home
    assert 'class="spine"' not in info
    assert "pipx install citadel-archive" in home
    assert "citadel onboard" not in info
    # The live tiles and the roadmap live on /info only.
    assert 'id="m-version"' in info
    assert 'id="m-version"' not in home


def test_home_owns_install_and_the_diagram() -> None:
    """The diagram and the install commands live on / and nowhere else.

    Both used to be repeated on /info, and the copies drifted. This widens
    test_each_public_page_owns_its_subject from the /info pair to every public
    page, so a future page cannot quietly grow a second copy of either.
    """
    home = legacy_public_markup("/")
    others = {
        path: legacy_public_markup(path)
        for path in ("/info", "/use-cases", "/contact", "/login")
    }

    assert 'class="spine"' in home
    assert "pipx install citadel-archive" in home
    assert 'id="m-version"' not in home
    for path, page in others.items():
        assert 'class="spine"' not in page, path
        assert "pipx install citadel-archive" not in page, path
    # The live version tile is /info's, and only /info's.
    assert 'id="m-version"' in others["/info"]


def test_home_hero_has_no_terminal_block() -> None:
    """The hero is type only. The terminal block was removed on purpose.

    Four hero variants were built and looked at; the one with a terminal made
    the page read as documentation rather than as an argument, and pushed the
    lede below the fold. The commands still ship, at the bottom of the page.
    This pins the decision so it cannot drift back in.
    """
    home = legacy_public_markup("/")
    hero = home[home.index('<header class="hero">'):home.index("</header>")]

    assert "<pre" not in hero
    assert 'class="term' not in hero
    # ...and the commands are still on the page, just not in the hero.
    assert "pipx install citadel-archive" in home


def test_home_rotator_has_a_fixed_word_list() -> None:
    """The rotating headline word carries exactly this list, in this order.

    A half-edited list is the likely failure: five words tuned together, one
    of them swapped later without the others. The sixth entry repeats the
    first and is not a word in the list — it is the seam that lets the CSS
    keyframe wrap from the last word back to the first without a jump cut.
    """
    client = TestClient(app, base_url="https://testserver")

    home = client.get("/").text
    rotator = re.search(r'<span class="roll">(.*?)</span></h1>', home, re.S)

    assert rotator is not None, "/ no longer carries a .roll rotator in its h1"
    words = re.findall(r">([^<>]+)</span>", rotator.group(1))
    assert words == [
        "decisions.",
        "dead ends.",
        "reasons.",
        "sessions.",
        "context.",
        "decisions.",
    ]


def test_home_motion_respects_reduced_motion() -> None:
    """Both ambient animations stop under prefers-reduced-motion: reduce.

    The glow and the word roll are the only two loops on the page, and both
    are CSS keyframes, so the only place this can be honoured is inside
    info.css's global reduced-motion block. A content assertion is weak, but a
    server-side test cannot evaluate a media query.
    """
    css = (server_module.STATIC_DIR / "info.css").read_text(encoding="utf-8")

    start = css.index("@media (prefers-reduced-motion: reduce)")
    block = css[start:css.index("\n}", start)]

    # Both animations exist...
    assert "@keyframes drift" in css
    assert "@keyframes rollup" in css
    assert "animation: drift" in css
    assert "animation: rollup" in css
    # ...and both are neutralised inside the reduced-motion block. !important
    # matters: each animation is declared later in the file at the same
    # specificity, so a plain override would lose the cascade.
    assert ".hero-glow { animation: none !important; }" in block
    # The track carries the animation, not the window. `.roll` clips and never
    # moves; neutralising it here would be a no-op and the words would keep
    # rolling with reduced motion on.
    assert ".roll-track { animation: none !important;" in block


def test_home_rotator_window_and_track_are_separate_elements() -> None:
    """The clipping element and the animated element must not be the same one.

    They were, once. `.roll` both clipped (`overflow: hidden` plus a fixed
    height) and carried the `rollup` animation, so the translate walked the
    clipping box down the page instead of scrolling the words through it, and
    every word rendered at once as a stack. Correct markup did not catch it,
    because the markup was already correct. This pins the structure instead.
    """
    css = (server_module.STATIC_DIR / "info.css").read_text(encoding="utf-8")

    # rindex, not index: `.roll-track` also appears inside the reduced-motion
    # block earlier in the file, where it is deliberately set to `animation: none`.
    # The real declaration is the later one.
    window = css[css.index(".roll {"):css.index("\n", css.index(".roll {"))]
    track = css[css.rindex(".roll-track {"):css.index("\n", css.rindex(".roll-track {"))]

    assert "overflow: hidden" in window, ".roll must be the clipping window"
    assert "animation:" not in window, (
        ".roll must not animate. If the element that clips is also the element "
        "that moves, the whole word list renders as a visible stack."
    )
    assert "animation: rollup" in track, ".roll-track must carry the animation"

    # The window must not lay its own children out in a column, and must not
    # stretch the track to its own height. Either one squashes six 1.03em items
    # into a 1.03em box, where they are shrinkable flex items saved only by the
    # automatic minimum size rule.
    assert "flex-direction: column" not in window, (
        ".roll must not be a column: it would stack the words rather than clip them"
    )
    assert "align-items: flex-start" in window, (
        ".roll must not stretch .roll-track, or the track is forced to the "
        "window's height and its six items become shrinkable inside it"
    )
    assert "flex-direction: column" in track, ".roll-track stacks the words"

    # Offsets in em, not percentages: percentages resolve against the track's
    # own height, so adding or removing a word silently drifts every step.
    frames = css[css.index("@keyframes rollup"):]
    frames = frames[:frames.index("\n}")]
    assert "%)" not in frames.replace("0%,", "").replace("%,", ""), (
        "rollup offsets must be in em, matching the item height"
    )

    client = TestClient(app, base_url="https://testserver")
    page = client.get("/").text
    assert 'class="roll"><span class="roll-track">' in page.replace("\n", "")


def test_no_vendor_name_on_user_facing_surfaces() -> None:
    """No page a reader can open names the retrieval dependency.

    Source, comments and imports still name it; this is about what a visitor
    sees. /openapi.json is included because its description string is served
    publicly at /docs, which is the surface everyone forgets.
    """
    vendor = "cognee"
    client = authed_client()

    pages = {
        path: client.get(path).text
        for path in ("/", "/info", "/use-cases", "/contact", "/login", "/app")
    }
    description = client.get("/openapi.json").json()["info"]["description"]

    for path, page in pages.items():
        assert vendor not in page.lower(), f"{path} names the retrieval vendor"
    assert vendor not in description.lower()


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
    # The fixture reports 3 GitHub repositories and 2 Linear issues.
    assert mesh.json()["stats"]["tracked_sources"] == 5
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
    assert updated_mesh.json()["stats"]["since_restart"]["feedback"] >= 2
    assert upgrade.status_code == 200


def test_learning_agent_run_audit_records_gateway_skip_reason() -> None:
    """A digest that posts nowhere must say why in the audit trail, not just that.

    The response body from POST /api/learning-agent/run already carries a
    ``reason`` on ``notifications.google_chat`` (e.g. "google_chat_disabled").
    The audit record written for that same call is the only trace left once
    nobody is watching the response — it must not collapse that reason down to
    a bare ``sent: false`` with no explanation.
    """
    client = authed_client()

    class SkippedGatewayAgent(FakeLearningAgent):
        async def run(self, **kwargs: Any) -> dict[str, Any]:
            result = await super().run(**kwargs)
            result["notifications"] = {
                "google_chat": {
                    "enabled": False,
                    "sent": False,
                    "reason": "google_chat_disabled",
                }
            }
            return result

    server_module.app.state.learning_agent = SkippedGatewayAgent()

    response = client.post("/api/learning-agent/run", json={"post_to_chat": True})
    assert response.status_code == 200
    assert response.json()["notifications"]["google_chat"]["reason"] == "google_chat_disabled"

    events = server_module.app.state.access_store.snapshot()["audit_events"]
    run_events = [event for event in events if event["action"] == "learning_agent.run"]
    assert run_events, "expected a learning_agent.run audit event"
    detail = run_events[-1]["detail"]
    assert detail["google_chat_sent"] is False
    assert detail["google_chat_reason"] == "google_chat_disabled", (
        "the audit trail must carry why the gateway did not send, not just that "
        f"it did not: {detail}"
    )


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


def test_cognify_verification_failure_is_a_failed_api_operation() -> None:
    class FailingCognify(FakeCitadel):
        async def cognify_dataset(
            self, *, dataset: Any = None, verify: bool = False, force: bool = False
        ) -> dict[str, Any]:
            return {
                "ok": False,
                "dataset": dataset or self.config.default_dataset,
                "graph_before": {"nodes": 5, "edges": 7},
                "graph_after": {"nodes": 5, "edges": 7},
                "graph_grew": False,
                "verify": verify,
                "verification": {"search_hit": False, "ok": False},
            }

    client = authed_client()
    app.state.citadel = FailingCognify()

    response = client.post("/api/cognify/run", json={"verify": True})

    assert response.status_code == 503
    assert response.json()["detail"]["result"]["ok"] is False
    events = app.state.access_store.snapshot()["audit_events"]
    cognify_events = [event for event in events if event["action"] == "cognify.run"]
    assert cognify_events[-1]["success"] is False


def test_cognify_run_requires_admin() -> None:
    client = authed_client("test-writer")

    response = client.post("/api/cognify/run", json={})

    assert response.status_code == 403


def test_lifecycle_requeue_failed_requires_admin() -> None:
    client = authed_client("test-writer")
    assert client.post("/api/lifecycle/requeue-failed").status_code == 403


def test_lifecycle_requeue_failed_returns_the_reset_count(monkeypatch) -> None:
    client = authed_client()
    import kb.server as server_mod

    citadel = server_mod.get_citadel()
    monkeypatch.setattr(type(citadel), "lifecycle_requeue_failed", lambda self: 7, raising=False)
    response = client.post("/api/lifecycle/requeue-failed")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "requeued": 7}


def test_lifecycle_requeue_failed_restarts_the_drain(monkeypatch) -> None:
    # Resetting rows to pending is inert on its own. next_wakeup_delay counts
    # only 'pending' and 'running' jobs, so while every job sat in 'failed' the
    # drain task had already returned and nothing was polling. Without this
    # kick the route answers 200 while the corpus stays frozen until the hourly
    # evolve pass happens to call resume_lifecycle_queue (2026-08-12).
    client = authed_client()
    import kb.server as server_mod

    citadel = server_mod.get_citadel()
    resumed: list[bool] = []
    monkeypatch.setattr(type(citadel), "lifecycle_requeue_failed", lambda self: 3, raising=False)
    monkeypatch.setattr(
        type(citadel),
        "resume_lifecycle_queue",
        lambda self: resumed.append(True),
        raising=False,
    )
    response = client.post("/api/lifecycle/requeue-failed")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "requeued": 3}
    assert resumed == [True], "requeue must restart the projection drain"


def test_admin_can_audit_combined_reconciliation_and_writer_cannot() -> None:
    admin = authed_client()

    response = admin.post(
        "/api/corpus/reconcile",
        json={"dataset": "masumi-network"},
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "no_repair_required"
    assert response.json()["apply"] is False

    writer = authed_client("test-writer")
    assert writer.post("/api/corpus/reconcile", json={}).status_code == 403


def test_admin_can_audit_oversized_chunk_reconciliation() -> None:
    client = authed_client()

    response = client.post(
        "/api/corpus/reconcile",
        json={"oversized": True},
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "no_oversized_chunks"
    assert response.json()["apply"] is False


def test_admin_can_request_interrupted_recovery() -> None:
    client = authed_client()

    response = client.post(
        "/api/corpus/reconcile",
        json={"apply": True, "recover": True},
    )

    assert response.status_code == 200
    events = app.state.access_store.snapshot()["audit_events"]
    reconcile_events = [event for event in events if event["action"] == "corpus.reconcile"]
    assert reconcile_events[-1]["detail"]["recover"] is True


def test_failed_zero_chunk_apply_is_a_failed_api_operation() -> None:
    class FailingReconcile(FakeCitadel):
        async def reconcile_corpus(
            self,
            *,
            dataset: Any = None,
            apply: bool = False,
            force: bool = False,
        ) -> dict[str, Any]:
            return {
                "ok": False,
                "dataset": dataset,
                "apply": apply,
                "force": force,
                "reason": "reconciliation_invariants_remain",
                "repair_operation_id": "repair-op-123",
                "repair_phase": "post_index_check",
                "repair_journal_error": "OSError",
                "post_repair_indexed": False,
                "post_repair_stored_budget_ok": True,
            }

    client = authed_client()
    app.state.citadel = FailingReconcile()

    response = client.post("/api/corpus/reconcile", json={"apply": True})

    assert response.status_code == 503
    assert response.json()["detail"]["result"]["reason"] == "reconciliation_invariants_remain"
    events = app.state.access_store.snapshot()["audit_events"]
    reconcile_events = [event for event in events if event["action"] == "corpus.reconcile"]
    assert reconcile_events[-1]["success"] is False
    assert reconcile_events[-1]["detail"] == {
        "operation": "corpus.reconcile",
        "apply": True,
        "force": False,
        "oversized": False,
        "ok": False,
        "reason": "reconciliation_invariants_remain",
        "repair_required": None,
        "repair_operation_id": "repair-op-123",
        "repair_phase": "post_index_check",
        "repair_journal_error": "OSError",
        "post_repair_indexed": False,
        "post_repair_stored_budget_ok": True,
    }


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


def test_ingest_inline_cognify_waits_for_its_lifecycle_operation() -> None:
    class LifecycleCitadel(FakeCitadel):
        legacy_cognify_calls = 0

        async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
            return IngestResult(
                True,
                "queued_not_confirmed",
                kwargs["dataset"] or "notes",
                tuple(kwargs["tags"]),
                source_revision_id="source-1",
                projection_job_id="job-1",
                projection_state="pending",
            )

        async def wait_for_lifecycle_operation(
            self,
            projection_job_id: str,
        ) -> dict[str, Any]:
            assert projection_job_id == "job-1"
            return {"projection_job_id": projection_job_id, "state": "searchable"}

        async def cognify_dataset(self, **kwargs: Any) -> dict[str, Any]:
            self.legacy_cognify_calls += 1
            return {"ok": True}

    client = authed_client()
    citadel = LifecycleCitadel()
    app.state.citadel = citadel

    response = client.post(
        "/ingest",
        json={"data": "lifecycle note", "tags": [], "cognify": True},
    )

    assert response.status_code == 200
    assert response.json()["cognified"] is True
    assert response.json()["projection_state"] == "searchable"
    assert citadel.legacy_cognify_calls == 0


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
    """The dashboard stays behind auth; anonymous callers are sent to /login.

    The dashboard moved from / to /app when the root became the landing page.
    What matters here is unchanged: an anonymous caller never receives
    index.html.
    """
    app.state.citadel = FakeCitadel()
    client = TestClient(app)

    response = client.get("/app", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "graphCanvas" not in response.text


def test_root_is_the_public_landing_page_for_everyone() -> None:
    """/ never serves the app, signed in or not.

    One URL, one body: a member must be able to open the landing page and send
    the link on instead of being bounced into the dashboard.
    """
    app.state.citadel = FakeCitadel()

    anon = TestClient(app).get("/")
    member = authed_client().get("/")

    for response in (anon, member):
        assert response.status_code == 200
        assert 'id="graphCanvas"' not in response.text
        assert "Citadel remembers your" in response.text


def test_ui_shell_is_served_after_login() -> None:
    client = authed_client()

    response = client.get("/app")

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


STATIC_DIR = Path(server_module.__file__).resolve().parent / "static"


def test_app_nav_has_five_primary_entries() -> None:
    """Home, Search, Graph, Review, Admin. Nothing else is a primary nav entry.

    Seven entries were the problem the redesign exists to fix: two of them
    nobody could tell apart, and the promotion queue that people actually needed
    was buried inside an admin-only Overview.

    This asserted four for exactly that reason, on the stated premise that the
    non-resident pages were "still reachable by route". For the Knowledge page
    that premise was false: it had no nav entry, no setPage() call and no hash
    link anywhere, so the graph was reachable only by typing the URL fragment
    and to every user it simply did not exist, while /api/mesh/graph was serving
    932 nodes in ~300ms. Graph is now resident, at the owner's request.

    Five is still a cap, not an invitation. The lesson of the seven-entry
    version stands: anything added here has to earn residency, and a page that
    cannot must at least be reachable from somewhere, which
    test_every_dashboard_page_is_reachable_from_the_ui now enforces.
    """
    client = authed_client()

    page = client.get("/app").text

    nav = re.search(r'<nav class="side-nav".*?</nav>', page, re.S)
    assert nav, "the /app shell no longer renders a <nav class=\"side-nav\">"
    labels = re.findall(r'<span class="nav-label">([^<]+)</span>', nav.group(0))

    assert labels == ["Home", "Search", "Graph", "Review", "Admin"], labels
    assert 'class="nav-link" data-page-target="overview"' not in page
    assert 'class="nav-link" data-page-target="ingest"' not in page
    # Review is writer-gated; Admin stays admin-gated.
    assert 'data-page-target="review" aria-label="Review" data-min-role="writer"' in nav.group(0)
    assert 'data-page-target="agents" aria-label="Admin" data-min-role="admin"' in nav.group(0)


def test_app_defaults_to_light() -> None:
    """Light on :root, dark under [data-theme="dark"] — not the reverse.

    The app and the public site are one product. If the dashboard kept its dark
    base and merely offered a light override, an OS-dark visitor would still get
    two different-looking products, which is the thing this replaced.
    """
    css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    root = re.search(r"^:root \{(.*?)^\}", css, re.S | re.M)
    dark = re.search(r'^:root\[data-theme="dark"\] \{(.*?)^\}', css, re.S | re.M)
    assert root and dark, "styles.css no longer declares both theme blocks"

    assert "color-scheme: light;" in root.group(1)
    assert "--surface: #ffffff;" in root.group(1)
    assert "--bg: #fafafa;" in root.group(1)
    assert "--text: #0a0a0a;" in root.group(1)

    assert "color-scheme: dark;" in dark.group(1)
    assert "--surface: #171717;" in dark.group(1)
    assert "--bg: #0a0a0a;" in dark.group(1)

    # prefers-color-scheme is deliberately not consulted: dark is an explicit,
    # remembered choice, exactly as on the public pages.
    assert "@media (prefers-color-scheme" not in css


def test_app_and_site_share_a_theme_key() -> None:
    """One storage key, so a theme chosen on the landing page survives the door.

    Two keys would mean picking dark on / and being handed a white dashboard a
    click later.
    """
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    info_js = (STATIC_DIR / "info.js").read_text(encoding="utf-8")

    assert "citadel-info-theme" in app_js
    assert "citadel-info-theme" in info_js
    # Same default on both sides: light when the key is absent.
    assert 'getAttribute("data-theme") || "light"' in app_js
    assert 'getAttribute("data-theme") || "light"' in info_js


def test_app_home_leads_with_search_and_three_numbers() -> None:
    """Home is search bar, three numbers, Needs you, Recent. No charts, no graph.

    The front page used to be an admin-only Overview dominated by a graph nobody
    could read, and human search was a nav item you had to go and find.
    """
    client = authed_client()

    page = client.get("/app").text
    home = re.search(r'<section class="page page-active" data-page="home".*?\n          </section>', page, re.S)
    assert home, "the shell no longer renders a home page section"
    home_markup = home.group(0)

    assert 'id="homeSearchForm"' in home_markup
    assert 'id="homeSearchQuery"' in home_markup
    for number in ("homeReadable", "homeCapturedWeek", "homeWaiting"):
        assert f'id="{number}"' in home_markup, number
    assert "Notes you can read" in home_markup
    assert "Captured this week" in home_markup
    assert "Waiting on you" in home_markup
    assert 'id="homeNeedsList"' in home_markup
    assert 'id="homeActivityList"' in home_markup
    # The title row carries the theme toggle, and Recent links on to Activity.
    assert 'id="homeThemeToggle"' in home_markup
    assert 'data-page-target="events"' in home_markup
    # The old seat checklist and the overview charts are not on the front page.
    assert 'id="homeChecklist"' not in home_markup
    assert "overviewChart" not in home_markup
    assert 'id="graphCanvas"' not in home_markup


def test_search_view_groups_central_before_node() -> None:
    """The web groups hits exactly as `_render_search` does in the CLI.

    Two surfaces over one vault have to teach one mental model: Central first,
    then shared traces, then your Node. If the CLI order changes, this fails.
    """
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    cli_py = Path(server_module.__file__).resolve().parent / "cli.py"
    cli = cli_py.read_text(encoding="utf-8")

    web_order = re.search(r"const SEARCH_SECTION_ORDER = \[(.*?)\];", app_js, re.S)
    cli_order = re.search(r"_SEARCH_SECTION_ORDER = \((.*?)\)", cli, re.S)
    assert web_order and cli_order, "the section order constant moved"

    web = re.findall(r'"([a-z_]+)"', web_order.group(1))
    cli_sections = re.findall(r'"([a-z_]+)"', cli_order.group(1))

    assert web == cli_sections, (web, cli_sections)
    assert web.index("central") < web.index("node")
    assert "isSingleLiteralQuery(query)" in app_js
    assert "renderSearchResults(returned, response, query)" in app_js
    assert "Search results" in authed_client().get("/app").text


def test_review_is_the_only_place_with_approve_and_reject() -> None:
    """Approve and Reject live on Review, and only admins get the buttons.

    /api/promotion/pending/{id}/approve requires admin + sources:sync, so
    rendering the button for a writer would offer an action that always 403s.
    """
    client = authed_client()

    page = client.get("/app").text
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert 'data-page="review" data-min-role="writer"' in page
    assert 'id="reviewQueueList"' in page
    assert 'id="reviewSourceList"' in page

    row = re.search(
        r"function promotionNeedsRow\(item\) \{(.*?)\n\}", app_js, re.S
    )
    assert row, "promotionNeedsRow moved"
    body = row.group(1)
    assert 'canUse("admin")' in body
    assert 'canUse("writer")' not in body
    assert '"Approve"' in body and '"Reject"' in body


def test_knowledge_mesh_canvas_is_reachable_from_the_knowledge_page() -> None:
    """The mesh canvas must live on a page the nav actually links to.

    It was markup inside the `overview` section. When the nav collapsed to four
    entries, `overview` was unlinked and `initialPage()` stopped returning it, so
    the production Knowledge Mesh became reachable only by typing #overview and
    only as an admin. Nothing caught it because the URL still resolved and the
    ids still existed, which is exactly why this asserts the owning page rather
    than the presence of the ids.
    """
    from html.parser import HTMLParser

    void = {"meta", "link", "br", "hr", "img", "input", "source", "path", "circle", "use"}

    class _Ancestry(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[tuple[str, str | None]] = []
            self.owning_page: str | None = None
            self.seen = False

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            data = dict(attrs)
            if data.get("id") == "graphCanvas":
                self.seen = True
                pages = [page for _, page in self.stack if page]
                self.owning_page = pages[-1] if pages else None
            if tag not in void:
                self.stack.append((tag, data.get("data-page")))

        def handle_endtag(self, tag: str) -> None:
            if tag in void:
                return
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

    client = authed_client()
    page = client.get("/app").text

    parser = _Ancestry()
    parser.feed(page)

    assert parser.seen, "/app no longer ships the mesh canvas at all"
    assert parser.owning_page == "knowledge", (
        f"the mesh canvas sits on the '{parser.owning_page}' page. It must be on "
        "'knowledge', which the nav links to; 'overview' is unlinked, so putting "
        "it there makes the graph reachable only by typing the hash."
    )
    # The rest of the canvas furniture has to travel with it, or the toolbar and
    # the inspector end up on a page the canvas is no longer on.
    for element_id in (
        "mesh",
        "meshAlert",
        "graphLegend",
        "selectedNode",
        "fitButton",
        "pauseButton",
        "graphDepthInput",
        "canvasEmpty",
        "realGraphEmpty",
        "graphMeta",
    ):
        assert page.count(f'id="{element_id}"') == 1, element_id


def test_graph_inspector_resolves_one_hop_document_neighbors() -> None:
    """#186: entity inspection may fall through to readable graph neighbors.

    The legacy dashboard is a hand-written browser script with no DOM test
    runner. This static contract keeps the bounded client behavior explicit;
    `node --check kb/static/app.js` remains the syntax check for the script.
    """
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    bearing = re.search(
        r"function isDocumentBearingNode\(node\) \{(.*?)\n\}", app_js, re.S
    )
    candidates = re.search(
        r"function documentCandidates\(node\) \{(.*?)\n\}", app_js, re.S
    )
    loader = re.search(
        r"async function loadNodeDocument\(node\) \{(.*?)\n\}", app_js, re.S
    )
    assert bearing and candidates and loader

    bearing_body = bearing.group(1)
    assert 'type.includes("document")' in bearing_body
    assert 'type.includes("chunk")' in bearing_body
    assert 'type.includes("summary")' in bearing_body

    candidate_body = candidates.group(1)
    assert "add(node)" in candidate_body
    assert "knowledgeNeighbors(node.id)" in candidate_body
    assert "seen.has(id)" in candidate_body

    loader_body = loader.group(1)
    assert "documentCandidates(node)" in loader_body
    assert "candidate.node.id" in loader_body
    assert "state.selectedId !== node.id" in loader_body
    assert "error?.status === 404" in loader_body
    assert "/not found/i.test(message)" in loader_body


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
        lifecycle_store_path=str(tmp_path / "lifecycle.sqlite3"),
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


def test_obsidian_delete_tombstones_lifecycle_source(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    client = authed_client("test-writer")
    citadel = app.state.citadel
    vault_id = client.post(
        "/api/obsidian/vaults",
        json={"vault_name": "Delete Vault"},
    ).json()["vault"]["id"]
    first = client.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "dataset": "notes",
            "documents": [{"path": "Old.md", "content": "old body"}],
        },
    )
    revision = first.json()["accepted"][0]["rev"]

    deleted = client.post(
        "/api/obsidian/sync/push",
        json={
            "vault_id": vault_id,
            "dataset": "notes",
            "documents": [
                {
                    "path": "Old.md",
                    "content": "",
                    "base_rev": revision,
                    "deleted": True,
                }
            ],
        },
    )

    assert deleted.status_code == 200
    assert deleted.json()["ingest_results"][0]["reason"] == "queued_not_confirmed"
    assert citadel.tombstone_calls[0]["source_key"] == f"obsidian:{vault_id}:Old.md"
    assert citadel.tombstone_calls[0]["dataset"] == "notes"


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


def test_readyz_reports_503_when_corpus_measurement_fails() -> None:
    class BrokenGraphCitadel(FakeCitadel):
        async def _graph_counts(self) -> dict[str, int]:
            raise RuntimeError("graph unavailable")

    class HealthySyncer:
        async def status(self) -> dict[str, Any]:
            return {"tracked_repositories": 50, "tracked_files": 0, "issue_count": 0}

    client = authed_client("test-reader")
    app.state.citadel = BrokenGraphCitadel()
    app.state.github_syncer = HealthySyncer()
    app.state.repo_content_syncer = HealthySyncer()
    app.state.linear_syncer = HealthySyncer()

    ready = client.get("/readyz")

    assert ready.status_code == 503
    assert ready.json()["ok"] is False
    assert ready.json()["corpus"] == {
        "ok": False,
        "tracked_sources": None,
        "indexed_docs": None,
        "indexed_edges": None,
        "degraded": "graph unavailable",
    }


def test_readyz_does_not_use_graph_nodes_as_projection_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MeasuredCorpus:
        async def corpus_health(self, *, limit: int) -> dict[str, Any]:
            assert limit == server_module._CORPUS_HEALTH_PROBE_LIMIT
            return {
                "relational_documents": 2,
                "probe_limit": 64,
                "probe_max_documents": 10000,
                "probe_documents": 2,
                "probe_pages": 1,
                "probe_complete": True,
                "probe_cap_exceeded": False,
                "probe_chunked_documents": 0,
                "probe_graph_documents": 2,
                "probe_fully_indexed_documents": 0,
                "probe_ok": False,
            }

    class GraphOnlyCitadel(FakeCitadel):
        def __init__(self) -> None:
            self.cognee = MeasuredCorpus()

        async def _graph_counts(self) -> dict[str, int]:
            return {"nodes": 280, "edges": 514}

    class BusySyncer:
        async def status(self) -> dict[str, Any]:
            return {"tracked_repositories": 50, "tracked_files": 0, "issue_count": 0}

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    client = authed_client("test-reader")
    app.state.citadel = GraphOnlyCitadel()
    app.state.github_syncer = BusySyncer()
    app.state.repo_content_syncer = BusySyncer()
    app.state.linear_syncer = BusySyncer()

    ready = client.get("/readyz")

    assert ready.status_code == 503
    corpus = ready.json()["corpus"]
    assert corpus["indexed_docs"] == 280
    assert corpus["probe_fully_indexed_documents"] == 0
    assert corpus["probe_ok"] is False


def test_readyz_rejects_a_partial_corpus_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PartialCorpus:
        async def corpus_health(self, *, limit: int) -> dict[str, Any]:
            assert limit == server_module._CORPUS_HEALTH_PROBE_LIMIT
            return {
                "relational_documents": 65,
                "probe_limit": 64,
                "probe_max_documents": 10000,
                "probe_documents": 64,
                "probe_pages": 1,
                "probe_complete": False,
                "probe_cap_exceeded": False,
                "probe_chunked_documents": 64,
                "probe_graph_documents": 64,
                "probe_fully_indexed_documents": 64,
                "probe_ok": True,
            }

    class PartialCitadel(FakeCitadel):
        def __init__(self) -> None:
            self.cognee = PartialCorpus()

        async def _graph_counts(self) -> dict[str, int]:
            return {"nodes": 280, "edges": 514}

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    client = authed_client("test-reader")
    app.state.citadel = PartialCitadel()

    ready = client.get("/readyz")

    assert ready.status_code == 503
    assert ready.json()["ok"] is False
    assert ready.json()["corpus"]["probe_complete"] is False
    assert ready.json()["corpus"]["probe_ok"] is True


def test_readyz_accepts_a_complete_multi_page_corpus_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MultiPageCorpus:
        async def corpus_health(self, *, limit: int) -> dict[str, Any]:
            assert limit == server_module._CORPUS_HEALTH_PROBE_LIMIT
            return {
                "relational_documents": 65,
                "probe_limit": 64,
                "probe_max_documents": 10_000,
                "probe_documents": 65,
                "probe_pages": 2,
                "probe_complete": True,
                "probe_cap_exceeded": False,
                "probe_chunked_documents": 65,
                "probe_graph_documents": 65,
                "probe_fully_indexed_documents": 65,
                "probe_ok": True,
            }

    class MultiPageCitadel(FakeCitadel):
        def __init__(self) -> None:
            self.cognee = MultiPageCorpus()

        async def _graph_counts(self) -> dict[str, int]:
            return {"nodes": 65, "edges": 64}

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_TASK", None)
    client = authed_client("test-reader")
    app.state.citadel = MultiPageCitadel()

    ready = client.get("/readyz")

    assert ready.status_code == 200
    assert ready.json()["corpus"]["probe_documents"] == 65


def test_readyz_rejects_inconsistent_complete_corpus_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InconsistentCorpus:
        async def corpus_health(self, *, limit: int) -> dict[str, Any]:
            assert limit == server_module._CORPUS_HEALTH_PROBE_LIMIT
            return {
                "relational_documents": 100,
                "probe_limit": 64,
                "probe_max_documents": 10000,
                "probe_documents": 0,
                "probe_pages": 1,
                "probe_complete": True,
                "probe_cap_exceeded": False,
                "probe_chunked_documents": 0,
                "probe_graph_documents": 0,
                "probe_fully_indexed_documents": 0,
                "probe_ok": True,
            }

    class InconsistentCitadel(FakeCitadel):
        def __init__(self) -> None:
            self.cognee = InconsistentCorpus()

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    client = authed_client("test-reader")
    app.state.citadel = InconsistentCitadel()

    ready = client.get("/readyz")

    assert ready.status_code == 503
    assert ready.json()["corpus"] == {
        "ok": False,
        "tracked_sources": None,
        "indexed_docs": None,
        "indexed_edges": None,
        "degraded": "complete bounded corpus probe does not cover the relational document total",
    }


def test_corpus_health_cache_is_shared_by_readyz_and_mesh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class MeasuredCorpus:
        async def corpus_health(self, *, limit: int) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            assert limit == server_module._CORPUS_HEALTH_PROBE_LIMIT
            return {
                "relational_documents": 1,
                "probe_limit": 64,
                "probe_max_documents": 10000,
                "probe_documents": 1,
                "probe_pages": 1,
                "probe_complete": True,
                "probe_cap_exceeded": False,
                "probe_chunked_documents": 1,
                "probe_graph_documents": 1,
                "probe_fully_indexed_documents": 1,
                "probe_ok": True,
            }

    class MeasuredCitadel(FakeCitadel):
        def __init__(self) -> None:
            self.cognee = MeasuredCorpus()

        async def _graph_counts(self) -> dict[str, int]:
            return {"nodes": 280, "edges": 514}

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    client = authed_client("test-reader")
    app.state.citadel = MeasuredCitadel()

    assert client.get("/readyz").status_code == 200
    assert client.get("/api/mesh").status_code == 200
    assert calls == 1


async def test_corpus_health_deduplicates_concurrent_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class Syncer:
        async def status(self) -> dict[str, Any]:
            return {"tracked_repositories": 1, "tracked_files": 0, "issue_count": 0}

    class MeasuredCorpus:
        async def corpus_health(self, *, limit: int) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {
                "relational_documents": 1,
                "probe_limit": limit,
                "probe_max_documents": 10000,
                "probe_documents": 1,
                "probe_pages": 1,
                "probe_complete": True,
                "probe_cap_exceeded": False,
                "probe_chunked_documents": 1,
                "probe_graph_documents": 1,
                "probe_fully_indexed_documents": 1,
                "probe_ok": True,
            }

    class MeasuredCitadel(FakeCitadel):
        def __init__(self) -> None:
            self.cognee = MeasuredCorpus()

        async def _graph_counts(self) -> dict[str, int]:
            return {"nodes": 1, "edges": 1}

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    app.state.citadel = MeasuredCitadel()
    app.state.github_syncer = Syncer()
    app.state.repo_content_syncer = Syncer()
    app.state.linear_syncer = Syncer()

    first, second = await asyncio.gather(
        server_module._corpus_health(), server_module._corpus_health()
    )

    assert calls == 1
    assert first == second


def test_readyz_rejects_measured_empty_corpus_when_sources_are_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyMeasuredCorpus:
        async def corpus_health(self, *, limit: int) -> dict[str, Any]:
            assert limit == server_module._CORPUS_HEALTH_PROBE_LIMIT
            return {
                "relational_documents": 0,
                "probe_limit": 64,
                "probe_max_documents": 10000,
                "probe_documents": 0,
                "probe_pages": 0,
                "probe_complete": True,
                "probe_cap_exceeded": False,
                "probe_chunked_documents": 0,
                "probe_graph_documents": 0,
                "probe_fully_indexed_documents": 0,
                "probe_ok": True,
            }

    class MeasuredEmptyCitadel(FakeCitadel):
        def __init__(self) -> None:
            self.cognee = EmptyMeasuredCorpus()

    class BusySyncer:
        async def status(self) -> dict[str, Any]:
            return {"tracked_repositories": 50, "tracked_files": 0, "issue_count": 0}

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    client = authed_client("test-reader")
    app.state.citadel = MeasuredEmptyCitadel()
    app.state.github_syncer = BusySyncer()
    app.state.repo_content_syncer = BusySyncer()
    app.state.linear_syncer = BusySyncer()

    ready = client.get("/readyz")

    assert ready.status_code == 503
    assert ready.json()["corpus"]["probe_ok"] is True
    assert ready.json()["corpus"]["ok"] is False


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


def test_single_literal_search_preserves_each_dataset_candidate() -> None:
    """Literal identifiers must survive the cross-dataset merge (#106)."""
    import asyncio as aio

    from kb.server import search_across_datasets

    class LiteralCitadel:
        config = FakeCitadel.config

        async def search(
            self, query: str, *, dataset: str, session_id: Any, top_k: int
        ) -> list[Any]:
            if dataset == "central":
                return [
                    {"id": "central-1", "text": "unrelated central note"},
                    {"id": "central-2", "text": "another central note"},
                ][:top_k]
            return [
                {"id": "node-1", "text": "unrelated node note"},
                {"id": "node-2", "text": "quokka-beacon-8823"},
            ][:top_k]

    merged = aio.run(
        search_across_datasets(
            LiteralCitadel(),
            query="quokka-beacon-8823",
            datasets=["central", "node"],
            sessions={},
            top_k=2,
        )
    )

    assert [result[1]["id"] for result in merged] == [
        "central-1",
        "central-2",
        "node-1",
        "node-2",
    ]


def test_search_single_literal_query_ranks_cross_dataset_match(monkeypatch: Any) -> None:
    """The API ranks the exact token before unrelated returned hits (#106)."""
    async def fake_search(*args: Any, **kwargs: Any) -> tuple[list[tuple[str, Any]], bool]:
        return [
            ("central", {"id": "central", "text": "unrelated central note"}),
            ("node", {"id": "node", "text": "quokka-beacon-8823"}),
        ], False

    monkeypatch.setattr(server_module, "_search_within_budget", fake_search)
    response = authed_client().post(
        "/search", json={"query": "quokka-beacon-8823", "top_k": 2}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["id"] == "node"
    assert body["results"][0]["_citadel"]["rank"] == 1


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


def test_search_query_length_is_bounded_like_its_sibling_filters() -> None:
    """`query` is capped at the model boundary, as repo/path/mode already are.

    The cap sits far above anything the product asks: the longest query in the
    bench corpus is 99 characters and the longest in this suite is 38, against
    a 2000 character ceiling. Everything a caller would plausibly type is still
    accepted; only an unbounded body is refused, and refused before the request
    reaches any of the work /search does per query.
    """
    client = authed_client("test-reader")

    # Ordinary queries are untouched, including a long natural-language one.
    for query in (
        "q",
        "MIP-003 availability type masumi-agent",
        "What is the full postgres connection string for the production database?",
        "policy + asset " * 20,
    ):
        assert len(query) <= server_module.MAX_SEARCH_QUERY_LENGTH
        assert client.post("/search", json={"query": query}).status_code == 200, query

    # Exactly at the cap is still a valid query.
    at_cap = "a" * server_module.MAX_SEARCH_QUERY_LENGTH
    assert client.post("/search", json={"query": at_cap}).status_code == 200

    # One character over is rejected by validation, not served.
    over_cap = "a" * (server_module.MAX_SEARCH_QUERY_LENGTH + 1)
    rejected = client.post("/search", json={"query": over_cap})
    assert rejected.status_code == 422

    # The shape that costs the most to match is refused at the same boundary,
    # so no amount of it reaches the query classifiers.
    whitespace_heavy = client.post(
        "/search", json={"query": "policy" + " " * server_module.MAX_SEARCH_QUERY_LENGTH}
    )
    assert whitespace_heavy.status_code == 422

    # An empty query is still rejected too — the lower bound did not move.
    assert client.post("/search", json={"query": ""}).status_code == 422


def test_search_timeout_budget_is_a_typed_failure() -> None:
    # An incomplete provider call is not evidence that the dataset is empty.
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
    assert r.status_code == 504
    assert r.json()["detail"] == {
        "code": "SEARCH_TIMEOUT",
        "message": "Search exceeded the configured server budget.",
    }


def test_search_genuine_empty_is_a_normal_success() -> None:
    class EmptyCitadel(FakeCitadel):
        async def search(self, query: str, **kwargs: Any) -> list[Any]:
            return []

    client = authed_client("test-reader")
    app.state.citadel = EmptyCitadel()

    response = client.post("/search", json={"query": "absent", "top_k": 3})

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert "code" not in body
    assert body.get("timed_out") in (None, False)


def test_search_qdrant_outage_is_a_typed_failure() -> None:
    class UnavailableCitadel(FakeCitadel):
        async def search(self, query: str, **kwargs: Any) -> list[Any]:
            raise server_module.QdrantProviderError("qdrant secret detail")

    client = authed_client("test-reader")
    app.state.citadel = UnavailableCitadel()

    response = client.post("/search", json={"query": "q", "top_k": 3})

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "QDRANT_UNAVAILABLE",
        "message": "Qdrant is unavailable.",
    }
    assert "secret detail" not in response.text


def _outage_adapter(generation: str) -> Any:
    """A real Qdrant adapter whose every call hits an unresolvable host.

    The client raises the SAME qdrant_client exception a stopped container
    produces, so ``_collection_exists`` wraps it in the real
    QdrantProviderError rather than a stand-in the adapter never emits.
    """
    from qdrant_client.http.exceptions import ResponseHandlingException

    from kb.qdrant_adapter import CitadelQdrantAdapter

    class _DeadClient:
        async def collection_exists(self, collection_name: str) -> bool:
            del collection_name
            raise ResponseHandlingException("Name or service not known")

        async def close(self) -> None:
            return None

    class _Embedding:
        async def embed_text(self, values: list[str]) -> list[list[float]]:
            return [[1.0] for _ in values]

        def get_vector_size(self) -> int:
            return 1

        def get_batch_size(self) -> int:
            return 16

    return CitadelQdrantAdapter(
        url="http://127.0.0.1:6333",
        api_key="test-key",
        embedding_engine=_Embedding(),
        database_name=generation,
        client_factory=_DeadClient,
    )


def test_search_provider_outage_inside_the_fanout_is_a_typed_failure(
    monkeypatch,
) -> None:
    """A route mapping is worth only as much as the layer below it.

    The existing outage test fakes ``Citadel.search`` itself, which cannot show
    whether anything between the adapter and the route degrades the error. Run
    the REAL adapter inside a fan-out arm, with a non-empty lifecycle document
    scope so the search genuinely reaches the vector call, and assert the typed
    503 still arrives.
    """
    from kb.qdrant_adapter import qdrant_document_scope, qdrant_scope

    monkeypatch.delenv("CITADEL_GENERATION_ID", raising=False)
    generation = "outage-generation"
    adapter = _outage_adapter(generation)

    class OutageCitadel(FakeCitadel):
        async def search(self, query: str, **kwargs: Any) -> list[Any]:
            del kwargs
            with qdrant_scope(
                mode="read", generation_id=generation, dataset="notes"
            ):
                with qdrant_document_scope(["searchable-revision-1"]):
                    return await adapter.search(
                        "DocumentChunk_text", query_text=query
                    )

    client = authed_client("test-reader")
    app.state.citadel = OutageCitadel()

    response = client.post("/search", json={"query": "q", "top_k": 3})

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "QDRANT_UNAVAILABLE",
        "message": "Qdrant is unavailable.",
    }
    assert "Name or service not known" not in response.text


def test_knowledge_alias_timeout_is_a_typed_failure(monkeypatch) -> None:
    async def timeout(*_args: Any, **_kwargs: Any) -> tuple[list[Any], bool]:
        return [], True

    monkeypatch.setattr(server_module, "_search_within_budget", timeout)
    client = authed_client("test-reader")

    response = client.get("/api/knowledge", params={"q": "absent"})

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "SEARCH_TIMEOUT"


def test_knowledge_alias_qdrant_outage_is_a_typed_failure(monkeypatch) -> None:
    async def unavailable(*_args: Any, **_kwargs: Any) -> tuple[list[Any], bool]:
        raise server_module.QdrantProviderError("provider detail")

    monkeypatch.setattr(server_module, "_search_within_budget", unavailable)
    client = authed_client("test-reader")

    response = client.get("/api/knowledge", params={"q": "q"})

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "QDRANT_UNAVAILABLE"
    assert "provider detail" not in response.text


def test_document_endpoint_for_result_covers_real_ids_only() -> None:
    # #28: ghsync/doc_/cognee-UUID ids are drillable; synthetic chunk: ids are not.
    from kb.server import document_endpoint_for_result

    uuid = "9dbe579d-eccb-51b6-9bba-13982cbaf69f"
    assert document_endpoint_for_result("ghsync:abc") == "/api/documents/ghsync:abc"
    assert document_endpoint_for_result("doc_123") == "/api/documents/doc_123"
    assert document_endpoint_for_result(uuid) == f"/api/documents/{uuid}"
    assert document_endpoint_for_result(
        "chunk-uuid", document_id="document-uuid"
    ) == "/api/documents/document-uuid"
    assert document_endpoint_for_result(
        "chunk:deadbeef", document_id="document-uuid"
    ) == "/api/documents/document-uuid"
    assert document_endpoint_for_result("../../api/access") == (
        "/api/documents/..%2F..%2Fapi%2Faccess"
    )
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


def test_search_drilldown_prefers_parent_document_id_for_chunk_hit(
    tmp_path: Any,
) -> None:
    class ParentDocumentSearchCitadel(DrilldownIsolationCitadel):
        async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "id": "chunk-b",
                    "document_id": "doc-b",
                    "text": "bob chunk text",
                }
            ]

    app.state.access_store = AccessStore(tmp_path / "access.json")
    app.state.obsidian_sync = ObsidianSyncStore(tmp_path / "obsidian.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    app.state.citadel = ParentDocumentSearchCitadel()
    app.state.knowledge_mesh = KnowledgeMesh(IsolationDatasetGateway())
    api = TestClient(app, base_url="https://testserver")
    bob = {"Authorization": f"Bearer {bob_token}"}
    try:
        response = api.post("/search", json={"query": "bob", "top_k": 1}, headers=bob)
        hit = response.json()["results"][0]
        endpoint = hit["_citadel"]["document_endpoint"]
        document = api.get(endpoint, headers=bob)
    finally:
        app.state.knowledge_mesh = None

    assert response.status_code == 200
    assert hit["_citadel"]["result_id"] == "chunk-b"
    assert hit["document_id"] == "doc-b"
    assert endpoint == "/api/documents/doc-b"
    assert hit["_citadel"]["retrieval"]["document_drilldown_available"] is True
    assert document.status_code == 200
    assert document.json()["document"]["body"] == "bob text"


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

    async def resolve_document_owner_ids(self, document_id: str) -> list[str] | None:
        # Mirrors the real service contract: the owner ids get_document would
        # carry in dataset_node_ids, None when the id would not resolve.
        document = self.cognee_documents.get(document_id)
        if document is None:
            return None
        return list(document.get("dataset_node_ids") or [document_id])


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


class DrilldownCountingCitadel(DrilldownSearchCitadel):
    """Counts which resolution surface /search actually pays for per hit."""

    def __init__(self) -> None:
        self.get_document_calls: list[str] = []
        self.owner_id_calls: list[str] = []

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        self.get_document_calls.append(document_id)
        return await super().get_document(document_id)

    async def resolve_document_owner_ids(self, document_id: str) -> list[str] | None:
        self.owner_id_calls.append(document_id)
        return await super().resolve_document_owner_ids(document_id)


def test_search_hint_never_assembles_document_bodies(tmp_path: Any) -> None:
    # The hint needs one boolean per unique id, so /search must resolve owner
    # ids through the seed-only read — never get_document, whose chunk-store
    # fallback probes 512 sibling ids and assembles up to ~1MB of body per
    # graph-missing hit, all discarded. This is the regression test whose
    # absence let that cost ship: it fails the moment /search touches
    # get_document again.
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    citadel = DrilldownCountingCitadel()
    app.state.citadel = citadel
    app.state.knowledge_mesh = KnowledgeMesh(IsolationDatasetGateway())
    api = TestClient(app, base_url="https://testserver")
    try:
        body = api.post(
            "/search",
            json={"query": "x"},
            headers={
                "Authorization": f"Bearer {bob_token}",
                "x-citadel-mcp-tool": "citadel_search",
            },
        ).json()
    finally:
        app.state.knowledge_mesh = None

    # The hint itself stays correct...
    assert _search_hint(body) == {
        "doc-c": True,
        "doc-a": False,
        "chunk-b": True,
        "entity-x": False,
    }
    # ...but no document body was assembled to compute it: one owner-id
    # resolution per unique cognee id, zero get_document calls.
    assert citadel.get_document_calls == []
    assert sorted(citadel.owner_id_calls) == [
        "chunk-b",
        "doc-a",
        "doc-c",
        "entity-x",
    ]
    # The audit row exposes the pass's cost instead of stopping the clock
    # before it (the pre-fix latency_ms was measured before the drill-down
    # pass, under-reporting exactly what it added).
    events = app.state.access_store.snapshot()["audit_events"]
    search_events = [
        event for event in events if event["detail"].get("operation") == "search"
    ]
    assert search_events
    assert isinstance(search_events[-1]["detail"].get("drilldown_ms"), (int, float))
    assert search_events[-1]["detail"]["latency_ms"] >= (
        search_events[-1]["detail"]["drilldown_ms"]
    )


def test_search_hint_pass_denies_once_its_deadline_expires(tmp_path: Any) -> None:
    # The pass runs outside _search_within_budget and the _SearchSlot, so it
    # carries its own deadline: a hung owner-id read is cancelled at the
    # budget, the id denies (the safe under-promise), and the remaining ids
    # deny without being attempted — the request never hangs on the hint.
    class _HungResolverCitadel(DrilldownCountingCitadel):
        config = CitadelConfig(
            tenant_id="test",
            default_dataset="notes",
            admin_key="test-admin",
            reader_keys=("test-reader",),
            writer_keys=("test-writer",),
            search_timeout_seconds=0.2,
        )

        async def resolve_document_owner_ids(
            self, document_id: str
        ) -> list[str] | None:
            self.owner_id_calls.append(document_id)
            await asyncio.sleep(30)  # cancelled by the pass deadline
            return None

    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    bob_token = admin.post(
        "/api/access/seats", json={"name": "Bob", "slug": "bob"}
    ).json()["token"]
    citadel = _HungResolverCitadel()
    app.state.citadel = citadel
    app.state.knowledge_mesh = KnowledgeMesh(IsolationDatasetGateway())
    api = TestClient(app, base_url="https://testserver")
    try:
        body = api.post(
            "/search",
            json={"query": "x"},
            headers={"Authorization": f"Bearer {bob_token}"},
        ).json()
    finally:
        app.state.knowledge_mesh = None

    assert _search_hint(body) == {
        "doc-c": False,
        "doc-a": False,
        "chunk-b": False,
        "entity-x": False,
    }
    # At most the first id was ever attempted; the rest were denied at the
    # already-expired deadline without a call.
    assert len(citadel.owner_id_calls) <= 1


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


def test_recent_contributions_lists_an_mcp_ingest(tmp_path: Any) -> None:
    """A write through MCP is a contribution, and the feed must show it.

    /ingest from the MCP surface records ONLY ``mcp.citadel_ingest`` (the
    durable ``ingest`` row is suppressed for MCP requests), and the feed
    selected the single action ``contribute`` — so a seat that ingested through
    MCP saw an empty "recent contributions" list right after a write that was
    independently proven to exist.
    """
    store = AccessStore(str(tmp_path / "access.json"))
    app.state.access_store = store
    writer = authed_client("test-writer")

    ingested = writer.post(
        "/ingest",
        json={"data": "Runbook: rotate the sync token before minting seats.", "tags": ["runbook"]},
        headers={"X-Citadel-MCP-Tool": "citadel_ingest"},
    )
    assert ingested.status_code == 200
    assert ingested.json()["accepted"] is True

    recent = writer.get("/api/contributions/recent?limit=10&mine=true")
    assert recent.status_code == 200
    payload = recent.json()
    actions = [event["action"] for event in payload["contributions"]]
    assert actions == ["mcp.citadel_ingest"], payload


def test_recent_contributions_omits_rejected_writes(tmp_path: Any) -> None:
    """The feed lists writes that landed, not writes that were attempted.

    Widening the selection past ``contribute`` pulls in surfaces that audit
    their failures too (a secret-blocked ingest records success=False), and a
    rejected write listed as a contribution is the same dishonesty in the
    other direction.
    """
    store = AccessStore(str(tmp_path / "access.json"))
    app.state.access_store = store
    writer = authed_client("test-writer")

    # The row a secret-blocked MCP ingest writes (kb/server.py record_mcp_audit,
    # success=bool(result.accepted)), recorded through the same store API.
    store.record_event(
        action="mcp.citadel_ingest",
        actor=None,
        success=False,
        dataset="seat:test-writer",
        detail={"surface": "mcp", "operation": "ingest", "blocked": "secret_content"},
    )

    recent = writer.get("/api/contributions/recent?limit=10")
    assert recent.status_code == 200
    assert recent.json()["contributions"] == []


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


def test_a_422_says_why_in_the_log_without_echoing_the_payload(
    tmp_path: Any, caplog: Any
) -> None:
    """A rejected write must be diagnosable from the node, not just from the caller.

    Production showed ten consecutive PUT .../capture-roots 422s with nothing in
    the deploy log but the status code, so there was no way to tell whether the
    payload was the wrong shape, too large, or missing a field.

    The values stay out. On these routes they are filesystem paths and note
    content, and a log line is the wrong place for them.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    client.post("/api/access/seats", json={"name": "Sarthi", "slug": "sarthi"})

    with caplog.at_level(logging.WARNING, logger="kb.server"):
        response = client.put(
            "/api/access/seats/sarthi/capture-roots",
            json={"roots": "/Users/secret/path-not-a-list"},
        )

    assert response.status_code == 422
    assert "detail" in response.json(), "the caller still gets FastAPI's normal body"

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "capture-roots" in logged and "roots" in logged, logged
    assert "/Users/secret/path-not-a-list" not in logged, "the payload value leaked"


def test_robots_txt_keeps_crawlers_off_the_api(tmp_path: Any) -> None:
    """The public pages share an origin with the app and the API (#105 adjacent).

    Every crawled /api path is a request that reaches the event loop even though
    it answers 401.
    """
    client = authed_client()

    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "Disallow: /api/" in body
    assert "Allow: /info" in body


def test_create_seat_provisions_the_cognee_dataset_row(tmp_path: Any) -> None:
    """A new seat gets its cognee Dataset row, not just a dataset name (#147).

    cognee only creates the row on the write path, so before this a seat whose
    holder had never ingested anything had no row at all and every search for
    them raised DatasetNotFoundError. Six of eleven live seats were in that
    state.

    What this pins is the wiring: the handler reaches the client and passes the
    seat's own dataset. It cannot pin that a row appears, because cognee is
    faked throughout this suite. The row creation itself is cognee's
    create_authorized_dataset, covered by its select-then-insert.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    provisioned: list[str] = []

    class _Cognee:
        async def ensure_dataset(self, name: str) -> bool:
            provisioned.append(name)
            return True

    class _Citadel(FakeCitadel):
        cognee = _Cognee()

    app.state.citadel = _Citadel()
    response = client.post(
        "/api/access/seats",
        json={"name": "Alice Smith", "slug": "alice"},
    )

    assert response.status_code == 200
    assert response.json()["node_dataset_provisioned"] is True
    assert provisioned == ["seat:alice"], "the seat's dataset row was never provisioned"


def test_create_seat_survives_a_failed_dataset_provision(tmp_path: Any) -> None:
    """Provisioning is best-effort on purpose, and says so when it fails.

    The seat and its token are already persisted by the time provisioning runs,
    so raising here would leave a half-created seat and no way to tell which
    half. The seat is returned, and the failure is reported rather than
    swallowed, so a backfill can find it.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    class _Cognee:
        async def ensure_dataset(self, name: str) -> bool:
            raise RuntimeError("relational store unavailable")

    class _Citadel(FakeCitadel):
        cognee = _Cognee()

    app.state.citadel = _Citadel()
    response = client.post(
        "/api/access/seats",
        json={"name": "Bob", "slug": "bob"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["token"].startswith("ctdl_"), "the seat must still be usable"
    assert payload["node_dataset_provisioned"] is False, "a failure must be visible"


async def test_session_traces_bootstrap_provisions_missing_dataset_without_ingest(
    monkeypatch: Any,
) -> None:
    from cognee import datasets as cognee_datasets
    from kb.server import ensure_session_traces_dataset

    provisioned: list[str] = []
    ingested: list[str] = []

    async def no_datasets() -> list[Any]:
        return []

    class _Cognee:
        async def ensure_dataset(self, name: str) -> bool:
            provisioned.append(name)
            return True

    class _Citadel:
        cognee = _Cognee()

        async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
            ingested.append(data)
            return IngestResult(True, "accepted", kwargs["dataset"], ())

    monkeypatch.setattr(cognee_datasets, "list_datasets", no_datasets)

    await ensure_session_traces_dataset(_Citadel())

    assert provisioned == [SESSION_TRACES_DATASET]
    assert ingested == []


async def test_session_traces_bootstrap_repairs_existing_dataset_idempotently(
    monkeypatch: Any,
) -> None:
    from cognee import datasets as cognee_datasets
    from kb.server import ensure_session_traces_dataset

    provisioned: list[str] = []
    ingested: list[str] = []

    async def existing_dataset() -> list[Any]:
        return [SimpleNamespace(name=SESSION_TRACES_DATASET)]

    class _Cognee:
        async def ensure_dataset(self, name: str) -> bool:
            provisioned.append(name)
            return False

    class _Citadel:
        cognee = _Cognee()

        async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
            ingested.append(data)
            return IngestResult(True, "accepted", kwargs["dataset"], ())

    monkeypatch.setattr(cognee_datasets, "list_datasets", existing_dataset)

    await ensure_session_traces_dataset(_Citadel())

    assert provisioned == [SESSION_TRACES_DATASET]
    assert ingested == []


async def test_backfill_seat_datasets_provisions_every_existing_seat(tmp_path: Any) -> None:
    """Seats that predate provisioning get their row on the next boot (#147).

    A boot pass rather than a script, so the repair is self-healing. One seat
    that cannot be provisioned must not stop the rest, and must not stop the
    node booting, so failures are counted rather than raised.
    """
    from kb.server import backfill_seat_datasets

    store = AccessStore(tmp_path / "access.json")
    for slug in ("alice", "bob", "carol"):
        store.create_seat(name=slug.title(), slug=slug, issue_token=False)

    seen: list[str] = []

    class _Cognee:
        async def ensure_dataset(self, name: str) -> bool:
            seen.append(name)
            if name == "seat:bob":
                raise RuntimeError("relational store unavailable")
            # carol was already provisioned by an earlier boot.
            return name != "seat:carol"

    counts = await backfill_seat_datasets(SimpleNamespace(cognee=_Cognee()), store)

    assert seen == ["seat:alice", "seat:bob", "seat:carol"], "every seat must be attempted"
    assert counts == {"seats": 3, "created": 1, "failed": 1}


def test_backfill_seat_datasets_is_wired_into_boot() -> None:
    """The backfill is only useful if boot actually calls it (#147).

    The two tests around this one exercise the function directly, so deleting
    the call from the lifespan leaves them green and the six broken seats
    unrepaired. The session-traces provisioner has its own focused tests above;
    this assertion pins the separate seat-backfill call in the lifespan.

    Reading the lifespan source is a blunt instrument and only proves the call
    is present, not that it runs. Running the real lifespan would drag in mesh
    rehydrate, both schedulers and cognee startup, which is a different and much
    slower test. This catches the failure that actually happens, someone tidying
    the call away, which is otherwise silent.
    """
    import inspect

    from kb import server

    source = inspect.getsource(server.lifespan)
    assert "backfill_seat_datasets" in source, "boot no longer backfills seat datasets"


def test_start_cognify_queue_resumes_due_work(monkeypatch: Any) -> None:
    from kb import server

    calls: list[str] = []

    class FakeCognee:
        def resume_cognify_queue(self) -> None:
            calls.append("resume")

    class FakeCitadel:
        cognee = FakeCognee()

    monkeypatch.setattr(server, "get_citadel", lambda: FakeCitadel())
    server._start_cognify_queue()

    assert calls == ["resume"]


def test_start_lifecycle_queue_resumes_due_projection_work(monkeypatch: Any) -> None:
    from kb import server

    calls: list[str] = []

    class FakeCitadel:
        def resume_lifecycle_queue(self) -> None:
            calls.append("resume")

    monkeypatch.setattr(server, "get_citadel", lambda: FakeCitadel())
    server._start_lifecycle_queue()

    assert calls == ["resume"]


async def test_stop_lifecycle_queue_awaits_worker_shutdown(monkeypatch: Any) -> None:
    from kb import server

    calls: list[str] = []

    class FakeCitadel:
        async def stop_lifecycle_queue(self) -> None:
            calls.append("stop")

    monkeypatch.setattr(server, "get_citadel", lambda: FakeCitadel())
    await server._stop_lifecycle_queue()

    assert calls == ["stop"]


async def test_backfill_seat_datasets_is_a_noop_without_a_cognee_client(tmp_path: Any) -> None:
    """Boot must not break where the client has no ensure_dataset to call."""
    from kb.server import backfill_seat_datasets

    store = AccessStore(tmp_path / "access.json")
    store.create_seat(name="Alice", slug="alice", issue_token=False)

    counts = await backfill_seat_datasets(SimpleNamespace(cognee=object()), store)

    assert counts == {"seats": 0, "created": 0, "failed": 0}


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


# --------------------------------------------------------------------------
# Token minting must hand over the credential that was asked for, or say no.
# An operator picked role=admin with a seat selected, got a 200, and held a
# writer token: every admin call 403'd while the UI had claimed admin. Three
# guards below: the form disables the ignored control, the server rejects an
# explicit role instead of dropping it, and the success panel describes what
# was actually minted from the server response.
# --------------------------------------------------------------------------


def test_seat_token_endpoint_rejects_an_explicit_role(tmp_path: Any) -> None:
    """POST /api/access/seats/{slug}/tokens 422s when 'role' is supplied.

    Pydantic silently drops undeclared fields, so before this the endpoint
    accepted {"role": "admin"} and returned a writer token: a credential
    different from the one requested, discovered only when admin calls 403.
    A matching role is rejected too; the field is never honoured, so accepting
    it sometimes would teach callers it works.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()
    created = client.post("/api/access/seats", json={"name": "Sarthi", "slug": "sarthi"})
    assert created.status_code == 200

    for role in ("admin", "writer"):
        denied = client.post(
            "/api/access/seats/sarthi/tokens",
            json={"token_name": "re-link", "role": role},
        )
        assert denied.status_code == 422, role
        detail = denied.json()["detail"]
        assert "role" in detail
        assert "/api/access/tokens" in detail

    # Without the field the endpoint still mints, scoped by the seat.
    minted = client.post("/api/access/seats/sarthi/tokens", json={"token_name": "re-link"})
    assert minted.status_code == 200
    payload = minted.json()
    assert payload["api_token"]["role"] == "writer"
    assert payload["principal"]["seat_slug"] == "sarthi"
    assert "seat:sarthi" in payload["api_token"]["allowed_datasets"]


def test_token_form_disables_role_when_a_seat_is_chosen() -> None:
    """Choosing a seat disables the role select and shows the why.

    The seat branch of the form posts only token_name, so the role selector is
    decoration there. It used to stay enabled, which is how an operator chose
    admin and believed it. The toggle must treat role exactly like the dataset
    fields it already disables, plus a visible hint carrying the reason.
    """
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    page = authed_client().get("/app").text

    toggle = re.search(r"function applyTokenSeatScopeToggle\(\) \{(.*?)\n\}", app_js, re.S)
    assert toggle, "applyTokenSeatScopeToggle moved"
    body = toggle.group(1)
    assert 'getElementById("accessRole")' in body
    assert 'getElementById("accessRoleSeatHint")' in body
    assert "roleSelect.disabled = hasSeat" in body
    assert "roleHint.hidden = !hasSeat" in body

    # The hint the toggle reveals exists in the markup and states the rule.
    assert 'id="accessRoleSeatHint"' in page
    assert "Seat tokens are always writer" in page
    assert "private-memory boundary" in page


def test_minted_token_summary_reads_the_server_response_not_the_form() -> None:
    """The success panel describes the minted credential from the response.

    Role and scope come from api_token/principal in the server's reply, never
    from the submitted form values, so a token minted differently from what
    the form claimed is visible the moment it is created. The summary is
    escaped like the token itself before entering innerHTML.
    """
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    fn = re.search(r"function mintedTokenSummary\(response\) \{(.*?)\n\}", app_js, re.S)
    assert fn, "mintedTokenSummary moved"
    body = fn.group(1)
    assert "api_token" in body
    assert "principal" in body
    assert "seat_slug" in body
    assert "allowed_datasets" in body
    # Never the requested values.
    assert "formData" not in body
    assert "FormData" not in body

    # The panel renders it, escaped, alongside the one-time token.
    assert "escapeHtml(mintedTokenSummary(response))" in app_js


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


def test_seat_writer_cannot_run_a_live_promotion(tmp_path: Any) -> None:
    """dry_run is caller-supplied, and with it false this endpoint writes
    durably into shared Central under a synthetic admin identity with no
    further approval on the auto-promote path. A writer may preview its own
    candidates; it may not grant itself the write by flipping the flag."""
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    created = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"})
    token = created.json()["token"]
    seat_client = TestClient(app, base_url="https://testserver")

    preview = seat_client.post(
        "/api/promote/run",
        json={"dataset": "seat:alice", "dry_run": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    live = seat_client.post(
        "/api/promote/run",
        json={"dataset": "seat:alice", "dry_run": False},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert preview.status_code == 200
    assert live.status_code == 403


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
        self.cognee.schedule_cognify = (
            lambda datasets: self.cognee.scheduled.append(list(datasets)) or True
        )

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        dataset = kwargs.get("dataset") or "notes"
        return IngestResult(True, "accepted", dataset, tuple(kwargs.get("tags") or ()))


class LifecycleShareCitadel(ShareCitadel):
    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        dataset = kwargs.get("dataset") or "notes"
        suffix = dataset.replace(":", "-")
        return IngestResult(
            True,
            "accepted",
            dataset,
            tuple(kwargs.get("tags") or ()),
            source_revision_id=f"source-{suffix}",
            projection_job_id=f"job-{suffix}",
            projection_state="pending",
        )


class CrossSeatTraceCitadel(FakeCitadel):
    """Stateful fake: shared traces ingested by one seat are searchable by another."""

    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []
        self.cognee = type("_FakeCognee", (), {})()
        self.cognee.scheduled: list[list[str]] = []
        self.cognee.schedule_cognify = (
            lambda datasets: self.cognee.scheduled.append(list(datasets)) or True
        )

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
    assert payload["accepted"] is True
    assert payload["cognify"] == "deferred"
    datasets = [call["dataset"] for call in app.state.citadel.ingest_calls]
    assert datasets == ["seat:alice", SESSION_TRACES_DATASET]
    assert app.state.citadel.cognee.scheduled == [["seat:alice", SESSION_TRACES_DATASET]]


def test_share_session_returns_one_lifecycle_operation_per_target(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()["token"]
    app.state.citadel = LifecycleShareCitadel()
    client = TestClient(app, base_url="https://testserver")
    root = str(tmp_path)
    register_seat_capture_roots(admin, "alice", [root])

    response = client.post(
        "/api/share-session",
        json={"data": "Task: lifecycle receipts", "cwd": root, "capture_roots": [root]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["operations"] == [
        {
            "dataset": "seat:alice",
            "accepted": True,
            "source_revision_id": "source-seat-alice",
            "projection_job_id": "job-seat-alice",
            "projection_state": "pending",
        },
        {
            "dataset": SESSION_TRACES_DATASET,
            "accepted": True,
            "source_revision_id": "source-session-traces",
            "projection_job_id": "job-session-traces",
            "projection_state": "pending",
        },
    ]


def test_share_session_surfaces_rejected_durable_queue(tmp_path: Any) -> None:
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    token = admin.post(
        "/api/access/seats", json={"name": "Alice", "slug": "alice"}
    ).json()["token"]
    citadel = ShareCitadel()
    citadel.cognee.schedule_cognify = lambda datasets: False
    app.state.citadel = citadel
    client = TestClient(app, base_url="https://testserver")
    root = str(tmp_path)
    register_seat_capture_roots(admin, "alice", [root])

    response = client.post(
        "/api/share-session",
        json={"data": "Task: queue failure", "cwd": root, "capture_roots": [root]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["accepted"] is True
    assert payload["cognify"] == "not_scheduled"
    assert payload["message"] == (
        "Shared Session Trace accepted, but indexing was not scheduled."
    )
    events = app.state.access_store.snapshot()["audit_events"]
    successful = [
        event
        for event in events
        if event["action"] == "share_session" and event["success"]
    ]
    assert successful[-1]["detail"]["cognify"] == "not_scheduled"


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
        self.cognee.schedule_cognify = (
            lambda datasets: self.cognee.scheduled.append(list(datasets)) or True
        )

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
        self.cognee.schedule_cognify = (
            lambda datasets: self.cognee.scheduled.append(list(datasets)) or True
        )

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
    assert payload["accepted"] is True
    assert payload["cognify"] == "deferred"
    assert [call["dataset"] for call in citadel.ingest_calls] == [
        "seat:alice",
        SESSION_TRACES_DATASET,
        SESSION_TRACES_DATASET,
    ]
    assert citadel.session_traces_attempts == 2
    assert citadel.cognee.scheduled == [["seat:alice", SESSION_TRACES_DATASET]]
    events = app.state.access_store.snapshot()["audit_events"]
    successful = [
        event
        for event in events
        if event["action"] == "share_session" and event["success"]
    ]
    assert successful[-1]["detail"]["accepted"] is True


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
    assert detail["operations"] == [
        {
            "dataset": "seat:alice",
            "accepted": True,
            "source_revision_id": None,
            "projection_job_id": None,
            "projection_state": None,
        },
        {
            "dataset": SESSION_TRACES_DATASET,
            "accepted": False,
            "source_revision_id": None,
            "projection_job_id": None,
            "projection_state": None,
        },
    ]
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


def test_repo_documentation_keeps_its_trust_when_a_trace_quotes_it() -> None:
    """A text collision with session-traces must not demote Central docs.

    The shared-trace marker is assigned by matching chunk TEXT, so any document
    a trace quotes verbatim inherits it. ADR-0017 stopped that from rewriting
    `doc_type`, but `trust` stayed demoted unconditionally, so Central
    documentation still came back carrying a trace's trust tier.

    Genuine traces are unaffected: their dataset attests them (ADR-0012), which
    the test above pins.
    """
    repo_doc = {
        "id": "chunk-1",
        "text": (
            "# masumi-network/sokosumi/README.md\n"
            "\n"
            "Repository: masumi-network/sokosumi\n"
            "Source: https://github.com/masumi-network/sokosumi/blob/f401d38/README.md\n"
            "Commit: f401d3896820ff35a82cf707818e308b67f5bce2\n"
            "Blob: a4b30a4548af239f695ba3cba1935b545e96d675\n"
            "\n"
            "---\n"
            "\n"
            "# Sokosumi Monorepo\n"
        ),
        server_module.SHARED_TRACE_MARKER: True,
    }

    envelope = server_module.with_result_metadata(repo_doc, 0, "masumi-network")["_citadel"]

    assert envelope["doc_type"] == "canonical-docs"
    assert envelope.get("trust") != "reference-only", envelope
    assert envelope["trust_tier"] == "unattested", envelope
    # Trace-only metadata must not ride along on a document that is not a trace.
    assert "author_seat" not in envelope


def test_a_real_trace_quoting_a_repo_path_stays_a_trace() -> None:
    """The ADR-0012 guard still holds: body prose cannot mint provenance."""
    trace = {
        "id": "chunk-2",
        "text": "I opened masumi-network/sokosumi/README.md and Repository: looked fine",
        server_module.SHARED_TRACE_MARKER: True,
    }

    envelope = server_module.with_result_metadata(trace, 0, "seat:bob")["_citadel"]

    assert envelope["doc_type"] == "session-trace"
    assert envelope["trust"] == "reference-only"
    assert envelope["trust_tier"] == "reference-only"


def test_result_metadata_promotes_lifecycle_receipt_identity() -> None:
    result = server_module.with_result_metadata(
        {
            "id": "chunk-1",
            "document_id": "source-1",
            "text": "current lifecycle evidence",
            "_lifecycle": {
                "schema_version": 1,
                "source_revision_id": "source-1",
                "projection_receipt_id": "receipt-1",
                "generation_id": "generation-1",
                "backend": "vector",
                "provider": "qdrant",
                "projection_version": "projection-v1",
                "state": "searchable",
            },
        },
        0,
        "central",
    )

    assert "_lifecycle" not in result
    assert result["_citadel"]["source_revision_id"] == "source-1"
    assert result["_citadel"]["projection_receipt_id"] == "receipt-1"
    assert result["_citadel"]["projection"] == {
        "generation_id": "generation-1",
        "backend": "vector",
        "provider": "qdrant",
        "projection_version": "projection-v1",
        "state": "searchable",
    }


# --- /partners contact form -------------------------------------------------
# The only unauthenticated write path in the service, so it gets its own tests
# rather than riding along on the page tests.


class FakeChatGateway:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, object]:
        self.sent.append(text)
        return {"ok": True}


def _reset_contact_limits() -> None:
    server_module._contact_hits.clear()
    server_module._contact_global_hits.clear()


def _enquiry(**overrides: str) -> dict[str, str]:
    body = {
        "name": "Ada Lovelace",
        "email": "ada@example.org",
        "organization": "Analytical Engines",
        "message": "DIGITAL-2026-AI-07, we need the memory work package.",
        "website": "",
    }
    body.update(overrides)
    return body


def test_contact_delivers_to_the_chat_gateway(monkeypatch) -> None:
    _reset_contact_limits()
    gateway = FakeChatGateway()
    monkeypatch.setattr(server_module, "contact_gateway", lambda: gateway)
    client = TestClient(app, base_url="https://testserver")

    response = client.post("/contact", json=_enquiry())

    assert response.status_code == 200
    assert response.json() == {"delivered": True, "stored": True}
    assert len(gateway.sent) == 1
    assert "Ada Lovelace" in gateway.sent[0]
    assert "DIGITAL-2026-AI-07" in gateway.sent[0]


def _contact_store_at(monkeypatch, tmp_path) -> Any:
    """Point the endpoint at a throwaway enquiry store."""
    from kb.contact_store import ContactStore

    store = ContactStore(str(tmp_path / "contacts.json"))
    monkeypatch.setattr(server_module, "get_contact_store", lambda: store)
    return store


def test_contact_is_stored_when_the_gateway_is_unconfigured(monkeypatch, tmp_path) -> None:
    """No gateway must not mean the enquiry is lost (ADR-0013, amended).

    Google Chat is unconfigured in production, so the old fail-closed 503 turned
    away every partner enquiry and kept no record. ADR-0013's rule is that an
    enquiry is never accepted into a void; a file on the state volume is not a
    void, and the sender should not be told to go away.
    """
    _reset_contact_limits()
    store = _contact_store_at(monkeypatch, tmp_path)
    monkeypatch.setattr(server_module, "contact_gateway", lambda: None)
    client = TestClient(app, base_url="https://testserver")

    response = client.post("/contact", json=_enquiry())

    assert response.status_code == 200
    assert response.json() == {"delivered": True, "stored": True}
    saved = store.recent()
    assert len(saved) == 1
    assert saved[0]["name"] == "Ada Lovelace"
    assert "DIGITAL-2026-AI-07" in saved[0]["message"]
    assert saved[0]["received_at"]


def test_contact_still_fails_closed_when_it_can_neither_store_nor_deliver(
    monkeypatch, tmp_path
) -> None:
    """The fail-closed rule survives; it just has a second chance first."""
    _reset_contact_limits()
    store = _contact_store_at(monkeypatch, tmp_path)

    def explode(_entry: Any) -> None:
        raise OSError("disk is gone")

    monkeypatch.setattr(store, "append", explode)
    monkeypatch.setattr(server_module, "contact_gateway", lambda: None)
    client = TestClient(app, base_url="https://testserver")

    response = client.post("/contact", json=_enquiry())

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_contact_is_kept_when_chat_delivery_fails(monkeypatch, tmp_path) -> None:
    """A stored enquiry must not ask the sender to send it a second time."""
    _reset_contact_limits()
    store = _contact_store_at(monkeypatch, tmp_path)

    class BrokenGateway:
        thread_key = "citadel-partner-contact"

        def post_digest(self, text: str, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("chat is down")

    monkeypatch.setattr(server_module, "contact_gateway", lambda: BrokenGateway())
    client = TestClient(app, base_url="https://testserver")

    response = client.post("/contact", json=_enquiry())

    assert response.status_code == 200
    assert response.json() == {"delivered": True, "stored": True}
    assert len(store.recent()) == 1


def test_contact_enquiries_endpoint_is_admin_only(monkeypatch, tmp_path) -> None:
    _contact_store_at(monkeypatch, tmp_path)
    client = TestClient(app, base_url="https://testserver")

    assert client.get("/api/contact/enquiries").status_code in {401, 403}


def test_an_admin_can_actually_read_the_enquiries(monkeypatch, tmp_path) -> None:
    """The half the admin-only test above cannot see.

    Asserting that a stranger is refused says nothing about whether anyone is
    admitted, so that test passed just as happily while the gate demanded a scope
    no credential could hold and the queue was readable by nobody. This one fails
    if the endpoint is walled off from its own owner.
    """
    store = _contact_store_at(monkeypatch, tmp_path)
    client = authed_client()
    client.post("/contact", json=_enquiry())
    assert len(store.recent()) == 1

    response = client.get("/api/contact/enquiries")

    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_contact_is_never_written_to_the_vault(monkeypatch, tmp_path) -> None:
    """The part of ADR-0013 that did NOT change.

    Unauthenticated public text must never reach the substrate agents read as
    authority. Storing enquiries on disk is a fourth destination, not a reversal
    of the vault rejection.
    """
    _reset_contact_limits()
    _contact_store_at(monkeypatch, tmp_path)
    monkeypatch.setattr(server_module, "contact_gateway", lambda: None)

    ingested: list[Any] = []

    class Tripwire:
        async def ingest(self, *args: Any, **kwargs: Any) -> Any:
            ingested.append((args, kwargs))
            raise AssertionError("a contact enquiry must never reach the vault")

    monkeypatch.setattr(server_module, "get_citadel", lambda: Tripwire())
    client = TestClient(app, base_url="https://testserver")

    client.post("/contact", json=_enquiry())

    assert ingested == []


def test_contact_honeypot_is_dropped_without_delivery(monkeypatch) -> None:
    _reset_contact_limits()
    gateway = FakeChatGateway()
    monkeypatch.setattr(server_module, "contact_gateway", lambda: gateway)
    client = TestClient(app, base_url="https://testserver")

    response = client.post("/contact", json=_enquiry(website="http://spam.example"))

    # 200 on purpose: a bot must not learn that it was filtered.
    assert response.status_code == 200
    assert gateway.sent == []


def test_contact_rate_limits_a_single_sender(monkeypatch) -> None:
    _reset_contact_limits()
    gateway = FakeChatGateway()
    monkeypatch.setattr(server_module, "contact_gateway", lambda: gateway)
    client = TestClient(app, base_url="https://testserver")
    headers = {"x-forwarded-for": "203.0.113.7"}

    codes = [
        client.post("/contact", json=_enquiry(), headers=headers).status_code
        for _ in range(server_module.CONTACT_PER_IP_LIMIT + 1)
    ]

    assert codes[:-1] == [200] * server_module.CONTACT_PER_IP_LIMIT
    assert codes[-1] == 429
    assert len(gateway.sent) == server_module.CONTACT_PER_IP_LIMIT


def test_contact_scrubs_chat_formatting_from_submitted_text() -> None:
    """Submitted text must not be able to forge a message in the org's space.

    Google Chat renders <url|label> as a link and *_~` as formatting, so a
    submitter could otherwise post something that reads like Citadel itself.
    """
    forged = "*Citadel admin:* rotate keys at <https://evil.example|the portal>"

    scrubbed = server_module.scrub_contact_field(forged)

    for char in "<>*_~`":
        assert char not in scrubbed
    assert "rotate keys at" in scrubbed


def test_contact_rejects_an_oversized_message(monkeypatch) -> None:
    _reset_contact_limits()
    gateway = FakeChatGateway()
    monkeypatch.setattr(server_module, "contact_gateway", lambda: gateway)
    client = TestClient(app, base_url="https://testserver")

    response = client.post("/contact", json=_enquiry(message="x" * 4001))

    assert response.status_code == 422
    assert gateway.sent == []


# --- M4: failed-auth throttle and weak-key guard -----------------------------


def test_repeated_failed_auth_is_throttled_with_retry_after() -> None:
    """Brute force gets a 429, not an unlimited stream of 401s."""
    server_module.reset_auth_throttle()
    client = TestClient(app, base_url="https://testserver")

    codes = [
        client.get("/api/mesh", headers={"Authorization": "Bearer wrong"}).status_code
        for _ in range(server_module.AUTH_FAIL_PER_IP_LIMIT + 3)
    ]

    assert codes[0] == 401
    assert codes[-1] == 429
    assert codes.count(401) == server_module.AUTH_FAIL_PER_IP_LIMIT
    blocked = client.get("/api/mesh", headers={"Authorization": "Bearer wrong"})
    assert blocked.headers["Retry-After"] == str(server_module.AUTH_FAIL_PER_IP_WINDOW_SECONDS)


def test_a_valid_credential_is_never_throttled(monkeypatch) -> None:
    """The whole point: an attacker's noise must not lock out the team.

    The credential is resolved before the limiter is consulted, so a caller who
    holds a real one keeps working no matter how deep the failure bucket is.
    """
    server_module.reset_auth_throttle()
    client = TestClient(app, base_url="https://testserver")
    for _ in range(server_module.AUTH_FAIL_PER_IP_LIMIT + 5):
        assert client.get("/api/mesh", headers={"Authorization": "Bearer wrong"})

    # Now the bucket is well over the limit. A recognised identity must sail past.
    monkeypatch.setattr(
        server_module,
        "request_identity",
        lambda _request: AccessIdentity(
            role="admin",
            actor_id="env:admin",
            actor_kind="env",
            actor_name="admin",
            source="env",
            seat_slug=None,
        ),
    )
    identity = server_module.require_role(
        SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/mesh")), "admin"
    )

    assert identity.role == "admin"


def test_a_blocked_attempt_does_not_extend_the_ban() -> None:
    """Our own clients retry 429 but never 401 (kb/retry.py).

    Counting blocked retries would let a stale-token client extend its own ban
    forever and hide a wrong credential behind "rate limited".
    """
    server_module.reset_auth_throttle()
    client = TestClient(app, base_url="https://testserver")
    for _ in range(server_module.AUTH_FAIL_PER_IP_LIMIT):
        client.get("/api/mesh", headers={"Authorization": "Bearer wrong"})

    before = len(server_module._auth_fail_global_hits)
    for _ in range(5):
        client.get("/api/mesh", headers={"Authorization": "Bearer wrong"})

    assert len(server_module._auth_fail_global_hits) == before


def test_weak_env_access_key_refuses_to_start(monkeypatch) -> None:
    monkeypatch.delenv("CITADEL_ALLOW_WEAK_ACCESS_KEYS", raising=False)
    monkeypatch.setattr(
        server_module,
        "get_citadel",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                admin_key="owner-admin-key", writer_keys=(), reader_keys=()
            )
        ),
    )

    with pytest.raises(SystemExit) as caught:
        server_module.enforce_access_key_strength()

    assert "CITADEL_ADMIN_KEY" in str(caught.value)
    assert "token_urlsafe" in str(caught.value)


def test_a_strong_env_access_key_starts_normally(monkeypatch) -> None:
    monkeypatch.delenv("CITADEL_ALLOW_WEAK_ACCESS_KEYS", raising=False)
    monkeypatch.setattr(
        server_module,
        "get_citadel",
        lambda: SimpleNamespace(
            config=SimpleNamespace(
                admin_key="x" * 64, writer_keys=(), reader_keys=()
            )
        ),
    )

    server_module.enforce_access_key_strength()


def test_the_weak_key_guard_can_be_overridden_explicitly(monkeypatch) -> None:
    """Deliberate and greppable, unlike a warning nobody reads."""
    monkeypatch.setenv("CITADEL_ALLOW_WEAK_ACCESS_KEYS", "true")
    monkeypatch.setattr(
        server_module,
        "get_citadel",
        lambda: SimpleNamespace(
            config=SimpleNamespace(admin_key="short", writer_keys=(), reader_keys=())
        ),
    )

    server_module.enforce_access_key_strength()


def test_a_timed_out_search_is_audited_as_a_failure(monkeypatch) -> None:
    """A search that returned nothing must not be recorded as a success (#50).

    The audit detail already carried timed_out, but success was hardcoded True,
    so /api/audit?view=failures never listed a timed-out search and any
    success-rate metric read 100% while callers got zero hits. That is why the
    "~20% silent failure rate" in #50 could never be quantified from the data
    the node itself records.
    """
    import asyncio as aio
    import dataclasses

    class SlowCitadel(FakeCitadel):
        config = dataclasses.replace(FakeCitadel.config, search_timeout_seconds=0.01)

        async def search(self, query: str, **kwargs: Any) -> list[Any]:
            await aio.sleep(0.3)
            return [{"id": "x"}]

    audited: list[dict[str, Any]] = []
    monkeypatch.setattr(
        server_module,
        "record_mcp_audit",
        lambda request, **kw: audited.append(kw),
    )

    async def forbidden_post_timeout_work(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("timeout response continued into provider or telemetry work")

    monkeypatch.setattr(
        server_module,
        "load_node_dataset_map",
        forbidden_post_timeout_work,
    )
    monkeypatch.setattr(
        server_module,
        "capture_search_feedback",
        forbidden_post_timeout_work,
    )

    client = authed_client("test-reader")
    app.state.citadel = SlowCitadel()

    response = client.post("/search", json={"query": "q", "top_k": 3})
    body = response.json()

    assert response.status_code == 504
    assert body["detail"]["code"] == "SEARCH_TIMEOUT"
    assert audited, "the search must be audited at all"
    entry = audited[-1]
    assert entry["detail"]["timed_out"] is True
    assert entry["success"] is False, "a timed-out search is not a success"


def test_a_normal_search_is_still_audited_as_a_success() -> None:
    """The guard against over-correcting: only the timeout path flips."""
    audited: list[dict[str, Any]] = []
    original = server_module.record_mcp_audit
    server_module.record_mcp_audit = lambda request, **kw: audited.append(kw)
    try:
        client = authed_client("test-reader")
        app.state.citadel = FakeCitadel()
        body = client.post("/search", json={"query": "q", "top_k": 3}).json()
    finally:
        server_module.record_mcp_audit = original

    assert body.get("timed_out") in (None, False)
    assert audited and audited[-1]["success"] is True


def test_api_mesh_reports_authoritative_corpus_totals_not_uptime_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/api/mesh must report the real corpus, not what this process happens to
    have seen since it booted.

    ADR-0018 added a `corpus` parameter to MeshState.snapshot for this, but this
    endpoint, the one the dashboard actually reads, kept calling snapshot without
    it. Production served `documents: 1, indexed_chunks: 1` against a real 17991
    indexed docs, so a healthy vault rendered as empty on login. The parameter
    existed and was simply never passed, which no test caught because every test
    asserted on snapshot() directly rather than through the endpoint.
    """
    client = authed_client()

    async def fake_corpus_health() -> dict[str, Any]:
        return {
            "ok": True,
            "tracked_sources": 317,
            "indexed_docs": 17991,
            "indexed_edges": 131843,
        }

    monkeypatch.setattr(server_module, "_corpus_health", fake_corpus_health)

    stats = client.get("/api/mesh").json()["stats"]

    assert stats["tracked_sources"] == 317, stats
    assert stats["nodes"] == 17991, stats
    assert stats["edges"] == 131843, stats
    # indexed_chunks duplicated `nodes` by construction and never counted a
    # chunk, so it is gone rather than published wrong (same call as
    # failed_chunks below).
    assert "indexed_chunks" not in stats
    # The uptime figures are still reported, just no longer disguised as totals.
    assert "since_restart" in stats
    assert stats["since_restart"]["documents"] != 317


def test_api_mesh_keeps_degraded_corpus_totals_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded corpus read must not present uptime counters as totals.

    `_corpus_health` is fail-soft and returns None totals on a transient graph
    error. The endpoint stays available, but marks the corpus figures unknown
    until an authoritative probe completes.
    """
    client = authed_client()

    async def degraded_corpus_health() -> dict[str, Any]:
        return {
            "ok": True,
            "tracked_sources": None,
            "indexed_docs": None,
            "indexed_edges": None,
            "degraded": "boom",
        }

    monkeypatch.setattr(server_module, "_corpus_health", degraded_corpus_health)

    response = client.get("/api/mesh")

    assert response.status_code == 200
    body = response.json()
    stats = body["stats"]
    assert stats["tracked_sources"] is None
    assert stats["nodes"] is None
    assert stats["edges"] is None
    indexes = {index["id"]: index for index in body["indexes"]}
    assert indexes["graph"]["status"] == "warming"
    assert indexes["vector"]["status"] == "warming"


@pytest.mark.asyncio
async def test_dashboard_corpus_health_is_bounded_and_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowCitadel(FakeCitadel):
        async def _graph_counts(self) -> dict[str, int]:
            await asyncio.sleep(0.05)
            return {"nodes": 5, "edges": 7}

    class Syncer:
        async def status(self) -> dict[str, Any]:
            return {"tracked_repositories": 0, "tracked_files": 0, "issue_count": 0}

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_TASK", None)
    app.state.citadel = SlowCitadel()
    app.state.github_syncer = Syncer()
    app.state.repo_content_syncer = Syncer()
    app.state.linear_syncer = Syncer()

    result = await asyncio.wait_for(server_module._bounded_corpus_health(), timeout=0.5)

    assert result == {
        "ok": False,
        "tracked_sources": None,
        "indexed_docs": None,
        "indexed_edges": None,
        "degraded": "corpus health timed out after 0.01s",
    }
    await asyncio.sleep(0.06)
    assert server_module._CORPUS_HEALTH_CACHE is not None
    assert server_module._CORPUS_HEALTH_CACHE[2]["indexed_docs"] == 5


def test_indexes_timeout_preserves_existing_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowCitadel(FakeCitadel):
        async def _graph_counts(self) -> dict[str, int]:
            await asyncio.sleep(30)
            return {"nodes": 5, "edges": 7}

    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_CACHE", None)
    monkeypatch.setattr(server_module, "_CORPUS_HEALTH_TASK", None)
    client = authed_client("test-reader")
    app.state.citadel = SlowCitadel()

    response = client.get("/api/indexes")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"indexes", "stats"}
    assert isinstance(body["indexes"], list)
    assert "since_restart" in body["stats"]
    indexes = {index["id"]: index for index in body["indexes"]}
    assert indexes["graph"]["status"] == "warming"
    assert indexes["graph"]["records"] is None
    assert indexes["vector"]["status"] == "warming"
    assert indexes["vector"]["records"] is None
    assert body["stats"]["nodes"] is None
    assert body["stats"]["tracked_sources"] is None


def test_api_mesh_activity_counters_are_scoped_not_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#196/#197 through the endpoint the dashboard reads.

    The activity counters missed ADR-0018's treatment: `searches`, `feedback`,
    `upgrades` and `errors` kept restart-scoped values at the top level of the
    stats payload, where they read as lifetime totals (measured 2026-08-02:
    top-level and since_restart `searches` were both 294). And `failed_chunks`
    was the `errors` counter surfaced a second time — it never counted chunks,
    so it is gone rather than renamed.
    """
    client = authed_client()

    async def fake_corpus_health() -> dict[str, Any]:
        return {"ok": True, "tracked_sources": 317, "indexed_docs": 17991}

    monkeypatch.setattr(server_module, "_corpus_health", fake_corpus_health)

    stats = client.get("/api/mesh").json()["stats"]

    for counter in ("searches", "feedback", "upgrades", "errors", "failed_chunks"):
        assert counter not in stats, f"{counter} is restart-scoped but top-level"
    since = stats["since_restart"]
    assert {"searches", "feedback", "upgrades", "errors"} <= set(since)
    # The window the counters cover ships with them.
    assert since["started_at"]
class FakeCorpusCognee:
    """Fake at the kb.cognee_client METHOD boundary.

    Every shape here (row dicts, totals dict, counts mapping, presence set) is
    the contract corpus_page/corpus_totals/corpus_chunk_counts/
    corpus_graph_presence themselves declare — cognee's own row shapes are
    pinned by the real-import tests in test_cognee_client.py, never invented
    here.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        totals: dict[str, Any],
        chunk_counts: dict[str, int] | None = None,
        graph_ids: set[str] | None = None,
        graph_ids_by_dataset: dict[str, set[str]] | None = None,
        chunk_lookup_raises: bool = False,
    ) -> None:
        self.rows = rows
        self.totals = totals
        self.chunk_counts = chunk_counts
        self.graph_ids = graph_ids
        self.graph_ids_by_dataset = graph_ids_by_dataset
        self.chunk_lookup_raises = chunk_lookup_raises
        self.seen_after: list[tuple[str | None, str | None]] = []
        self.graph_calls: list[tuple[list[str], list[str] | None]] = []

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple[Any, str]:
        from datetime import datetime

        return (datetime.fromisoformat(row["created_at"]), row["id"])

    async def corpus_page(
        self,
        *,
        after_created_at: str | None = None,
        after_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        from datetime import datetime

        self.seen_after.append((after_created_at, after_id))
        rows = sorted((dict(row) for row in self.rows), key=self._key)
        if after_created_at and after_id:
            boundary = (datetime.fromisoformat(after_created_at), after_id)
            rows = [row for row in rows if self._key(row) > boundary]
        return rows[:limit]

    async def corpus_totals(self) -> dict[str, Any]:
        return dict(self.totals)

    async def corpus_chunk_counts(self, document_ids: list[str]) -> dict[str, int] | None:
        if self.chunk_lookup_raises:
            raise RuntimeError("vector store unavailable")
        if self.chunk_counts is None:
            return None
        return {
            document_id: self.chunk_counts[document_id]
            for document_id in document_ids
            if document_id in self.chunk_counts
        }

    async def corpus_graph_presence(
        self,
        document_ids: list[str],
        *,
        datasets: list[str] | None = None,
    ) -> set[str] | None:
        self.graph_calls.append((document_ids, datasets))
        if self.graph_ids_by_dataset is not None:
            available: set[str] = set()
            for dataset in datasets or []:
                available.update(self.graph_ids_by_dataset.get(dataset, set()))
            return {doc_id for doc_id in document_ids if doc_id in available}
        if self.graph_ids is None:
            return None
        return {doc_id for doc_id in document_ids if doc_id in self.graph_ids}


def _corpus_row(row_id: str, created_at: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "id": row_id,
        "name": f"doc-{row_id}",
        "content_hash": f"hash-{row_id}",
        "raw_content_hash": None,
        "mime_type": "text/plain",
        "token_count": 10,
        "data_size": 100,
        "created_at": created_at,
        "updated_at": created_at,
        "owner_id": "owner-1",
        "datasets": ["masumi-network"],
        "citadel_tags": [],
    }
    row.update(overrides)
    return row


def _corpus_totals(documents: int, default_owner: int | None = None) -> dict[str, Any]:
    owned = documents if default_owner is None else default_owner
    return {
        "documents": documents,
        "documents_default_owner": owned,
        "documents_other_owners": documents - owned,
        "by_dataset": {"masumi-network": documents},
        "by_dataset_default_owner": {"masumi-network": owned},
    }


def _corpus_client(fake: FakeCorpusCognee) -> TestClient:
    client = authed_client()
    server_module.app.state.citadel.cognee = fake
    return client


def test_corpus_census_reports_rows_presence_and_totals() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fake = FakeCorpusCognee(
        [
            _corpus_row(
                "doc-a",
                (now - timedelta(hours=2)).isoformat(),
                citadel_tags=["alpha"],
            ),
            _corpus_row("doc-b", (now - timedelta(hours=1)).isoformat()),
        ],
        totals=_corpus_totals(2),
        chunk_counts={"doc-a": 3},
        graph_ids={"doc-a"},
    )
    client = _corpus_client(fake)

    response = client.get("/api/corpus")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["documents_returned"] == 2
    assert body["documents_total"] == 2
    assert body["next_cursor"] is None
    assert body["totals"] == _corpus_totals(2)
    first, second = body["documents"]
    assert first["id"] == "doc-a"
    assert first["citadel_tags"] == ["alpha"]
    assert first["datasets"] == ["masumi-network"]
    assert first["chunk_count"] == 3
    assert first["in_graph"] is True
    # doc-b was measured and has nothing indexed: 0/False, not null.
    assert second["chunk_count"] == 0
    assert second["in_graph"] is False
    assert body["notes"] == []


def test_corpus_census_checks_each_dataset_graph() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fake = FakeCorpusCognee(
        [
            _corpus_row(
                "doc-central",
                (now - timedelta(hours=2)).isoformat(),
                datasets=["central"],
            ),
            _corpus_row(
                "doc-alice",
                (now - timedelta(hours=1)).isoformat(),
                datasets=["seat:alice"],
            ),
        ],
        totals=_corpus_totals(2),
        chunk_counts={"doc-central": 1, "doc-alice": 1},
        graph_ids_by_dataset={
            "central": {"doc-central"},
            "seat:alice": {"doc-alice"},
        },
    )
    client = _corpus_client(fake)

    body = client.get("/api/corpus").json()

    assert [row["in_graph"] for row in body["documents"]] == [True, True]
    assert fake.graph_calls == [
        (["doc-central"], ["central"]),
        (["doc-alice"], ["seat:alice"]),
    ]


def test_corpus_census_pages_with_an_opaque_cursor() -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    all_ids = ["doc-a", "doc-b", "doc-c"]
    fake = FakeCorpusCognee(
        [
            _corpus_row(row_id, (now - timedelta(hours=hours)).isoformat())
            for row_id, hours in (("doc-a", 3), ("doc-b", 2), ("doc-c", 1))
        ],
        totals=_corpus_totals(3),
        chunk_counts={},
        graph_ids=set(all_ids),
    )
    client = _corpus_client(fake)

    page_one = client.get("/api/corpus?limit=2").json()
    assert [row["id"] for row in page_one["documents"]] == ["doc-a", "doc-b"]
    assert page_one["documents_returned"] == 2
    assert page_one["next_cursor"]

    page_two = client.get(f"/api/corpus?limit=2&cursor={page_one['next_cursor']}").json()
    assert [row["id"] for row in page_two["documents"]] == ["doc-c"]
    assert page_two["next_cursor"] is None


def test_corpus_census_clamps_a_future_dated_cursor_so_later_rows_land() -> None:
    """One future-dated created_at must not pin the cursor past every real row.

    doc-future sits 30 days ahead and ends the first page. Unclamped, its
    timestamp becomes a cursor that no row written in the next month can
    exceed, so the walk silently stalls while reporting ok. Clamped to
    now+skew, a row that lands after the cursor was minted still comes back on
    the next page (rows inside the skew window may repeat, which is the
    accepted cost).
    """
    import base64 as _base64
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fake = FakeCorpusCognee(
        [
            _corpus_row("doc-a", (now - timedelta(hours=1)).isoformat()),
            _corpus_row("doc-future", (now + timedelta(days=30)).isoformat()),
            _corpus_row("doc-far-future", (now + timedelta(days=60)).isoformat()),
        ],
        totals=_corpus_totals(3),
        chunk_counts={},
        graph_ids={"doc-a", "doc-future", "doc-far-future"},
    )
    client = _corpus_client(fake)

    page_one = client.get("/api/corpus?limit=2").json()
    assert [row["id"] for row in page_one["documents"]] == ["doc-a", "doc-future"]
    cursor = page_one["next_cursor"]
    assert cursor

    decoded = json.loads(_base64.urlsafe_b64decode(cursor.encode("ascii")))
    cursor_time = datetime.fromisoformat(decoded["t"])
    ceiling = datetime.now(timezone.utc) + timedelta(
        seconds=server_module.CORPUS_CURSOR_MAX_SKEW_SECONDS + 5
    )
    assert cursor_time <= ceiling, "cursor was not clamped to now+skew"

    # A real row arrives after the cursor was handed out, just past the skew
    # window. With the future-dated cursor it would never be seen.
    fake.rows.append(
        _corpus_row("doc-later", (now + timedelta(seconds=601)).isoformat())
    )

    page_two = client.get(f"/api/corpus?limit=3&cursor={cursor}").json()
    assert "doc-later" in [row["id"] for row in page_two["documents"]]


def test_corpus_census_clamps_a_stored_future_cursor_on_read() -> None:
    """The write-side clamp is not enough: a cursor minted elsewhere (or before
    a clamp change) can still carry a far-future timestamp, so the read side
    clamps too."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fake = FakeCorpusCognee(
        [
            _corpus_row("doc-later", (now + timedelta(seconds=601)).isoformat()),
            _corpus_row("doc-far-future", (now + timedelta(days=60)).isoformat()),
        ],
        totals=_corpus_totals(2),
        chunk_counts={},
        graph_ids={"doc-later", "doc-far-future"},
    )
    client = _corpus_client(fake)
    handcrafted = server_module._encode_corpus_cursor(
        (now + timedelta(days=30)).isoformat(), "doc-zzz"
    )

    body = client.get(f"/api/corpus?cursor={handcrafted}").json()

    assert "doc-later" in [row["id"] for row in body["documents"]]


def test_corpus_census_degrades_presence_to_null_not_zero() -> None:
    """0 and 'not measured' are different answers — the difference between
    'accepted but never indexed' and 'we did not look'."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    fake = FakeCorpusCognee(
        [_corpus_row("doc-a", (now - timedelta(hours=1)).isoformat())],
        totals=_corpus_totals(1),
        chunk_lookup_raises=True,
        graph_ids=None,
    )
    client = _corpus_client(fake)

    response = client.get("/api/corpus")

    assert response.status_code == 200
    body = response.json()
    row = body["documents"][0]
    assert row["chunk_count"] is None
    assert row["in_graph"] is None
    notes = " ".join(body["notes"])
    assert "chunk_count" in notes and "not measured" in notes
    assert "in_graph" in notes


def test_corpus_census_requires_an_admin() -> None:
    writer = authed_client("test-writer")

    assert writer.get("/api/corpus").status_code == 403


def test_corpus_census_rejects_unauthenticated_callers() -> None:
    server_module.app.state.citadel = FakeCitadel()
    client = TestClient(app, base_url="https://testserver")

    assert client.get("/api/corpus").status_code == 401


def test_api_mesh_publishes_the_real_graph_edge_total_not_the_projection() -> None:
    """`stats.edges` must come from the graph, not from the in-memory projection.

    This pins the PRODUCER half of the #232 fix. `_corpus_health` supplies
    `indexed_edges` and `kb/mesh.py` consumes it with a silent fallback to
    `len(self.edges)`, so reverting the producer alone changes nothing that any
    other test observes: every mesh unit test injects the corpus dict directly.
    That is the shape a guard takes when it ships inert.

    The projection and the graph are given deliberately different edge counts,
    so the assertion can only pass when the real total is the one published.
    Live production before the fix: `stats.edges` 5511 against 131843 real, and
    5511 was byte-identical to `since_restart.projection_edges`.
    """
    real_edges = 131843

    class PopulatedCitadel(FakeCitadel):
        async def _graph_counts(self) -> dict[str, int]:
            return {"nodes": 21086, "edges": real_edges}

    class BusySyncer:
        async def status(self) -> dict[str, Any]:
            return {"tracked_repositories": 49, "tracked_files": 69, "issue_count": 200}

    client = authed_client("test-reader")
    app.state.citadel = PopulatedCitadel()
    app.state.github_syncer = BusySyncer()
    app.state.repo_content_syncer = BusySyncer()
    app.state.linear_syncer = BusySyncer()

    stats = client.get("/api/mesh").json()["stats"]
    projection = stats["since_restart"]["projection_edges"]

    assert projection != real_edges, (
        "the fixture no longer discriminates: the projection and the graph agree, "
        "so this test would pass with the producer reverted"
    )
    assert stats["edges"] == real_edges, (
        f"stats.edges is {stats['edges']}, the in-memory projection, not the "
        f"graph's {real_edges}. _corpus_health stopped supplying indexed_edges "
        "and kb/mesh.py fell back silently."
    )
    assert stats["tracked_sources"] == 318


def test_a_skipped_repo_content_sync_is_not_recorded_as_a_sync() -> None:
    """A pass that did no work must not be stamped onto the source.

    `run()` returns `skipped: True` when another pass already holds the state
    file. That result carries no checked_at and no counts, so recording it marks
    the source "synced" for a pass that ingested nothing: the bookkeeping-success
    failure mode this repo has produced more than once.

    The control run comes FIRST and must record. Without it this test asserts an
    empty list against a spy that may never have been wired to the object the
    route actually uses, which is exactly how the first version of it passed with
    the guard removed.
    """
    recorded: list[dict[str, Any]] = []

    class ScriptedSyncer:
        def __init__(self, result: dict[str, Any]) -> None:
            self._result = result

        async def status(self) -> dict[str, Any]:
            return {"tracked_files": 0}

        async def run(self, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
            return dict(self._result, dry_run=dry_run)

    admin = authed_client("test-admin")
    mesh = server_module.get_mesh()
    original = mesh.record_repo_content_sync

    async def spy(config: Any, result: dict[str, Any]) -> None:
        recorded.append(result)

    mesh.record_repo_content_sync = spy  # type: ignore[method-assign]
    try:
        app.state.repo_content_syncer = ScriptedSyncer(
            {"ok": True, "enabled": True, "files_ingested": 2, "repositories": []}
        )
        control = admin.post("/api/repo-content-sync/run", json={})
        assert control.status_code == 200
        assert len(recorded) == 1, (
            "the spy never fired on a real pass, so this test cannot detect the "
            "defect it exists for: it is not wired to the object the route uses"
        )

        app.state.repo_content_syncer = ScriptedSyncer(
            {
                "ok": True,
                "enabled": True,
                "skipped": True,
                "reason": "repo_content_sync_already_running",
            }
        )
        response = admin.post("/api/repo-content-sync/run", json={})
    finally:
        mesh.record_repo_content_sync = original  # type: ignore[method-assign]

    assert response.status_code == 200
    assert response.json()["skipped"] is True
    assert len(recorded) == 1, (
        "a skipped pass was recorded to the mesh, stamping the source 'synced' "
        f"with no checked_at and no counts: {recorded[1:]}"
    )
