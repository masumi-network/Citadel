# Vault Backup Mirror

The **Vault Backup Mirror** is a private, text-first GitHub repository that holds
redundant copies of vault evidence from the live **Organization Vault** on Railway.
It is the NAS-style backup counterpart to operational storage—not a second search
index and not where agents query by default.

## Repositories

See also [public-and-private.md](public-and-private.md).

| Repository | Visibility | Role |
|---|---|---|
| [Citadel](https://github.com/masumi-network/Citadel) | Public | Application, MCP, skills, docs |
| [Vault-Backup-Mirror](https://github.com/masumi-network/Vault-Backup-Mirror) | Private | Durable mirror of vault evidence and history |

Live retrieval, embeddings, and the knowledge mesh stay on Railway (Postgres /
pgvector / Kuzu). The mirror is for recovery, audit, diffs, and rebuild inputs.

## What gets mirrored (Phase 1)

- Source snapshots and pointers (not full secret-bearing dumps)
- Repository daily updates
- Vault contributions (ingest summaries / normalized notes)
- Conflict resolutions
- Manifests with hashes, actors, timestamps, and source pointers

## What does not get mirrored

- Secrets, credentials, API tokens, or excluded material
- Embeddings, vector index files, or graph database files
- Large binaries by default (use object storage + manifest refs if needed later)

## Layout

```text
manifests/
  latest.json                 # pointer to last successful export
snapshots/
  YYYY-MM-DD/
    YYYYMMDD-HHMMSSZ/
      manifest.json
```

The current exporter is manifest-only. It tracks configured state files by path,
size, timestamp, and SHA-256 hash, but does not copy raw state file contents.
Future commits should stay small, reviewable, and diff-friendly (Markdown, JSON,
JSONL).

## Current status

Verified on 2026-06-02:

- Repository exists at
  [masumi-network/Vault-Backup-Mirror](https://github.com/masumi-network/Vault-Backup-Mirror).
- Visibility: private.
- Default branch: `main`.
- Initial scaffold commit: `deeb1c9`.
- Top-level scaffold: `.gitignore`, `README.md`, `manifests/`, `snapshots/`.
- Manifest-only export is implemented through `/api/backup-mirror`,
  `/api/backup-mirror/run`, and `scripts/run_backup_mirror.py`.
- Opt-in GitHub push is implemented through the GitHub Contents API. Push remains
  disabled by default and requires `CITADEL_BACKUP_MIRROR_PUSH_ENABLED=true`
  plus a dedicated mirror token.

Operational checkpoint on 2026-06-03:

- Local tests pass for the backup-mirror API and cron wrapper.
- Production commit `3c70e92` serves `/api/backup-mirror/run`.
- Hosted dry-run through `scripts/run_backup_mirror.py` returned `ok=true`,
  `dry_run=true`, `written=false`, `published=false`, and tracked 3 manifest
  entries.
- Non-dry-run writes and GitHub publishing remain disabled until
  `CITADEL_BACKUP_MIRROR_ENABLED=true`, `CITADEL_BACKUP_MIRROR_PUSH_ENABLED=true`,
  and a dedicated mirror token are configured.

## Configuration (Citadel)

Set on the Railway web service when mirror export is enabled:

```bash
CITADEL_BACKUP_MIRROR_REPO=masumi-network/Vault-Backup-Mirror
CITADEL_BACKUP_MIRROR_ENABLED=false   # true for non-dry-run manifest writes
CITADEL_BACKUP_MIRROR_PUSH_ENABLED=false
CITADEL_BACKUP_MIRROR_BRANCH=main
CITADEL_BACKUP_MIRROR_ROOT_PATH=/data/.citadel/backup_mirror
CITADEL_BACKUP_MIRROR_TOKEN=github_pat_...   # only when push is enabled
```

The GitHub token used for pushes must have `contents: write` on the private mirror
repo only. Use a dedicated fine-grained or machine token—never commit it.

Dry-run the cron wrapper before enabling writes:

```bash
CITADEL_RUN_MODE=backup-mirror
CITADEL_BACKUP_MIRROR_TARGET_URL=https://citadel.utxo.ag
CITADEL_BACKUP_MIRROR_ACCESS_KEY=ctdl_...
CITADEL_BACKUP_MIRROR_DRY_RUN=true
uv run python scripts/run_railway.py
```

When `CITADEL_BACKUP_MIRROR_PUSH_ENABLED=true`, Citadel pushes only
`manifests/latest.json` and the dated `snapshots/.../manifest.json` through the
GitHub Contents API. Raw state files, access tokens, embeddings, vector indexes,
and graph database files are not uploaded.

## Operational notes

- Target size: ~1 GB practical ceiling; review at ~5 GB ([ADR 0001](adr/0001-github-vault-backup-mirror.md)).
- If `snapshots/` has no dated export folders, that is expected until the first
  export run.
- Agents connect via MCP to Railway; admins may browse the mirror in GitHub for audit.

## Related

- [Organization Vault plan](organization-vault-plan.md) §3.3
- [Architecture deepening §3](architecture-deepening-opportunities.md)
