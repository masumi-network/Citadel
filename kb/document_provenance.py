"""Server-written provenance attached to durable Cognee documents."""

from __future__ import annotations

import json
from typing import Any

PROVENANCE_KEY = "citadel_provenance"
PROVENANCE_KIND_PROMOTION = "promotion"
TRUST_REFERENCE = "reference-only"
TRUST_UNATTESTED = "unattested"
_TRUST_TIERS = frozenset({TRUST_REFERENCE, TRUST_UNATTESTED})


def promotion_provenance(
    *,
    promoted_by: str,
    promoted_at: str,
    source_dataset: str,
) -> dict[str, Any]:
    """Build the metadata written after a promotion path accepts a document."""
    return {
        "kind": PROVENANCE_KIND_PROMOTION,
        "trust_tier": TRUST_UNATTESTED,
        "promoted_by": promoted_by,
        "promoted_at": promoted_at,
        "source_dataset": source_dataset,
    }


def parse_document_provenance(external_metadata: Any) -> dict[str, str]:
    """Return only validated server provenance from Cognee external metadata."""
    if isinstance(external_metadata, str):
        try:
            external_metadata = json.loads(external_metadata)
        except (TypeError, ValueError):
            return {}
    if not isinstance(external_metadata, dict):
        return {}
    raw = external_metadata.get(PROVENANCE_KEY)
    if not isinstance(raw, dict):
        return {}
    if raw.get("kind") != PROVENANCE_KIND_PROMOTION:
        return {}

    promoted_by = raw.get("promoted_by")
    promoted_at = raw.get("promoted_at")
    if not isinstance(promoted_by, str) or not promoted_by.strip():
        return {}
    if not isinstance(promoted_at, str) or not promoted_at.strip():
        return {}

    result = {
        "kind": PROVENANCE_KIND_PROMOTION,
        "promoted_by": promoted_by.strip()[:200],
        "promoted_at": promoted_at.strip()[:80],
    }
    trust_tier = raw.get("trust_tier")
    if trust_tier in _TRUST_TIERS:
        result["trust_tier"] = trust_tier
    source_dataset = raw.get("source_dataset")
    if isinstance(source_dataset, str) and source_dataset.strip():
        result["source_dataset"] = source_dataset.strip()[:200]
    return result
