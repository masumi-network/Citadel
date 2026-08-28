"""Regression: trust_tier/doc_type must see in-progress _citadel metadata."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from kb.access import SESSION_TRACES_DATASET, AccessStore
from kb.server import (
    SHARED_TRACE_MARKER,
    app,
    with_result_id,
    with_result_metadata,
)


def test_with_result_metadata_marks_session_traces() -> None:
    out = with_result_metadata(
        {
            "id": "trace-1",
            "title": "Dead-end route",
            "text": "Nested HTTP to /api/session deadlocked tools/list",
        },
        0,
        SESSION_TRACES_DATASET,
    )
    envelope = out["_citadel"]
    assert envelope["dataset"] == SESSION_TRACES_DATASET
    assert envelope["trust"] == "reference-only"
    assert envelope["doc_type"] == "session-trace"
    assert envelope["trust_tier"] == "reference-only"


def test_trace_body_cannot_outrank_the_reference_only_stamp() -> None:
    """A trace whose text mentions /skills/ must not classify as a skill doc."""
    out = with_result_metadata(
        {
            "id": "trace-2",
            "title": "Dead end",
            "text": "tried the /skills/masumi SKILL.md flow, docs.masumi said otherwise",
        },
        0,
        SESSION_TRACES_DATASET,
    )
    envelope = out["_citadel"]
    assert envelope["doc_type"] == "session-trace"
    assert envelope["trust_tier"] == "reference-only"


def test_deduped_node_copy_of_a_shared_trace_keeps_reference_only() -> None:
    """A volunteered trace is dual-written to the author's Node and session-traces.

    The Node copy wins dedup (search_across_datasets), and ``reference-only`` is
    stamped off the dataset alone — so the author's own dead-end trace used to
    come back with trust=None and, because its body mentioned ``/skills/``,
    doc_type=skill / trust_tier=verified. A record of what did NOT work was
    presented to an agent as verified knowledge.
    """
    from test_server import FakeCitadel, authed_client

    trace = (
        "# Compact Session Context\nAuthor-Seat: carol\n"
        "Dead end: tried /skills/masumi flow, it does not work"
    )

    class DualWritten(FakeCitadel):
        async def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [{"id": "trace-1", "text": trace, "dataset": kwargs["dataset"]}]

    app.state.access_store = AccessStore(Path(tempfile.mkdtemp()) / "access.json")
    admin = authed_client()
    token = admin.post("/api/access/seats", json={"name": "Carol", "slug": "carol"}).json()[
        "token"
    ]
    app.state.citadel = DualWritten()
    client = TestClient(app, base_url="https://testserver")

    response = client.post(
        "/search",
        json={"query": "dead end", "top_k": 5},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    hits = response.json()["results"]
    assert len(hits) == 1
    envelope = hits[0]["_citadel"]
    # The Node copy still wins dedup — only the trust marker rides along.
    assert envelope["dataset"] == "seat:carol"
    assert envelope["trust"] == "reference-only"
    assert envelope["doc_type"] == "session-trace"
    assert envelope["trust_tier"] == "reference-only"
    # The internal marker must never reach a caller.
    assert SHARED_TRACE_MARKER not in hits[0]


def test_content_sha256_stable_across_query_dependent_distance() -> None:
    """The same chunk served for two queries carries two cosine distances.

    content_sha256 must identify the chunk, not the query, or
    retrieval_eval.trust_observations stops seeing the same (id, sha) pair
    across questions and silently loses the metadata-stability check.
    """
    base = {"id": "chunk-1", "document_id": "doc-1", "text": "same chunk body"}
    near = with_result_metadata({**base, "distance": 0.10}, 0, "central")
    far = with_result_metadata({**base, "distance": 0.90}, 0, "central")

    assert near["_citadel"]["content_sha256"] == far["_citadel"]["content_sha256"]
    # The distance itself still reaches the caller for ranking.
    assert near["distance"] == 0.10
    assert far["distance"] == 0.90


def test_chunk_id_fallback_stable_across_query_dependent_distance() -> None:
    """An id-less hit derives ``chunk:<hash>`` from content, never the query."""
    near = with_result_id({"text": "same chunk body", "distance": 0.10})
    far = with_result_id({"text": "same chunk body", "distance": 0.90})

    assert near["id"] == far["id"]
    assert near["id"].startswith("chunk:")
    assert near["distance"] == 0.10
