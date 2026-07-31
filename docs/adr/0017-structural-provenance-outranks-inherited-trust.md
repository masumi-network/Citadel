# Structural Provenance Outranks Inherited Trust In Classification

- Status: Accepted
- Date: 2026-07-31
- Extends: [ADR-0012](0012-attested-trust-vs-content-hint.md), [ADR-0011](0011-shared-session-traces.md).

When a hit carries the structural header a governed sync writes — repository,
source URL, commit and blob — that provenance determines its `doc_type`, even if
the hit also inherited `reference-only` trust from a **Shared Session Trace**
match. Trust still governs what a reader may rely on; it no longer decides what
a document *is*.

ADR-0012 established that trust is attested from the dataset a hit was read out
of, never from body text, so that a trace mentioning `/skills/` cannot dress
itself as a skill document. Classification then took a shortcut: anything
carrying `reference-only` was labelled a trace.

That shortcut collapsed once the shared-trace marker was assigned by matching
chunk *text* against the session-traces dataset. Any document a trace quoted
verbatim inherited `reference-only` and was reclassified. Measured on
2026-07-31: **100 of 100 sampled hits** came back `session-trace` — READMEs,
`SKILL.md` files, design specs, Linear issues, everything. `session-trace` is an
ambient type, so `mode="docs"` filtered every result away and returned nothing
for every query, including ones whose answer was an ingested `.md` file sitting
in the vault.

A text collision is evidence that two documents share words. It is not evidence
about where either came from. The `Repository`/`Source`/`Commit`/`Blob` header is
written by the repo-content syncer, is structural rather than keyword-based, and
cannot be produced by a digest quoting an issue title — so it is the stronger
signal about origin, and it wins.

**Considered Options**

- **Attribute by dataset instead of by text:** the correct fix, and it is what
  the shared-trace marker is really reaching for. Blocked today because hits
  expose no reliable per-hit dataset attribution on the read path; that is
  tracked separately. Text is currently the only signal available.
- **Drop the shared-trace marker entirely:** would restore classification and
  reintroduce exactly what ADR-0011 added it for — a volunteered trace,
  dual-written to the author's **Node**, coming back as ordinary knowledge.
- **Let `exclude_ambient` consult only `doc_type`, leave classification alone:**
  necessary but provably insufficient. Verified: with classification unchanged,
  every hit is still `session-trace`, still ambient, still filtered.
- **Narrow the exception to structural provenance (chosen):** keeps ADR-0012's
  guarantee where it matters — body *prose* still cannot mint trust — while
  letting a governed sync's own header identify its output.

**Consequences**

- Only a full structural header qualifies. A body that merely mentions a repo
  name, or contains a `Source:` line, does not.
- The guard ADR-0012 installed still holds, with a test: a session trace
  mentioning `/skills/` has no such header and stays a trace at
  `reference-only`.
- `exclude_ambient` no longer consults `trust_tier`, for the reason
  `canonical_only` stopped consulting it — `reference-only` is the only tier the
  server can attest, so requiring anything else was unsatisfiable and removed
  every hit.
- Measured effect on a 20-question golden set, same corpus in both runs:
  Linear slots in returned context 24/100 → 0/76, repository documents kept
  63 → 63, recall@5 unchanged at 0.75, MRR 0.725 → 0.75.
- Classification is load-bearing again, so its failures are now visible as
  wrong `doc_type` rather than as a silently empty result set.
