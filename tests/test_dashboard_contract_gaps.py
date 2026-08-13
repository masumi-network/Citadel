"""Server-side gaps the dashboard port needs closed.

Each test names the gap from docs/dashboard-api-contract.md that it pins. The
theme running through all of them is that the redesigned views were specified
against data no endpoint returned, and the worst case, gap 7, had the UI about
to assert a security property nothing checked.

Every one of these fields is additive. The tests assert that too: a field the
ported pages already read must not move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import kb.server as server_module
from kb.access import AccessStore, now_iso
from kb.mesh import MeshState
from kb.promotion_queue import build_pending_item, scan_candidate
from kb.promotion_refs import ReferenceAssessment
from kb.server import app

from test_server import authed_client

# One repo root for every static-asset pin below, the same way
# tests/test_mesh_stats_readers.py locates kb/static.
REPO = Path(server_module.__file__).resolve().parent.parent

# Split so this file cannot be flagged by its own secret scanner in CI.
FAKE_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


# --------------------------------------------------------------------------
# Gap 7: per-promotion secret scan
# --------------------------------------------------------------------------


def test_promotion_candidate_with_a_secret_is_scanned_and_blocked() -> None:
    """The Review view says "secret scan passed". Something must actually scan.

    Before this, nothing scanned a promotion candidate at all. The queue carried
    `sensitive` from LLM enrichment, which is a model's opinion about tone, not
    a check for credentials, so the UI would have asserted a property the system
    never tested.
    """
    result = scan_candidate(f"Deploy notes. The token is {FAKE_TOKEN} and it works.")

    assert result["blocked"] is True
    assert result["ok"] is False
    assert result["highest_severity"] == "critical"
    assert result["finding_count"] >= 1
    assert any(f["category"] == "github_token" for f in result["findings"])


def test_a_clean_candidate_passes_the_scan() -> None:
    """A pass has to be a real pass, or the blocked case means nothing."""
    result = scan_candidate("We chose smoothstep edges because the diagram reads better.")

    assert result["ok"] is True
    assert result["blocked"] is False
    assert result["finding_count"] == 0
    assert result["findings"] == []


def test_the_scan_result_never_carries_the_secret_it_found() -> None:
    """The verdict is served to a reviewer, so it must not republish the secret.

    This is the failure that would turn a security feature into a leak: the
    scanner finds a credential in a candidate and the API hands it back in the
    finding. Asserted on the serialised payload, not the field names.
    """
    import json

    serialised = json.dumps(scan_candidate(f"key={FAKE_TOKEN}"))

    assert FAKE_TOKEN not in serialised
    assert "A1b2C3d4" not in serialised
    # What may appear is metadata about the match, never the match.
    assert "pattern=github_token" in serialised


def test_a_queued_candidate_carries_its_scan_and_the_reviewer_sees_a_failure(
    tmp_path: Any,
) -> None:
    """A failing scan reaches the reviewer rather than being swallowed."""
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()

    item = build_pending_item(
        seat_slug="alice",
        seat_dataset="seat:alice",
        candidate_text=f"Rotate this: {FAKE_TOKEN}",
        assessment=ReferenceAssessment(status="new_org_project", reason="no_match"),
        created_at=now_iso(),
    )
    app.state.access_store.add_promotion_pending(item)

    payload = admin.get("/api/promotion/pending").json()

    assert payload["count"] == 1
    served = payload["items"][0]
    assert served["secret_scan"]["blocked"] is True
    assert served["secret_scan"]["highest_severity"] == "critical"
    assert served["secret_scan"]["scanned_at"]
    # The body is still stripped: the scan does not reopen it.
    assert "candidate_text" not in served
    assert FAKE_TOKEN not in str(served)


def test_an_item_queued_before_the_scan_existed_is_scanned_on_read(
    tmp_path: Any,
) -> None:
    """Legacy rows must not render as "passed" merely because they are old.

    Items enqueued before this feature carry secret_scan: None. Serving that
    would either show a claim nobody checked or force the UI to invent one, so
    the read path scans them, and marks the verdict as deferred.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()

    item = build_pending_item(
        seat_slug="alice",
        seat_dataset="seat:alice",
        candidate_text=f"legacy row holding {FAKE_TOKEN}",
        assessment=ReferenceAssessment(status="new_org_project", reason="no_match"),
        created_at=now_iso(),
    )
    # Simulate a row written before the field existed.
    legacy = type(item)(**{**item.to_dict(), "repo_hints": (), "secret_scan": None})
    app.state.access_store.add_promotion_pending(legacy)

    served = admin.get("/api/promotion/pending").json()["items"][0]

    assert served["secret_scan"] is not None, "an unscanned item must not reach the UI"
    assert served["secret_scan"]["blocked"] is True
    assert served["secret_scan"]["deferred"] is True


