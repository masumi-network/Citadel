from __future__ import annotations

import json
from typing import Any

import pytest

from kb import llm_enrichment
from kb.config import CitadelConfig
from kb.learning import LearningProcess
from kb.llm_enrichment import (
    EnrichedChunk,
    EnrichmentOutcome,
    enrich_source_material,
    openrouter_chat,
    paragraph_chunks,
    parse_enriched_chunks,
    parse_json_payload,
    redacted_preview,
)
from kb.mesh import MeshState
from kb.models import IngestResult


@pytest.fixture(autouse=True)
def clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CITADEL_LLM_ENRICHMENT_ENABLED",
        "CITADEL_LLM_ENRICHMENT_THRESHOLD_CHARS",
        "CITADEL_DETERMINISTIC_CHUNK_CHARS",
        "CITADEL_LLM_MODEL",
        "OPENROUTER_API_KEY",
        "LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def enable_enrichment(monkeypatch: pytest.MonkeyPatch, *, threshold: str = "10") -> None:
    monkeypatch.setenv("CITADEL_LLM_ENRICHMENT_ENABLED", "true")
    monkeypatch.setenv("CITADEL_LLM_ENRICHMENT_THRESHOLD_CHARS", threshold)


LONG_MATERIAL = (
    "First paragraph about the architecture decision.\n\n"
    "Second paragraph about the rollout plan and operational runbook details."
)


def test_paragraph_chunks_split_on_blank_lines_and_respect_max_chars() -> None:
    chunks = paragraph_chunks(LONG_MATERIAL, max_chars=60)

    assert len(chunks) == 3
    assert chunks[0].startswith("First paragraph")
    assert chunks[1].startswith("Second paragraph")
    assert chunks[1] + chunks[2] == LONG_MATERIAL.split("\n\n", 1)[1]
    assert all(len(chunk) <= 60 for chunk in chunks)
    # Small paragraphs are grouped back together under a large cap.
    assert paragraph_chunks(LONG_MATERIAL, max_chars=10_000) == [LONG_MATERIAL]


def test_paragraph_chunks_split_oversized_paragraphs_without_dropping_text() -> None:
    material = "row one\nrow two\n" + ("x" * 31)

    chunks = paragraph_chunks(material, max_chars=12)

    assert all(len(chunk) <= 12 for chunk in chunks)
    assert "".join(chunks) == material


def test_deterministic_source_chunks_preserve_whitespace_and_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CITADEL_CHUNK_BUDGET_TOKENS", "32")
    data = "  α\n\n界界界\t" + ("emoji 🧭 " * 60) + "  "

    chunks = llm_enrichment.deterministic_source_chunks(data, max_chars=1200)

    assert "".join(chunks) == data
    assert len(chunks) > 1
    assert all(
        llm_enrichment._count_bpe(chunk) <= 32
        for chunk in chunks
        if llm_enrichment._count_bpe(chunk) is not None
    )


def test_deterministic_source_chunks_repeat_repo_header() -> None:
    prefix = (
        "# org/repo/README.md\n\n"
        "Repository: org/repo\n"
        "Source: https://github.com/org/repo/blob/main/README.md\n"
        "Commit: abc\n"
        "Blob: def\n"
        "\n---\n\n"
    )
    body = "first body line\nsecond body line\n" + ("z" * 40)

    chunks = llm_enrichment.deterministic_source_chunks(
        prefix + body,
        max_chars=150,
    )

    assert len(chunks) > 1
    assert all(chunk.startswith(prefix) for chunk in chunks)
    assert "".join(chunk[len(prefix) :] for chunk in chunks) == body


def test_repo_header_over_chunk_limit_fails_closed() -> None:
    prefix = (
        "# org/repo/README.md\n\n"
        "Repository: org/repo\n"
        "Source: https://github.com/org/repo/blob/main/README.md\n"
        "Commit: abc\n"
        "Blob: def\n"
        "\n---\n\n"
    )

    with pytest.raises(ValueError, match="provenance header exceeds"):
        llm_enrichment.deterministic_source_chunks(prefix + "body", max_chars=10)


