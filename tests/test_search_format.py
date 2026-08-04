from __future__ import annotations

import re
import time
from collections.abc import Callable

import pytest

from kb import search_format, session_trace
from kb.search_format import (
    DOC_TYPE_CANONICAL,
    apply_query_ranking,
    apply_spec_mode_ranking,
    extract_hex_needles,
    filter_hits,
    infer_content_hint,
    infer_doc_type,
    infer_trust_tier,
    is_docs_mode_query,
    is_spec_mode_query,
    is_token_asset_query,
    normalize_search_hit,
    shape_search_payload,
)


def test_spec_mode_detects_api_cues() -> None:
    assert is_spec_mode_query("masumi payment POST /purchase required fields schema")
    assert is_spec_mode_query("MIP-003 status enum")
    assert not is_spec_mode_query("what did the team ship this week")


def test_token_asset_query_and_hex_needles() -> None:
    policy = "a" * 56
    assert is_token_asset_query("Masumi USDCx mainnet unit")
    assert is_token_asset_query(f"lookup {policy} asset id")
    assert extract_hex_needles(f"unit {policy}ff00") == [f"{policy}ff00".lower()]
    assert is_docs_mode_query("USDM payment token")
    assert is_docs_mode_query("anything", mode="docs")
    assert not is_token_asset_query("what did the team ship this week")


def test_asset_id_ranking_prefers_exact_hex_over_fuzzy_chat() -> None:
    policy = "b" * 56
    fuzzy = {
        "title": "Linear chat about tokens",
        "text": "someone mentioned USDCx in standup",
        "score": 0.99,
        "url": "https://linear.app/masumi/issue/ABC-1",
    }
    exact = {
        "id": f"doc-{policy}",
        "title": "Official token pointer",
        "path": f"docs/tokens/{policy}.md",
        "url": f"https://docs.masumi.network/tokens/{policy}",
        "text": "verify against skill/official docs — do not invent hex",
        "score": 0.2,
    }
    ranked = apply_query_ranking([fuzzy, exact], f"Masumi USDCx {policy}")
    assert ranked[0] is exact
    assert "docs.masumi" in (ranked[0].get("url") or "")


def test_docs_mode_excludes_ambient() -> None:
    payload = {
        "results": [
            {
                "title": "tokens skill",
                "path": "skills/masumi/SKILL.md",
                "text": "USDCx payment unit — verify against official docs",
                "score": 0.5,
            },
            {
                "text": "GitHub org daily digest mentioning USDCx",
                "score": 0.99,
            },
        ]
    }
    shaped = shape_search_payload(payload, query="USDCx payment token", mode="docs")
    assert shaped["docs_mode"] is True
    assert all(h["doc_type"] != "activity" for h in shaped["results"])
    assert any("not sole authority" in w.lower() or "skills/masumi" in w for w in shaped["warnings"])


def test_shape_timeout_sets_code() -> None:
    shaped = shape_search_payload(
        {"results": [], "timed_out": True, "truncated": True},
        query="x",
    )
    assert shaped["code"] == "TIMEOUT"
    assert shaped["timed_out"] is True


def test_infer_doc_type_and_trust() -> None:
    """doc_type describes shape; trust_tier reports only attested provenance."""
    spec = {
        "title": "MIP-003",
        "path": "MIPs/MIP-003/MIP-003.md",
        "url": "https://github.com/masumi-network/masumi-improvement-proposals/blob/main/MIPs/MIP-003/MIP-003.md",
    }
    assert infer_doc_type(spec) == "spec"
    # Shaped like a spec, but carrying no server envelope: no attested
    # provenance was ever consulted, and the tier says exactly that instead of
    # implying a check happened ("unattested" is reserved for consulted-and-
    # nothing-attested).
    assert infer_trust_tier(spec) == "unknown"
    assert infer_content_hint(spec) == "looks-like-spec"
    # With an envelope the channel WAS consulted; a non-trace dataset attests
    # nothing, which is what "unattested" states.
    assert infer_trust_tier({**spec, "_citadel": {"dataset": "masumi-network"}}) == "unattested"

    activity = {"text": "GitHub org daily digest for masumi-network"}
    assert infer_doc_type(activity) == "activity"
    assert infer_trust_tier(activity) == "unknown"
    assert (
        infer_trust_tier({**activity, "_citadel": {"dataset": "masumi-network"}})
        == "unattested"
    )

    trace = {"_citadel": {"dataset": "session-traces", "trust": "reference-only"}}
    assert infer_doc_type(trace) == "session-trace"
    assert infer_trust_tier(trace) == "reference-only"


