from __future__ import annotations

from kb.search_format import (
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
    # Shaped like a spec, but the vault stores no provenance for it, so it may
    # not claim authority — only that it looks like one.
    assert infer_trust_tier(spec) == "unattested"
    assert infer_content_hint(spec) == "looks-like-spec"

    activity = {"text": "GitHub org daily digest for masumi-network"}
    assert infer_doc_type(activity) == "activity"
    assert infer_trust_tier(activity) == "unattested"

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
        assert infer_trust_tier(item) == "unattested", item
        assert normalize_search_hit(item)["trust_tier"] == "unattested", item


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
