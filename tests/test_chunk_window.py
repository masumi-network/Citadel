"""Chunk budget, embed-window overflow detection, and the un-chunkable ingest gate (#227).

The defect these cover: cognee sizes chunks with a budget counted in GPT-4o BPE
tokens, while the deployed embedder (fastembed ``BAAI/bge-small-en-v1.5``)
truncates at 512 wordpieces and raises nothing when it does. Measured on this
tree at the shipped default of 8191, 245 of 274 chunks over a 172-document probe
corpus exceeded the window, worst 12,642 wordpieces.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest

from kb import chunk_window


REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeWordpieceTokenizer:
    """Stands in for the model's own tokenizer: one wordpiece per character."""

    def __init__(self) -> None:
        self.calls = 0

    def count(self, text: str) -> int:
        self.calls += 1
        return len(text)


@pytest.fixture(autouse=True)
def _clean_window_state() -> Any:
    """Snapshot both budget variables.

    ``apply_chunk_budget`` writes ``os.environ`` directly, which monkeypatch does
    not undo, so without this one test's clamp decides the next test's budget.
    """
    saved = {
        name: os.environ.get(name)
        for name in (chunk_window.CHUNK_BUDGET_ENV, chunk_window.COGNEE_BUDGET_ENV)
    }
    chunk_window.reset_embed_window_report()
    chunk_window.reset_applied_budget()
    yield
    chunk_window.reset_embed_window_report()
    chunk_window.reset_applied_budget()
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# --------------------------------------------------------------------------
# 1. The budget is labelled an observation, not a bound.
# --------------------------------------------------------------------------


def test_budget_is_an_integer_the_operator_can_lower() -> None:
    assert isinstance(chunk_window.OBSERVED_CHUNK_BUDGET_TOKENS, int)
    assert chunk_window.OBSERVED_CHUNK_BUDGET_TOKENS > 0


def test_budget_env_override_wins(monkeypatch: Any) -> None:
    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "192")
    assert chunk_window.resolve_chunk_budget() == 192


def test_budget_env_override_ignores_garbage(monkeypatch: Any) -> None:
    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "not-a-number")
    assert chunk_window.resolve_chunk_budget() == chunk_window.OBSERVED_CHUNK_BUDGET_TOKENS


def test_nothing_in_the_shipped_text_claims_the_budget_cannot_overflow() -> None:
    """No comment, log line or doc may claim this budget is a bound.

    Three budgets picked by sampling-and-multiplying were already refuted by the
    next content class (384 by Korean prose, 352 by minified JSON, 284 by this
    tree's own minified JS at 1,710 wordpieces). A file that calls the number
    safe teaches the next reader to stop measuring.
    """
    forbidden = (
        "cannot overflow",
        "can never exceed",
        "never exceeds",
        "guarantees the window",
        "guaranteed to fit",
        "always fits",
        "most conservative known budget",
        "safe for all content",
    )
    targets = [
        REPO_ROOT / "kb" / "chunk_window.py",
        REPO_ROOT / ".env.example",
    ]
    for path in targets:
        text = path.read_text(encoding="utf-8").lower()
        for phrase in forbidden:
            assert phrase not in text, f"{path.name} claims a bound it does not have: {phrase!r}"


def test_the_budget_is_explicitly_labelled_an_observation() -> None:
    text = (REPO_ROOT / "kb" / "chunk_window.py").read_text(encoding="utf-8").lower()
    assert "observation" in text
    assert "not a bound" in text


# --------------------------------------------------------------------------
# 2. The detector at the embed boundary.
# --------------------------------------------------------------------------


def test_detector_fires_on_a_planted_over_window_chunk(caplog: Any) -> None:
    """Plant a chunk past the window and prove the detector says so.

    A detector that has never fired is indistinguishable from one with nothing
    to detect, so this plants the input rather than waiting for real content.
    """
    tokenizer = _FakeWordpieceTokenizer()
    planted = "x" * 1710  # the worst real chunk measured on this tree, in wordpieces
    with caplog.at_level(logging.WARNING, logger="kb.chunk_window"):
        over = chunk_window.record_embed_batch(
            [planted], count_tokens=tokenizer.count, window=512
        )

    assert over == 1
    report = chunk_window.embed_window_report()
    assert report["over"] == 1
    assert report["checked"] == 1
    assert report["worst_tokens"] == 1710
    assert report["window"] == 512
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("embed window exceeded" in message for message in messages)
    assert any("1710" in message for message in messages)