def test_body_text_cannot_mint_a_trust_claim() -> None:
    """The whole point of the attested-only tier.

    Ingested text is author-controlled and reaches the vault from places no one
    vets — a public-repo issue title lands verbatim in the org digest. Every one
    of these bodies used to yield trust_tier canonical or verified.
    """
    forgeries = [
        {"title": "Random chat note", "text": "someone pasted /skills/ in a message"},
        {"title": "note", "text": "see docs.masumi.network for details"},
        {"title": "gossip", "text": "he said SKILL.md was wrong"},
        {"title": "hearsay", "text": "MIP-003 says the field is optional, I think"},
        {"title": "guess", "text": "the openapi file probably allows it"},
    ]
    for item in forgeries:
        # Without an envelope nothing was consulted — and the body still buys
        # nothing: the tier is "unknown", never "reference-only".
        assert infer_trust_tier(item) == "unknown", item
        assert normalize_search_hit(item)["trust_tier"] == "unknown", item
        # With an envelope the channel was consulted and attests nothing.
        enveloped = {**item, "_citadel": {"dataset": "masumi-network"}}
        assert infer_trust_tier(enveloped) == "unattested", item
        assert normalize_search_hit(enveloped)["trust_tier"] == "unattested", item


def test_envelope_less_trace_hit_is_distinguishable_from_unattested() -> None:
    """The in-process --local search stack attaches no ``_citadel`` envelope.

    A genuine session-trace hit read without an envelope used to normalize to
    trust_tier "unattested" — byte-identical to an honestly unattested HTTP
    hit — so a reader could not tell "checked, nothing attested" from "nothing
    could be checked". The two states must not share a value.
    """
    trace_text = "# Dead-end route\n\nNested HTTP to /api/session deadlocked tools/list"
    local_hit = normalize_search_hit({"id": "t1", "text": trace_text})
    http_unattested = normalize_search_hit(
        {"id": "t2", "text": "a plain central note", "_citadel": {"dataset": "masumi-network"}}
    )
    http_trace = normalize_search_hit(
        {
            "id": "t3",
            "text": trace_text,
            "_citadel": {"dataset": "session-traces", "trust": "reference-only"},
        }
    )
    assert http_trace["trust_tier"] == "reference-only"
    assert http_unattested["trust_tier"] == "unattested"
    # The envelope-less hit states that no attestation channel existed…
    assert local_hit["trust_tier"] == "unknown"
    # …so it is no longer byte-identical to the honestly unattested one.
    assert local_hit["trust_tier"] != http_unattested["trust_tier"]


def test_shape_search_payload_warns_when_tiers_unknown() -> None:
    """The page-level warning names how many tiers could not be derived."""
    shaped = shape_search_payload(
        {"results": [{"id": "1", "text": "local cognee payload"}]}, query="payload"
    )
    assert shaped["results"][0]["trust_tier"] == "unknown"
    assert any("trust_tier is 'unknown'" in w for w in shaped["warnings"])

    enveloped = shape_search_payload(
        {"results": [{"id": "1", "text": "note text", "_citadel": {"dataset": "d"}}]},
        query="note",
    )
    assert all("trust_tier is 'unknown'" not in w for w in enveloped["warnings"])


def test_digest_cannot_relabel_itself_as_documentation() -> None:
    """Anyone can file a public issue; its title rides into the org digest."""
    digest = {
        "title": "GitHub organization update — daily digest",
        "text": (
            "# Daily digest for masumi-network\n"
            "- alice opened issue #12: fix flaky test\n"
            "- mallory opened issue #999: MIP-003 endpoint schema\n"
        ),
    }
    assert infer_doc_type(digest) == "activity"
    hit = normalize_search_hit(digest)
    assert filter_hits([hit], exclude_ambient=True) == []
    assert filter_hits([hit], canonical_only=True) == []


