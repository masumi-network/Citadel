# Use GitHub As The Phase 1 Vault Backup Mirror

- Superseded by: [ADR-0022](0022-evidence-is-retained-and-attested-at-capture.md). The manifest-only mirror is replaced by retained attested evidence.

Citadel uses a private GitHub repository as the Phase 1 **Vault Backup Mirror** for text-heavy, diffable vault history: source snapshots, repository daily updates, vault contributions, conflict resolutions, and manifests.

**Canonical mirror repository (private):**  
https://github.com/masumi-network/Vault-Backup-Mirror

**Application repository (public):**  
https://github.com/masumi-network/Citadel

GitHub gives low-cost private storage, readable diffs, commit history, and familiar access controls, but it is not the live retrieval store and should not be used as a 1 TB blob backup target; large bodies can move to object storage later while GitHub keeps traceable manifests and metadata.

See [`docs/vault-backup-mirror.md`](../vault-backup-mirror.md) for layout and sync policy.

**Consequences**

- The live server remains responsible for fast search, indexing, and Knowledge Mesh retrieval.
- The mirror should avoid secrets, credentials, embeddings, vector index files, graph database files, and large binaries by default.
- The mirror should stay text-first, with 1 GB as the practical target ceiling and 5 GB as the review point.