def test_detector_is_silent_on_in_window_chunks(caplog: Any) -> None:
    """The other direction: a detector that always fires is also useless."""
    tokenizer = _FakeWordpieceTokenizer()
    with caplog.at_level(logging.WARNING, logger="kb.chunk_window"):
        over = chunk_window.record_embed_batch(
            ["y" * 511, "z" * 512], count_tokens=tokenizer.count, window=512
        )

    assert over == 0
    report = chunk_window.embed_window_report()
    assert report["over"] == 0
    assert report["checked"] == 2
    assert report["worst_tokens"] == 512
    assert not [rec for rec in caplog.records if "embed window exceeded" in rec.getMessage()]


def test_detector_never_logs_the_content_it_measured(caplog: Any) -> None:
    """This repository is public and ingest carries user text: log a digest, not the chunk."""
    secret = "SUPERSECRET" + "q" * 1000
    with caplog.at_level(logging.WARNING, logger="kb.chunk_window"):
        chunk_window.record_embed_batch([secret], count_tokens=len, window=512)

    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert "SUPERSECRET" not in joined
    assert "sha256:" in joined


def test_window_is_read_from_the_model_not_from_a_constant() -> None:
    """The window comes from the tokenizer the embedder will actually use."""

    class _Tok:
        truncation = {"max_length": 384, "stride": 0}

        def encode(self, text: str) -> Any:  # pragma: no cover - not called here
            raise AssertionError

    class _Model:
        tokenizer = _Tok()

    class _TextEmbedding:
        model = _Model()

    class _Engine:
        embedding_model = _TextEmbedding()

    assert chunk_window.resolve_model_window(_Engine()) == 384


def test_unresolvable_window_is_recorded_rather_than_assumed() -> None:
    """No window means no measurement — say so, do not invent 512."""

    class _Engine:
        embedding_model = object()

    assert chunk_window.resolve_model_window(_Engine()) is None


def test_detector_reports_when_it_could_not_measure() -> None:
    chunk_window.record_unmeasurable_batch(2)
    assert chunk_window.embed_window_report()["unmeasured"] == 2


# --------------------------------------------------------------------------
# 3. The clamp has to reach the real chunk budget.
# --------------------------------------------------------------------------


def test_clamp_reaches_get_max_chunk_tokens_after_a_cognee_operation_warmed_the_caches(
    monkeypatch: Any,
) -> None:
    """Clearing ``get_embedding_config`` alone does NOT change the chunk budget.

    ``get_max_chunk_tokens()`` reads ``get_vector_engine().embedding_engine``, and
    ``create_vector_engine`` is ``closing_lru_cache``d, so the warm entry keeps
    handing back the engine built at the old budget. Measured on this tree:
    clearing the config cache left it at 8191, clearing the embedding-engine cache
    as well left it at 8191, and only clearing the vector-engine cache moved it.
    """
    from cognee.infrastructure.databases.vector.embeddings.config import get_embedding_config
    from cognee.infrastructure.llm.utils import get_max_chunk_tokens

    # A cheap engine keeps this off the network and off the ONNX loader while
    # still exercising all three real caches.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-large")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "3072")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-used")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "lancedb")
    monkeypatch.delenv(chunk_window.COGNEE_BUDGET_ENV, raising=False)
    monkeypatch.delenv(chunk_window.CHUNK_BUDGET_ENV, raising=False)
    chunk_window.reset_applied_budget()
    get_embedding_config.cache_clear()

    # A cognee operation runs BEFORE the guard, warming every cache at 8191.
    warm = get_max_chunk_tokens()
    assert warm > chunk_window.OBSERVED_CHUNK_BUDGET_TOKENS, (
        f"expected the un-clamped default to be larger than the budget, got {warm}"
    )

    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "256")
    applied = chunk_window.apply_chunk_budget()

    assert applied == 256
    assert os.environ[chunk_window.COGNEE_BUDGET_ENV] == "256"
    assert get_max_chunk_tokens() == 256, (
        "the clamp did not reach the real chunk budget: a warm create_vector_engine "
        "entry is still serving an embedding engine built at the old budget"
    )