def test_repo_scoping_is_not_satisfied_by_the_dataset_name() -> None:
    """Central is named after the org, so repo=<org> matched every hit in it."""
    central_hit = normalize_search_hit(
        {"id": "c", "text": "a note about nothing", "_citadel": {"dataset": "masumi-network"}}
    )
    assert filter_hits([central_hit], repo="masumi-network") == []

    real = normalize_search_hit(
        {"id": "r", "text": "x", "url": "https://github.com/masumi-network/citadel/blob/x"}
    )
    assert filter_hits([real], repo="masumi-network") == [real]


def test_seat_slug_cannot_relabel_a_seats_notes() -> None:
    """A team seat innocently named "devhub" is not documentation."""
    note = {
        "title": "personal scratch",
        "text": "auth is optional lol",
        "_citadel": {"dataset": "seat:devhub"},
    }
    assert infer_doc_type(note) == "other"
    assert infer_trust_tier(note) == "unattested"


def test_normalize_prefers_reference_only_over_stale_trust_tier() -> None:
    """Server used to infer trust_tier before attaching _citadel (wrong derived)."""
    hit = normalize_search_hit(
        {
            "title": "Dead-end route",
            "text": "Nested HTTP to /api/session deadlocked tools/list",
            "_citadel": {
                "dataset": "session-traces",
                "trust": "reference-only",
                "trust_tier": "derived",
                "doc_type": "other",
            },
        }
    )
    assert hit["doc_type"] == "session-trace"
    assert hit["trust_tier"] == "reference-only"


def test_spec_mode_ranking_prefers_specs_over_activity() -> None:
    hits = [
        {"text": "GitHub org daily digest", "score": 0.9},
        {"title": "MIP-003", "path": "MIPs/MIP-003/MIP-003.md", "score": 0.4},
        {"text": "SKILL.md payment endpoints", "path": "skills/masumi/SKILL.md", "score": 0.5},
    ]
    ranked = apply_spec_mode_ranking(hits)
    assert "MIP-003" in str(ranked[0].get("title") or ranked[0].get("path"))


def test_shape_search_payload_filters_and_schema() -> None:
    payload = {
        "results": [
            {
                "id": "1",
                "title": "MIP-003",
                "path": "MIPs/MIP-003/MIP-003.md",
                "url": "https://github.com/masumi-network/masumi-improvement-proposals/blob/x",
                "text": "Agent statuses and purchase request body",
                "score": 0.8,
                "_citadel": {"dataset": "masumi-network", "rank": 1, "provenance": {}},
            },
            {
                "id": "2",
                "text": "GitHub org daily digest mentioning cardano-dev-skills",
                "score": 0.99,
                "_citadel": {"dataset": "masumi-network", "rank": 2},
            },
        ],
        "timed_out": False,
    }
    shaped = shape_search_payload(
        payload,
        query="MIP-003 endpoint schema",
        types=["spec"],
        repo="masumi-improvement-proposals",
    )
    assert shaped["ok"] is True
    assert shaped["spec_mode"] is True
    assert len(shaped["results"]) == 1
    hit = shaped["results"][0]
    assert hit["doc_type"] == "spec"
    assert hit["content_hint"] == "looks-like-spec"
    assert hit["trust_tier"] == "unattested"
    assert hit["title"]
    assert hit["snippet"]
    assert "url" in hit and "path" in hit and "repo" in hit

    canonical = shape_search_payload(payload, query="x", canonical_only=True, apply_spec_ranking=False)
    # canonical_only is a shape filter, not a trust filter — it must never be
    # satisfied by a tier, because a tier can no longer be earned from content.
    assert all(h["doc_type"] in {"spec", "skill", "canonical-docs"} for h in canonical["results"])


def test_filter_hits_path_substring() -> None:
    hits = [
        normalize_search_hit({"path": "MIPs/MIP-003/MIP-003.md", "text": "spec"}),
        normalize_search_hit({"path": "README.md", "text": "other"}),
    ]
    filtered = filter_hits(hits, path="**/MIP-003/**")
    assert len(filtered) == 1
    assert "MIP-003" in (filtered[0]["path"] or "")


