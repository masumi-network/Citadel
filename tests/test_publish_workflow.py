"""Static release-workflow contract tests."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"

_IMAGE_MEDIA_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)

_AMD64_IMAGE = "sha256:" + ("a" * 64)
_ARM64_IMAGE = "sha256:" + ("b" * 64)
_AMD64_ATTESTATION = "sha256:" + ("c" * 64)
_ARM64_ATTESTATION = "sha256:" + ("d" * 64)

# Same shape as a BuildKit provenance:mode=max index: each linux/<arch>
# image digest is repeated on the matching attestation-manifest.
_PROVENANCE_INDEX = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": _AMD64_IMAGE,
            "platform": {"os": "linux", "architecture": "amd64"},
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": _ARM64_IMAGE,
            "platform": {"os": "linux", "architecture": "arm64"},
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": _AMD64_ATTESTATION,
            "annotations": {
                "vnd.docker.reference.type": "attestation-manifest",
                "vnd.docker.reference.digest": _AMD64_IMAGE,
            },
            "platform": {"os": "unknown", "architecture": "unknown"},
        },
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": _ARM64_ATTESTATION,
            "annotations": {
                "vnd.docker.reference.type": "attestation-manifest",
                "vnd.docker.reference.digest": _ARM64_IMAGE,
            },
            "platform": {"os": "unknown", "architecture": "unknown"},
        },
    ],
}


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(workflow: str, name: str, next_name: str | None = None) -> str:
    start = workflow.split(f"  {name}:\n", 1)[1]
    if next_name is None:
        return start
    return start.split(f"\n  {next_name}:\n", 1)[0]


def _linux_image_digests(index: dict[str, Any], architecture: str) -> list[str]:
    found: list[str] = []
    for manifest in index["manifests"]:
        platform = manifest.get("platform") or {}
        annotations = manifest.get("annotations") or {}
        if (
            platform.get("os") == "linux"
            and platform.get("architecture") == architecture
            and manifest.get("mediaType") in _IMAGE_MEDIA_TYPES
            and annotations.get("vnd.docker.reference.type") != "attestation-manifest"
        ):
            found.append(manifest["digest"])
    return found


def _smoke_digest_jq_program() -> str:
    smoke = _job(_workflow(), "smoke-image", "attest-image")
    start = smoke.index("[.manifests[]")
    end = smoke.index("end'", start)
    return smoke[start : end + 3]


def test_release_trigger_guard_and_python_build_gate() -> None:
    workflow = _workflow()
    guard = _job(workflow, "release-guard", "build")
    build = _job(workflow, "build", "stage-image")

    assert 'tags:\n      - "v*"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert r"^v[0-9]+\.[0-9]+\.[0-9]+$" in guard
    assert 'git merge-base --is-ancestor HEAD "origin/main"' in guard
    assert "commits/${commit}/check-runs" in guard
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
    assert ".platform.os == \"linux\"" in smoke
    assert ".platform.architecture == $architecture" in smoke
    assert "application/vnd.oci.image.manifest.v1+json" in smoke
    assert "application/vnd.docker.distribution.manifest.v2+json" in smoke
    assert 'vnd.docker.reference.type' in smoke
    assert "attestation-manifest" in smoke
    assert "length == 1" in smoke
    assert 'test "$(grep -c "$platform_digest"' not in smoke
    assert 'docker pull --platform "$PLATFORM" "${IMAGE_NAME}@${PLATFORM_DIGEST}"' in smoke
    assert '"${IMAGE_NAME}@${PLATFORM_DIGEST}"' in smoke
    assert "import cognee" in smoke
    assert "import kb" in smoke
    assert "import scripts" in smoke
    assert 'distribution("citadel-archive")' in smoke
    assert "scripts/run_railway.py" in smoke
    assert "skills/citadel-data-boundary/SKILL.md" in smoke
    assert "skills/citadel/SKILL.md" in smoke
    assert "count_adr_records() > 0" in smoke
    assert "CITADEL_EXPECTED_VERSION" in smoke
    assert 'Path("/src").exists()' in smoke


def test_platform_digest_uniqueness_ignores_provenance_duplicate() -> None:
    raw = json.dumps(_PROVENANCE_INDEX)
    assert raw.count(_AMD64_IMAGE) == 2
    assert raw.count(_ARM64_IMAGE) == 2
    assert _linux_image_digests(_PROVENANCE_INDEX, "amd64") == [_AMD64_IMAGE]
    assert _linux_image_digests(_PROVENANCE_INDEX, "arm64") == [_ARM64_IMAGE]


def test_attestation_on_linux_platform_is_not_selected() -> None:
    index = {
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": _AMD64_IMAGE,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": _AMD64_ATTESTATION,
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": _AMD64_IMAGE,
                },
                "platform": {"os": "linux", "architecture": "amd64"},
            },
        ]
    }
    assert _linux_image_digests(index, "amd64") == [_AMD64_IMAGE]


def test_workflow_jq_selects_one_image_digest_under_provenance() -> None:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is required to execute the publish smoke filter")
    raw = json.dumps(_PROVENANCE_INDEX)
    program = _smoke_digest_jq_program()
    for architecture, expected in (("amd64", _AMD64_IMAGE), ("arm64", _ARM64_IMAGE)):
        completed = subprocess.run(
            [jq, "-r", "--arg", "architecture", architecture, program],
            input=raw,
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == expected


def test_keyless_attestation_and_pypi_fail_closed_on_oci_gates() -> None:
    workflow = _workflow()
    attestation = _job(workflow, "attest-image", "publish-pypi")
    pypi = _job(workflow, "publish-pypi", "promote-image")

    assert "needs: [stage-image, smoke-image]" in attestation
    assert "id-token: write" in attestation
    assert "attestations: write" in attestation
    assert "artifact-metadata: write" in attestation
    assert "uses: docker/login-action@v3" in attestation
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
    assert 'gh release view "${GITHUB_REF_NAME}"' in release
    assert 'gh release create "${GITHUB_REF_NAME}"' in release
    assert ":latest" not in workflow.lower()
    assert "${VERSION%.*}" not in workflow
