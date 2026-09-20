from __future__ import annotations

import base64
import datetime
import hashlib
import json
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

from kb import release_verify as rv
from kb.release_verify import (
    BundleVerificationError,
    ReleaseVerifyError,
    SigstoreCertificateVerifier,
    canonical_manifest_bytes,
    expected_workflow_identity,
    normalize_platform,
    verify_release,
)


VERSION = "1.0.0"
REPOSITORY = "masumi-network/Citadel"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _ca_key_usage(key_cert_sign: bool = True) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=key_cert_sign,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )


def _root(
    validity: tuple[datetime.datetime, datetime.datetime] | None = None,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-fulcio-root")])
    now = _now()
    not_valid_before, not_valid_after = validity or (
        now - datetime.timedelta(days=1),
        now + datetime.timedelta(days=3650),
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_ca_key_usage(), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _intermediate(
    signer_key: ec.EllipticCurvePrivateKey,
    signer: x509.Certificate,
    *,
    name: str,
    path_length: int | None = 0,
    key_cert_sign: bool = True,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(signer.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=path_length), critical=True
        )
        .add_extension(_ca_key_usage(key_cert_sign=key_cert_sign), critical=True)
        .sign(signer_key, hashes.SHA256())
    )
    return key, certificate


def _leaf(
    root_key: ec.EllipticCurvePrivateKey,
    root: x509.Certificate,
    *,
    identity: str,
    issuer: str,
    validity: tuple[datetime.datetime, datetime.datetime] | None = None,
    code_signing: bool = True,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    not_valid_before, not_valid_after = validity or (
        now - datetime.timedelta(minutes=5),
        now + datetime.timedelta(minutes=10),
    )
    der_issuer = b"\x0c" + bytes([len(issuer)]) + issuer.encode("utf-8")
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(root.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(identity)]),
            critical=True,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.UnrecognizedExtension(
                ObjectIdentifier(rv._FULCIO_ISSUER_OID_V2), der_issuer
            ),
            critical=False,
        )
    )
    if code_signing:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False
        )
    certificate = builder.sign(root_key, hashes.SHA256())
    return key, certificate


def _manifest(**overrides: Any) -> dict[str, Any]:
    manifest = {
        "schema": rv.MANIFEST_SCHEMA,
        "version": VERSION,
        "source_sha": "a" * 40,
        "repository": REPOSITORY,
        "workflow_identity": expected_workflow_identity(REPOSITORY, VERSION),
        "oci_index_digest": "sha256:" + "1" * 64,
        "images": {
            "linux/amd64": rv.IMAGE_REPOSITORY + "@sha256:" + "2" * 64,
            "linux/arm64": rv.IMAGE_REPOSITORY + "@sha256:" + "3" * 64,
        },
        "artifacts": {
            "citadel_archive-1.0.0-py3-none-any.whl": "sha256:" + "4" * 64,
            "citadel_archive-1.0.0.tar.gz": "sha256:" + "5" * 64,
        },
    }
    manifest.update(overrides)
    return manifest


def _key_id(key: ec.EllipticCurvePrivateKey) -> bytes:
    public = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(public).digest()


def _verifier(
    root: x509.Certificate,
    rekor_key: ec.EllipticCurvePrivateKey,
) -> SigstoreCertificateVerifier:
    return SigstoreCertificateVerifier(
        trust_roots=[root],
        rekor_public_keys={_key_id(rekor_key): rekor_key.public_key()},
    )