def test_filter_hits_source_scopes_to_the_syncer_that_wrote_the_hit() -> None:
    """``source=`` selects what a hit IS, not what its body talks about.

    Measured on production: citadel_linear_search for a Linear-shaped query
    returned four copies of a repo's ``docs/agents/issue-tracker.md`` above the
    one real Linear issue, because the tool ran the general vault search with
    no source filter. Both texts below are production shapes.
    """
    repo_doc_about_linear = {
        "id": "repo-1",
        "chunk_index": 0,
        "text": (
            "# masumi-network/sokosumi/docs/agents/issue-tracker.md\n"
            "\n"
            "Repository: masumi-network/sokosumi\n"
            "Source: https://github.com/masumi-network/sokosumi/blob/1e03b94/docs/agents/issue-tracker.md\n"
            "Commit: 1e03b94\n"
            "Blob: 354a0ab\n"
            "\n"
            "---\n"
            "\n"
            "# Issue tracker: Linear\n"
            "\n"
            "Issues and PRDs for this repo live in **Linear**, team **Sokosumi**.\n"
        ),
    }
    linear_issue = {
        "id": "issue-1",
        "chunk_index": 0,
        "text": (
            "# Linear DES-144: Subscription Credits missing\n"
            "\n"
            "- **State:** Backlog (backlog)\n"
            "- **Team:** Design\n"
            "- **URL:** https://linear.app/masumi/issue/DES-144/subscription-credits-missing\n"
        ),
    }

    filtered = filter_hits([repo_doc_about_linear, linear_issue], source="linear-issue")

    assert [hit["id"] for hit in filtered] == ["issue-1"]


def test_filter_hits_source_reads_the_server_provenance_envelope() -> None:
    """The server already resolved provenance; the filter reads it, never re-guesses."""
    hits = [
        {
            "id": "repo-1",
            "text": "prose that names Linear a lot",
            "_citadel": {"provenance": {"source": "repo-content", "basis": "content-header"}},
        },
        {
            "id": "issue-1",
            "text": "body without a parseable header of its own",
            "_citadel": {
                "provenance": {
                    "source": "linear-issue",
                    "issue": "DES-144",
                    "basis": "content-header",
                }
            },
        },
    ]

    assert [hit["id"] for hit in filter_hits(hits, source="linear-issue")] == ["issue-1"]


def test_filter_hits_source_refuses_a_forged_header_on_a_later_chunk() -> None:
    """Chunk 1+ starts are author-controlled text, so a header there is forgeable."""
    forged = {
        "id": "forged",
        "chunk_index": 3,
        "text": "# Linear DES-999: not really an issue\n\n- **Team:** Design\n",
    }

    assert filter_hits([forged], source="linear-issue") == []


def test_filter_hits_reads_server_citadel_envelope() -> None:
    hits = [
        {
            "id": "1",
            "text": "MIP-003 payment schema",
            "url": "https://github.com/masumi-network/agent/blob/main/docs/MIP-003.md",
            "_citadel": {
                "doc_type": "spec",
                "trust_tier": "canonical",
                "provenance": {"path": "docs/MIP-003.md"},
            },
        },
        {
            "id": "2",
            "text": "daily digest noise",
            "_citadel": {"doc_type": "activity", "trust_tier": "ambient"},
        },
    ]
    filtered = filter_hits(
        hits,
        types=["spec"],
        repo="masumi-network/agent",
        canonical_only=True,
    )
    assert len(filtered) == 1
    assert filtered[0]["id"] == "1"


def test_compact_search_filters_omits_empty() -> None:
    from kb.search_format import compact_search_filters

    assert compact_search_filters(top_k=10) == {"top_k": 10}
    assert compact_search_filters(
        types=["spec"],
        repo=" agent ",
        path="",
        canonical_only=True,
        exclude_ambient=True,
        mode="docs",
        dataset="notes",
    ) == {
        "types": ["spec"],
        "repo": "agent",
        "canonical_only": True,
        "exclude_ambient": True,
        "mode": "docs",
        "dataset": "notes",
    }


