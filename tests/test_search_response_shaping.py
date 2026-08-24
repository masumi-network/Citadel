"""Search response shaping: the four defects measured against production 2026-08-02.

Across 15 result pages / 117 slots, every hit shipped with an empty
``_citadel.provenance``, no relevance signal of any kind, no honest empty/low
confidence path, and a ``mode=docs`` call that was byte-identical to the plain
call down to its ``search_id``. Each test here fails on the pre-fix code.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from kb.config import CitadelConfig
from kb.conflicts import KnowledgeConflictStore
from kb.mesh import MeshState
from kb.search_format import (
    apply_query_ranking,
    filter_hits,
    is_search_content_hit,
    normalize_search_hit,
    parse_content_header,
    shape_public_search_hit,
)
from kb.server import (
    SearchBody,
    app,
    result_provenance,
    search_across_datasets,
    with_result_metadata,
)


def repo_doc_text(repo: str, path: str, body: str) -> str:
    """Exactly the shape format_repo_content_document writes."""
    return (
        f"# {repo}/{path}\n"
        "\n"
        f"Repository: {repo}\n"
        f"Source: https://github.com/{repo}/blob/f401d38/{path}\n"
        "Commit: f401d3896820ff35a82cf707818e308b67f5bce2\n"
        "Blob: a4b30a4548af239f695ba3cba1935b545e96d675\n"
        "\n"
        "---\n"
        "\n"
        f"{body}\n"
    )


LINEAR_NOTE_TEXT = (
    "# Linear SOK-623: coworker init UX\n"
    "\n"
    "- **State:** In Review (started)\n"
    "- **Team:** Sokosumi\n"
    "- **Priority:** 2\n"
    "- **Updated:** 2026-07-30\n"
    "- **URL:** https://linear.app/masumi/issue/SOK-623/coworker-init-ux\n"
    "\n"
    "The description block. It may even quote - **URL:** https://evil.example\n"
    "and that quote must never be credited as the issue's URL.\n"
)


class PageCitadel:
    """Returns a fixed candidate page and records the top_k it was asked for."""

    config = CitadelConfig(
        tenant_id="test",
        default_dataset="notes",
        admin_key="test-admin",
        reader_keys=("test-reader",),
        writer_keys=("test-writer",),
    )

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = list(results)
        self.requested_top_k: list[int] = []
        self.requested_kwargs: list[dict[str, Any]] = []

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.requested_top_k.append(kwargs["top_k"])
        self.requested_kwargs.append(dict(kwargs))
        return self.results[: kwargs["top_k"]]

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        return None


class CollisionCitadel:
    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        if kwargs["dataset"] == "seat:alice":
            return [{"type": "IndexSchema", "text": "shared answer"}]
        return [
            {
                "type": "DocumentChunk",
                "document_id": "central-document",
                "text": "shared answer",
            }
        ]


class FailingSearchCitadel:
    config = PageCitadel.config

    async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError(f"contextless query reached provider: {query} {kwargs}")


def shaped_client(citadel: Any) -> TestClient:
    app.state.citadel = citadel
    app.state.mesh = MeshState()
    app.state.conflict_store = KnowledgeConflictStore(
        Path(tempfile.mkdtemp()) / "conflicts.json"
    )
    client = TestClient(app, base_url="https://testserver")
    response = client.post("/admin/session", json={"access_key": "test-admin"})
    assert response.status_code == 200
    return client


def test_public_search_hit_drops_engine_fields_and_keeps_drilldown() -> None:
    raw = {
        "id": "chunk-1",
        "document_id": "document-1",
        "type": "IndexSchema",
        "text": "Rotate keys quarterly.",
        "source_url": "https://example.com/runbook",
        "source_pipeline": "internal-pipeline",
        "source_task": "internal-task",
        "source_node_set": "internal-node-set",
        "source_user": "default-user@example.invalid",
        "metadata": {"citadel_tags": ["ops"], "private": "internal"},
        "_citadel": {
            "rank": 1,
            "dataset": "masumi-network",
            "result_id": "chunk-1",
            "document_endpoint": "/api/documents/document-1",
            "retrieval": {"document_drilldown_available": True},
            "unexpected_internal": "drop-me",
        },
    }

    shaped = shape_public_search_hit(raw)

    assert shaped["id"] == "chunk-1"
    assert shaped["document_id"] == "document-1"
    assert shaped["qa_id"] == "chunk-1"
    assert shaped["text"] == "Rotate keys quarterly."
    assert shaped["source"] == "https://example.com/runbook"
    assert shaped["tags"] == ["ops"]
    assert shaped["dataset"] == "masumi-network"
    assert shaped["_citadel"]["document_endpoint"] == "/api/documents/document-1"
    forbidden = {
        "type",
        "metadata",
        "source_pipeline",
        "source_task",
        "source_node_set",
        "source_user",
    }
    assert forbidden.isdisjoint(shaped)
    assert "unexpected_internal" not in shaped["_citadel"]
    assert "projection" not in shaped["_citadel"]


def test_public_search_hit_uses_real_reference_locator() -> None:
    shaped = shape_public_search_hit(
        {
            "id": "linear-1",
            "source": "lifecycle",
            "text": "Subscription credits are missing.",
            "_citadel": {
                "provenance": {"source": "linear-issue"},
                "references": [
                    {
                        "title": "SOK-563",
                        "source_locator": "https://linear.app/masumi/issue/SOK-563/credits",
                    }
                ],
            },
        }
    )
    assert shaped["url"] == "https://linear.app/masumi/issue/SOK-563/credits"
    assert shaped["source_locator"] == shaped["url"]
    assert shaped["source_type"] == "linear-issue"
    assert shaped["citation"]["source_locator"] == shaped["url"]
    assert shaped["source"] != "lifecycle"


def test_public_search_hit_drops_unresolved_lifecycle_marker() -> None:
    shaped = shape_public_search_hit(
        {
            "id": "lifecycle-1",
            "source": "lifecycle",
            "source_type": "lifecycle",
            "text": "Retained source without connector metadata.",
            "_citadel": {"provenance": {"source": "lifecycle"}},
        }
    )

    assert shaped["source"] is None
    assert shaped["source_type"] is None
    assert shaped["url"] is None
    assert shaped["citation"] == {}
    assert shaped["_citadel"]["provenance"] == {}


def test_public_search_hit_uses_retained_source_locator_metadata() -> None:
    shaped = shape_public_search_hit(
        {
            "id": "linear-1",
            "source": "lifecycle",
            "metadata": {
                "source_locator": "https://linear.app/masumi/issue/SOK-563/credits"
            },
            "text": "# Linear SOK-563: Subscription credits",
        }
    )

    assert shaped["source_locator"] == "https://linear.app/masumi/issue/SOK-563/credits"
    assert shaped["citation"]["source_locator"] == shaped["source_locator"]


def test_public_search_hit_drops_nonfinite_relevance_and_keeps_finite_values() -> None:
    finite = shape_public_search_hit(
        {
            "text": "finite relevance",
            "_citadel": {
                "relevance": {"term_coverage": 0.5, "retriever_score": 0.42}
            },
        }
    )
    assert finite["_citadel"]["relevance"] == {
        "term_coverage": 0.5,
        "retriever_score": 0.42,
    }

    nonfinite = shape_public_search_hit(
        {
            "text": "nonfinite relevance",
            "_citadel": {
                "relevance": {
                    "term_coverage": float("inf"),
                    "retriever_score": float("nan"),
                }
            },
        }
    )
    relevance = nonfinite["_citadel"]["relevance"]
    assert "term_coverage" not in relevance
    assert "retriever_score" not in relevance


def test_typed_search_content_requires_a_document_identity() -> None:
    valid_index = {
        "type": "IndexSchema",
        "document_id": "document-1",
        "text": "A source-backed chunk.",
    }
    valid_chunk = {
        "type": "DocumentChunk",
        "document_id": "document-1",
        "text": "A production source-backed chunk.",
    }
    engine_row = {
        "type": "IndexSchema",
        "text": "Internal index schema row",
        "source_user": "default-user@example.invalid",
    }
    other_typed_row = {
        "type": "TextDocument",
        "document_id": "document-1",
        "text": "Internal document node",
    }

    assert is_search_content_hit(valid_index) is True
    assert is_search_content_hit(valid_chunk) is True
    assert is_search_content_hit(engine_row) is False
    assert is_search_content_hit(other_typed_row) is False
    assert is_search_content_hit({"type": "IndexSchema", "document_id": "document-1"}) is False


def test_invalid_primary_row_cannot_hide_valid_secondary_hit() -> None:
    merged = asyncio.run(
        search_across_datasets(
            CollisionCitadel(),
            query="shared answer",
            datasets=["seat:alice", "central"],
            sessions={},
            top_k=5,
        )
    )

    assert merged == [
        (
            "central",
            {
                "type": "DocumentChunk",
                "document_id": "central-document",
                "text": "shared answer",
            },
        )
    ]


def test_contextless_decision_query_does_not_call_search_provider() -> None:
    merged = asyncio.run(
        search_across_datasets(
            FailingSearchCitadel(),
            query="What did we decide about this?",
            datasets=["seat:alice", "central"],
            sessions={},
            top_k=5,
        )
    )

    assert merged == []


def test_contextless_decision_query_returns_clean_clarification() -> None:
    response = shaped_client(FailingSearchCitadel()).post(
        "/search",
        json={"query": "What did we decide about this?", "top_k": 5},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"] == []
    assert payload["code"] == "QUERY_CONTEXT_REQUIRED"
    assert payload["clarification_required"] is True
    assert payload["answerable"] is False
    assert "no subject" in payload["message"]
    assert "known_datasets" not in payload
    assert payload.get("warnings") in (None, [])


def test_search_endpoint_keeps_recorded_document_chunk_shape() -> None:
    document_id = "0c9d5df0-0000-4000-8000-000000000002"
    raw = {
        "id": "0c9d5df0-0000-4000-8000-000000000001",
        "created_at": 1782742208187,
        "updated_at": 1782742208187,
        "version": 1,
        "type": "DocumentChunk",
        "text": "synthetic chunk body",
        "chunk_index": 0,
        "document_id": document_id,
        "document_name": "text_0123456789abcdef0123456789abcdef",
        "metadata": {"index_fields": ["text"]},
        "_citadel": {
            "content_hint": "unclassified",
            "content_sha256": "0" * 64,
            "dataset": "seat:synthetic",
            "doc_type": "other",
            "document_endpoint": f"/api/documents/{document_id}",
            "provenance": {},
            "rank": 1,
            "result_id": "0c9d5df0-0000-4000-8000-000000000001",
        },
    }

    response = shaped_client(PageCitadel([raw])).post(
        "/search",
        json={"query": "synthetic chunk", "top_k": 5},
    )

    assert response.status_code == 200
    hits = response.json()["results"]
    assert hits
    assert hits[0]["text"] == "synthetic chunk body"
    assert hits[0]["document_id"] == document_id


def test_public_shape_keeps_validated_lifecycle_receipt_identity() -> None:
    promoted = with_result_metadata(
        {
            "type": "DocumentChunk",
            "document_id": "document-1",
            "text": "projected content",
            "_lifecycle": {
                "source_revision_id": "source-1",
                "projection_receipt_id": "receipt-1",
                "generation_id": "generation-1",
                "backend": "vector",
                "provider": "qdrant",
                "projection_version": "projection-v1",
                "state": "searchable",
                "private": "drop-me",
            },
        },
        0,
        "notes",
    )

    shaped = shape_public_search_hit(promoted)
    envelope = shaped["_citadel"]
    assert envelope["source_revision_id"] == "source-1"
    assert envelope["projection_receipt_id"] == "receipt-1"
    assert envelope["projection"] == {
        "generation_id": "generation-1",
        "backend": "vector",
        "provider": "qdrant",
        "projection_version": "projection-v1",
        "state": "searchable",
    }
    assert "private" not in envelope["projection"]


def test_public_search_shape_rejects_malformed_engine_values() -> None:
    for value in (None, 7, ["internal"], {"type": ["IndexSchema"], "text": "internal"}):
        assert is_search_content_hit(value) is False

    malformed = shape_public_search_hit(
        {
            "id": {"private": "value"},
            "document_id": ["private"],
            "text": "public content",
            "_citadel": {"rank": "first", "dataset": ["private"]},
        },
        index=2,
    )
    assert malformed["id"] is None
    assert malformed["document_id"] is None
    assert malformed["rank"] == 3
    assert malformed["dataset"] is None


# --- defect 1: provenance was empty on 117 of 117 hits ----------------------


def test_repo_content_header_populates_provenance() -> None:
    """The header the syncer writes is parsed back into the envelope."""
    text = repo_doc_text("masumi-network/sokosumi-docs", "docs/install.md", "# Install\n\nRun it.")

    provenance = result_provenance({"id": "c1", "text": text})

    assert provenance["repo"] == "masumi-network/sokosumi-docs"
    assert provenance["path"] == "docs/install.md"
    assert provenance["source_url"] == (
        "https://github.com/masumi-network/sokosumi-docs/blob/f401d38/docs/install.md"
    )
    assert provenance["commit"] == "f401d3896820ff35a82cf707818e308b67f5bce2"
    assert provenance["blob"] == "a4b30a4548af239f695ba3cba1935b545e96d675"
    # Header values are body text, so they are labelled as such — they must
    # never be mistaken for attested metadata.
    assert provenance["basis"] == "content-header"

    envelope = with_result_metadata({"id": "c1", "text": text}, 0, "masumi-network")["_citadel"]
    assert envelope["provenance"]["repo"] == "masumi-network/sokosumi-docs"


def test_linear_header_populates_issue_id_and_url() -> None:
    provenance = result_provenance({"id": "c2", "text": LINEAR_NOTE_TEXT})

    assert provenance["issue"] == "SOK-623"
    assert provenance["title"] == "coworker init UX"
    assert provenance["source_url"] == (
        "https://linear.app/masumi/issue/SOK-623/coworker-init-ux"
    )
    # The URL quoted inside the description was not credited.
    assert "evil.example" not in provenance["source_url"]


def test_lifecycle_source_key_preserves_linear_scope_after_public_shaping() -> None:
    raw = {
        "id": "linear-lifecycle-1",
        "source": "lifecycle",
        "source_type": "lifecycle",
        "metadata": {
            "source_key": "linear:issue:issue-123",
            "tags": ["linear-issue", "linear-sync"],
        },
        "text": "Subscription credits are missing from the issue description.",
    }

    enriched = with_result_metadata(raw, 0, "masumi-network", query="subscription credits")
    public = shape_public_search_hit(enriched)

    assert public["_citadel"]["provenance"]["source"] == "linear-issue"
    assert public["_citadel"]["provenance"]["basis"] == "lifecycle-source-key"
    assert filter_hits([public], source="linear-issue") == [public]


def test_lifecycle_source_key_preserves_linear_context_after_public_shaping() -> None:
    raw = {
        "id": "linear-context-lifecycle-1",
        "source": "lifecycle",
        "source_type": "lifecycle",
        "metadata": {
            "source_key": "linear:comment:comment-123",
            "source_locator": "https://linear.app/masumi/issue/SOK-623/coworker-init-ux",
        },
        "text": "The comment records the requested workflow change.",
    }

    enriched = with_result_metadata(raw, 0, "masumi-network", query="workflow change")
    public = shape_public_search_hit(enriched)

    assert public["source_type"] == "linear-context"
    assert public["source"] == public["source_locator"]
    assert public["citation"]["source_locator"] == public["source_locator"]
    assert public["_citadel"]["retrieval"]["citations_available"] is True
    assert public["_citadel"]["provenance"] == {
        "source": "linear-context",
        "source_url": public["source_locator"],
        "basis": "lifecycle-source-key",
    }
    assert filter_hits([public], source="linear-context") == [public]


def test_search_endpoint_applies_linear_scope_to_lifecycle_results() -> None:
    client = shaped_client(
        PageCitadel(
            [
                {
                    "id": "linear-lifecycle-2",
                    "source": "lifecycle",
                    "source_type": "lifecycle",
                    "metadata": {"source_key": "linear:issue:issue-456"},
                    "text": "Subscription credits are missing from the issue description.",
                }
            ]
        )
    )

    response = client.post(
        "/search",
        json={"query": "subscription credits", "top_k": 1, "source": "linear-issue"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [hit["id"] for hit in body["results"]] == ["linear-lifecycle-2"]
    assert body["filtering"]["candidates_matched"] == 1


def test_search_endpoint_applies_linear_context_scope_to_lifecycle_results() -> None:
    client = shaped_client(
        PageCitadel(
            [
                {
                    "id": "linear-context-lifecycle-2",
                    "source": "lifecycle",
                    "source_type": "lifecycle",
                    "metadata": {
                        "source_key": "linear:comment:comment-456",
                        "source_locator": "https://linear.app/masumi/issue/SOK-623/coworker-init-ux",
                    },
                    "text": "The comment records the requested workflow change.",
                }
            ]
        )
    )

    response = client.post(
        "/search",
        json={"query": "workflow change", "top_k": 1, "source": "linear-context"},
    )

    assert response.status_code == 200
    body = response.json()
    assert [hit["id"] for hit in body["results"]] == ["linear-context-lifecycle-2"]
    assert body["results"][0]["source_type"] == "linear-context"
    assert body["filtering"]["applied"]["source"] == "linear-context"
    assert body["filtering"]["candidates_matched"] == 1


def test_search_endpoint_exact_linear_key_rejects_cross_reference() -> None:
    exact = LINEAR_NOTE_TEXT.replace("SOK-623", "SOK-563")
    client = shaped_client(
        PageCitadel(
            [
                {
                    "id": "cross-reference",
                    "source": "lifecycle",
                    "source_type": "lifecycle",
                    "metadata": {"source_key": "linear:issue:issue-618"},
                    "text": (
                        "# Linear SOK-618: Other issue\n\n"
                        "The description mentions SOK-563."
                    ),
                },
                {
                    "id": "exact-issue",
                    "source": "lifecycle",
                    "source_type": "lifecycle",
                    "metadata": {"source_key": "linear:issue:issue-563"},
                    "text": exact,
                },
            ]
        )
    )

    response = client.post(
        "/search",
        json={"query": "SOK-563", "top_k": 5, "source": "linear-issue"},
    )

    assert response.status_code == 200
    assert [hit["id"] for hit in response.json()["results"]] == ["exact-issue"]


def test_lifecycle_search_marks_retained_document_drilldown_available() -> None:
    class Source:
        dataset = "notes"

    class Store:
        def get_source_revision(self, source_revision_id: str) -> Any:
            return Source() if source_revision_id == "source-1" else None

    citadel = PageCitadel(
        [
            {
                "id": "chunk-1",
                "document_id": "source-1",
                "source": "lifecycle",
                "_lifecycle": {
                    "source_revision_id": "source-1",
                    "projection_receipt_id": "receipt-1",
                    "backend": "vector",
                    "provider": "qdrant",
                    "projection_version": "v1",
                    "state": "searchable",
                },
                "text": "A retained source.",
            }
        ]
    )
    citadel.lifecycle_store = Store()
    client = shaped_client(citadel)

    response = client.post("/search", json={"query": "retained source", "top_k": 1})

    assert response.status_code == 200
    hit = response.json()["results"][0]
    assert hit["document_drilldown_available"] is True
    assert hit["_citadel"]["document_endpoint"] == "/api/documents/source-1"


def test_lifecycle_source_key_preserves_repo_scope_after_public_shaping() -> None:
    raw = {
        "id": "repo-lifecycle-1",
        "source": "lifecycle",
        "source_type": "lifecycle",
        "metadata": {
            "source_key": "github:masumi-network/Sokosumi-MCP:path:README.md",
        },
        "text": "MCP payment integration details.",
    }

    enriched = with_result_metadata(raw, 0, "masumi-network", query="MCP payment")
    public = shape_public_search_hit(enriched)

    assert public["_citadel"]["provenance"]["source"] == "repo-content"
    assert public["_citadel"]["provenance"]["repo"] == "masumi-network/Sokosumi-MCP"
    assert public["_citadel"]["provenance"]["path"] == "README.md"
    assert filter_hits([public], repo="masumi-network/Sokosumi-MCP") == [public]
    assert filter_hits([public], path="README.md") == [public]


def test_lifecycle_source_descriptor_fails_closed_for_malformed_keys() -> None:
    malformed = [
        "linear:issue:issue:123",
        "github:masumi-network/Sokosumi-MCP:unexpected:path:README.md",
        "github:masumi-network/Sokosumi-MCP:path:README.md\nforged",
        "github:masumi-network/Sokosumi-MCP:path:README.md\u0085forged",
    ]

    for source_key in malformed:
        provenance = result_provenance(
            {
                "id": "malformed-source-key",
                "source": "lifecycle",
                "metadata": {"source_key": source_key},
                "text": "untrusted lifecycle result",
            }
        )
        assert provenance["source"] == "lifecycle"
        assert "repo" not in provenance
        assert "path" not in provenance


def test_lifecycle_parent_source_key_wins_for_chunked_results() -> None:
    provenance = result_provenance(
        {
            "id": "chunked-repo",
            "source": "lifecycle",
            "metadata": {
                "source_key": "chunk-parent:opaque:0",
                "lifecycle_parent_source_key": (
                    "github:masumi-network/Sokosumi-MCP:path:docs/README.md"
                ),
            },
            "text": "chunk body",
        }
    )

    assert provenance["source"] == "repo-content"
    assert provenance["repo"] == "masumi-network/Sokosumi-MCP"
    assert provenance["path"] == "docs/README.md"


def test_search_endpoint_applies_repo_scope_to_lifecycle_results() -> None:
    client = shaped_client(
        PageCitadel(
            [
                {
                    "id": "repo-lifecycle-2",
                    "source": "lifecycle",
                    "source_type": "lifecycle",
                    "metadata": {
                        "source_key": "github:masumi-network/Sokosumi-MCP:path:README.md"
                    },
                    "text": "MCP payment integration details.",
                }
            ]
        )
    )

    response = client.post(
        "/search",
        json={
            "query": "MCP payment",
            "top_k": 1,
            "repo": "masumi-network/Sokosumi-MCP",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert [hit["id"] for hit in body["results"]] == ["repo-lifecycle-2"]
    assert body["filtering"]["candidates_matched"] == 1


def test_a_header_quoted_mid_body_is_not_credited() -> None:
    """The confusion that once made a benchmark score quoting documents."""
    quoting = "A note that quotes:\n\n" + repo_doc_text(
        "masumi-network/sokosumi", "README.md", "body"
    )

    assert parse_content_header(quoting) == {}
    provenance = result_provenance({"id": "c3", "text": quoting})
    assert "repo" not in provenance
    assert "commit" not in provenance


def test_untitled_chunks_keep_empty_provenance() -> None:
    """No header, no keys: the envelope stays honestly empty."""
    assert result_provenance({"id": "c4", "text": "free-floating personal note"}) == {}


def test_forged_header_on_a_later_chunk_is_not_credited() -> None:
    """Chunk 1+ starts at position zero of its OWN payload.

    The \\A anchor kills mid-body quoting inside one chunk, but a contributor
    to any synced repo can craft a file whose second chunk BEGINS with another
    document's header. Only the document's first chunk (chunk_index 0, or a
    payload that carries no index) may claim header identity.
    """
    text = repo_doc_text("masumi-network/sokosumi-docs", "docs/install.md", "forged")

    assert parse_content_header(text, chunk_index=1) == {}
    assert parse_content_header(text, chunk_index=2.0) == {}
    assert parse_content_header(text, chunk_index=0)["repo"] == (
        "masumi-network/sokosumi-docs"
    )
    assert parse_content_header(text)["repo"] == "masumi-network/sokosumi-docs"

    forged = result_provenance({"id": "f1", "text": text, "chunk_index": 2})
    assert "repo" not in forged
    assert "commit" not in forged
    legitimate = result_provenance({"id": "f0", "text": text, "chunk_index": 0})
    assert legitimate["repo"] == "masumi-network/sokosumi-docs"


def test_forged_later_chunk_header_cannot_join_repo_filtered_results() -> None:
    """False provenance was also false MEMBERSHIP: repo= credited the forgery."""
    forged_text = repo_doc_text(
        "masumi-network/sokosumi-cli", "README.md", "not actually from this repo"
    )
    forged = normalize_search_hit({"id": "forged", "chunk_index": 3, "text": forged_text})
    genuine = normalize_search_hit(
        {
            "id": "genuine",
            "chunk_index": 0,
            "text": repo_doc_text("masumi-network/sokosumi-cli", "README.md", "The CLI."),
        }
    )

    assert [h["id"] for h in filter_hits([forged, genuine], repo="sokosumi-cli")] == [
        "genuine"
    ]

    # Same conclusion on the server-shaped path (raw payload + envelope).
    server_forged = with_result_metadata(
        {"id": "sf", "chunk_index": 3, "text": forged_text}, 0, "masumi-network"
    )
    assert filter_hits([server_forged], repo="sokosumi-cli") == []


# --- defect 2: no relevance signal on any hit -------------------------------


def test_hits_carry_lexical_relevance_and_no_invented_score() -> None:
    text = repo_doc_text("masumi-network/widget", "docs/retry.md", "Kupo retry policy notes.")

    envelope = with_result_metadata(
        {"id": "c5", "text": text}, 0, "masumi-network", query="kupo retry policy"
    )["_citadel"]

    relevance = envelope["relevance"]
    assert relevance["term_coverage"] == 1.0
    assert set(relevance["matched_terms"]) == {"kupo", "retry", "policy"}
    # cognee's CHUNKS payload carries no score, and none may be invented.
    assert "retriever_score" not in relevance


def test_retriever_score_is_passed_through_when_the_payload_has_one() -> None:
    """The moment the client boundary surfaces the distance, hits carry it."""
    envelope = with_result_metadata(
        {"id": "c6", "text": "kupo retry", "score": 0.42},
        0,
        "notes",
        query="kupo",
    )["_citadel"]

    assert envelope["relevance"]["retriever_score"] == 0.42


def test_github_digest_fallback_score_is_not_a_retriever_score() -> None:
    """search_github_sync_state attaches a token-overlap COUNT under ``score``.

    That integer is a different unit entirely; passing it through surfaced an
    unbounded count as ``retriever_score`` and flipped
    ``retriever_scores_available`` true on the one search path that has no
    retriever at all — contradicting the pass-through's own contract.
    """
    envelope = with_result_metadata(
        {
            "id": "ghsync:abc123",
            "source": "github_sync_state",
            "title": "GitHub source digest",
            "content": "sokosumi repository digest content",
            "score": 7,
        },
        0,
        "github-sync",
        query="sokosumi digest",
    )["_citadel"]

    assert "retriever_score" not in envelope["relevance"]


# --- defect 3: absent content returned confident nonsense -------------------


def test_absent_content_query_is_flagged_not_confident() -> None:
    citadel = PageCitadel(
        [
            {"id": "n1", "text": "notes about the dashboard layout"},
            {"id": "n2", "text": "linear board grooming session"},
            {"id": "n3", "text": "weekly planning discussion"},
        ]
    )
    client = shaped_client(citadel)

    response = client.post("/search", json={"query": "quantum zebra teleportation"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 3
    relevance = body["relevance"]
    assert relevance["retriever_scores_available"] is False
    assert relevance["max_term_coverage"] == 0.0
    assert relevance["no_lexical_match"] is True
    assert any("No result contains any query term" in w for w in body["warnings"])
    for hit in body["results"]:
        assert hit["_citadel"]["relevance"]["term_coverage"] == 0.0


def test_matching_content_is_not_flagged() -> None:
    """The marker must be earnable in both directions, not another inert guard."""
    citadel = PageCitadel(
        [{"id": "n1", "text": "the kupo retry policy is exponential backoff"}]
    )
    client = shaped_client(citadel)

    response = client.post("/search", json={"query": "kupo retry policy"})

    body = response.json()
    assert body["relevance"]["no_lexical_match"] is False
    assert body["relevance"]["max_term_coverage"] == 1.0
    assert not any(
        "No result contains any query term" in w for w in body.get("warnings", [])
    )


def test_general_search_ranks_lexical_match_before_provider_noise() -> None:
    citadel = PageCitadel(
        [
            {"id": "noise", "text": "unrelated deployment note"},
            {"id": "answer", "text": "subscription credits are missing"},
        ]
    )
    client = shaped_client(citadel)

    response = client.post("/search", json={"query": "subscription credits", "top_k": 1})

    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "answer"


# --- defect 4: mode=docs was a no-op and filters matched body text ----------


def test_docs_mode_gets_its_own_search_id() -> None:
    """Same query, same hits, same count — the ids must still differ.

    Measured: the plain call and the mode=docs call returned the same page AND
    the same search_id, so telemetry could not tell them apart.
    """
    results = [
        {"id": "d1", "text": repo_doc_text("masumi-network/a", "docs/one.md", "guide one")},
        {"id": "d2", "text": repo_doc_text("masumi-network/b", "docs/two.md", "guide two")},
    ]
    plain = shaped_client(PageCitadel(results)).post(
        "/search", json={"query": "sokosumi install guide"}
    )
    docs = shaped_client(PageCitadel(results)).post(
        "/search", json={"query": "sokosumi install guide", "mode": "docs"}
    )

    assert plain.status_code == 200 and docs.status_code == 200
    assert plain.json()["search_id"] != docs.json()["search_id"]
    assert docs.json()["docs_mode"] is True
    assert docs.json()["mode"] == "docs"
    assert plain.json()["docs_mode"] is False
    assert "mode" not in plain.json()


def test_search_id_stays_deterministic_for_identical_calls() -> None:
    """Distinguishing filtered calls must not break follow-up feedback linking."""
    results = [{"id": "d1", "text": repo_doc_text("masumi-network/a", "docs/one.md", "guide")}]
    first = shaped_client(PageCitadel(results)).post(
        "/search", json={"query": "sokosumi install guide", "mode": "docs"}
    )
    second = shaped_client(PageCitadel(results)).post(
        "/search", json={"query": "sokosumi install guide", "mode": "docs"}
    )

    assert first.json()["search_id"] == second.json()["search_id"]


def test_docs_mode_ranks_within_a_class_by_term_coverage() -> None:
    """Ten same-class hits used to keep their input order whatever the query."""
    miss = {
        "id": "m",
        "text": repo_doc_text("masumi-network/a", "docs/misc.md", "unrelated prose"),
    }
    match = {
        "id": "h",
        "text": repo_doc_text(
            "masumi-network/b", "docs/retry.md", "kupo indexer retry policy"
        ),
    }

    ranked = apply_query_ranking([miss, match], "kupo retry policy", mode="docs")

    assert ranked[0] is match, [r.get("id") for r in ranked]


def test_single_literal_query_ranks_exact_match_above_unrelated_hit() -> None:
    """A unique identifier must outrank a cross-dataset semantic neighbour."""
    miss = {"id": "miss", "text": "Central note with unrelated prose"}
    match = {"id": "hit", "text": "UAT marker quokka-beacon-8823"}

    ranked = apply_query_ranking([miss, match], "quokka-beacon-8823")

    assert ranked[0] is match


def test_general_query_uses_lexical_evidence_when_provider_order_is_weak() -> None:
    miss = {"id": "miss", "text": "unrelated deployment note"}
    match = {"id": "hit", "text": "subscription credits are missing"}

    ranked = apply_query_ranking([miss, match], "subscription credits")

    assert ranked[0] is match


def test_general_query_keeps_provider_order_without_lexical_evidence() -> None:
    first = {"id": "first", "text": "semantic neighbor one"}
    second = {"id": "second", "text": "semantic neighbor two"}

    ranked = apply_query_ranking([first, second], "subscription credits")

    assert [item["id"] for item in ranked] == ["first", "second"]


def test_repo_filter_is_identity_not_body_substring() -> None:
    """repo="sokosumi-cli" returned a sokosumi-DOCS file that mentioned the CLI."""
    docs_file = normalize_search_hit(
        {
            "id": "docs",
            "text": repo_doc_text(
                "masumi-network/sokosumi-docs",
                "docs/install.md",
                "Install the CLI:\n\n    npm install -g sokosumi-cli\n",
            ),
        }
    )
    cli_file = normalize_search_hit(
        {
            "id": "cli",
            "text": repo_doc_text("masumi-network/sokosumi-cli", "README.md", "The CLI."),
        }
    )

    kept = filter_hits([docs_file, cli_file], repo="sokosumi-cli")

    assert [h["id"] for h in kept] == ["cli"]
    # Identity still matches the org prefix and the exact full name.
    assert len(filter_hits([docs_file, cli_file], repo="masumi-network")) == 2
    assert [
        h["id"]
        for h in filter_hits([docs_file, cli_file], repo="masumi-network/sokosumi-docs")
    ] == ["docs"]


def test_repo_filter_fails_closed_without_identity() -> None:
    """A hit that cannot say which repo it is from never satisfies repo=."""
    anonymous = normalize_search_hit(
        {"id": "anon", "text": "prose that merely mentions sokosumi-cli"}
    )

    assert filter_hits([anonymous], repo="sokosumi-cli") == []


def test_filtered_search_over_fetches_and_fills_the_page() -> None:
    """top_k=5 with a filter used to return whatever survived from 5 candidates."""
    noise = [{"id": f"n{i}", "text": f"digest chatter number {i}"} for i in range(8)]
    matching = [
        {
            "id": f"w{i}",
            "text": repo_doc_text("masumi-network/widget", f"docs/page{i}.md", f"widget {i}"),
        }
        for i in range(7)
    ]
    citadel = PageCitadel(noise + matching)
    client = shaped_client(citadel)

    response = client.post(
        "/search",
        json={
            "query": "widget docs",
            "top_k": 5,
            "repo": "masumi-network/widget",
            "types": ["repo-content"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    # Over-fetched: the retriever was asked for more than top_k candidates.
    assert citadel.requested_top_k == [15]
    assert citadel.requested_kwargs[0]["repo"] == "masumi-network/widget"
    assert citadel.requested_kwargs[0]["source"] == "repo-content"
    # And the page is FULL, not the 0 of 5 the old order of operations left.
    assert len(body["results"]) == 5
    assert all(
        hit["_citadel"]["provenance"]["repo"] == "masumi-network/widget"
        for hit in body["results"]
    )
    assert body["filtering"]["candidates_fetched"] == 15
    assert body["filtering"]["candidates_matched"] == 7
    assert body["filtering"]["returned"] == 5
    assert body["filtering"]["applied"]["repo"] == "masumi-network/widget"
    assert body["filtering"]["applied"]["source"] == "repo-content"
    assert "types" not in body["filtering"]["applied"]


def test_multiple_connector_type_aliases_fail_closed() -> None:
    """Ambiguous legacy connector aliases must not broaden the search."""
    filters = SearchBody(
        query="recent work",
        types=["repo-content", "linear-issue"],
    ).filter_kwargs()

    assert filters["source"] is None
    assert filters["types"] == ["repo-content", "linear-issue"]


def test_empty_retrieval_with_filters_gets_dataset_help_not_filter_blame() -> None:
    """candidates_fetched == 0: nothing was excluded by filters.

    The old gate fired on matched < top_k alone, so an empty retrieval under a
    filter claimed "post-retrieval filters excluded the rest" (literally false)
    AND suppressed the known_datasets help — exactly when the caller needed it.
    """
    client = shaped_client(PageCitadel([]))

    response = client.post(
        "/search",
        json={"query": "widget docs", "top_k": 5, "repo": "masumi-network/widget"},
    )

    body = response.json()
    assert body["results"] == []
    assert body["filtering"]["candidates_fetched"] == 0
    assert not any(
        "post-retrieval filters excluded" in warning
        for warning in body.get("warnings", [])
    )
    assert "note" in body
    assert body["known_datasets"]


def test_short_page_where_filters_excluded_nothing_is_not_blamed_on_filters() -> None:
    """matched == fetched < top_k: retrieval found nothing else, say so."""
    citadel = PageCitadel(
        [
            {
                "id": "w0",
                "text": repo_doc_text("masumi-network/widget", "docs/only.md", "widget"),
            }
        ]
    )
    client = shaped_client(citadel)

    response = client.post(
        "/search",
        json={"query": "widget docs", "top_k": 5, "repo": "masumi-network/widget"},
    )

    body = response.json()
    assert len(body["results"]) == 1
    assert body["filtering"]["candidates_fetched"] == 1
    assert body["filtering"]["candidates_matched"] == 1
    assert not any(
        "post-retrieval filters excluded" in warning
        for warning in body.get("warnings", [])
    )


def test_short_filtered_page_is_documented_honestly() -> None:
    citadel = PageCitadel(
        [{"id": f"n{i}", "text": f"digest chatter number {i}"} for i in range(9)]
        + [
            {
                "id": "w0",
                "text": repo_doc_text("masumi-network/widget", "docs/only.md", "widget"),
            }
        ]
    )
    client = shaped_client(citadel)

    response = client.post(
        "/search",
        json={"query": "widget docs", "top_k": 5, "repo": "masumi-network/widget"},
    )

    body = response.json()
    assert len(body["results"]) == 1
    assert body["filtering"]["candidates_matched"] == 1
    assert any(
        "post-retrieval filters excluded" in warning for warning in body["warnings"]
    )


# --- head-of-document truncation hides the match ----------------------------


def test_match_context_survives_head_truncation() -> None:
    """The envelope carries a window around the match, far past any head cut.

    MCP trims ``text`` to its first ~2,000 characters. For a 6,000-character
    document whose answer sits at the end, the head shows the provenance header
    and imports — exactly where the answer is not. ``_citadel.relevance.
    match_context`` is computed from the FULL text before any trim.
    """
    filler = "\n".join(f"unrelated line {i} about nothing in particular" for i in range(120))
    text = repo_doc_text(
        "masumi-network/widget",
        "docs/retry.md",
        filler + "\n\nThe kupo retry policy is exponential backoff with jitter.\n",
    )
    assert len(text) > 4000

    envelope = with_result_metadata(
        {"id": "c7", "text": text}, 0, "masumi-network", query="kupo retry policy"
    )["_citadel"]

    context = envelope["relevance"]["match_context"]
    assert context["offset"] > 2000
    assert "kupo retry policy" in context["text"].lower()


def test_cli_snippet_windows_long_texts_around_the_match() -> None:
    filler = " ".join(f"word{i}" for i in range(400))
    text = filler + " the kupo retry policy is exponential backoff"

    hit = normalize_search_hit({"id": "x", "text": text}, query="kupo retry policy")

    assert hit["snippet"].startswith("…")
    assert "kupo retry policy" in hit["snippet"]
    assert hit["term_coverage"] == 1.0
    # Short texts keep the whole-head snippet: nothing to window.
    short = normalize_search_hit({"id": "y", "text": "kupo retry policy"}, query="kupo")
    assert short["snippet"] == "kupo retry policy"
