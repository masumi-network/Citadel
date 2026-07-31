# Citadel benchmark harness

Repeatable measurements for the Citadel node. Every number here is meant to be
re-runnable by someone who does not trust it, which is the bar for anything that
ends up on a public page.

Read-only. Nothing in this directory writes to the vault.

## Running it

```bash
export CITADEL_MCP_ACCESS_TOKEN=...          # a seat token with kb:search
export GITHUB_TOKEN=...                      # only for refreshing ground truth

python scripts/bench/fetch_ground_truth.py   # once, or when the corpus changes
python scripts/bench/search_bench.py --repeats 3 --top-k 5 --out results.json
```

## What it measures

| metric | meaning |
|---|---|
| `recall_at_k` | fraction of questions where the document that answers it appears in the top k |
| `mrr` | mean reciprocal rank over the same questions |
| `blocked_probe_hit_rate` | fraction of *deliberately unanswerable* questions that returned their target anyway |
| `latency_ms_p50` / `p95` | **client round-trip**, timed around `urlopen`, so it includes DNS, TCP, TLS and the network path from wherever you ran it. It is NOT server-side latency: on 2026-07-31 a run measuring 269ms from a laptop corresponded to 107-141ms in the Railway edge logs. Quote it as "round-trip from our client", and read server-side timing from the platform logs instead. |
| `distinct_source_ratio_mean` | distinct source documents per result page, divided by ALL hits. 1.00 means every slot is a different document; lower means one document is occupying slots the caller pays context for. A hit whose source path cannot be resolved counts against this exactly like a duplicate does. |
| `distinct_source_ratio_resolvable_only` | the same ratio over hits whose source path resolved. Report both: the first is the pessimistic bound, this is what the resolvable evidence supports. |

## How a hit is scored

Ground truth is the source document that should answer the question. A returned
chunk counts as that document if either:

1. its first line is the repo-content header `# <owner>/<repo>/<path>`, or
2. it shares at least `MIN_SHARED_SHINGLES` distinct 12-word runs with the
   cached copy of the file.

Rule 2 exists because only the first chunk of a document carries its source
path. Without it, every later chunk scores as a miss.

`MIN_SHARED_SHINGLES` is 3, not 1. During calibration, a threshold of 1 matched
`sokosumi-cli/AGENTS.md` against `sokosumi/AGENTS.md` on a single shared line of
template boilerplate, and scored a false positive. Three independent shared runs
did not. If you add near-duplicate documents to the golden set, re-check this.

## The blocked probes

Question ids beginning with `b` target documents the ingest-time security
scanner rejects, so their answers are not in the vault. They are expected to
miss, and `blocked_probe_hit_rate` should be 0.

They are not filler. They turn "the scanner drops 13% of repo content" from a
sync-log statistic into a measured retrieval loss, and they are the regression
test for any change to the scanner: if a blocked probe starts hitting, either
the block was lifted or the content arrived through a path that skips the gate.

## Reading the results honestly

- `dataset` on a hit is not trustworthy **when you pass one explicitly**.
  Requesting `dataset=masumi-network` and `dataset=seat:sarthi` returns
  byte-identical hits (same `content_sha256`) with the label rewritten to match
  the request. On a default search the per-hit label does track the
  `sections` routing, so it is not always an echo. Never derive isolation
  claims from this field.
- `mode="docs"` returned zero results for a question whose answer is in an
  ingested `.md` file. Treat that filter as unverified until it is fixed.
- `_citadel.provenance` is `{}` on every hit observed so far, and
  `document_name` is `text_<md5>`. That is why scoring falls back to the body.
- Latency here excludes CLI process startup. `citadel search` adds roughly 0.4 s
  of Python import and connection setup on top.