def _bundle(
    *,
    leaf_key: ec.EllipticCurvePrivateKey,
    leaf: x509.Certificate,
    manifest: dict[str, Any],
    rekor_key: ec.EllipticCurvePrivateKey,
) -> tuple[bytes, dict[str, Any]]:
    payload = canonical_manifest_bytes(manifest)
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": "release-manifest.json",
                "digest": {"sha256": hashlib.sha256(payload).hexdigest()},
            }
        ],
        "predicateType": "https://citadel.dev/release-manifest/v1",
        "predicate": manifest,
    }
    statement_bytes = json.dumps(
        statement, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signature = leaf_key.sign(
        rv._dsse_pae(b"application/vnd.in-toto+json", statement_bytes),
        ec.ECDSA(hashes.SHA256()),
    )
    signature_value = base64.b64encode(signature).decode("ascii")
    verifier_value = base64.b64encode(
        leaf.public_bytes(serialization.Encoding.PEM)
    ).decode("ascii")
    envelope = {
        "payload": base64.b64encode(statement_bytes).decode("ascii"),
        "payloadType": "application/vnd.in-toto+json",
        "signatures": [{"keyid": "", "sig": signature_value}],
    }
    envelope_bytes = json.dumps(
        envelope, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    transparency_body = json.dumps(
        {
            "apiVersion": "0.0.1",
            "kind": "dsse",
            "spec": {
                "envelopeHash": {
                    "algorithm": "sha256",
                    "value": hashlib.sha256(envelope_bytes).hexdigest(),
                },
                "payloadHash": {
                    "algorithm": "sha256",
                    "value": hashlib.sha256(statement_bytes).hexdigest(),
                },
                "signatures": [
                    {"signature": signature_value, "verifier": verifier_value}
                ],
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    integrated_time = int(_now().timestamp())
    key_id = _key_id(rekor_key)
    set_payload = json.dumps(
        {
            "body": base64.b64encode(transparency_body).decode("ascii"),
            "integratedTime": integrated_time,
            "logID": key_id.hex(),
            "logIndex": 0,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signed_entry_timestamp = rekor_key.sign(
        set_payload,
        ec.ECDSA(hashes.SHA256()),
    )
    root_hash = hashlib.sha256(b"\x00" + transparency_body).digest()
    checkpoint_header = (
        "rekor-test\n1\n"
        + base64.b64encode(root_hash).decode("ascii")
    )
    checkpoint_note = checkpoint_header + "\n"
    checkpoint_signature = rekor_key.sign(
        checkpoint_note.encode("utf-8"),
        ec.ECDSA(hashes.SHA256()),
    )
    checkpoint_value = base64.b64encode(key_id[:4] + checkpoint_signature).decode("ascii")
    bundle = {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "verificationMaterial": {
            "certificate": {
                "rawBytes": base64.b64encode(
                    leaf.public_bytes(serialization.Encoding.DER)
                ).decode("ascii")
            },
            "tlogEntries": [
                {
                    "logIndex": "0",
                    "logId": {"keyId": base64.b64encode(key_id).decode("ascii")},
                    "kindVersion": {"kind": "dsse", "version": "0.0.1"},
                    "integratedTime": str(integrated_time),
                    "inclusionPromise": {
                        "signedEntryTimestamp": base64.b64encode(
                            signed_entry_timestamp
                        ).decode("ascii")
                    },
                    "inclusionProof": {
                        "logIndex": "0",
                        "rootHash": base64.b64encode(root_hash).decode("ascii"),
                        "treeSize": "1",
                        "hashes": [],
                        "checkpoint": {
                            "envelope": (
                                checkpoint_note
                                + "\n"
                                + f"— rekor-test {checkpoint_value}\n"
                            )
                        },
                    },
                    "canonicalizedBody": base64.b64encode(
                        transparency_body
                    ).decode("ascii"),
                }
            ],
        },
        "dsseEnvelope": envelope,
    }
    return payload, bundle


def _sign(manifest: dict[str, Any]):
    root_key, root = _root()
    identity = expected_workflow_identity(
        str(manifest["repository"]), str(manifest["version"])
    )
    leaf_key, leaf = _leaf(
        root_key, root, identity=identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key,
        leaf=leaf,
        manifest=manifest,
        rekor_key=rekor_key,
    )
    verifier = _verifier(root, rekor_key)
    return payload, bundle, verifier, root_key, root, leaf_key, rekor_key


def test_verified_manifest_selects_exact_platform_image() -> None:
    manifest = _manifest()
    payload, bundle, verifier, *_ = _sign(manifest)

    amd = verify_release(
        manifest_bytes=payload, bundle=bundle, platform="x86_64", verifier=verifier
    )
    assert amd.selected_platform == "linux/amd64"
    assert amd.selected_image == manifest["images"]["linux/amd64"]

    arm = verify_release(
        manifest_bytes=payload, bundle=bundle, platform="aarch64", verifier=verifier
    )
    assert arm.selected_platform == "linux/arm64"
    assert arm.selected_image == manifest["images"]["linux/arm64"]
    assert arm.source_sha == manifest["source_sha"]
    assert arm.oci_index_digest == manifest["oci_index_digest"]
    assert arm.workflow_identity == manifest["workflow_identity"]


@pytest.mark.parametrize(
    "host,expected",
    [
        ("x86_64", "linux/amd64"),
        ("amd64", "linux/amd64"),
        ("linux/amd64", "linux/amd64"),
        ("arm64", "linux/arm64"),
        ("aarch64", "linux/arm64"),
        ("linux/arm64", "linux/arm64"),
    ],
)
def test_platform_normalization(host: str, expected: str) -> None:
    assert normalize_platform(host) == expected


def test_unsupported_platform_fails_closed() -> None:
    manifest = _manifest()
    payload, bundle, verifier, *_ = _sign(manifest)
    with pytest.raises(ReleaseVerifyError, match="unsupported platform"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="windows/amd64",
            verifier=verifier,
        )


def test_tampered_manifest_body_breaks_signature() -> None:
    manifest = _manifest()
    payload, bundle, verifier, *_ = _sign(manifest)
    tampered = payload.replace(b'"' + b"a" * 40 + b'"', b'"' + b"b" * 40 + b'"')
    assert tampered != payload
    with pytest.raises(
        BundleVerificationError, match="subject does not bind|signature does not match"
    ):
        verify_release(
            manifest_bytes=tampered, bundle=bundle, platform="amd64", verifier=verifier
        )


def test_version_mismatch_fails_closed() -> None:
    manifest = _manifest()
    payload, bundle, verifier, *_ = _sign(manifest)
    with pytest.raises(ReleaseVerifyError, match="does not match expected"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=verifier,
            expected_version="2.0.0",
        )


def test_wrong_workflow_identity_in_certificate_is_rejected() -> None:
    manifest = _manifest()
    root_key, root = _root()
    wrong_identity = expected_workflow_identity("attacker/Citadel", VERSION)
    leaf_key, leaf = _leaf(
        root_key, root, identity=wrong_identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key,
        leaf=leaf,
        manifest=manifest,
        rekor_key=rekor_key,
    )
    verifier = _verifier(root, rekor_key)
    with pytest.raises(BundleVerificationError, match="identity does not match"):
        verify_release(
            manifest_bytes=payload, bundle=bundle, platform="amd64", verifier=verifier
        )


def test_wrong_oidc_issuer_is_rejected() -> None:
    manifest = _manifest()
    root_key, root = _root()
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    leaf_key, leaf = _leaf(
        root_key, root, identity=identity, issuer="https://accounts.google.com"
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key,
        leaf=leaf,
        manifest=manifest,
        rekor_key=rekor_key,
    )
    verifier = _verifier(root, rekor_key)
    with pytest.raises(BundleVerificationError, match="OIDC issuer"):
        verify_release(
            manifest_bytes=payload, bundle=bundle, platform="amd64", verifier=verifier
        )


def test_certificate_from_untrusted_root_is_rejected() -> None:
    manifest = _manifest()
    payload, bundle, _verifier_instance, *_values, rekor_key = _sign(manifest)
    _foreign_key, foreign_root = _root()
    foreign_verifier = _verifier(foreign_root, rekor_key)
    with pytest.raises(BundleVerificationError, match="chain to a trusted"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=foreign_verifier,
        )


def test_invalid_signed_entry_timestamp_is_rejected() -> None:
    manifest = _manifest()
    payload, bundle, verifier, *_ = _sign(manifest)
    entry = bundle["verificationMaterial"]["tlogEntries"][0]
    entry["inclusionPromise"]["signedEntryTimestamp"] = base64.b64encode(
        b"invalid"
    ).decode("ascii")
    with pytest.raises(BundleVerificationError, match="signed entry timestamp"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=verifier,
        )


def test_checkpoint_signature_excludes_separator_blank_line() -> None:
    manifest = _manifest()
    payload, bundle, verifier, *_values, rekor_key = _sign(manifest)
    entry = bundle["verificationMaterial"]["tlogEntries"][0]
    envelope = entry["inclusionProof"]["checkpoint"]["envelope"]
    header, _signature_block = envelope.split("\n\n", 1)
    bad_note = header + "\n\n"
    bad_signature = rekor_key.sign(
        bad_note.encode("utf-8"),
        ec.ECDSA(hashes.SHA256()),
    )
    key_id = _key_id(rekor_key)
    entry["inclusionProof"]["checkpoint"]["envelope"] = (
        bad_note
        + "— rekor-test "
        + base64.b64encode(key_id[:4] + bad_signature).decode("ascii")
        + "\n"
    )
    with pytest.raises(BundleVerificationError, match="checkpoint signature"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=verifier,
        )


def test_certificate_expired_at_authenticated_integration_time_is_rejected() -> None:
    manifest = _manifest()
    root_key, root = _root()
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    now = _now()
    leaf_key, leaf = _leaf(
        root_key,
        root,
        identity=identity,
        issuer=rv.FULCIO_OIDC_ISSUER,
        validity=(
            now - datetime.timedelta(days=2),
            now - datetime.timedelta(days=1),
        ),
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key,
        leaf=leaf,
        manifest=manifest,
        rekor_key=rekor_key,
    )
    with pytest.raises(
        BundleVerificationError, match="not valid at authenticated integration time"
    ):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=_verifier(root, rekor_key),
        )




def test_non_canonical_manifest_bytes_are_rejected() -> None:
    manifest = _manifest()
    _payload, bundle, verifier, *_ = _sign(manifest)
    import json

    spaced = json.dumps(manifest, sort_keys=True).encode("utf-8")
    assert spaced != canonical_manifest_bytes(manifest)
    with pytest.raises(ReleaseVerifyError, match="canonical"):
        verify_release(
            manifest_bytes=spaced, bundle=bundle, platform="amd64", verifier=verifier
        )


def test_missing_platform_image_fails_closed() -> None:
    manifest = _manifest()
    del manifest["images"]["linux/arm64"]
    payload, bundle, verifier, *_ = _sign(manifest)
    with pytest.raises(ReleaseVerifyError, match="missing the linux/arm64 image"):
        verify_release(
            manifest_bytes=payload, bundle=bundle, platform="amd64", verifier=verifier
        )


def test_unpinned_image_reference_fails_closed() -> None:
    manifest = _manifest()
    manifest["images"]["linux/amd64"] = rv.IMAGE_REPOSITORY + ":1.0.0"
    payload, bundle, verifier, *_ = _sign(manifest)
    with pytest.raises(ReleaseVerifyError, match="pinned to a digest"):
        verify_release(
            manifest_bytes=payload, bundle=bundle, platform="amd64", verifier=verifier
        )


def test_default_verifier_requires_rekor_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _root_key, root = _root()
    roots_path = tmp_path / "fulcio-roots.pem"
    roots_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv("CITADEL_RELEASE_TRUST_ROOTS", str(roots_path))
    monkeypatch.delenv("CITADEL_RELEASE_REKOR_PUBLIC_KEY", raising=False)
    with pytest.raises(ReleaseVerifyError, match="CITADEL_RELEASE_REKOR_PUBLIC_KEY"):
        rv.default_release_verifier()


def test_default_verifier_requires_trust_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CITADEL_RELEASE_TRUST_ROOTS", raising=False)
    with pytest.raises(ReleaseVerifyError, match="CITADEL_RELEASE_TRUST_ROOTS"):
        rv.default_release_verifier()


def test_release_from_another_repository_is_rejected() -> None:
    manifest = _manifest(repository="attacker/Citadel")
    payload, bundle, verifier, *_ = _sign(manifest)
    with pytest.raises(ReleaseVerifyError, match="release repository"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=verifier,
        )


def _inject_chain(
    bundle: dict[str, Any], certificates: list[x509.Certificate]
) -> None:
    bundle["verificationMaterial"]["x509CertificateChain"] = {
        "certificates": [
            {
                "rawBytes": base64.b64encode(
                    cert.public_bytes(serialization.Encoding.DER)
                ).decode("ascii")
            }
            for cert in certificates
        ]
    }


def test_valid_intermediate_chain_verifies() -> None:
    manifest = _manifest()
    root_key, root = _root()
    inter_key, inter = _intermediate(root_key, root, name="test-fulcio-intermediate")
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    leaf_key, leaf = _leaf(
        inter_key, inter, identity=identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key, leaf=leaf, manifest=manifest, rekor_key=rekor_key
    )
    _inject_chain(bundle, [inter])
    verified = verify_release(
        manifest_bytes=payload,
        bundle=bundle,
        platform="amd64",
        verifier=_verifier(root, rekor_key),
    )
    assert verified.selected_platform == "linux/amd64"


def test_forged_non_ca_intermediate_is_rejected() -> None:
    manifest = _manifest()
    root_key, root = _root()
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    # A legitimate Fulcio leaf issued by the trusted root for another workflow.
    legit_key, legit_leaf = _leaf(
        root_key,
        root,
        identity="https://github.com/other/repo/.github/workflows/x.yml@refs/tags/v9",
        issuer=rv.FULCIO_OIDC_ISSUER,
    )
    # The attacker mints a child under the legitimate leaf key, carrying the
    # Citadel publish identity, and presents the legitimate leaf as an intermediate.
    attacker_key, attacker_leaf = _leaf(
        legit_key, legit_leaf, identity=identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=attacker_key, leaf=attacker_leaf, manifest=manifest, rekor_key=rekor_key
    )
    _inject_chain(bundle, [legit_leaf])
    with pytest.raises(BundleVerificationError, match="non-CA issuer"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=_verifier(root, rekor_key),
        )


def test_intermediate_without_key_cert_sign_is_rejected() -> None:
    manifest = _manifest()
    root_key, root = _root()
    inter_key, inter = _intermediate(
        root_key, root, name="weak-intermediate", key_cert_sign=False
    )
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    leaf_key, leaf = _leaf(
        inter_key, inter, identity=identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key, leaf=leaf, manifest=manifest, rekor_key=rekor_key
    )
    _inject_chain(bundle, [inter])
    with pytest.raises(BundleVerificationError, match="cannot sign certificates"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=_verifier(root, rekor_key),
        )


def test_chain_exceeds_path_length_is_rejected() -> None:
    manifest = _manifest()
    root_key, root = _root()
    inter_b_key, inter_b = _intermediate(root_key, root, name="inter-b", path_length=0)
    inter_a_key, inter_a = _intermediate(
        inter_b_key, inter_b, name="inter-a", path_length=0
    )
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    leaf_key, leaf = _leaf(
        inter_a_key, inter_a, identity=identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key, leaf=leaf, manifest=manifest, rekor_key=rekor_key
    )
    _inject_chain(bundle, [inter_a, inter_b])
    with pytest.raises(BundleVerificationError, match="path length"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=_verifier(root, rekor_key),
        )


def test_leaf_without_code_signing_eku_is_rejected() -> None:
    manifest = _manifest()
    root_key, root = _root()
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    leaf_key, leaf = _leaf(
        root_key,
        root,
        identity=identity,
        issuer=rv.FULCIO_OIDC_ISSUER,
        code_signing=False,
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key, leaf=leaf, manifest=manifest, rekor_key=rekor_key
    )
    with pytest.raises(BundleVerificationError, match="code-signing"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=_verifier(root, rekor_key),
        )


def test_expired_trust_root_is_rejected() -> None:
    manifest = _manifest()
    now = _now()
    root_key, root = _root(
        validity=(now - datetime.timedelta(days=800), now - datetime.timedelta(days=1))
    )
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    leaf_key, leaf = _leaf(
        root_key, root, identity=identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key, leaf=leaf, manifest=manifest, rekor_key=rekor_key
    )
    with pytest.raises(
        BundleVerificationError, match="not valid at authenticated integration time"
    ):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=_verifier(root, rekor_key),
        )


def test_pinned_bundle_intermediate_chains_to_root() -> None:
    # The pinned trust set holds both a Fulcio intermediate and the self-signed
    # root. The bundle carries no intermediate, so the path must be built through
    # the pinned intermediate and verified up to the terminal self-signed root.
    manifest = _manifest()
    root_key, root = _root()
    inter_key, inter = _intermediate(root_key, root, name="test-fulcio-intermediate")
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    leaf_key, leaf = _leaf(
        inter_key, inter, identity=identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key, leaf=leaf, manifest=manifest, rekor_key=rekor_key
    )
    verifier = SigstoreCertificateVerifier(
        trust_roots=[inter, root],
        rekor_public_keys={_key_id(rekor_key): rekor_key.public_key()},
    )
    verified = verify_release(
        manifest_bytes=payload, bundle=bundle, platform="amd64", verifier=verifier
    )
    assert verified.selected_platform == "linux/amd64"


def test_root_only_bundle_still_verifies() -> None:
    # A minimal pinned set with only the self-signed root, and a leaf signed
    # directly by that root, must still verify.
    manifest = _manifest()
    payload, bundle, verifier, *_ = _sign(manifest)
    verified = verify_release(
        manifest_bytes=payload, bundle=bundle, platform="amd64", verifier=verifier
    )
    assert verified.selected_platform == "linux/amd64"


def test_leaf_missing_its_intermediate_is_rejected() -> None:
    # The leaf is signed by an intermediate that is neither in the bundle chain
    # nor in the pinned trust set, so no path to a pinned anchor can be built.
    manifest = _manifest()
    root_key, root = _root()
    inter_key, inter = _intermediate(root_key, root, name="absent-intermediate")
    identity = expected_workflow_identity(REPOSITORY, VERSION)
    leaf_key, leaf = _leaf(
        inter_key, inter, identity=identity, issuer=rv.FULCIO_OIDC_ISSUER
    )
    rekor_key = ec.generate_private_key(ec.SECP256R1())
    payload, bundle = _bundle(
        leaf_key=leaf_key, leaf=leaf, manifest=manifest, rekor_key=rekor_key
    )
    with pytest.raises(BundleVerificationError, match="chain to a trusted"):
        verify_release(
            manifest_bytes=payload,
            bundle=bundle,
            platform="amd64",
            verifier=_verifier(root, rekor_key),
        )