# --------------------------------------------------------------------------
# Gaps 1 and 2: /api/me/summary counts
# --------------------------------------------------------------------------


def test_me_summary_gains_readable_count_and_weekly_capture_without_losing_fields(
    tmp_path: Any,
) -> None:
    """Additive: every field the ported Home already reads stays put."""
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    payload = client.get("/api/me/summary").json()

    for existing in (
        "ok",
        "seat_slug",
        "node_label",
        "node_dataset",
        "search_datasets",
        "document_count",
        "pending_promotions",
        "last_ingest_at",
        "recent_activity",
        "empty",
        "checklist",
    ):
        assert existing in payload, f"/api/me/summary lost {existing}"

    assert "readable_document_count" in payload
    assert "captured_last_7d" in payload
    # Null, never a fabricated zero: zero is a claim the caller can read nothing.
    assert payload["readable_document_count"] is None or isinstance(
        payload["readable_document_count"], int
    )


def test_readable_document_count_is_scoped_to_the_caller(tmp_path: Any) -> None:
    """Gap 1, and the constraint that matters more than the number.

    The count is summed over resolve_search_datasets() for the calling identity,
    so a dataset the caller cannot search can never be added in. Here Bob's Node
    holds documents and Alice must not see them counted.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    alice_token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()[
        "token"
    ]
    admin.post("/api/access/seats", json={"name": "Bob", "slug": "bob"})

    # A generous per-dataset census, including a seat Alice has no access to.
    counts = {"masumi-network": 10, "seat:alice": 3, "seat:bob": 99}

    class _Cognee:
        async def document_counts_by_dataset(self) -> dict[str, int]:
            return counts

    class _Citadel:
        config = app.state.citadel.config
        cognee = _Cognee()

    original = app.state.citadel
    app.state.citadel = _Citadel()
    try:
        alice = TestClient(app, base_url="https://testserver")
        payload = alice.get(
            "/api/me/summary", headers={"Authorization": f"Bearer {alice_token}"}
        ).json()
    finally:
        app.state.citadel = original

    total = payload["readable_document_count"]
    assert total is not None
    # Whatever Alice can search, Bob's 99 are not part of it.
    assert total < 99, "another seat's documents were counted"
    assert "seat:bob" not in (payload["search_datasets"] or [])


def test_captured_last_7d_counts_only_recent_successful_node_ingests(
    tmp_path: Any,
) -> None:
    """Gap 2, from the durable audit trail rather than the transient mesh.

    The dashboard derives this today by filtering the mesh event page, so it
    drops after every redeploy. Counting audit rows fixes that, but only if the
    window, the success flag and the dataset are all honoured.
    """
    import json
    from datetime import UTC, datetime, timedelta

    from kb.server import _captured_last_7d

    now = datetime.now(UTC)
    rows = [
        # (dataset, success, accepted, age_days) -> counted?
        ("seat:alice", True, True, 1),  # yes
        ("seat:alice", True, True, 6),  # yes
        ("seat:alice", True, True, 30),  # no: outside the window
        ("seat:alice", False, True, 1),  # no: failed
        ("seat:alice", True, False, 1),  # no: rejected by the server
        ("seat:bob", True, True, 1),  # no: another seat's Node
    ]
    events = []
    for index, (dataset, success, accepted, age) in enumerate(rows):
        events.append(
            {
                "id": f"audit_{index}",
                "action": "ingest",
                "actor_id": None,
                "actor_kind": None,
                "actor_name": None,
                "role": None,
                "dataset": dataset,
                "success": success,
                "detail": {"accepted": accepted},
                "created_at": (now - timedelta(days=age)).isoformat(),
            }
        )
    # Seeded on disk rather than through record_event, which stamps its own
    # created_at and so cannot express the out-of-window row.
    path = tmp_path / "access.json"
    path.write_text(
        json.dumps({"principals": [], "tokens": [], "audit_events": events}),
        encoding="utf-8",
    )
    app.state.access_store = AccessStore(path)

    assert _captured_last_7d("seat:alice") == 2
    assert _captured_last_7d("seat:bob") == 1
    # No seat means no Node, so None rather than zero.
    assert _captured_last_7d(None) is None


# --------------------------------------------------------------------------
# Gap 8: per-source failure state
# --------------------------------------------------------------------------


def test_sources_report_last_error_and_a_success_clears_it(tmp_path: Any) -> None:
    """Gap 8. A source that failed for any reason must be visible as failing.

    "Failing" was inferred from open_conflicts and a GitHub-only scan flag, so
    an expired token or an upstream 500 looked healthy.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    payload = client.get("/api/sources").json()
    for source in payload["sources"]:
        assert "last_error" in source, source.get("source_type")
        assert "last_error_at" in source, source.get("source_type")
    repo_content = [s for s in payload["sources"] if s["source_type"] == "github_repo_content"][0]
    assert repo_content["last_error"] is None

    app.state.access_store.record_event(
        action="repo_content_sync.run",
        actor=None,
        success=False,
        detail={"error_type": "GitHubAPIError", "error": "GitHub API returned 502"},
    )
    failed = client.get("/api/sources").json()
    repo_content = [s for s in failed["sources"] if s["source_type"] == "github_repo_content"][0]
    assert repo_content["last_error"] == "GitHub API returned 502"
    assert repo_content["last_error_at"]

    # A later success clears it, or a source that failed once looks broken forever.
    app.state.access_store.record_event(
        action="repo_content_sync.run", actor=None, success=True, detail={}
    )
    recovered = client.get("/api/sources").json()
    repo_content = [s for s in recovered["sources"] if s["source_type"] == "github_repo_content"][0]
    assert repo_content["last_error"] is None


