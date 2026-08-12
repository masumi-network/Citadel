"""In-image proof that the baked embedding engine works offline.

Gated behind CITADEL_EMBEDDING_BAKE_SMOKE=1 and run by CI inside the built
image with --network none, so both regressions shipped on 2026-08-12 fail
in CI instead of production: the silent TikToken chunk-sizing fallback and
the offline pin that blocked fastembed's ONNX weight download.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("CITADEL_EMBEDDING_BAKE_SMOKE") != "1",
    reason="only meaningful inside the baked Docker image",
)


def test_baked_engine_embeds_offline_with_model_tokenizer() -> None:
    from cognee.infrastructure.databases.vector.embeddings.FastembedEmbeddingEngine import (
        FastembedEmbeddingEngine,
    )
    from cognee.infrastructure.llm.tokenizer.HuggingFace.adapter import HuggingFaceTokenizer

    model = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    dimensions = int(os.environ.get("EMBEDDING_DIMENSIONS", "384"))

    # Raises "Could not load model ... from any source" when the ONNX weights
    # are not loadable from the image: the offline-pin regression.
    engine = FastembedEmbeddingEngine(model=model, dimensions=dimensions)

    # The resolver never raises; a missing baked tokenizer silently degrades
    # to TikToken: the chunk-sizing regression. The isinstance is the only
    # observable signal.
    assert isinstance(engine.tokenizer, HuggingFaceTokenizer), type(engine.tokenizer).__name__
    assert engine.tokenizer.count_tokens("citadel embedding smoke") > 0

    [vector] = list(engine.embedding_model.embed(["citadel embedding smoke"]))
    assert len(vector) == dimensions
