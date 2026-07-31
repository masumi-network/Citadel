# Ingested Documents Are Byte-Stable For An Unchanged Source

- Status: Accepted
- Date: 2026-07-31
- Supersedes: nothing. Precondition for [ADR-0010](0010-structured-knowledge-durable-source-of-truth.md).

A document rendered from a source that has not changed must be byte-identical to
the last time it was rendered. Rendering must depend only on the source's
content and version, never on when the render happened.

`format_repo_content_document` stamped `Retrieved: {checked_at}` into the body of
every **Source Snapshot** it produced. An unchanged file therefore produced
different text on every sync. Content-hash matching at ingest could not
recognise it as the same document, and the text-keyed dedup on the read path
could not collapse the copies, so each sync added another. On 2026-07-31
`masumi-network/sokosumi/README.md` existed as 29 distinct documents, one query
returned it in 8 of 8 result slots under 8 document ids, and across a 20-question
golden set the worst-case single-source share of the top five was mean 0.49,
max 1.00 — half the average result set was repeats of one file.

`Commit` and `Blob` already pin the exact version the text came from and stay
stable while the file is unchanged, which is the property that makes a document
deduplicable at all. When the file changes they change with it, so removing the
timestamp loses nothing that identifies the snapshot.

**Considered Options**

- **Keep the timestamp, dedup on the read path instead:** collapse same-source
  hits at query time and leave storage alone. Rejected — it treats the symptom
  for one consumer while the vault keeps growing copies, and every other reader
  (mesh, drill-down, promotion, backup) still sees them.
- **Keep the timestamp, exclude it from the content hash:** narrower, but the
  body is what gets chunked and embedded, so the copies remain textually
  distinct to the vector store and to any future contradiction check. The hash
  is not the only thing that has to agree.
- **Move retrieval time into document metadata rather than deleting it:** the
  honest place for it, and worth doing when a **Source Snapshot** carries
  structured provenance. Deferred rather than rejected: hits currently expose an
  empty `provenance`, so there is nowhere to put it that survives retrieval.
- **Drop the timestamp (chosen):** the version is already pinned twice over.

**Consequences**

- Any renderer of vault documents is subject to this rule, not just repo
  content: no wall-clock, no run id, no counter in the body.
- Re-ingesting an unchanged source is now detectable as a no-op, which is the
  precondition for revising a page in place instead of filing a new
  contribution.
- It does not remove copies already stored. Those predate this decision and
  cannot be deleted safely while [ADR-0001](0001-github-vault-backup-mirror.md)
  is unimplemented and prior versions are unrecoverable.
- A test asserts two renders of an unchanged file are byte-identical and that no
  `Retrieved:` line returns; that test is the guard against this class of
  regression rather than against the one string.
