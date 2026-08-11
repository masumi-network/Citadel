# Publishing `citadel-archive`

A `v*` tag starts one release workflow for the Python package and production
OCI image. The workflow stages the image by source SHA, smokes both platform
digests, records supply-chain evidence, publishes PyPI, copies the same OCI
index to the exact version, then creates the GitHub Release.

Workflow: [`.github/workflows/publish.yml`](.github/workflows/publish.yml).

## Published artifacts

- PyPI package: `citadel-archive==0.5.0`.
- OCI index: `ghcr.io/masumi-network/citadel:0.5.0` for `linux/amd64` and
  `linux/arm64`.
- Internal staging tag: `ghcr.io/masumi-network/citadel:sha-<source-sha>`.
- GitHub Release assets: wheel, sdist, and `citadel-image-receipt.txt`.

Only the exact OCI version is a public release tag. The workflow does not
publish `latest`, major, or minor aliases. Operators should deploy the
version-plus-digest reference from the release receipt:

```text
ghcr.io/masumi-network/citadel:0.5.0@sha256:<index-digest>
```

The image build emits BuildKit maximum-mode provenance and an SBOM. After both
platform digest smokes pass, GitHub creates a keyless OIDC provenance
attestation for the OCI index digest. v0.5 does not use a separate Cosign
signature.

## One-time setup

### PyPI trusted publisher

1. Sign in at <https://pypi.org>.
2. Go to **Account > Publishing > Add a pending publisher** and enter:
   - **PyPI Project Name:** `citadel-archive`
   - **Owner:** `masumi-network`
   - **Repository name:** `Citadel`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
3. In GitHub **Settings > Environments**, create `pypi` and add required
   reviewers.

The workflow uses PyPI Trusted Publishing. It needs no PyPI API token.

### GitHub container registry

The workflow uses `GITHUB_TOKEN` with job-scoped package permissions. The
production image carries the repository source label before its first push, so
GHCR links it to the public `masumi-network/Citadel` repository and inherits
that repository's visibility. Confirm the organization permits public package
creation and artifact attestations before tagging. No registry password or
signing key is required.

## Cut v0.5.0

Before tagging, require the integrated CI gate, Docker release gates, and both
independent release reviews to pass for the exact commit.

```bash
python -c 'from kb import __version__; assert __version__ == "0.5.0"'

git tag v0.5.0
git push origin v0.5.0
```

The workflow rejects tags outside `v<major>.<minor>.<patch>`, package-version
mismatches, tagged commits outside `main`, and commits without a successful
`CI gate` check. It refuses to overwrite an existing OCI version tag.

The irreversible order is:

1. Push the `sha-$GITHUB_SHA` OCI staging index with provenance and SBOM.
2. Resolve and smoke the `linux/amd64` and `linux/arm64` child digests.
3. Attest the staged index through GitHub OIDC.
4. Publish PyPI, copy the same digest to `0.5.0`, create GitHub Release.

If PyPI succeeds and OCI promotion fails before the exact tag exists, use
GitHub's **Re-run failed jobs** action for that workflow run. The successful
PyPI job does not rerun. If an exact OCI version exists with another digest,
or a GitHub Release has already shipped, never move it. Cut a patch release.

## Verify the release

Download `citadel-image-receipt.txt` from the GitHub Release. Confirm it names
the reviewed source SHA and the same index digest reported by GHCR.

```bash
pipx install citadel-archive==0.5.0
citadel --help

docker buildx imagetools inspect \
  ghcr.io/masumi-network/citadel:0.5.0@sha256:<index-digest>

docker pull \
  ghcr.io/masumi-network/citadel:0.5.0@sha256:<index-digest>

gh attestation verify \
  oci://ghcr.io/masumi-network/citadel@sha256:<index-digest> \
  --repo masumi-network/Citadel
```

The unauthenticated `docker pull` is the public-visibility check.

For local package checks before tagging:

```bash
uv build
uv pip install --system twine
python -m twine check dist/*
```

`kb/__init__.py` is the version source. Hatch reads it through
`[tool.hatch.version]`. PyPI rejects an already published version.
