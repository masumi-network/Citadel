# Technology Stack

Last updated: 2026-08-08
Owner: architect

## Current production

| Component | Version or provider | Status | Evidence |
| --- | --- | --- | --- |
| Python | `>=3.11` | Completed | VERIFIED: `pyproject.toml` declares the runtime floor. |
| Citadel API | FastAPI and Uvicorn | Completed | VERIFIED: `requirements.txt` and `scripts/run_railway.py`. |
| Memory engine | Cognee `>=1.2.2,<1.3.0` | Completed | VERIFIED: `requirements.txt:6` and `pyproject.toml:41`. Runtime compatibility work depends on private Cognee APIs. |
| Relational and vector store | PostgreSQL plus PGVector | Completed | VERIFIED: `.env.example:284-299` and `docs/operations.md:37-43`. |
| Graph store | Cognee `kuzu` setting, resolved to Ladybug in the installed package | Completed | VERIFIED: `.env.example:300` and the pinned Cognee package source. |
| Hosted deployment | Railway with `railway.toml` | Completed | VERIFIED: `railway.toml` and `docs/operations.md`. |

## Qdrant candidate generation

| Component | Candidate rule | Status | Evidence |
| --- | --- | --- | --- |
| Memory engine | Exact Cognee `1.4.1` with `cryptography==50.0.0` | In Progress | VERIFIED: upstream metadata caps cryptography below 50, so a reviewed metadata patch is required before the candidate is installable. The disposable forced runtime passed the private API contract. |
| Adapter | Audited Citadel patch based on official community adapter `0.3.0` from PR `#149` commit `7311f4572b3ec328f3c2fe5ba3d49a6a79d6ae29` | In Progress | VERIFIED: the official adapter loses CHUNKS fields, leaves raw-ID retrieve and delete unscoped, allows same-ID cross-dataset overwrite, and converts query failures to empty results. The official commit is a source baseline, not a release-ready dependency. |
| Qdrant server and client | Candidate spike pins server `1.19.0` and client `1.19.0`; image digest recorded before release | Planned | VERIFIED: PyPI reported client `1.19.0`, and the official source exposes server tag `v1.19.0`. The disposable auth probe passed on server `1.18.1` and client `1.18.0`; it does not verify 1.19.0. Official guidance expects server and SDK minor versions to match. |
| Qdrant storage | Persistent `/qdrant/storage`, API key, private network, snapshot restore | Planned | VERIFIED: official Qdrant deployment, security, and monitoring guidance. |
| Relational store | SQLite Lite for v0.5; PostgreSQL later optional | Planned | VERIFIED: Cognee `1.4.1` supports both. REPORTED: the user selected SQLite for the low-cost Railway release. |
| Graph | Independent volume or provider namespace | Planned | INFERRED: graph and relational state belong to the same whole-generation acceptance boundary. |

## Candidate packaging constraints

- VERIFIED: plain pip cannot resolve Cognee `1.4.1` with Citadel's `cryptography==50.0.0` floor from current upstream metadata. A uv-only override does not make Railway, local Docker, or `pip install citadel-archive` reproducible.
- VERIFIED: the selected adapter metadata declares Python `>=3.11,<=3.13`. That specifier excludes Python versions above `3.13.0`, including later `3.13.x` releases under standard version ordering.
- INFERRED: pin the first portable release image to Python `3.12` while the upstream Python constraint and Cognee cryptography metadata are patched and independently install-tested.
- INFERRED: release packaging must use a reviewable exact fork commit or a published upstream fix. A local wheel and a resolver override are test inputs only.
- INFERRED: every release target sets `TELEMETRY_DISABLED=true` and leaves external payload tracing unconfigured. Cognee telemetry and trace payloads are outside v0.5 acceptance scope.

## Portable release targets

- INFERRED: one app image and one smoke script should be shared by local Compose, Railway, and DigitalOcean Droplets.
- INFERRED: Railway v0.5 uses app plus Qdrant with SQLite on the app volume, generated secrets, and private networking. PostgreSQL remains a later optional profile.
- INFERRED: local development uses Compose with a real Qdrant server. Qdrant local mode is reserved for unit tests because its data format is not server-compatible and official guidance rejects it for benchmarking or production.
- VERIFIED: DigitalOcean App Platform cannot host this stateful shape because it has no persistent volumes. The supported DigitalOcean target is a Droplet running the Compose bundle.
