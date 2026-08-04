from __future__ import annotations

from pathlib import Path

from kb.agent_workflows import (
    build_verify_query,
    extract_verify_cues,
    normalize_local_search_results,
    shape_prepare_pr_context,
    shape_verify_report,
)


def test_extract_cues_prefers_mip_tokens() -> None:
    cues = extract_verify_cues("See MIP-003 for purchase endpoint schema and token header")
    assert any("MIP" in c.upper() or "mip" in c.lower() for c in cues)


def test_build_verify_query_enables_spec_mode(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("payment purchase statuses")
    q = build_verify_query(path, path.read_text())
    assert "schema" in q or "endpoint" in q


def test_normalize_local_search_results() -> None:
    assert normalize_local_search_results([{"text": "a"}])["results"][0]["text"] == "a"
    wrapped = normalize_local_search_results({"results": [{"text": "b"}], "timed_out": True})
    assert wrapped["timed_out"] is True


def test_local_search_pipeline_reports_unknown_trust() -> None:
    """The --local pipeline (normalize_local_search_results → shape_search_payload).

    It hands cognee payloads to the shaper with no ``_citadel`` envelope, so no
    attested provenance is ever consulted; the shaped hits must say so
    ("unknown") instead of reading exactly like honestly unattested HTTP hits.
    """
    from kb.search_format import shape_search_payload

    payload = normalize_local_search_results(
        [{"id": "c1", "text": "# Dead-end route\n\nsession trace body"}]
    )
    shaped = shape_search_payload(payload, query="dead-end route")
    assert shaped["results"][0]["trust_tier"] == "unknown"
    assert any("trust_tier is 'unknown'" in w for w in shaped["warnings"])


def test_shape_verify_and_prepare() -> None:
    path = Path("payment.md")
    payload = {
        "results": [
            {
                "title": "MIP-003",
                "path": "MIPs/MIP-003/MIP-003.md",
                "url": "https://github.com/masumi-network/masumi-improvement-proposals/x",
                "text": "purchase endpoint MIP-003",
                "score": 0.8,
            }
        ]
    }
    report = shape_verify_report(
        path=path, file_text="MIP-003 purchase endpoint", search_payload=payload, query="MIP-003"
    )
    # This fixture's hits carry no _citadel envelope, so no attested provenance
    # was consulted: the tier says "unknown", not "unattested".
    assert report["doc_shaped_sources"][0]["trust_tier"] == "unknown"
    assert report["doc_shaped_sources"][0]["content_hint"] == "looks-like-spec"
    assert report["known_overlaps"]
    brief = shape_prepare_pr_context(repo="cardano-dev-skills", topic="masumi", search_payload=payload)
    assert brief["command"] == "prepare-pr-context"
    assert brief["agent_instruction"]