def test_an_explicit_cognee_budget_is_respected(monkeypatch: Any) -> None:
    """An operator who sets cognee's own variable is not silently overruled."""
    monkeypatch.delenv(chunk_window.CHUNK_BUDGET_ENV, raising=False)
    monkeypatch.setenv(chunk_window.COGNEE_BUDGET_ENV, "300")
    chunk_window.reset_applied_budget()

    assert chunk_window.apply_chunk_budget() == 300
    assert os.environ[chunk_window.COGNEE_BUDGET_ENV] == "300"


# --------------------------------------------------------------------------
# 4. Refuse and record; never mangle.
# --------------------------------------------------------------------------


def test_unbroken_word_over_budget_is_reported(monkeypatch: Any) -> None:
    """cognee splits words only on ' ' and [.;!?…。！？] — not on newlines or tabs.

    So one minified line is one word. At a budget of 256 such a word either
    raises ``ValueError`` out of ``chunk_by_sentence`` (killing the whole
    dataset's pipeline run) or is emitted verbatim as an over-window chunk.
    """
    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "64")
    blob = "a" * 4000  # no space, no sentence ending: one word to cognee
    span = chunk_window.check_chunkable(f"intro text.\n{blob}\nmore text.")

    assert span is not None
    assert span.tokens > 64
    assert span.budget == 64
    assert span.fingerprint.startswith("sha256:")


def test_ordinary_prose_is_not_reported(monkeypatch: Any) -> None:
    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "64")
    prose = "The payment service escrows funds until the seller submits a result. " * 40
    assert chunk_window.check_chunkable(prose) is None


def test_check_chunkable_never_rewrites_the_text(monkeypatch: Any) -> None:
    """The policy is refuse and record. A splitter that edits content is not on the menu.

    An earlier attempt gated on UTF-8 bytes and corrupted two of this project's
    own documents inside fenced config blocks: ``SEVERITY=high`` became
    ``SEVERITY=hig h`` and ``CITADEL_GOOGLE_CHAT_SPACE_NAME`` stopped existing in
    the index. ``check_chunkable`` returns a verdict; it has no return path that
    can carry modified text.
    """
    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "64")
    source = "SEVERITY=high\nCITADEL_GOOGLE_CHAT_SPACE_NAME=spaces/AAA\n" + ("b" * 4000)
    span = chunk_window.check_chunkable(source)

    assert span is not None
    assert not hasattr(span, "text")
    assert not any(isinstance(value, str) and "SEVERITY" in value for value in vars(span).values())


def test_byte_prefilter_is_a_sound_lower_bound() -> None:
    """The prefilter skips words whose UTF-8 length is under the budget.

    That is sound only because every BPE token covers at least one UTF-8 byte,
    so byte length is an upper bound on token count. It is a prefilter, never the
    gate: the verdict is taken in BPE tokens, which is the unit that actually
    raises.
    """
    import tiktoken

    encoding = tiktoken.encoding_for_model("gpt-4o")
    for sample in ("héllo wörld", "🚀🔥💡", "плати́ть", "a" * 300, "日本語テキスト"):
        assert len(encoding.encode(sample)) <= len(sample.encode("utf-8"))


@pytest.mark.parametrize("delimiter", [" ", ".", ";", "!", "?", "…", "。", "！", "？"])
def test_word_segmentation_matches_cognees_delimiters(delimiter: Any) -> None:
    """Segment on exactly what cognee segments on, or the gate under-measures.

    ``chunk_by_word`` breaks on a single space and on the sentence endings, and on
    nothing else — notably not on ``\\n`` or ``\\t``.
    """
    monkeypatch_free = f"aaa{delimiter}bbb"
    assert chunk_window.split_cognee_words(monkeypatch_free) == ["aaa" + delimiter, "bbb"]