# --- docs mode returned zero for everything -------------------------------
#
# 2026-07-31: `mode="docs"` matched nothing on prod, for any query, including
# ones whose answer was an ingested .md file. Three independent causes, each
# sufficient on its own. All three are pinned below.

REPO_DOC_TEXT = (
    "# masumi-network/sokosumi/README.md\n"
    "\n"
    "Repository: masumi-network/sokosumi\n"
    "Source: https://github.com/masumi-network/sokosumi/blob/f401d38/README.md\n"
    "Commit: f401d3896820ff35a82cf707818e308b67f5bce2\n"
    "Blob: a4b30a4548af239f695ba3cba1935b545e96d675\n"
    "\n"
    "---\n"
    "\n"
    "# Sokosumi Monorepo\n\nSokosumi is a marketplace platform.\n"
)


def test_repo_content_header_classifies_as_documentation() -> None:
    """Cause 3: infer_doc_type had no repo-content rule and returned 'other'."""
    assert infer_doc_type({"text": REPO_DOC_TEXT}) == "canonical-docs"


def test_repo_doc_survives_a_shared_trace_text_collision() -> None:
    """Cause 1: a text collision with session-traces relabelled real docs.

    The marker is assigned by matching chunk TEXT against the session-traces
    dataset, so any document a trace quoted verbatim inherited reference-only
    and was reclassified a trace. Structural provenance must win.
    """
    hit = {"text": REPO_DOC_TEXT, "_citadel": {"trust": "reference-only"}}

    assert infer_doc_type(hit) == "canonical-docs"


def test_exclude_ambient_does_not_consult_the_trust_tier() -> None:
    """Cause 2: the trust half of the condition was unsatisfiable.

    `reference-only` is the only tier the server can attest, so every hit
    carries it and requiring `trust_tier != reference-only` removed everything.
    """
    hits = [
        normalize_search_hit({"text": REPO_DOC_TEXT}, index=0),
        normalize_search_hit({"text": "GitHub org daily digest"}, index=1),
    ]
    for hit in hits:
        hit["trust_tier"] = "reference-only"

    kept = filter_hits(hits, exclude_ambient=True)

    assert len(kept) == 1, kept
    assert kept[0]["doc_type"] == "canonical-docs"


def test_docs_mode_returns_the_documentation_and_drops_the_issue() -> None:
    """End to end: the shape that returned zero results on prod."""
    payload = {
        "results": [
            # Both Linear shapes exactly as the syncer writes them.
            {
                "text": (
                    "# Linear SOK-623: coworker init UX\n\n"
                    "- **State:** In Review (started)\n"
                    "- **Team:** Sokosumi\n"
                    "- **URL:** https://linear.app/masumi/issue/SOK-623/coworker-init-ux"
                )
            },
            {"text": REPO_DOC_TEXT},
            {"text": "# Linear workspace sync\n\nSynced 200 issues.\n\n- **SOK-670** [Triage] x"},
        ]
    }

    shaped = shape_search_payload(payload, query="Sokosumi monorepo structure", mode="docs")

    assert shaped["docs_mode"] is True
    assert len(shaped["results"]) == 1, shaped["results"]
    assert "sokosumi/README.md" in shaped["results"][0]["text"]


def test_the_linear_workspace_digest_counts_as_ambient() -> None:
    """It is titled "Linear workspace sync", not "Linear sync".

    ACTIVITY_RE required the two words adjacent, so the single document holding
    120 issue titles classified as `other` — which is not ambient — and was
    never excluded. It is the strongest magnet in the corpus.
    """
    digest = {"text": "# Linear workspace sync\n\nSynced 200 issues.\n\n- **SOK-670** [Triage] x"}

    assert infer_doc_type(digest) == "activity"
    assert filter_hits([normalize_search_hit(digest, index=0)], exclude_ambient=True) == []