def test_deterministic_chunks_survive_cognee_paragraph_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from cognee.tasks.chunks import chunk_by_paragraph

    chunk_by_sentence_module = importlib.import_module(
        "cognee.tasks.chunks.chunk_by_sentence"
    )

    monkeypatch.setenv("CITADEL_CHUNK_BUDGET_TOKENS", "256")
    encoding = llm_enrichment.chunk_window._bpe_encoding()
    assert encoding is not None
    monkeypatch.setattr(
        chunk_by_sentence_module,
        "get_word_size",
        lambda text: len(encoding.encode(text)),
    )

    prefix = (
        "# org/repo/notes.md\n\n"
        "Repository: org/repo\n"
        "Source: https://github.com/org/repo/blob/main/notes.md\n"
        "Commit: abc\n"
        "Blob: def\n"
        "\n---\n\n"
    )
    data = prefix + ("semantic retrieval details " * 500)
    chunks = llm_enrichment.deterministic_source_chunks(data)

    assert len(chunks) > 1
    for chunk in chunks:
        downstream = list(chunk_by_paragraph(chunk, max_chunk_size=256))
        assert len(downstream) == 1
        assert downstream[0]["text"] == chunk
        assert downstream[0]["chunk_size"] <= 256


def test_markdown_horizontal_rule_is_not_treated_as_repo_header() -> None:
    data = "# Plain note\n\n---\n\n" + ("body " * 80)

    chunks = llm_enrichment.deterministic_source_chunks(data, max_chars=40)

    assert len(chunks) > 1
    assert not any(chunk.startswith("# Plain note\n\n---\n\n# Plain note") for chunk in chunks)


def test_disabled_enrichment_is_a_pure_passthrough() -> None:
    outcome = enrich_source_material(LONG_MATERIAL)

    assert outcome.used_llm is False
    assert outcome.reason == "disabled"
    assert outcome.chunks == (EnrichedChunk(text=LONG_MATERIAL),)
    assert outcome.chunked is False