def test_newline_is_not_a_word_boundary() -> None:
    assert chunk_window.split_cognee_words("aaa\nbbb") == ["aaa\nbbb"]


def test_ingest_refuses_content_cognee_cannot_chunk(monkeypatch: Any) -> None:
    """Refuse and record. One such document fails the whole dataset's pipeline run."""
    import asyncio

    from kb.config import CitadelConfig
    from kb.service import Citadel
    from tests.test_service import FakeCognee

    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "64")
    fake = FakeCognee()
    citadel = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    poisoned = "A note about the bundle.\n" + ("z" * 4000)
    result = asyncio.run(citadel.ingest(poisoned))

    assert not result.accepted
    assert result.reason == "unchunkable_content"
    assert fake.remember_calls == [], "refused content must not reach the vault"


def test_ingest_accepts_ordinary_content_at_the_same_budget(monkeypatch: Any) -> None:
    """The other direction: the gate must not swallow prose."""
    import asyncio

    from kb.config import CitadelConfig
    from kb.service import Citadel
    from tests.test_service import FakeCognee

    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "64")
    fake = FakeCognee()
    citadel = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = asyncio.run(citadel.ingest("A perfectly ordinary engineering note about caching."))

    assert result.accepted
    assert len(fake.remember_calls) == 1


def test_ingest_stores_the_callers_bytes_unchanged(monkeypatch: Any) -> None:
    """Citadel adds no transformation between the caller's text and the store.

    The invariant worth having is that chunking never edits content. cognee's own
    chunker joins batched paragraphs with a space, so byte-identical reassembly is
    not ours to assert downstream — what is ours is that nothing on this side
    rewrites, pads or splits the document on its way in.
    """
    import asyncio

    from kb.config import CitadelConfig
    from kb.service import Citadel
    from tests.test_service import FakeCognee

    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "256")
    fake = FakeCognee()
    citadel = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    source = (
        "```bash\nSEVERITY=high\nCITADEL_GOOGLE_CHAT_SPACE_NAME=spaces/AAAA\n```\n"
        "Trailing prose with  double  spaces and a\ttab.\n"
    )
    result = asyncio.run(citadel.ingest(source))

    assert result.accepted
    assert fake.remember_calls[0]["data"] == source


def test_unchunkable_guard_can_be_turned_off(monkeypatch: Any) -> None:
    import asyncio

    from kb.config import CitadelConfig
    from kb.service import Citadel
    from tests.test_service import FakeCognee

    monkeypatch.setenv(chunk_window.CHUNK_BUDGET_ENV, "64")
    monkeypatch.setenv(chunk_window.GUARD_ENV, "false")
    fake = FakeCognee()
    citadel = Citadel(CitadelConfig(default_dataset="notes"), cognee=fake)

    result = asyncio.run(citadel.ingest("A note.\n" + ("z" * 4000)))

    assert result.accepted
    assert len(fake.remember_calls) == 1


def test_word_segmentation_agrees_with_cognee() -> None:
    """Checked against the real ``chunk_by_word``, not against a description of it."""
    from cognee.tasks.chunks.chunk_by_word import chunk_by_word

    samples = [
        "Hello world. This is a sentence!\nAnd a new line.",
        "no-spaces-at-all\nsecond\tline",
        "trailing spaces after a period.   then more",
        "日本語。テキスト？おわり",
        "a" * 500,
        "",
    ]
    for sample in samples:
        theirs = [word for word, _kind in chunk_by_word(sample)]
        mine = chunk_window.split_cognee_words(sample)
        assert "".join(mine) == sample, "segmentation must be lossless"
        assert max((len(w) for w in mine), default=0) >= max(
            (len(w) for w in theirs), default=0
        ), "our longest segment must not be shorter than cognee's longest word"