def test_a_source_error_is_redacted_before_it_is_published(tmp_path: Any) -> None:
    """/api/sources is reader-gated, and upstream errors quote URLs with tokens."""
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    app.state.access_store.record_event(
        action="repo_content_sync.run",
        actor=None,
        success=False,
        detail={"error": f"401 fetching https://x?access_token={FAKE_TOKEN}"},
    )

    body = client.get("/api/sources").text

    assert FAKE_TOKEN not in body
    assert "A1b2C3d4" not in body


# --------------------------------------------------------------------------
# Gap 9: a seat-less token is a status, not an absence
# --------------------------------------------------------------------------


def test_seatless_tokens_are_explicit_on_both_endpoints(tmp_path: Any) -> None:
    """Gap 9. A token with no seat authenticates and then cannot search.

    That state has cost debugging time because it reads as "invalid token". It
    existed only as the absence of a seat_slug, discoverable by joining two
    endpoints client-side.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    client.post("/api/access/seats", json={"name": "Alice", "slug": "alice"})
    client.post(
        "/api/access/tokens",
        json={"name": "research-agent", "role": "reader", "kind": "service_account"},
    )

    seats = client.get("/api/access/seats").json()
    assert seats["seats"], "the seated principal is still listed"
    assert seats["seatless_token_count"] >= 1
    orphan = [t for t in seats["seatless_tokens"] if t["name"] == "research-agent"]
    assert orphan, "a token with no seat must appear as its own row"
    assert orphan[0]["seat_slug"] is None
    assert orphan[0]["can_search"] is False, "no seat and no dataset means no search"

    # And the same fact is stated on the token itself.
    access = client.get("/api/access").json()
    by_name = {token["name"]: token for token in access["tokens"]}
    assert by_name["research-agent"]["seatless"] is True
    assert by_name["research-agent"]["seat_slug"] is None
    seated = [t for t in access["tokens"] if t["seat_slug"] == "alice"]
    assert seated and seated[0]["seatless"] is False


# --------------------------------------------------------------------------
# Gap 10: audit paging
# --------------------------------------------------------------------------


def test_access_audit_is_paged_and_walkable(tmp_path: Any) -> None:
    """Gap 10. The array grew forever; the UI rendered twelve rows of it."""
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    for index in range(30):
        app.state.access_store.record_event(
            action=f"test.event.{index}", actor=None, success=True, detail={}
        )

    first = client.get("/api/access?limit=10").json()
    assert len(first["audit_events"]) == 10
    assert first["audit_events_returned"] == 10
    assert first["audit_events_total"] >= 30
    assert first["next_cursor"]

    second = client.get(f"/api/access?limit=10&cursor={first['next_cursor']}").json()
    assert len(second["audit_events"]) == 10
    first_ids = {event["id"] for event in first["audit_events"]}
    second_ids = {event["id"] for event in second["audit_events"]}
    assert not (first_ids & second_ids), "pages must not overlap"

    # Ordering within a page is unchanged (oldest first), so an untouched
    # consumer reading audit_events still sees what it expects.
    created = [event["created_at"] for event in first["audit_events"]]
    assert created == sorted(created)

    # A stale cursor degrades to the newest page rather than erroring, so the
    # Admin view cannot be wedged by a cursor from a trimmed store.
    stale = client.get("/api/access?limit=5&cursor=audit_does_not_exist")
    assert stale.status_code == 200
    assert len(stale.json()["audit_events"]) == 5


def test_access_snapshot_keeps_its_existing_shape(tmp_path: Any) -> None:
    """Paging must not disturb the keys the Admin view already reads."""
    app.state.access_store = AccessStore(tmp_path / "access.json")
    client = authed_client()

    payload = client.get("/api/access").json()

    for existing in ("ok", "bootstrap_keys", "principals", "tokens", "audit_events"):
        assert existing in payload, f"/api/access lost {existing}"
    assert isinstance(payload["audit_events"], list)


def test_seat_home_is_not_empty_after_a_restart_wipes_the_mesh_projection(
    tmp_path: Any,
) -> None:
    """A redeploy must not make a populated seat render as "nothing captured".

    `document_count` used to come only from the in-memory mesh projection, which
    is rebuilt per process and is empty immediately after a restart. That zero
    fed `capture_done`, which flipped `empty` true, so a seat holding thousands
    of notes showed the onboarding empty state on every deploy. This service
    redeploys on every merge to main, so that was most of any given day.

    The mesh here is deliberately left EMPTY, which is exactly the post-restart
    state, so this test fails if the count ever goes back to the projection.
    """
    app.state.access_store = AccessStore(tmp_path / "access.json")
    admin = authed_client()
    alice_token = admin.post("/api/access/seats", json={"name": "Alice", "slug": "alice"}).json()[
        "token"
    ]

    class _Cognee:
        async def document_counts_by_dataset(self) -> dict[str, int]:
            return {"masumi-network": 10, "seat:alice": 42}

    class _Citadel:
        config = app.state.citadel.config
        cognee = _Cognee()

    original = app.state.citadel
    app.state.citadel = _Citadel()
    app.state.mesh = MeshState()  # a fresh process: projection holds nothing
    try:
        alice = TestClient(app, base_url="https://testserver")
        payload = alice.get(
            "/api/me/summary", headers={"Authorization": f"Bearer {alice_token}"}
        ).json()
    finally:
        app.state.citadel = original

    assert payload["document_count"] == 42, payload
    assert payload["empty"] is False, "a populated seat rendered as empty"


def test_every_dashboard_page_is_reachable_from_the_ui() -> None:
    """A page with no way in is invisible, however healthy its data is.

    The Knowledge/graph page shipped complete: canvas, depth control, legend,
    metrics strip, node inspector. It had no `data-page-target` anywhere, so the
    only way to reach it was to type the URL hash by hand, and the graph looked
    to users like a feature that did not exist. Meanwhile /api/mesh/graph was
    returning 932 nodes and 5952 edges in ~300ms.

    `locked` and `overview` are deliberately unreachable: `locked` is the
    fallback setPage() routes to when a caller lacks the role, and a comment at
    app.js initialPage() records that `overview` was intentionally dropped from
    the nav.
    """
    from pathlib import Path
    import re

    html = Path("kb/static/index.html").read_text(encoding="utf-8")
    pages = set(re.findall(r'data-page="([a-z-]+)"', html))
    targets = set(re.findall(r'data-page-target="([a-z-]+)"', html))
    intentionally_unreachable = {"locked", "overview"}
    # Genuinely unreachable, same defect as the graph page: built, complete, and
    # with no nav entry, no setPage() call and no hash link anywhere. Listed
    # rather than silently subtracted so this test still fails on a NEW orphan.
    # Not fixed here because each needs a role decision (settings and audit are
    # plausibly admin-only) and guessing at that is a security call, not a UI
    # one. Tracked separately.
    known_unreachable = {"audit", "feedback", "settings"}

    orphaned = pages - targets - intentionally_unreachable - known_unreachable

    assert not orphaned, (
        f"pages with no way to reach them from the UI: {sorted(orphaned)}. "
        "Add a nav entry or a data-page-target button, or list it as "
        "intentionally unreachable with a reason."
    )


def test_a_corrupt_sync_state_file_shows_red_instead_of_500(tmp_path: Any) -> None:
    """#148: a state file that fails to parse must surface as an error'd
    source. Before this, status() flattened corruption to an empty state, so
    the source showed "ready" with zero documents — indistinguishable from a
    node that had never synced."""
    from kb.config import CitadelConfig
    from kb.github_sync import GitHubOrgSyncer
    from kb.service import Citadel

    client = authed_client()
    state_path = tmp_path / "github_state.json"
    state_path.write_text('{"version": 1, "repos"', encoding="utf-8")
    config = CitadelConfig(github_sync_state_path=str(state_path))
    app.state.github_syncer = GitHubOrgSyncer(Citadel(config), org="masumi-network")

    response = client.get("/api/sources?type=github")

    assert response.status_code == 200
    source = response.json()["sources"][0]
    assert source["status"] == "error"
    assert "github_state.json" in source["last_error"]


def test_home_reads_the_readable_corpus_count_not_the_node_only_one() -> None:
    """Gap 1, on the client side.

    The server field exists and is tested above; this pins that the
    hand-written dashboard actually renders it. Both wrong sources are numbers
    that are present, plausible and adjacent on payloads Home already fetches:
    `/api/mesh` `stats.tracked_sources` counts sources, not documents, and
    `/api/me/summary` `document_count` is Node-only, so it drops everything the
    caller can read in Central. Neither failure throws or renders blank; each
    just paints a confident wrong figure, which is why the guard has to name the
    field rather than assert the tile is non-empty.

    Also pins the null handling: a missing count renders a dash with a reason,
    never `0`, matching web/src/pages/app/index.tsx.
    """
    import re

    app_js = (REPO / "kb" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (REPO / "kb" / "static" / "index.html").read_text(encoding="utf-8")

    body = re.search(r"function homeReadableCount\(\)\s*\{(.*?)\n\}", app_js, re.DOTALL)
    assert body, "homeReadableCount() not found in kb/static/app.js"
    source = body.group(1)

    assert "readable_document_count" in source, (
        "Home's 'Notes you can read' does not read readable_document_count"
    )
    assert "document_count" not in source.replace("readable_document_count", ""), (
        "Home fell back to the Node-only document_count; that number excludes Central"
    )
    assert "tracked_sources" not in source, (
        "Home read a source count for a document tile"
    )
    # Null renders as a dash plus a reason, not as zero.
    assert "return null" in source, "a missing count must be null, never 0"
    assert 'id="homeReadableNote"' in index_html, "no element to render why the count is missing"
    assert "countOrDash(homeReadableCount())" in app_js, (
        "the tile does not route the count through countOrDash"
    )
    assert "Not reported by this node yet" in app_js and "Unavailable" in app_js, (
        "a failed fetch and an absent field must render different reasons"
    )


def test_event_graph_focus_matches_exact_source_identity() -> None:
    """#126: an organization must not select a source by substring coincidence.

    The legacy dashboard has no DOM test runner, but this resolver is pure once
    its graph lookup and timeline envelope are supplied. Execute the extracted
    function with an exact source label, a near-match label, and a URL-only
    decoy so the regression exercises matching behavior, not only source text.
    """
    import re
    import subprocess

    app_js = (REPO / "kb" / "static" / "app.js").read_text(encoding="utf-8")
    resolver = re.search(
        r"function relatedNodeForEvent\(event\) \{.*?\n\}\n\nfunction findGraphNode",
        app_js,
        re.S,
    )
    assert resolver, "relatedNodeForEvent moved"
    resolver_source = resolver.group(0).split("\n\nfunction findGraphNode", 1)[0]

    script = f"""
{resolver_source}
const nodes = [
  {{ id: "near", type: "source", label: "GitHub / acme-docs" }},
  {{ id: "url-only", type: "source", label: "Other source", metadata: {{ url: "https://acme.example" }} }},
  {{ id: "exact", type: "source", label: "GitHub / acme" }},
];
function findGraphNode(predicate) {{ return nodes.find(predicate) || null; }}
function timelineEnvelope(event) {{ return event.timeline || {{ dataset: null }}; }}
const matched = relatedNodeForEvent({{ details: {{ org: " ACME " }} }});
if (!matched || matched.id !== "exact") process.exit(1);
if (relatedNodeForEvent({{ details: {{ org: "acme-docs" }} }}).id !== "near") process.exit(2);
if (relatedNodeForEvent({{ details: {{ org: "acme.example" }} }}) !== null) process.exit(3);
"""
    result = subprocess.run(
        ["node", "--input-type=commonjs", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_graph_inspector_walks_payload_edges_to_nearest_document() -> None:
    """The #186 inspector chain dead-ended on ~100% of rendered nodes: its
    candidates (the node itself plus one-hop document-bearing neighbors) are
    chunk/summary ids that /api/documents 404s, while nearly every rendered
    node reaches a TextDocument within <=3 hops of is_part_of/made_from edges
    INSIDE the already-loaded payload (measured live: 174 nodes at 2 hops, 130
    at 3). Pin the BFS walk, that the inspector actually consults it first,
    and that a node with no reachable document gets a typed empty state
    instead of a silent dead-end."""
    import re

    app_js = (REPO / "kb" / "static" / "app.js").read_text(encoding="utf-8")

    walk = re.search(
        r"function nearestDocumentThroughEdges\(node\)\s*\{(.*?)\n\}",
        app_js,
        re.DOTALL,
    )
    assert walk, "nearestDocumentThroughEdges() not found in kb/static/app.js"
    assert '"is_part_of"' in app_js and '"made_from"' in app_js, (
        "the document walk must follow is_part_of and made_from edges"
    )
    assert "DOCUMENT_WALK_MAX_HOPS = 3" in app_js, (
        "the document walk must cap its BFS depth at 3 hops"
    )

    candidates = re.search(
        r"function documentCandidates\(node\)\s*\{(.*?)\n\}", app_js, re.DOTALL
    )
    assert candidates, "documentCandidates() not found in kb/static/app.js"
    assert "nearestDocumentThroughEdges(node)" in candidates.group(1), (
        "documentCandidates does not consult the payload walk; the inspector "
        "is back to 404-ing chunk/summary ids"
    )
    assert "No document reachable from this node" in app_js, (
        "a node with no reachable document must render a typed empty state"
    )


def test_graph_aggregate_toggle_writes_state() -> None:
    """state.graphAggregate was read exactly once (buildForceGraphData) and
    never written, so Knowledge Mesh aggregation was permanently on and the
    legend chips could never restore Documents/Chunks — the view rendered 306
    of 1002 payload nodes with no way to see the rest. Pin the toggle wiring
    end to end: a control exists, it writes the flag, and the flag still
    gates aggregation."""
    import re

    app_js = (REPO / "kb" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (REPO / "kb" / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="graphAggregateButton"' in index_html, (
        "no toolbar control for Knowledge Mesh aggregation"
    )
    assert re.search(r"state\.graphAggregate\s*=", app_js), (
        "nothing writes state.graphAggregate; aggregation is permanently on"
    )
    assert "setGraphAggregate(!state.graphAggregate)" in app_js, (
        "the aggregate control does not toggle the flag"
    )
    assert "state.graphAggregate !== false" in app_js, (
        "buildForceGraphData no longer gates aggregation on the flag"
    )


def test_graph_dataset_filter_rides_on_mesh_graph_url() -> None:
    """Seat-visibility contract with the backend: GET /api/mesh/graph gains an
    optional dataset=<name> query param (server filters pre-cap, response
    shape unchanged). Pin the frontend half: the URL builder appends the param
    only when a dataset is chosen — absence must stay byte-identical to the
    org-wide request — and a node that rejects the param (400/404/422, e.g.
    the backend has not landed) degrades to the unfiltered view with a
    visible notice instead of a broken graph page."""
    import re

    app_js = (REPO / "kb" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (REPO / "kb" / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="graphDatasetFilter"' in index_html, (
        "no dataset filter control on the graph toolbar"
    )
    url_builder = re.search(r"function meshGraphUrl\(\)\s*\{(.*?)\n\}", app_js, re.DOTALL)
    assert url_builder, "meshGraphUrl() not found in kb/static/app.js"
    assert "state.graphDataset" in url_builder.group(1), (
        "the mesh graph URL does not read the dataset filter state"
    )
    assert "&dataset=${encodeURIComponent(dataset)}" in url_builder.group(1), (
        "the dataset param is not appended (or not URL-encoded)"
    )
    assert "[400, 404, 422].includes(Number(error?.status))" in app_js, (
        "a backend without the dataset param must degrade, not error"
    )
    assert "Dataset filter unavailable" in app_js, (
        "the degrade path must be visible to the user, not silent"
    )


def test_graph_pointer_area_floors_small_node_hit_radius() -> None:
    """nodeVal spans 2-9 with nodeRelSize 4, which is a 1-3 screen-px target
    for low-degree entities at the fit-out zoom of a ~1000-node mesh —
    visible but effectively unclickable. Pin the shadow-canvas pointer paint
    and its zoom-compensated screen-space floor."""
    app_js = (REPO / "kb" / "static" / "app.js").read_text(encoding="utf-8")

    assert "nodePointerAreaPaint" in app_js, (
        "no pointer-area override; tiny nodes stay unclickable at fit-out zoom"
    )
    assert "MIN_NODE_HIT_RADIUS_PX" in app_js, (
        "the pointer area has no named minimum radius"
    )
    assert "MIN_NODE_HIT_RADIUS_PX / (globalScale || 1)" in app_js, (
        "the hit-radius floor must be screen-space (divided by the zoom scale), "
        "or it shrinks with the same fit-out zoom that caused the defect"
    )


def test_graph_depth_control_is_gone() -> None:
    """User decision on FE-01: the depth slider was a third hiding mechanism
    stacked on aggregation and the server cap — a client-side hop-limit from
    the focus node that manufactured confusion while the user always wants
    full connectivity. Pin its absence end to end (markup, styles, state,
    hop filter) so it cannot quietly return."""
    app_js = (REPO / "kb" / "static" / "app.js").read_text(encoding="utf-8")
    index_html = (REPO / "kb" / "static" / "index.html").read_text(encoding="utf-8")
    styles_css = (REPO / "kb" / "static" / "styles.css").read_text(encoding="utf-8")

    assert "graphDepthInput" not in index_html, (
        "the depth control markup is back on the graph toolbar"
    )
    assert "graphDepthInput" not in app_js, (
        "app.js still wires the deleted depth control"
    )
    assert "applyGraphDepth" not in app_js, (
        "the hop-limit filter is back; rendering must always use the full "
        "loaded payload"
    )
    assert "state.graphDepth" not in app_js, (
        "the depth state plumbing is back"
    )
    assert "graph-depth-control" not in index_html and "graph-depth-control" not in styles_css, (
        "dead depth-control markup or styles are back"
    )


def test_graph_inspector_prefers_sourcing_chunks_over_nearest_document() -> None:
    """Live user report on 13c1586: clicking a person entity (then a related
    entity) showed an arbitrary daily-update digest. Entities carry no
    is_part_of/made_from edges, so the nearest-document walk returns null for
    EVERY entity, and the one-hop fallback picked the first document-bearing
    neighbor in payload edge order — degree-biased toward digests that
    name-drop dozens of entities (live 2026-08-13: entities 'patricktobler'
    and its committed-neighbor both resolved to '# masumi-network GitHub
    daily update'). Chunks name the entities they mention through `contains`
    edges (868 of the payload's 900 contains edges are chunk->entity), so the
    inspector now tries sourcing chunks first — resolved to their parent
    documents and ranked by how many chunks mention the node — and the
    nearest-walk fallback says out loud that it is not provenance."""
    import re
    from pathlib import Path

    import kb.server as server_module

    repo = Path(server_module.__file__).resolve().parent.parent
    app_js = (repo / "kb" / "static" / "app.js").read_text(encoding="utf-8")

    assert 'SOURCING_RELATIONSHIP = "contains"' in app_js, (
        "the sourcing edge type (chunk->entity contains) is not pinned"
    )
    sourcing = re.search(
        r"function sourcingDocumentCandidates\(node\)\s*\{(.*?)\n\}", app_js, re.DOTALL
    )
    assert sourcing, "sourcingDocumentCandidates() not found in kb/static/app.js"
    assert "parentDocumentInView" in sourcing.group(1), (
        "sourcing chunks must resolve to their parent documents when in view"
    )
    # Structural pins on the two load-bearing lines (logic verified by review,
    # 2026-08-13): gutting either must fail here, not only deleting the function.
    assert 'nodeKind(chunk) !== "chunk"' in sourcing.group(1), (
        "the other-endpoint kind filter is gone; Entity->Entity contains edges "
        "(32 in the live payload) would count as sourcing chunks"
    )
    assert ".sort((a, b) => b.count - a.count)" in sourcing.group(1), (
        "the sourcing-count ranking is gone; the digest that name-drops a node "
        "once would tie with the document actually about it"
    )
    assert '"Sources this node"' in sourcing.group(1), (
        "sourcing candidates lost their provenance label"
    )
    candidates = re.search(
        r"function documentCandidates\(node\)\s*\{(.*?)\n\}", app_js, re.DOTALL
    )
    assert candidates, "documentCandidates() not found in kb/static/app.js"
    body = candidates.group(1)
    assert "sourcingDocumentCandidates(node)" in body, (
        "the inspector does not consult sourcing chunks at all"
    )
    assert body.index("sourcingDocumentCandidates(node)") < body.index(
        "nearestDocumentThroughEdges(node)"
    ), "sourcing candidates must be tried before the nearest-document walk"
    assert "Nearest document in view — not a direct source" in app_js, (
        "the nearest-walk fallback must label itself as not being provenance"
    )
