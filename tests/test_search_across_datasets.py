from __future__ import annotations

import asyncio
from typing import Any

from kb.server import search_across_datasets


def test_exact_secondary_dataset_match_beats_unrelated_primary_result() -> None:
    class FakeCitadel:
        async def search(
            self,
            query: str,
            *,
            dataset: str,
            session_id: Any,
            top_k: int,
        ) -> list[dict[str, str]]:
            if dataset == "central":
                return [{"id": "central-1", "text": "unrelated operational note"}]
            return [{"id": "node-1", "text": "quokka-beacon-8823"}]

    merged = asyncio.run(
        search_across_datasets(
            FakeCitadel(),
            query="quokka-beacon-8823",
            datasets=["central", "node"],
            sessions={},
            top_k=2,
        )
    )

    assert [dataset for dataset, _result in merged] == ["node", "central"]
    assert merged[0][1]["id"] == "node-1"