def test_material_below_threshold_is_not_sent_to_the_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_enrichment(monkeypatch, threshold="100000")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def explode(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("LLM must not be called below the threshold")

    monkeypatch.setattr(llm_enrichment, "openrouter_chat", explode)

    outcome = enrich_source_material(LONG_MATERIAL)

    assert outcome.reason == "below_threshold"
    assert outcome.chunks == (EnrichedChunk(text=LONG_MATERIAL),)


def test_missing_api_key_falls_back_to_paragraph_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_enrichment(monkeypatch)

    outcome = enrich_source_material(LONG_MATERIAL)

    assert outcome.used_llm is False
    assert outcome.reason == "no_api_key"
    assert [chunk.text for chunk in outcome.chunks] == paragraph_chunks(LONG_MATERIAL)


def test_malformed_llm_output_falls_back_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_enrichment(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_enrichment, "openrouter_chat", lambda *a, **k: "definitely { not json"
    )

    outcome = enrich_source_material(LONG_MATERIAL)

    assert outcome.used_llm is False
    assert outcome.reason == "unparseable_output"
    assert [chunk.text for chunk in outcome.chunks] == paragraph_chunks(LONG_MATERIAL)


def test_llm_call_failure_falls_back_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_enrichment(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm_enrichment, "openrouter_chat", lambda *a, **k: None)

    outcome = enrich_source_material(LONG_MATERIAL)

    assert outcome.reason == "llm_failed"
    assert outcome.used_llm is False


def test_partial_llm_output_skips_bad_entries_and_keeps_good_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_enrichment(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    payload = json.dumps(
        {
            "chunks": [
                {"text": "Good chunk", "summary": "One line", "tags": ["ops", "OPS", 7]},
                {"summary": "missing text"},
                "not a dict",
                {"text": "  ", "tags": ["empty-text"]},
                {"text": "Second good chunk", "tags": "not-a-list"},
            ]
        }
    )
    monkeypatch.setattr(
        llm_enrichment, "openrouter_chat", lambda *a, **k: f"```json\n{payload}\n```"
    )

    outcome = enrich_source_material(LONG_MATERIAL)

    assert outcome.used_llm is True
    assert outcome.reason == "llm"
    assert len(outcome.chunks) == 2
    assert outcome.chunks[0].text == "Good chunk"
    assert outcome.chunks[0].summary == "One line"
    assert outcome.chunks[0].tags == ("ops",)
    assert outcome.chunks[1].tags == ()


def test_security_flagged_material_is_never_sent_to_the_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_enrichment(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def explode(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("flagged content must never reach the LLM")

    monkeypatch.setattr(llm_enrichment, "openrouter_chat", explode)
    flagged = (
        "Deploy notes\n\nghp_0123456789abcdefghijklmnopqrstuvwxyz123456\n\nMore text"
    )

    outcome = enrich_source_material(flagged)

    assert outcome.used_llm is False
    assert outcome.reason == "security_flagged"
    assert outcome.chunks  # deterministic fallback still produces ingestable chunks


def test_redacted_preview_masks_secrets_for_logging() -> None:
    preview = redacted_preview(
        "config api_key=supersecretvalue1234 and token: ctdl_abcdef123456 end"
    )

    assert "supersecretvalue1234" not in preview
    assert "ctdl_abcdef123456" not in preview
    assert "[REDACTED]" in preview


def test_parse_json_payload_tolerates_fences_and_prose() -> None:
    assert parse_json_payload('Sure! ```json\n{"chunks": []}\n```') == {"chunks": []}
    assert parse_json_payload('prefix {"a": 1} suffix') == {"a": 1}
    assert parse_json_payload("[1, 2]") == [1, 2]
    assert parse_json_payload("no json here") is None


def test_parse_enriched_chunks_accepts_bare_lists() -> None:
    chunks = parse_enriched_chunks('[{"text": "A", "tags": ["x"]}]')

    assert chunks == [EnrichedChunk(text="A", summary=None, tags=("x",))]


def test_openrouter_chat_returns_none_without_credentials() -> None:
    assert openrouter_chat([], model="deepseek/deepseek-v4-flash", operation="test") is None


class RecordingCitadel:
    config = CitadelConfig(default_dataset="notes")

    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []
        self.cognify_calls: list[list[str]] = []
        self.cognee = self

    async def ingest(self, data: str, **kwargs: Any) -> IngestResult:
        self.ingest_calls.append({"data": data, **kwargs})
        return IngestResult(True, "accepted", "notes", tuple(kwargs.get("tags") or ()))

    async def improve(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    def schedule_cognify(self, datasets: list[str]) -> None:
        self.cognify_calls.append(datasets)


async def test_learning_process_ingests_enriched_chunks_with_merged_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = EnrichmentOutcome(
        chunks=(
            EnrichedChunk(text="Chunk one", summary="First part", tags=("alpha",)),
            EnrichedChunk(text="Chunk two", tags=("beta",)),
        ),
        used_llm=True,
        reason="llm",
        model="deepseek/deepseek-v4-flash",
    )
    monkeypatch.setattr("kb.learning.enrich_source_material", lambda data: outcome)
    citadel = RecordingCitadel()
    mesh = MeshState()
    learning = LearningProcess(citadel, mesh=mesh)

    result = await learning.learn("Raw long material", tags=["team"])
    snapshot = await mesh.snapshot(citadel.config)

    assert len(citadel.ingest_calls) == 2
    assert citadel.cognify_calls == [["notes"]]
    assert citadel.ingest_calls[0]["data"] == "Summary: First part\n\nChunk one"
    assert citadel.ingest_calls[0]["tags"] == ["team", "alpha"]
    assert citadel.ingest_calls[1]["tags"] == ["team", "beta"]
    assert result.accepted_chunks == 2
    assert result.enrichment == {
        "used_llm": True,
        "reason": "llm",
        "chunks": 2,
        "model": "deepseek/deepseek-v4-flash",
    }
    enrichment_events = [
        event for event in snapshot["events"] if event["type"] == "enrichment"
    ]
    assert enrichment_events[0]["details"]["used_llm"] is True
    assert enrichment_events[0]["details"]["chunks"] == 2


async def test_learning_process_keeps_single_ingest_when_enrichment_disabled() -> None:
    citadel = RecordingCitadel()
    learning = LearningProcess(citadel)

    result = await learning.learn("Plain note", tags=["ops"])

    assert len(citadel.ingest_calls) == 1
    assert citadel.ingest_calls[0]["data"] == "Plain note"
    assert result.enrichment is None
    assert result.chunk_ingests == ()


async def test_learning_process_bounds_long_plain_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CITADEL_DETERMINISTIC_CHUNK_CHARS", "32")
    citadel = RecordingCitadel()
    learning = LearningProcess(citadel)

    result = await learning.learn("line one\nline two\n" + ("z" * 80))

    assert result.accepted_chunks > 1
    assert len(citadel.ingest_calls) == result.accepted_chunks
    assert all(len(call["data"]) <= 32 for call in citadel.ingest_calls)
    assert citadel.cognify_calls == [["notes"]]


async def test_learning_process_survives_enrichment_explosions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(data: str) -> EnrichmentOutcome:
        raise RuntimeError("enricher crashed")

    monkeypatch.setattr("kb.learning.enrich_source_material", explode)
    citadel = RecordingCitadel()
    learning = LearningProcess(citadel)

    result = await learning.learn("Material survives")

    assert result.ingest.accepted is True
    assert len(citadel.ingest_calls) == 1
