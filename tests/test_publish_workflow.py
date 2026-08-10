"""Static release-workflow contract tests."""

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(workflow: str, name: str, next_name: str | None = None) -> str:
    start = workflow.split(f"  {name}:\n", 1)[1]
    if next_name is None:
        return start
    return start.split(f"\n  {next_name}:\n", 1)[0]


def test_release_trigger_guard_and_python_build_gate() -> None:
    workflow = _workflow()
    guard = _job(workflow, "release-guard", "build")
    build = _job(workflow, "build", "stage-image")

    assert 'tags:\n      - "v*"' in workflow
    assert "cancel-in-progress: false" in workflow
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" in guard
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" "origin/main"' in guard
    assert "commits/${GITHUB_SHA}/check-runs" in guard
    assert 'select(.name == "CI gate" and .conclusion == "success")' in guard
    assert "needs: release-guard" in build
    assert 'test "${RELEASE_TAG#v}" =' in build


def test_image_is_staged_once_for_both_platforms_with_buildkit_evidence() -> None:
    workflow = _workflow()
    stage = _job(workflow, "stage-image", "smoke-image")

    assert "IMAGE_NAME: ghcr.io/masumi-network/citadel" in workflow
    assert "target: production" in stage
    assert "platforms: linux/amd64,linux/arm64" in stage
    assert "push: true" in stage
    assert "tags: ${{ env.IMAGE_NAME }}:sha-${{ github.sha }}" in stage
    assert "provenance: mode=max" in stage
    assert "sbom: true" in stage


def test_each_platform_digest_is_resolved_and_smoked() -> None:
    workflow = _workflow()
    smoke = _job(workflow, "smoke-image", "attest-image")

    assert "platform: linux/amd64" in smoke
    assert "architecture: amd64" in smoke
    assert "platform: linux/arm64" in smoke
    assert "architecture: arm64" in smoke
    assert '"${IMAGE_NAME}@${INDEX_DIGEST}" --raw' in smoke
    assert ".platform.architecture == $architecture" in smoke
    assert 'docker pull --platform "$PLATFORM" "${IMAGE_NAME}@${PLATFORM_DIGEST}"' in smoke
    assert '"${IMAGE_NAME}@${PLATFORM_DIGEST}"' in smoke
    assert "import cognee, kb; assert kb.__version__ ==" in smoke


def test_keyless_attestation_and_pypi_fail_closed_on_oci_gates() -> None:
    workflow = _workflow()
    attestation = _job(workflow, "attest-image", "publish-pypi")
    pypi = _job(workflow, "publish-pypi", "promote-image")

    assert "needs: [stage-image, smoke-image]" in attestation
    assert "id-token: write" in attestation
    assert "attestations: write" in attestation
    assert "artifact-metadata: write" in attestation
    assert "uses: actions/attest@v4" in attestation
    assert "subject-digest: ${{ needs.stage-image.outputs.digest }}" in attestation
    assert "push-to-registry: true" in attestation
    assert "needs: [build, attest-image]" in pypi
    assert "environment: pypi" in pypi
    assert "uses: pypa/gh-action-pypi-publish@release/v1" in pypi
    assert "cosign" not in workflow.lower()


def test_promotion_and_release_use_only_the_exact_immutable_version() -> None:
    workflow = _workflow()
    promotion = _job(workflow, "promote-image", "github-release")
    release = _job(workflow, "github-release")

    assert "needs: [stage-image, attest-image, publish-pypi]" in promotion
    assert 'VERSION="${VERSION#v}"' in promotion
    assert 'imagetools inspect "${IMAGE_NAME}:${VERSION}"' in promotion
    assert '--tag "${IMAGE_NAME}:${VERSION}"' in promotion
    assert '"${IMAGE_NAME}@${INDEX_DIGEST}"' in promotion
    assert '--metadata-file "$metadata"' in promotion
    assert 'test "$promoted_digest" = "$INDEX_DIGEST"' in promotion
    assert "needs: [build, stage-image, promote-image]" in release
    assert "citadel-image-receipt.txt" in release
    assert '"$IMAGE_NAME" "$VERSION" "$INDEX_DIGEST" "$GITHUB_SHA"' in release
    assert 'gh release create "${GITHUB_REF_NAME}"' in release
    assert ":latest" not in workflow.lower()
    assert "${VERSION%.*}" not in workflow
