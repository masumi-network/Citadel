"""The CLI ``--local`` search path must attest provenance from what it read.

Two implementations answer "what is this hit's doc_type / trust_tier":
``kb.server.with_result_metadata`` for the HTTP route, and
``kb.search_format.normalize_search_hit`` for CLI ``--json`` output. The second
re-derives from scratch, and its only attested signal is
``item["_citadel"]["dataset"]``.

Nothing on the local path ever attached that envelope, so a genuine
session-trace hit came back ``trust_tier: "unattested"`` — byte-identical to an
honestly unattested hit. These tests pin the two paths to the same answer for
the same bytes, and pin the marker that says whether the tier was OBSERVED or
merely defaulted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import pytest

from kb.agent_workflows import normalize_local_search_results
from kb.search_format import (
    DOC_TYPE_TRACE,
    TRUST_BASIS_ATTESTED,
    TRUST_BASIS_UNKNOWN,
    TRUST_REFERENCE,
    TRUST_UNATTESTED,
    normalize_search_hit,
    shape_search_payload,
)

SESSION_TRACES = "session-traces"
QUERY = "kupo retry policy"

# What cognee hands back on the local path: no `_citadel` envelope at all.
RAW_TRACE_HIT: dict[str, Any] = {
    "id": "trace-abc-123",
    "text": "# Session trace\nAuthor-Seat: seat:probe\nWe changed the kupo retry policy.",
}


def _local_hits(raw: list[dict[str, Any]], *, dataset: str | None) -> list[dict[str, Any]]:
    """Run hits through the exact seam ``kb.cli._search_local`` uses."""
    payload = normalize_local_search_results(raw, dataset=dataset)
    return shape_search_payload(payload, query=QUERY)["results"]


def _http_hit(raw: dict[str, Any], *, dataset: str) -> dict[str, Any]:
    """Run one hit through the seam the live HTTP route uses."""
    from kb.server import with_result_metadata

    served = with_result_metadata(dict(raw), 0, dataset, query=QUERY)
    return normalize_search_hit(served, index=0, query=QUERY)


def test_local_and_http_paths_agree_on_a_session_trace() -> None:
    """Same bytes, same dataset, two implementations — one answer.

    Before the fix the local path returned ``other`` / ``unattested`` for a hit
    the HTTP route labels ``session-trace`` / ``reference-only``.
    """
    local = _local_hits([RAW_TRACE_HIT], dataset=SESSION_TRACES)[0]
    served = _http_hit(RAW_TRACE_HIT, dataset=SESSION_TRACES)

    assert local["trust_tier"] == TRUST_REFERENCE
    assert local["doc_type"] == DOC_TYPE_TRACE
    assert (local["doc_type"], local["trust_tier"]) == (served["doc_type"], served["trust_tier"])


def test_local_hit_reports_the_dataset_it_was_read_out_of() -> None:
    """The dataset is the attesting signal; a hit that drops it cannot be checked."""
    hit = _local_hits([RAW_TRACE_HIT], dataset=SESSION_TRACES)[0]
    assert hit["dataset"] == SESSION_TRACES
    assert hit["_citadel"]["dataset"] == SESSION_TRACES


def test_local_seat_hit_stays_unattested_but_says_so_from_an_observation() -> None:
    """Attaching the envelope must not hand every local hit a trust upgrade."""
    hit = _local_hits([RAW_TRACE_HIT], dataset="seat:probe")[0]
    assert hit["trust_tier"] == TRUST_UNATTESTED
    assert hit["trust_tier_basis"] == TRUST_BASIS_ATTESTED


def test_a_defaulted_trust_tier_is_marked_unknown_not_unattested() -> None:
    """No envelope means the authority was never consulted.

    ``unattested`` alone cannot distinguish "checked, no provenance" from
    "never checked", so the basis is emitted alongside it for a caller to
    branch on.
    """
    hit = _local_hits([RAW_TRACE_HIT], dataset=None)[0]
    assert hit["trust_tier"] == TRUST_UNATTESTED
    assert hit["trust_tier_basis"] == TRUST_BASIS_UNKNOWN


def test_http_hits_report_an_attested_basis() -> None:
    served = _http_hit(RAW_TRACE_HIT, dataset=SESSION_TRACES)
    assert served["trust_tier_basis"] == TRUST_BASIS_ATTESTED


def test_non_dict_hit_reports_an_unknown_basis() -> None:
    assert normalize_search_hit("a bare string")["trust_tier_basis"] == TRUST_BASIS_UNKNOWN


def test_normalize_local_search_results_does_not_overwrite_a_real_envelope() -> None:
    """A hit that already carries provenance keeps it; we only fill a gap."""
    already = {"id": "x", "text": "note", "_citadel": {"dataset": "central", "trust": "x"}}
    out = normalize_local_search_results([already], dataset=SESSION_TRACES)
    assert out["results"][0]["_citadel"] == {"dataset": "central", "trust": "x"}


def test_normalize_local_search_results_keeps_its_old_shape() -> None:
    """Existing callers pass no dataset and must be unaffected."""
    assert normalize_local_search_results([{"text": "a"}])["results"][0]["text"] == "a"
    wrapped = normalize_local_search_results({"results": [{"text": "b"}], "timed_out": True})
    assert wrapped["timed_out"] is True


# --- the CLI end to end: tests/test_cli_commands.py monkeypatches _search_local
# --- away entirely, so nothing exercised the real handler.


class _FakeConfig:
    default_dataset = "central"
    github_sync_dataset = "github-sync"
    github_sync_session = "gh"
    default_session = "default"


class _FakeCitadel:
    """Stands in for the in-process server stack; records the dataset asked for."""

    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.asked_for: str | None = None

    @classmethod
    def from_env(cls) -> "_FakeCitadel":
        return cls()

    def resolve_search_dataset(self, dataset: str | None) -> str:
        return dataset or self.config.default_dataset

    async def search(
        self,
        query: str,
        *,
        dataset: str | None = None,
        session_id: str | None = None,
        top_k: int = 10,
    ) -> list[Any]:
        self.asked_for = dataset
        return [dict(RAW_TRACE_HIT)]


def _search_args(dataset: str | None) -> argparse.Namespace:
    return argparse.Namespace(
        command="search",
        query=QUERY,
        local=True,
        dataset=dataset,
        session=None,
        top_k=5,
        json=True,
    )


@pytest.mark.parametrize(
    ("dataset", "expected_tier"),
    [(SESSION_TRACES, TRUST_REFERENCE), ("seat:probe", TRUST_UNATTESTED)],
)
def test_search_local_json_carries_the_attested_tier(
    monkeypatch: Any, capsys: Any, dataset: str, expected_tier: str
) -> None:
    import kb.cli as cli
    import kb.service as service

    monkeypatch.setattr(service, "Citadel", _FakeCitadel)
    assert asyncio.run(cli._search_local(_search_args(dataset))) == 0

    payload = json.loads(capsys.readouterr().out)
    hit = payload["results"][0]
    assert hit["trust_tier"] == expected_tier
    assert hit["trust_tier_basis"] == TRUST_BASIS_ATTESTED
    assert hit["dataset"] == dataset


def test_search_local_falls_back_to_the_configured_default_dataset(
    monkeypatch: Any, capsys: Any
) -> None:
    """``--local`` without ``--dataset`` still knows which dataset it read."""
    import kb.cli as cli
    import kb.service as service

    monkeypatch.setattr(service, "Citadel", _FakeCitadel)
    assert asyncio.run(cli._search_local(_search_args(None))) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["dataset"] == _FakeConfig.default_dataset
