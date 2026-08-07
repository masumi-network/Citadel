# Publishing `citadel-archive` to PyPI

The CLI publishes to PyPI as **`citadel-archive`** (the installed command stays
`citadel`). Releases are automated via GitHub Actions + **PyPI Trusted
Publishing** (OIDC) — no API tokens are stored. Workflow:
[`.github/workflows/publish.yml`](.github/workflows/publish.yml).

## One-time setup (admin, ~2 min)

1. Sign in at <https://pypi.org>.
2. Go to **Account → Publishing → Add a pending publisher** and enter:
   - **PyPI Project Name:** `citadel-archive`
   - **Owner:** `masumi-network`
   - **Repository name:** `Citadel-Archive`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
3. (Recommended) In GitHub → **Settings → Environments**, create an environment
   named `pypi` and add required reviewers so a human approves each publish.

The first successful run creates the project; no token is ever needed.

## Cut a release

```bash
# 1. bump the version + update the changelog
#    edit kb/__init__.py  ->  __version__ = "0.4.1"
#    move [Unreleased] notes into a [0.4.1] section in CHANGELOG.md

# 2. commit + tag + push the tag
git add kb/__init__.py CHANGELOG.md
git commit -m "release: v0.4.1"
git tag v0.4.1
git push origin main --tags
```

Pushing the `v*` tag triggers the workflow: it builds the sdist + wheel, runs
`twine check`, then publishes to PyPI via OIDC. Watch it under the repo's
**Actions** tab.

## Verify

```bash
pipx install citadel-archive          # the lightweight client CLI
citadel --help
# extra:
pip  install "citadel-archive[server]" # + run the Node/MCP server
```

## Local dry-run (optional, before tagging)

```bash
uv build                      # -> dist/citadel_archive-*.whl + .tar.gz
uv pip install --system twine && python -m twine check dist/*
```

## Notes

- **Version is the source of truth in `kb/__init__.py`** (`__version__`; hatch
  reads it via `[tool.hatch.version]`). The tag must match
  (`v0.4.1` → `__version__ = "0.4.1"`). PyPI rejects re-uploading an existing
  version, so always bump before tagging.
- The base package has zero third-party dependencies (pure stdlib); the
  `[server]` extra pulls the heavy stack on demand. Keep that split when adding
  dependencies.
