# ADR-0012: `trust_tier` reports attestation; `content_hint` reports shape

- Status: Accepted
- Date: 2026-07-23
- Supersedes: nothing. Extends ADR-0009 (mesh read isolation) and ADR-0011 (shared session traces).

## Context

Search hits carried a `trust_tier` (`canonical` / `verified` / `derived` /
`ambient` / `reference-only`) alongside `doc_type`. Both were produced by
`infer_doc_type()`, which greps a concatenation of the hit's own fields —
including `text`, `content` and `summary`, i.e. the ingested body.

Ingested text is written by whoever contributed it, so the tier was
author-controlled:

```
{"title": "Random chat note", "text": "someone pasted /skills/ in a message"}
    -> doc_type=skill,          trust_tier=verified
{"title": "note", "text": "see docs.masumi.network for details"}
    -> doc_type=canonical-docs, trust_tier=canonical
```

Both then satisfied `canonical_only`, the filter whose purpose is excluding
unvetted material. Meanwhile `AGENTS.md`, the cursor/windsurf rules, the
SessionStart hook and `docs/mcp/README.md` all instructed agents to *prefer*
`trust_tier: canonical|verified` for API/spec claims. The system asked agents to
trust a field that anyone able to write into the vault could set.

The cheapest write path is not even authenticated. A GitHub issue title on a
public repo is copied verbatim into the org digest
(`kb/repository_update.py`), and that digest is ingested into Central
(`kb/github_sync.py`). An issue titled `MIP-003 endpoint schema` flipped the
entire digest from `activity`/`ambient` to `spec`/`canonical`.

### Why the obvious fix does not work

The natural repair is to classify from server-attested locators (path, url,
provenance) and ignore the body. Measured against the live node, that fails:
**0 of 6 real hits carried a `path`, `url`, `_citadel.provenance` or
`citadel_tags`**, and `document_name` is only `text_<md5>`.
`Citadel.ingest(data, dataset, tags, session_id)` accepts no locator, and the
sync formatters put the source *inside the text* rather than in a field. A
locator-only classifier therefore labels everything `other`/`derived` and
`canonical_only` returns nothing at all.

The one remaining signal, `dataset`, is weak too: on the hosted node the same
query issued with three different `dataset` values returned byte-identical
documents, each labelled with whatever was requested (see Consequences).

## Decision

Split the two questions that were conflated into one field.

**`trust_tier` answers "what did the server attest?"** and may never be derived
from content. Today exactly one value can be attested — `reference-only`, from
the dataset a hit was read out of — so the tier is `reference-only` or
`unattested`. A tier stored by an older build is recomputed rather than echoed
back, so a persisted `canonical` cannot re-enter the system.

**`content_hint` answers "what does this text look like?"** (`looks-like-spec`,
`looks-like-skill`, …, `unclassified`). It is body-derived and therefore
steerable by whoever wrote the text. That is acceptable *because nothing may act
on it as authority*: it orders results and labels them for a reader.

Consequences for the filters:

- `canonical_only` selects on shape alone and no longer consults any tier. It is
  a relevance filter, and is documented as one everywhere it is exposed.
- `exclude_ambient` drops activity/issue/trace shapes **and** anything attested
  `reference-only`.
- Activity/issue classification runs *before* spec/skill, so ambient material
  cannot relabel itself past `exclude_ambient`.
- The dataset name left the content haystack entirely. A team seat innocently
  named `devhub` or `mip-003` was relabelling every personal note in it, and
  `repo="masumi-network"` matched every hit in Central because Central is named
  after the org.

## Consequences

Agents lose a signal that was never real. Any workflow that said "prefer
canonical" now says "start here, then verify" — `citadel verify` /
`prepare-pr-context` renamed `canonical_sources` to `doc_shaped_sources`, select
on shape, and no longer fall back to every hit when nothing qualifies.

Ranking is unaffected: it keeps using the body-derived shape, so recall and
ordering are unchanged. Fixing the classification order additionally made a real
spec outrank a higher-scoring digest on spec queries.

**This does not make forgery impossible — it makes it non-authoritative.** A
crafted body can still shape `content_hint` and still enter
`doc_shaped_sources`. What it can no longer do is claim the vault vouched for
it. That is the honest position while the vault stores no provenance.

**The attestation is weaker than it looks.** `reference-only` derives from the
dataset label, and that label is an echo of the caller's request rather than
proven provenance. Two things limit the damage: the shared-trace marker
propagates `reference-only` across dedup by content key rather than by label,
and everything else is already `unattested`. Whether the dataset parameter
failing to scope has consequences beyond labelling is **not** established here
and is tracked separately.

## Follow-up

Provenance at ingest is the change that would let a hit earn a tier above
`unattested`: `Citadel.ingest` would take `path` / `source_url` / `source`, the
sync writers would populate them, and `trust_tier` could then be derived from a
record the server wrote. Until then, `canonical` and `verified` remain defined
constants that nothing assigns.