def test_a_trace_still_cannot_dress_itself_as_documentation() -> None:
    """The guard this fix relaxes must still hold for actual traces.

    A session trace mentioning /skills/ must not read as a skill doc just
    because the body says so — it lacks the structural header.
    """
    trace = {
        "text": "ran the agent against /skills/masumi/SKILL.md and it worked",
        "_citadel": {"dataset": "session-traces"},
    }

    assert infer_doc_type(trace) == "session-trace"
    assert infer_trust_tier(trace) == "reference-only"


# --- updated_at: cognee epoch timestamps must survive normalization ----------
#
# Production hits carry ``updated_at``/``created_at`` as epoch-millis ints
# (cognee DataPoint), the ``_citadel`` envelope has NO ``created_at``, and
# ``metadata`` holds only ``index_fields`` — so every source the field reads
# from was dead and the schema shipped a freshness field that was always null.


def test_updated_at_epoch_millis_becomes_iso() -> None:
    hit = normalize_search_hit({"updated_at": 1785361521790, "text": "note"})
    assert hit["updated_at"] == "2026-07-29T21:45:21+00:00"


def test_updated_at_epoch_seconds_becomes_iso() -> None:
    hit = normalize_search_hit({"updated_at": 1782742208, "text": "note"})
    assert hit["updated_at"] == "2026-06-29T14:10:08+00:00"


def test_updated_at_iso_string_passes_through() -> None:
    hit = normalize_search_hit({"updated_at": "2026-07-29T21:45:21+00:00", "text": "note"})
    assert hit["updated_at"] == "2026-07-29T21:45:21+00:00"


def test_updated_at_nonsense_yields_none_not_a_wrong_date() -> None:
    # 0/negative must not become 1970; a huge value must not become year 56000;
    # values between the seconds and millis windows have no sane reading in
    # either unit; True is an int subclass and must not be read as a timestamp.
    for junk in (0, -5, 1_782_742_208_187_000, 100_000_000_000, 4_999_999_999, True):
        hit = normalize_search_hit({"updated_at": junk, "text": "note"})
        assert hit["updated_at"] is None, junk


def test_updated_at_survives_the_real_production_hit_shape() -> None:
    """Exact shape captured from the live node on 2026-08-03 (values synthetic).

    The load-bearing facts: top-level ``updated_at`` is epoch millis, the
    ``_citadel`` envelope carries NO ``created_at`` key, and ``metadata`` holds
    only ``index_fields``. A fixture that invents ``created_at`` in the
    envelope would pass against a shape production does not have.
    """
    hit = {
        "id": "0c9d5df0-0000-4000-8000-000000000001",
        "created_at": 1782742208187,
        "updated_at": 1782742208187,
        "version": 1,
        "type": "DocumentChunk",
        "text": "synthetic chunk body",
        "chunk_index": 0,
        "document_id": "0c9d5df0-0000-4000-8000-000000000002",
        "document_name": "text_0123456789abcdef0123456789abcdef",
        "belongs_to_set": None,
        "feedback_weight": 0,
        "importance_weight": None,
        "ontology_valid": False,
        "source_chunk_id": None,
        "source_content_hash": None,
        "source_node_set": None,
        "source_pipeline": None,
        "source_task": None,
        "source_user": None,
        "topological_rank": 0,
        "metadata": {"index_fields": ["text"]},
        "_citadel": {
            "content_hint": "unclassified",
            "content_sha256": "0" * 64,
            "dataset": "seat:synthetic",
            "doc_type": "other",
            "document_endpoint": "/api/documents/0c9d5df0-0000-4000-8000-000000000002",
            "provenance": {},
            "rank": 1,
            "relevance": None,
            "result_id": "0c9d5df0-0000-4000-8000-000000000001",
            "retrieval": "chunks",
            "trust": "unattested",
            "trust_tier": "unattested",
        },
    }
    assert "created_at" not in hit["_citadel"]
    normalized = normalize_search_hit(hit)
    assert normalized["updated_at"] == "2026-06-29T14:10:08+00:00"
    assert isinstance(normalized["updated_at"], str)


