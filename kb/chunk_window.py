"""Chunk budget for the embedder, and overflow detection at the embed boundary (#227).

The defect
----------
cognee sizes chunks with a budget counted in GPT-4o BPE tokens
(``EmbeddingConfig.embedding_max_completion_tokens``, shipped default 8191), and
``FastembedEmbeddingEngine.get_tokenizer`` builds a ``TikTokenTokenizer(model="gpt-4o")``
to count them. The model that then embeds those chunks is fastembed
``BAAI/bge-small-en-v1.5``, whose own tokenizer declares
``{'max_length': 512, 'stride': 0, 'strategy': 'longest_first', 'direction': 'right'}``
and silently drops everything past that. Nothing raises. Measured on this tree at
the shipped 8191, over a 172-document probe corpus: 245 of 274 emitted chunks
were past the window, the worst at 12,642 wordpieces.

Why there is no arithmetic that fixes this
------------------------------------------
The expansion from BPE tokens to wordpieces is a property of the content, and
three budgets chosen by measuring a ratio and dividing have each been refuted by
the next content class to arrive:

    384  refuted by Korean prose            (529 wordpieces)   [reported, 2026-08-03]
    352  refuted by minified JSON           (589 wordpieces)   [reported, 2026-08-03]
    284  refuted by this tree's minified JS (1,710 wordpieces) [measured here]
         and by a Korean-prose probe at a per-chunk ratio of 2.05, which is
         already past the 1.795 the 284 was derived from [measured here]

The first two rows are carried over from earlier work and were not re-run for
this module; the third was measured in this tree. The Korean case is a
constructed probe rather than vault content, because this repository holds no
non-Latin text to sample.

A second mechanism removes the last hope of an arithmetic answer.
``chunk_by_paragraph`` compares against the budget only when the accumulating
chunk is non-empty (``cognee/tasks/chunks/chunk_by_paragraph.py:40``), so a
single over-budget "word" is emitted verbatim no matter what the budget says.
Measured: one line of minified JS in ``kb/webui`` is 1,392 BPE tokens and comes
out as one 1,710-wordpiece chunk at every budget from 512 down to 192 — at 192
that is 7.2x the budget it supposedly obeys.

What this module ships instead
------------------------------
1. ``OBSERVED_CHUNK_BUDGET_TOKENS`` — an observation, not a bound. See its note.
2. ``record_embed_batch`` — a detector at the embed boundary that measures the
   chunk actually handed to the model, in the model's own units, and says so.
   Predicting an overflow is arithmetic; measuring one is evidence.
3. ``check_chunkable`` — a verdict for the ingest chokepoint on content that
   cannot be chunked at all. It refuses and records. It never edits text.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# The integer, and what it is.
#
# 256 is an OBSERVATION and NOT A BOUND. It is the largest budget in the sweep
# below at which every content class that cognee is able to split landed inside
# the deployed model's window. It is not a guarantee about content that has not
# been measured, and the paragraph above explains why no integer could be.
#
# Sweep over 172 documents (this tree's markdown, Python, minified JS, configs,
# plus probes in ko/zh/ja/ar/hi/th/ru, emoji, maths, base64, hex, UUIDs), run
# through cognee's own ``chunk_by_paragraph`` and counted with the deployed
# model's own tokenizer:
#
#   budget  chunks  over-window  worst wordpieces
#     8191     274    245 (89%)            12,642
#      512   2,922    709 (24%)             1,710
#      384   3,933    527 (13%)             1,710
#      352   4,316    187 ( 4%)             1,710
#      284   5,414     28 (0.5%)            1,710
#      256   6,064      3 (0.05%)           1,710
#      224   7,028      3 (0.04%)           1,710
#      192   8,237      3 (0.04%)           1,710
#
# The three that survive at 256 are the same three that survive at 192: one
# un-splittable minified-JS line, per the mechanism above. Below 256 the chunk
# count keeps climbing and buys nothing against them, which is the whole reason
# to stop here rather than lower. Raise or lower it with CITADEL_CHUNK_BUDGET_TOKENS
# when the detector reports a class this sweep did not contain.
OBSERVED_CHUNK_BUDGET_TOKENS = 256

CHUNK_BUDGET_ENV = "CITADEL_CHUNK_BUDGET_TOKENS"
COGNEE_BUDGET_ENV = "EMBEDDING_MAX_COMPLETION_TOKENS"
DETECTOR_ENV = "CITADEL_EMBED_WINDOW_DETECTOR"
GUARD_ENV = "CITADEL_UNCHUNKABLE_GUARD"

# cognee's ``chunk_by_word`` breaks on a single space and on these sentence
# endings — " .;!?…。！？" — and on nothing else. Not on "\n", not on "\t". That is
# why one line of minified JS is one word to it.
_WORD_SPLIT = re.compile(r"[^ .;!?…。！？]*(?:[.;!?…。！？] *|[ ])?")

# Per-chunk warnings are capped so one bad cognify pass cannot bury the log.
# The counters in ``embed_window_report`` stay exact regardless.
_MAX_WARNINGS = 50

_REPORT: dict[str, int | None] = {}
_APPLIED_BUDGET: int | None = None
_DETECTOR_ANNOUNCED = False


def _fresh_report() -> dict[str, int | None]:
    return {
        "checked": 0,
        "over": 0,
        "worst_tokens": 0,
        "window": None,
        "unmeasured": 0,
        "warnings_emitted": 0,
    }


_REPORT = _fresh_report()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def _positive_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_chunk_budget() -> int:
    """Return the chunk budget in GPT-4o BPE tokens.

    Precedence: Citadel's own knob, then cognee's variable if an operator set it
    directly, then the observation above.
    """
    explicit = _positive_int(os.getenv(CHUNK_BUDGET_ENV))
    if explicit is not None:
        return explicit
    inherited = _positive_int(os.getenv(COGNEE_BUDGET_ENV))
    if inherited is not None:
        return inherited
    return OBSERVED_CHUNK_BUDGET_TOKENS


def reset_applied_budget() -> None:
    """Forget that the budget was applied. For tests and for a deliberate re-clamp."""
    global _APPLIED_BUDGET
    _APPLIED_BUDGET = None


def apply_chunk_budget(*, force: bool = False) -> int:
    """Write the budget where cognee reads it, and make it reach ``get_max_chunk_tokens``.

    Setting the environment variable is not enough on its own. ``get_max_chunk_tokens``
    resolves ``get_vector_engine().embedding_engine``, and three caches sit on that
    path: ``get_embedding_config`` (``lru_cache``), ``create_embedding_engine``
    (``lru_cache``) and ``_create_vector_engine`` (``closing_lru_cache``). Measured
    on this tree with the caches warm at 8191: clearing the config cache left the
    answer at 8191, clearing the embedding-engine cache as well left it at 8191,
    and only clearing the vector-engine cache moved it to 256. The vector engine
    is the one that matters, because it captured the embedding engine object.

    Returns the budget now in force.
    """
    global _APPLIED_BUDGET

    target = resolve_chunk_budget()
    if not force and _APPLIED_BUDGET == target:
        return target

    previous = os.environ.get(COGNEE_BUDGET_ENV)
    os.environ[COGNEE_BUDGET_ENV] = str(target)
    _APPLIED_BUDGET = target

    if previous != str(target):
        logger.info(
            "Chunk budget set to %s BPE tokens via %s (was %s). This is an observed "
            "value, not a bound: %s reports chunks that exceed the model's window.",
            target,
            COGNEE_BUDGET_ENV,
            previous or "unset",
            "kb.chunk_window",
        )

    _clear_cognee_budget_caches()
    return target


def _clear_cognee_budget_caches() -> None:
    """Evict every cache between the environment variable and ``get_max_chunk_tokens``.

    Skipped entirely when cognee has not been imported yet: there is nothing warm
    to evict, and importing it here purely to clear empty caches would drag the
    whole dependency into a path that only wanted to set a variable.
    """
    if "cognee" not in sys.modules:
        return
    try:
        from cognee.infrastructure.databases.vector.create_vector_engine import (
            _create_vector_engine,
        )
        from cognee.infrastructure.databases.vector.embeddings.config import (
            get_embedding_config,
        )
        from cognee.infrastructure.databases.vector.embeddings.get_embedding_engine import (
            create_embedding_engine,
        )
    except Exception:  # pragma: no cover - a cognee bump that moved these
        logger.exception(
            "Could not reach cognee's embedding caches; the chunk budget may not "
            "have taken effect in this process"
        )
        return

    try:
        get_embedding_config.cache_clear()
        create_embedding_engine.cache_clear()
        _create_vector_engine.cache_clear()
    except Exception:
        # Same degradation as the import guard above: a cognee bump that stops
        # wrapping any of these in ``lru_cache`` must not break every write.
        logger.exception(
            "Could not clear cognee's embedding caches; the chunk budget may not "
            "have taken effect in this process"
        )


# ---------------------------------------------------------------------------
# Detector at the embed boundary
# ---------------------------------------------------------------------------


def embed_window_report() -> dict[str, int | None]:
    """Counts since process start (or since the last reset)."""
    return dict(_REPORT)


def reset_embed_window_report() -> None:
    global _REPORT
    _REPORT = _fresh_report()


def _fingerprint(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def record_embed_batch(
    texts: Iterable[str],
    *,
    count_tokens: Callable[[str], int],
    window: int,
) -> int:
    """Measure what is about to be embedded and report anything past the window.

    ``count_tokens`` counts in the model's own units and must not truncate, or the
    count comes back equal to the window and the overflow disappears into the
    thing that caused it. Returns how many inputs were over.

    Logs a digest and a length, never the content: this repository is public and
    ingest carries user text.
    """
    over = 0
    for text in texts:
        try:
            tokens = int(count_tokens(text))
        except Exception:  # pragma: no cover - a tokenizer that raises is unmeasured
            _REPORT["unmeasured"] = int(_REPORT["unmeasured"] or 0) + 1
            continue

        _REPORT["checked"] = int(_REPORT["checked"] or 0) + 1
        _REPORT["window"] = window
        if tokens > int(_REPORT["worst_tokens"] or 0):
            _REPORT["worst_tokens"] = tokens

        if tokens <= window:
            continue

        over += 1
        _REPORT["over"] = int(_REPORT["over"] or 0) + 1
        emitted = int(_REPORT["warnings_emitted"] or 0)
        if emitted < _MAX_WARNINGS:
            _REPORT["warnings_emitted"] = emitted + 1
            logger.warning(
                "embed window exceeded: %d tokens against a %d window (%.2fx), "
                "%d characters, chunk %s. The model truncates the remainder and "
                "raises nothing, so that text is in the store and not retrievable.",
                tokens,
                window,
                tokens / window if window else 0.0,
                len(text),
                _fingerprint(text),
            )
            if _REPORT["warnings_emitted"] == _MAX_WARNINGS:
                logger.warning(
                    "embed window exceeded: %d per-chunk lines emitted, suppressing "
                    "further ones for this process. Counts stay exact in "
                    "kb.chunk_window.embed_window_report().",
                    _MAX_WARNINGS,
                )
    return over


def record_unmeasurable_batch(count: int) -> None:
    """Record inputs the detector could not measure. Not the same as zero overflows."""
    _REPORT["unmeasured"] = int(_REPORT["unmeasured"] or 0) + int(count)


def _model_tokenizer(engine: Any) -> Any | None:
    """The tokenizer the embedding model will actually use, or None."""
    model = getattr(engine, "embedding_model", None)
    inner = getattr(model, "model", None)
    return getattr(inner, "tokenizer", None)


def resolve_model_window(engine: Any) -> int | None:
    """Read the window off the model's own tokenizer.

    Returns None when it cannot be read. None means not measured; it does not mean
    512, and nothing here substitutes a default for it.
    """
    tokenizer = _model_tokenizer(engine)
    truncation = getattr(tokenizer, "truncation", None)
    if not isinstance(truncation, dict):
        return None
    value = truncation.get("max_length")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _untruncated_counter(engine: Any) -> Callable[[str], int] | None:
    """A counter using a truncation-free copy of the model's own tokenizer.

    The live tokenizer truncates, so counting with it caps every answer at the
    window and hides the number worth reporting.
    """
    cached = getattr(engine, "_citadel_window_counter", None)
    if cached is not None:
        return cached
    if getattr(engine, "_citadel_window_counter_failed", False):
        return None

    tokenizer = _model_tokenizer(engine)
    if tokenizer is None:
        return None
    try:
        from tokenizers import Tokenizer

        clone = Tokenizer.from_str(tokenizer.to_str())
        clone.no_truncation()
    except Exception:
        # Cache the failure too: without this marker every embed batch repeats
        # the clone attempt and logs another traceback, so one bad cognify pass
        # buries the log under identical stack traces.
        logger.exception("Could not build an untruncated counter for the embed detector")
        try:
            engine._citadel_window_counter_failed = True
        except Exception:  # pragma: no cover - slotted engine
            pass
        return None

    def count(text: str) -> int:
        return len(clone.encode(text).ids)

    try:
        engine._citadel_window_counter = count
    except Exception:  # pragma: no cover - slotted engine
        pass
    return count


def detector_enabled() -> bool:
    raw = os.getenv(DETECTOR_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def install_embed_window_detector() -> str | None:
    """Wrap the deployed embedder's ``embed_text`` so overflows become log lines.

    Returns the name of the class that was wrapped, or None. Says so out loud when
    the configured provider is one it does not cover, because a detector nobody
    installed looks exactly like a detector with nothing to report.

    Called on every cognee operation, so each outcome is announced once per process
    rather than once per call.
    """
    global _DETECTOR_ANNOUNCED

    if not detector_enabled():
        if not _DETECTOR_ANNOUNCED:
            _DETECTOR_ANNOUNCED = True
            logger.info("Embed-window detector disabled by %s", DETECTOR_ENV)
        return None

    provider = (os.getenv("EMBEDDING_PROVIDER") or "").strip().lower()
    if provider != "fastembed":
        if not _DETECTOR_ANNOUNCED:
            _DETECTOR_ANNOUNCED = True
            logger.warning(
                "Embed-window detector covers the fastembed engine; EMBEDDING_PROVIDER "
                "is %r, so chunks that exceed this embedder's window will not be "
                "reported.",
                provider or "unset",
            )
        return None

    try:
        from cognee.infrastructure.databases.vector.embeddings.FastembedEmbeddingEngine import (
            FastembedEmbeddingEngine,
        )
    except Exception:  # pragma: no cover - fastembed extra missing
        logger.exception("Embed-window detector could not import the fastembed engine")
        return None

    original = FastembedEmbeddingEngine.embed_text
    if getattr(original, "_citadel_window_detector", False):
        return FastembedEmbeddingEngine.__name__
    _DETECTOR_ANNOUNCED = True

    async def embed_text(self: Any, text: Any) -> Any:
        inputs = text if isinstance(text, list) else [text]
        window = resolve_model_window(self)
        counter = _untruncated_counter(self)
        if window is None or counter is None:
            record_unmeasurable_batch(len(inputs))
        else:
            record_embed_batch(inputs, count_tokens=counter, window=window)
        return await original(self, text)

    embed_text._citadel_window_detector = True  # type: ignore[attr-defined]
    embed_text.__wrapped__ = original  # type: ignore[attr-defined]
    FastembedEmbeddingEngine.embed_text = embed_text  # type: ignore[method-assign]
    logger.info("Embed-window detector installed on %s", FastembedEmbeddingEngine.__name__)
    return FastembedEmbeddingEngine.__name__


# ---------------------------------------------------------------------------
# The un-chunkable ingest verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnchunkableSpan:
    """A verdict about content, carrying no content.

    Deliberately has no field holding the offending text. The policy is refuse and
    record: a caller cannot accidentally log the word, and there is no return path
    that could hand back an edited document.
    """

    tokens: int
    budget: int
    char_length: int
    fingerprint: str
    # False when ``tokens`` is the arithmetic floor rather than a count, because
    # the word was too long to hand to the tokenizer on the ingest path. The field
    # exists so nothing downstream can quote a floor as a measurement.
    tokens_are_exact: bool = True

    def describe_tokens(self) -> str:
        """How the number may be spoken out loud."""
        if self.tokens_are_exact:
            return f"{self.tokens} BPE tokens"
        return f"at least {self.tokens} BPE tokens"


def _iter_cognee_words(text: str) -> Iterable[str]:
    """Yield segments exactly where cognee's ``chunk_by_word`` segments.

    Lazy on purpose. ``check_chunkable`` needs the first word that fails, not a
    list of every word: building the list for a 2,000,000-byte document allocated
    166,667 strings and peaked at 10.28 MB on this tree, all of it thrown away.
    """
    for match in _WORD_SPLIT.finditer(text):
        word = match.group(0)
        if word:
            yield word


def split_cognee_words(text: str) -> list[str]:
    """The whole segmentation, as a list.

    Joining the result reproduces the input. Any coarser rule (``str.split()``, for
    one, which breaks on newlines that cognee ignores) under-measures the longest
    word and lets past the documents this check exists to catch.
    """
    return list(_iter_cognee_words(text))


_BPE_ENCODING: Any | None = None
_BPE_ENCODING_FAILED = False


def _bpe_encoding() -> Any | None:
    """The same encoding cognee counts the budget in: tiktoken for gpt-4o."""
    global _BPE_ENCODING, _BPE_ENCODING_FAILED
    if _BPE_ENCODING is not None or _BPE_ENCODING_FAILED:
        return _BPE_ENCODING
    try:
        import tiktoken

        _BPE_ENCODING = tiktoken.encoding_for_model("gpt-4o")
    except Exception:
        _BPE_ENCODING_FAILED = True
        logger.exception(
            "Could not load the gpt-4o encoding; un-chunkable content will pass "
            "the ingest check unmeasured"
        )
    return _BPE_ENCODING


@lru_cache(maxsize=1)
def max_token_bytes() -> int | None:
    """The longest token in the encoding's vocabulary, in UTF-8 bytes.

    Read off the vocabulary rather than written down, because a tiktoken or cognee
    bump can move it. Cached because the scan over ~200,000 vocabulary entries costs
    19 ms and ``check_chunkable`` runs per ingest.

    Returns None when it cannot be read. None turns the shortcut in
    ``check_chunkable`` off and leaves the exact measurement in its place: the
    slower answer, never a different one.
    """
    encoding = _bpe_encoding()
    if encoding is None:
        return None
    try:
        longest = max(len(token) for token in encoding._mergeable_ranks)
    except Exception:  # pragma: no cover - tiktoken moved the vocabulary
        logger.exception(
            "Could not read the encoding's longest token; the un-chunkable check "
            "will measure every oversized word in full"
        )
        return None
    return longest if longest > 0 else None


def guard_enabled() -> bool:
    raw = os.getenv(GUARD_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def check_chunkable(text: str, *, budget: int | None = None) -> UnchunkableSpan | None:
    """Report the first word cognee cannot fit in the budget, or None.

    Such a word has two fates, both bad. If it is the trailing one,
    ``chunk_by_sentence`` raises ``ValueError`` (``chunk_by_sentence.py:94``) and
    ``run_tasks`` turns that single item into a whole-dataset
    ``PipelineRunFailedError`` (``run_tasks.py:153-158``), so one document stops
    every other document in the dataset from being indexed, on this pass and on
    every later one. Anywhere else it is emitted verbatim as an over-budget chunk
    and the embedder truncates it.

    Byte length brackets the answer from both sides, which is what keeps this
    affordable on an ``async`` ingest path:

    * a token covers **at least** one UTF-8 byte, so a word within ``budget``
      bytes is within ``budget`` tokens and is skipped untokenized;
    * a token covers **at most** ``max_token_bytes()`` UTF-8 bytes, and the token
      byte strings concatenate back to the word, so a word longer than
      ``budget * max_token_bytes()`` is over the budget however the merges fall.
      Tokenizing a 2,000,000-byte word to learn that costs 571 ms of the event
      loop, measured on this tree, and the answer is only ever read as "over".

    Between those two the word is measured exactly. Neither bracket decides
    differently from the tokenizer; they decide the same thing sooner.
    """
    limit = budget if budget is not None else resolve_chunk_budget()
    encoding = _bpe_encoding()
    if encoding is None:
        return None
    ceiling = max_token_bytes()

    for word in _iter_cognee_words(text):
        span_bytes = len(word.encode("utf-8", "replace"))
        if span_bytes <= limit:
            continue
        if ceiling is not None and span_bytes > limit * ceiling:
            return UnchunkableSpan(
                tokens=-(-span_bytes // ceiling),
                budget=limit,
                char_length=len(word),
                fingerprint=_fingerprint(word),
                tokens_are_exact=False,
            )
        tokens = len(encoding.encode(word, disallowed_special=()))
        if tokens > limit:
            return UnchunkableSpan(
                tokens=tokens,
                budget=limit,
                char_length=len(word),
                fingerprint=_fingerprint(word),
            )
    return None
