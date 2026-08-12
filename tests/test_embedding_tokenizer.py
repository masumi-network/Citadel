"""The embedding tokenizer must match the embedding model, not a fallback.

Production logs on 2026-08-12 showed cognee's resolver falling back to
approximate TikToken counts because ``transformers`` was not installed, so
every chunk boundary was mis-sized against the BGE embedding model.
"""

from cognee.infrastructure.llm.tokenizer.HuggingFace.adapter import HuggingFaceTokenizer
from cognee.infrastructure.llm.tokenizer.resolver import resolve_embedding_tokenizer


def test_fastembed_bge_resolves_to_its_real_tokenizer() -> None:
    tokenizer = resolve_embedding_tokenizer(provider="fastembed", model="BAAI/bge-small-en-v1.5")
    assert isinstance(tokenizer, HuggingFaceTokenizer)