# --- input hardening: matching cost stays proportional to input size ---------
#
# Every pattern below is applied to text this module does not control: a query
# the caller typed, or the body of a document some contributor got ingested.
# A pattern whose cost grows faster than its input turns a merely long string
# into a slow one, and these run on the request path where that time is not
# otherwise bounded. The guard is a table so a newly added pattern has to
# declare a worst-case input before it can ship (see the coverage test below).

# Big enough that a pattern growing faster than its input misses the budget by
# a wide margin, small enough that a proportional one finishes in ~1ms.
_LARGE_INPUT = 40_000
# Deliberately loose. On this machine every pattern below finishes in under
# 10ms, so a ~200x cushion absorbs a slow or loaded CI box without letting a
# disproportionate pattern through.
_PROPORTIONAL_BUDGET_SECONDS = 2.0

_HEX56 = "a" * 56

# (pattern name, method, builder taking the input size)
_WORST_CASE_INPUTS: list[tuple[str, str, Callable[[int], str]]] = [
    ("SPEC_QUERY_RE", "search", lambda n: "request" + " " * n),
    ("SPEC_QUERY_RE", "search", lambda n: "status" + " " * n),
    ("SPEC_QUERY_RE", "search", lambda n: "mip-" + "1" * n),
    ("SPEC_PATH_RE", "search", lambda n: "mip-" + "1" * n),
    ("SPEC_PATH_RE", "search", lambda n: "." * n + "yml"),
    ("ACTIVITY_RE", "search", lambda n: "linear" + " " * n),
    ("ACTIVITY_RE", "search", lambda n: "linear" + " " * n + "x"),
    ("ACTIVITY_RE", "search", lambda n: "linear " * (n // 7)),
    ("ACTIVITY_RE", "search", lambda n: "daily" + " " * n),
    ("REPO_CONTENT_HEADER_RE", "search", lambda n: "# a/b/c" + "\n" * n),
    ("REPO_CONTENT_HEADER_RE", "search", lambda n: "# a/b/c" + " \n" * (n // 2)),
    ("REPO_CONTENT_HEADER_PARSE_RE", "match", lambda n: "# a/b/c" + "\n" * n),
    ("REPO_CONTENT_HEADER_PARSE_RE", "match", lambda n: " " * n + "#x"),
    ("LINEAR_HEADER_PARSE_RE", "match", lambda n: " " * n + "# Linear"),
    ("LINEAR_HEADER_PARSE_RE", "match", lambda n: "# Linear" + "\t" * n),
    ("LINEAR_HEADER_FIELD_RE", "match", lambda n: "- **URL:** v" + " " * n),
    ("LINEAR_HEADER_FIELD_RE", "match", lambda n: "- **" + "A" * n),
    ("LINEAR_HEADER_FIELD_RE", "match", lambda n: "-" + " " * n),
    ("HEX_ASSET_RE", "findall", lambda n: "a" * n),
    ("HEX_ASSET_RE", "findall", lambda n: (_HEX56[:-1] + "z") * (n // 56)),
    ("TOKEN_ASSET_QUERY_RE", "search", lambda n: "policy" + " " * n),
    ("TOKEN_ASSET_QUERY_RE", "search", lambda n: "policy" + " " * n + "+"),
    ("TOKEN_ASSET_QUERY_RE", "search", lambda n: "policy" + " +" * (n // 2)),
    ("TOKEN_ASSET_QUERY_RE", "search", lambda n: "payment" + " " * n),
    ("TOKEN_ASSET_QUERY_RE", "search", lambda n: "mainnet" + " " * n),
    ("_QUERY_TOKEN_RE", "findall", lambda n: "a" * n),
    ("_QUERY_TOKEN_RE", "findall", lambda n: "a." * (n // 2)),
    ("_GITHUB_REPO_URL_RE", "search", lambda n: "github.com/" + "a" * n),
    ("_GITHUB_REPO_URL_RE", "search", lambda n: "github.com/" + "a/" * (n // 2)),
    ("_GITHUB_REPO_URL_RE", "search", lambda n: "github.com/a/b " * (n // 15)),
]

_SESSION_TRACE_WORST_CASE_INPUTS: list[tuple[str, str, Callable[[int], str]]] = [
    ("_AUTHOR_SEAT_LINE", "search", lambda n: "Author-Seat:" + " " * n),
    ("_AUTHOR_SEAT_LINE", "search", lambda n: "Author-Seat:" + "\n" * n),
    ("_AUTHOR_SEAT_LINE", "search", lambda n: "Author-Seat:\n" * (n // 13)),
]


def _module_patterns(module: object) -> dict[str, re.Pattern[str]]:
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, re.Pattern)
    }


@pytest.mark.parametrize(
    ("module", "table"),
    [
        (search_format, _WORST_CASE_INPUTS),
        (session_trace, _SESSION_TRACE_WORST_CASE_INPUTS),
    ],
    ids=["search_format", "session_trace"],
)
def test_every_pattern_has_a_declared_worst_case_input(
    module: object, table: list[tuple[str, str, Callable[[int], str]]]
) -> None:
    """A pattern added without a worst-case input fails here, not in production."""
    declared = {name for name, _method, _build in table}
    compiled = set(_module_patterns(module))

    assert compiled - declared == set(), (
        f"{getattr(module, '__name__', module)} patterns with no worst-case input "
        f"declared in the timing guard: {sorted(compiled - declared)}"
    )
    assert declared - compiled == set(), (
        f"timing guard names patterns that no longer exist: {sorted(declared - compiled)}"
    )


@pytest.mark.parametrize(
    ("module", "table"),
    [
        (search_format, _WORST_CASE_INPUTS),
        (session_trace, _SESSION_TRACE_WORST_CASE_INPUTS),
    ],
    ids=["search_format", "session_trace"],
)
def test_pattern_cost_stays_proportional_to_input_size(
    module: object, table: list[tuple[str, str, Callable[[int], str]]]
) -> None:
    """Matching a long input costs about what its length suggests, not more.

    A pattern that can split the same run of whitespace many ways re-tries every
    split, so its cost climbs far faster than the text it is reading. These are
    read from caller-supplied queries and ingested document bodies, so the
    length of the input is not something this module gets to assume.
    """
    patterns = _module_patterns(module)
    over_budget: list[str] = []

    for name, method, build in table:
        text = build(_LARGE_INPUT)
        run = getattr(patterns[name], method)
        started = time.perf_counter()
        run(text)
        elapsed = time.perf_counter() - started
        if elapsed > _PROPORTIONAL_BUDGET_SECONDS:
            over_budget.append(f"{name}.{method} on {len(text)} chars took {elapsed:.2f}s")

    assert not over_budget, (
        "matching cost grew faster than the input for: " + "; ".join(over_budget)
    )


def test_token_asset_query_accepts_the_same_spellings_of_policy_asset() -> None:
    """The linear spelling of the policy/asset alternative changes no verdicts."""
    for query in (
        "policyasset",
        "policy asset",
        "policy+asset",
        "policy + asset",
        "policy  +  asset",
        "policy\t+\tasset",
        "POLICY+ASSET",
        "what is the policy+asset for usdm",
    ):
        assert is_token_asset_query(query), query

    for query in (
        "policyholder assets",
        "the policy of the company",
        "policy++asset",
        "what did the team ship this week",
    ):
        assert not is_token_asset_query(query), query


def test_repo_content_header_still_classifies_a_synced_document() -> None:
    """Narrowing the title-line junction keeps real syncer output matching."""
    header = (
        "# masumi-network/Citadel/kb/server.py\n"
        "\n"
        "Repository: masumi-network/Citadel\n"
        "Source: https://github.com/masumi-network/Citadel/blob/abc123/kb/server.py\n"
        "Commit: abc123\n"
        "Blob: deadbeef\n"
        "\n"
        "def search(): ...\n"
    )
    assert infer_doc_type({"text": header}) == DOC_TYPE_CANONICAL

    # Carriage returns and extra blank lines still match, as they did before.
    assert search_format.REPO_CONTENT_HEADER_RE.search(
        header.replace("kb/server.py\n\n", "kb/server.py\r\n\n\n", 1)
    )
    # A document that only mentions the words does not.
    assert not search_format.REPO_CONTENT_HEADER_RE.search(
        "Repository: a/b talked about in prose\nSource: none\n"
    )
