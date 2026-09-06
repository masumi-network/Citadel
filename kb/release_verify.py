"""Single trust boundary for a signed Citadel release manifest.

This module parses a deterministic signed release manifest and returns an
immutable, fully verified release object. Nothing else in the CLI, the local
deploy path, or the release script may trust a manifest field before this
module verifies the Sigstore bundle that signs the exact manifest bytes.

Verification is genuine asymmetric cryptography. The default verifier loads
the leaf certificate from the standard Sigstore bundle, walks the certificate
chain to an operator-supplied trust root, checks the Fulcio identity
extensions (OIDC issuer and workflow Subject Alternative Name), and verifies
the DSSE signature over the in-toto envelope that binds the exact manifest
bytes.

The verifier requires a standard Sigstore transparency-log entry and an
operator-pinned Rekor public key. It verifies the signed entry timestamp,
Merkle inclusion proof, signed checkpoint, and certificate validity at the
authenticated Rekor integration time.

Every failure fails closed. There is no unsigned fallback path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cryptography import x509


class ReleaseVerifyError(RuntimeError):
    """Raised when a release manifest cannot be trusted. Always fail closed."""


class BundleVerificationError(ReleaseVerifyError):
    """Raised when the signing bundle does not bind the manifest bytes."""


FULCIO_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
IMAGE_REPOSITORY = "ghcr.io/masumi-network/citadel"
RELEASE_REPOSITORY = "masumi-network/Citadel"
SUPPORTED_PLATFORMS = ("linux/amd64", "linux/arm64")
MANIFEST_SCHEMA = "citadel.release-manifest/v1"

# Fulcio identity extension OIDs. The V2 issuer is a DER-encoded UTF8String;
# the deprecated V1 issuer is a raw UTF-8 string.
_FULCIO_ISSUER_OID_V2 = "1.3.6.1.4.1.57264.1.8"
_FULCIO_ISSUER_OID_V1 = "1.3.6.1.4.1.57264.1.1"

_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PINNED_IMAGE = re.compile(r"^(?P<repo>\S+)@(?P<digest>sha256:[0-9a-f]{64})$")

_PLATFORM_ALIASES = {
    "linux/amd64": "linux/amd64",
    "amd64": "linux/amd64",
    "x86_64": "linux/amd64",
    "x86-64": "linux/amd64",
    "linux/arm64": "linux/arm64",
    "arm64": "linux/arm64",
    "aarch64": "linux/arm64",
}


def normalize_platform(platform: str) -> str:
    """Map a host or platform string to a supported ``linux/<arch>`` value.

    Unsupported platforms fail closed rather than defaulting to a build.
    """
    key = str(platform).strip().lower()
    normalized = _PLATFORM_ALIASES.get(key)
    if normalized is None:
        raise ReleaseVerifyError(f"unsupported platform: {platform!r}")
    return normalized


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Return the one deterministic byte encoding of a release manifest.

    CI signs exactly these bytes and the verifier re-derives them, so the
    signature covers a single unambiguous encoding.
    """
    return json.dumps(
        dict(manifest),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def expected_workflow_identity(repository: str, version: str) -> str:
    return (
        f"https://github.com/{repository}/.github/workflows/"
        f"publish.yml@refs/tags/v{version}"
    )


@dataclass(frozen=True)
class VerifiedRelease:
    """Immutable, signature-verified release facts."""

    version: str
    source_sha: str
    repository: str
    workflow_identity: str
    oci_index_digest: str
    image_amd64: str
    image_arm64: str
    artifacts: Mapping[str, str]
    selected_platform: str
    selected_image: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))

    def image_for(self, platform: str) -> str:
        normalized = normalize_platform(platform)
        return self.image_amd64 if normalized == "linux/amd64" else self.image_arm64

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_sha": self.source_sha,
            "repository": self.repository,
            "workflow_identity": self.workflow_identity,
            "oci_index_digest": self.oci_index_digest,
            "images": {
                "linux/amd64": self.image_amd64,
                "linux/arm64": self.image_arm64,
            },
            "artifacts": dict(self.artifacts),
            "selected_platform": self.selected_platform,
            "selected_image": self.selected_image,
            "manifest_sha256": self.manifest_sha256,
        }


@runtime_checkable
class BundleVerifier(Protocol):
    """Verifies that a bundle signs ``payload`` under an expected identity."""

    def verify(
        self,
        *,
        payload: bytes,
        bundle: Mapping[str, Any],
        expected_issuer: str,
        expected_identity: str,
    ) -> None: ...


def _require_str(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReleaseVerifyError(f"release manifest field {key!r} must be a non-empty string")
    return value


def _require_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ReleaseVerifyError(f"release manifest field {key!r} must be an object")
    return value


def verify_release(
    *,
    manifest_bytes: bytes,
    bundle: Mapping[str, Any],
    platform: str,
    verifier: BundleVerifier,
    expected_version: str | None = None,
) -> VerifiedRelease:
    """Return trusted release facts, or raise. No field is trusted before the
    bundle verifies the exact manifest bytes."""
    selected_platform = normalize_platform(platform)
    if not isinstance(manifest_bytes, (bytes, bytearray)):
        raise ReleaseVerifyError("manifest bytes must be raw bytes")
    payload = bytes(manifest_bytes)
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseVerifyError("release manifest is not valid UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        raise ReleaseVerifyError("release manifest must be a JSON object")
    if canonical_manifest_bytes(parsed) != payload:
        raise ReleaseVerifyError("release manifest bytes are not in canonical form")

    # Claimed identity is derived from the claimed repository and version. The
    # signature (verified next) is what actually binds these bytes to that
    # identity, so nothing here is trusted until verification succeeds.
    repository = _require_str(parsed, "repository")
    if repository != RELEASE_REPOSITORY:
        raise ReleaseVerifyError(
            f"release repository must be {RELEASE_REPOSITORY!r}"
        )
    version = _require_str(parsed, "version")
    if not _SEMVER.fullmatch(version):
        raise ReleaseVerifyError("release version must be an exact semantic version")
    if expected_version is not None and version != expected_version:
        raise ReleaseVerifyError(
            f"release version {version!r} does not match expected {expected_version!r}"
        )
    identity = expected_workflow_identity(RELEASE_REPOSITORY, version)
    if not isinstance(verifier, BundleVerifier):
        raise ReleaseVerifyError("a bundle verifier is required")
    try:
        verifier.verify(
            payload=payload,
            bundle=bundle,
            expected_issuer=FULCIO_OIDC_ISSUER,
            expected_identity=identity,
        )
    except ReleaseVerifyError:
        raise
    except Exception as error:  # noqa: BLE001 - any verifier fault fails closed
        raise BundleVerificationError("release bundle verification failed") from error

    # The bytes are now trusted. Validate the full schema of the trusted bytes.
    if parsed.get("schema") != MANIFEST_SCHEMA:
        raise ReleaseVerifyError("unsupported release manifest schema")
    source_sha = _require_str(parsed, "source_sha")
    if not _SOURCE_SHA.fullmatch(source_sha):
        raise ReleaseVerifyError("release source SHA must be a 40-character git revision")
    if _require_str(parsed, "workflow_identity") != identity:
        raise ReleaseVerifyError("release workflow identity does not match repository and version")
    oci_index_digest = _require_str(parsed, "oci_index_digest")
    if not _DIGEST.fullmatch(oci_index_digest):
        raise ReleaseVerifyError("OCI index digest must be a sha256 digest")

    images = _require_mapping(parsed, "images")
    resolved: dict[str, str] = {}
    for wanted in SUPPORTED_PLATFORMS:
        reference = images.get(wanted)
        if not isinstance(reference, str):
            raise ReleaseVerifyError(f"release manifest is missing the {wanted} image")
        match = _PINNED_IMAGE.fullmatch(reference)
        if match is None or match.group("repo") != IMAGE_REPOSITORY:
            raise ReleaseVerifyError(f"{wanted} image must be {IMAGE_REPOSITORY} pinned to a digest")
        resolved[wanted] = reference

    artifacts_raw = _require_mapping(parsed, "artifacts")
    if not artifacts_raw:
        raise ReleaseVerifyError("release manifest must record at least one artifact hash")
    artifacts: dict[str, str] = {}
    for name, digest in artifacts_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ReleaseVerifyError("release artifact name must be a non-empty string")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ReleaseVerifyError(f"release artifact {name!r} must record a sha256 digest")
        artifacts[name] = digest

    return VerifiedRelease(
        version=version,
        source_sha=source_sha,
        repository=repository,
        workflow_identity=identity,
        oci_index_digest=oci_index_digest,
        image_amd64=resolved["linux/amd64"],
        image_arm64=resolved["linux/arm64"],
        artifacts=artifacts,
        selected_platform=selected_platform,
        selected_image=resolved[selected_platform],
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
    )


# --- Default Sigstore certificate verifier (cryptography) --------------------


def _load_certificate(value: Any) -> "x509.Certificate":
    from cryptography import x509

    if not isinstance(value, str) or "BEGIN CERTIFICATE" not in value:
        raise BundleVerificationError("bundle certificate must be a PEM string")
    try:
        return x509.load_pem_x509_certificate(value.encode("utf-8"))
    except ValueError as error:
        raise BundleVerificationError("bundle certificate is not a valid PEM certificate") from error


def _decode_signature(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise BundleVerificationError("bundle signature must be a base64 string")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise BundleVerificationError("bundle signature is not valid base64") from error

def _public_key_id(public_key: Any) -> bytes:
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
    )

    try:
        encoded = public_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    except Exception as error:  # noqa: BLE001 - malformed operator key fails closed
        raise ReleaseVerifyError("Rekor public key cannot be encoded") from error
    return hashlib.sha256(encoded).digest()


def load_rekor_public_keys(path: str | Path) -> dict[bytes, Any]:
    """Load the operator-pinned Rekor verification key."""
    from cryptography.hazmat.primitives import serialization

    try:
        public_key = serialization.load_pem_public_key(
            Path(path).expanduser().read_bytes()
        )
    except Exception as error:  # noqa: BLE001 - malformed operator key fails closed
        raise ReleaseVerifyError("Rekor public key PEM is unreadable") from error
    return {_public_key_id(public_key): public_key}


def _public_key_verify(certificate: "x509.Certificate", signature: bytes, message: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    public_key = certificate.public_key()
    try:
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        elif isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise BundleVerificationError("unsupported release signing key type")
    except InvalidSignature as error:
        raise BundleVerificationError("release signature does not match the manifest") from error


def _assert_issued_by(child: "x509.Certificate", issuer: "x509.Certificate") -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    if child.issuer != issuer.subject:
        raise BundleVerificationError("release certificate chain has a broken issuer link")
    public_key = issuer.public_key()
    try:
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                child.signature,
                child.tbs_certificate_bytes,
                ec.ECDSA(child.signature_hash_algorithm),  # type: ignore[arg-type]
            )
        elif isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(
                child.signature,
                child.tbs_certificate_bytes,
                padding.PKCS1v15(),
                child.signature_hash_algorithm,  # type: ignore[arg-type]
            )
        else:
            raise BundleVerificationError("unsupported certificate authority key type")
    except InvalidSignature as error:
        raise BundleVerificationError("release certificate is not signed by its issuer") from error


def _certificate_valid_at(
    certificate: "x509.Certificate",
    verified_at: datetime,
) -> None:
    not_before = getattr(certificate, "not_valid_before_utc", None)
    not_after = getattr(certificate, "not_valid_after_utc", None)
    if not_before is None:
        not_before = certificate.not_valid_before.replace(tzinfo=UTC)
    if not_after is None:
        not_after = certificate.not_valid_after.replace(tzinfo=UTC)
    if not_before <= verified_at <= not_after:
        return
    raise BundleVerificationError(
        "release signing certificate was not valid at authenticated integration time"
    )


def _basic_constraints(certificate: "x509.Certificate") -> Any:
    from cryptography import x509

    try:
        return certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        return None


def _assert_leaf_certificate(certificate: "x509.Certificate") -> None:
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    constraints = _basic_constraints(certificate)
    if constraints is not None and constraints.ca:
        raise BundleVerificationError(
            "release signing certificate must not be a certificate authority"
        )
    try:
        extended_key_usage = certificate.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
    except x509.ExtensionNotFound as error:
        raise BundleVerificationError(
            "release signing certificate has no code-signing extended key usage"
        ) from error
    if ExtendedKeyUsageOID.CODE_SIGNING not in extended_key_usage:
        raise BundleVerificationError(
            "release signing certificate is not authorized for code signing"
        )


def _assert_ca_certificate(certificate: "x509.Certificate") -> Any:
    from cryptography import x509

    constraints = _basic_constraints(certificate)
    if constraints is None or not constraints.ca:
        raise BundleVerificationError(
            "release certificate chain includes a non-CA issuer"
        )
    try:
        key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound as error:
        raise BundleVerificationError(
            "release certificate authority has no key usage extension"
        ) from error
    if not key_usage.key_cert_sign:
        raise BundleVerificationError(
            "release certificate authority cannot sign certificates"
        )
    return constraints


def _select_trust_root(
    supplied: Sequence["x509.Certificate"],
    roots: Sequence["x509.Certificate"],
) -> list["x509.Certificate"]:
    """Resolve the supplied certificates into one concrete path that ends in a
    pinned trust anchor.

    A pinned certificate is a terminal trust anchor only when no other pinned
    certificate issued it (a self-signed root, or an operator-pinned top). A pinned
    intermediate is never treated as terminal while its pinned issuer exists: the
    path is extended through it and its signature is verified up to the anchor.
    """
    from cryptography.hazmat.primitives.serialization import Encoding

    def _same_public_key(a: "x509.Certificate", b: "x509.Certificate") -> bool:
        return (
            a.public_key().public_numbers()  # type: ignore[attr-defined]
            == b.public_key().public_numbers()  # type: ignore[attr-defined]
        )

    def _pinned_match(cert: "x509.Certificate") -> "x509.Certificate | None":
        for root in roots:
            if cert.subject == root.subject and _same_public_key(cert, root):
                return root
        return None

    def _pinned_issuer(cert: "x509.Certificate") -> "x509.Certificate | None":
        cert_der = cert.public_bytes(Encoding.DER)
        for root in roots:
            if root.public_bytes(Encoding.DER) == cert_der:
                continue  # never treat a certificate as its own issuer
            if cert.issuer != root.subject:
                continue
            try:
                _assert_issued_by(cert, root)
            except BundleVerificationError:
                continue
            return root
        return None

    path = list(supplied)
    fingerprints = {cert.public_bytes(Encoding.DER) for cert in path}
    while True:
        top = path[-1]
        pinned = _pinned_match(top)
        if pinned is not None and _pinned_issuer(pinned) is None:
            # `top` is a pinned trust anchor: trust the pinned object, not the
            # bundle-supplied copy.
            path[-1] = pinned
            return path
        issuer = _pinned_issuer(top)
        if issuer is None:
            raise BundleVerificationError(
                "release certificate does not chain to a trusted Sigstore root"
            )
        fingerprint = issuer.public_bytes(Encoding.DER)
        if fingerprint in fingerprints:
            raise BundleVerificationError(
                "release certificate does not chain to a trusted Sigstore root"
            )
        fingerprints.add(fingerprint)
        path.append(issuer)


def _verify_chain(
    leaf: "x509.Certificate",
    chain: Sequence["x509.Certificate"],
    roots: Sequence["x509.Certificate"],
    *,
    verified_at: datetime,
) -> None:
    full_path = _select_trust_root([leaf, *chain], roots)
    # Every certificate, including the pinned root, must be valid at the
    # authenticated integration time.
    for certificate in full_path:
        _certificate_valid_at(certificate, verified_at)
    # Each certificate must be signed by the next, up into the pinned root.
    for index in range(len(full_path) - 1):
        _assert_issued_by(full_path[index], full_path[index + 1])
    # The leaf must be an end-entity code-signing certificate, never a CA.
    _assert_leaf_certificate(full_path[0])
    # Every issuer (intermediates and the pinned root) must be a CA that can sign
    # certificates, and its BasicConstraints path length must cover the number of
    # subordinate CA certificates beneath it.
    for index in range(1, len(full_path)):
        constraints = _assert_ca_certificate(full_path[index])
        subordinate_ca_count = index - 1
        if (
            constraints.path_length is not None
            and subordinate_ca_count > constraints.path_length
        ):
            raise BundleVerificationError(
                "release certificate chain exceeds the authority path length"
            )


def _fulcio_issuer(certificate: "x509.Certificate") -> str | None:
    from cryptography import x509
    from cryptography.x509.oid import ObjectIdentifier

    for oid, der in ((_FULCIO_ISSUER_OID_V2, True), (_FULCIO_ISSUER_OID_V1, False)):
        try:
            extension = certificate.extensions.get_extension_for_oid(ObjectIdentifier(oid))
        except x509.ExtensionNotFound:
            continue
        raw = extension.value.value  # type: ignore[attr-defined]
        if not der:
            return raw.decode("utf-8")
        # V2 issuer is a DER-encoded UTF8String: tag 0x0c, length, then bytes.
        if len(raw) >= 2 and raw[0] == 0x0C:
            length = raw[1]
            return raw[2 : 2 + length].decode("utf-8")
        return raw.decode("utf-8")
    return None


def _verify_identity(
    certificate: "x509.Certificate", expected_issuer: str, expected_identity: str
) -> None:
    from cryptography import x509

    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound as error:
        raise BundleVerificationError("release certificate has no subject alternative name") from error
    uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    if expected_identity not in uris:
        raise BundleVerificationError(
            "release certificate identity does not match the Citadel publish workflow"
        )
    issuer = _fulcio_issuer(certificate)
    if issuer != expected_issuer:
        raise BundleVerificationError(
            "release certificate OIDC issuer is not the GitHub Actions token issuer"
        )


def _load_der_certificate(value: Any) -> "x509.Certificate":
    from cryptography import x509

    if not isinstance(value, Mapping):
        raise BundleVerificationError("bundle certificate entry is malformed")
    encoded = value.get("rawBytes")
    if not isinstance(encoded, str) or not encoded.strip():
        raise BundleVerificationError("bundle certificate rawBytes is missing")
    try:
        raw = base64.b64decode(encoded, validate=True)
        return x509.load_der_x509_certificate(raw)
    except (ValueError, binascii.Error) as error:
        raise BundleVerificationError("bundle certificate DER is invalid") from error


def _standard_bundle_material(
    bundle: Mapping[str, Any],
) -> tuple["x509.Certificate", list["x509.Certificate"], Mapping[str, Any]]:
    from cryptography.hazmat.primitives.serialization import Encoding

    material = bundle.get("verificationMaterial")
    if not isinstance(material, Mapping):
        raise BundleVerificationError("Sigstore verificationMaterial is missing")
    certificate_entry = material.get("certificate")
    chain_container = material.get("x509CertificateChain")
    if isinstance(certificate_entry, Mapping):
        leaf = _load_der_certificate(certificate_entry)
        chain_entries = (
            chain_container.get("certificates", [])
            if isinstance(chain_container, Mapping)
            else []
        )
        chain = [_load_der_certificate(item) for item in chain_entries]
    elif isinstance(chain_container, Mapping):
        chain_entries = chain_container.get("certificates", [])
        if not isinstance(chain_entries, list) or not chain_entries:
            raise BundleVerificationError("Sigstore certificate chain is missing")
        certificates = [_load_der_certificate(item) for item in chain_entries]
        leaf, chain = certificates[0], certificates[1:]
    else:
        raise BundleVerificationError("Sigstore certificate material is missing")
    if chain and leaf.public_bytes(Encoding.DER) == chain[0].public_bytes(Encoding.DER):
        chain = chain[1:]
    return leaf, chain, material


def _require_transparency_material(
    material: Mapping[str, Any],
    *,
    expected_kinds: frozenset[str],
) -> tuple[Mapping[str, Any], int, bytes, bytes]:
    entries = material.get("tlogEntries")
    if not isinstance(entries, list) or len(entries) != 1:
        raise BundleVerificationError(
            "Sigstore bundle must contain exactly one transparency-log entry"
        )
    entry = entries[0]
    if not isinstance(entry, Mapping):
        raise BundleVerificationError("Sigstore transparency-log entry is malformed")
    kind_version = entry.get("kindVersion")
    kind = kind_version.get("kind") if isinstance(kind_version, Mapping) else None
    if kind not in expected_kinds:
        raise BundleVerificationError("Sigstore transparency-log kind is not supported")
    integrated_time_value = entry.get("integratedTime")
    if not isinstance(integrated_time_value, str):
        raise BundleVerificationError(
            "Sigstore transparency-log integrated time is missing"
        )
    try:
        integrated_time = int(integrated_time_value)
        if integrated_time <= 0:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise BundleVerificationError(
            "Sigstore transparency-log integrated time is invalid"
        ) from error
    log_index_value = entry.get("logIndex")
    if not isinstance(log_index_value, str):
        raise BundleVerificationError("Sigstore transparency-log index is missing")
    log_id = entry.get("logId")
    key_id_value = log_id.get("keyId") if isinstance(log_id, Mapping) else None
    if not isinstance(key_id_value, str) or not key_id_value.strip():
        raise BundleVerificationError("Sigstore transparency-log key ID is missing")
    try:
        key_id = base64.b64decode(key_id_value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise BundleVerificationError("Sigstore transparency-log key ID is invalid") from error
    if not key_id:
        raise BundleVerificationError("Sigstore transparency-log key ID is empty")

    inclusion_promise = entry.get("inclusionPromise")
    if not isinstance(inclusion_promise, Mapping):
        raise BundleVerificationError("Sigstore inclusion promise is missing")
    _decode_signature(inclusion_promise.get("signedEntryTimestamp"))

    body_value = entry.get("canonicalizedBody")
    if not isinstance(body_value, str) or not body_value.strip():
        raise BundleVerificationError("Sigstore canonicalized transparency body is missing")
    try:
        canonicalized_body = base64.b64decode(body_value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise BundleVerificationError("Sigstore canonicalized transparency body is invalid") from error

    proof = entry.get("inclusionProof")
    if not isinstance(proof, Mapping) or not isinstance(proof.get("checkpoint"), Mapping):
        raise BundleVerificationError(
            "Sigstore inclusion proof with signed checkpoint is missing"
        )
    return entry, integrated_time, key_id, canonicalized_body


def _canonical_transparency_entry(
    *,
    body: bytes,
    integrated_time: int,
    key_id: bytes,
    log_index: int,
) -> bytes:
    return json.dumps(
        {
            "body": base64.b64encode(body).decode("ascii"),
            "integratedTime": integrated_time,
            "logID": key_id.hex(),
            "logIndex": log_index,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _verify_rekor_signature(
    public_key: Any,
    signature: bytes,
    message: bytes,
    *,
    label: str,
) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

    try:
        if isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        elif isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(signature, message)
        else:
            raise BundleVerificationError("unsupported Rekor public key type")
    except InvalidSignature as error:
        raise BundleVerificationError(f"Sigstore {label} signature is invalid") from error


def _transparency_int(entry: Mapping[str, Any], key: str) -> int:
    try:
        value = int(str(entry.get(key, "")))
    except (TypeError, ValueError) as error:
        raise BundleVerificationError(
            f"Sigstore transparency field {key!r} is invalid"
        ) from error
    if value < 0:
        raise BundleVerificationError(f"Sigstore transparency field {key!r} is negative")
    return value


def _decode_b64(value: Any, *, label: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise BundleVerificationError(f"Sigstore {label} must be base64")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise BundleVerificationError(f"Sigstore {label} is invalid base64") from error


def _hash_leaf(leaf: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + leaf).digest()


def _hash_children(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _decomp_inclusion_proof(index: int, size: int) -> tuple[int, int]:
    inner = (index ^ (size - 1)).bit_length()
    border = (index >> inner).bit_count()
    return inner, border


def _verify_merkle_inclusion(
    *,
    entry: Mapping[str, Any],
    canonicalized_body: bytes,
) -> None:
    proof = entry.get("inclusionProof")
    if not isinstance(proof, Mapping):
        raise BundleVerificationError("Sigstore inclusion proof is missing")
    proof_index = _transparency_int(proof, "logIndex")
    tree_size = _transparency_int(proof, "treeSize")
    if tree_size <= proof_index:
        raise BundleVerificationError("Sigstore inclusion proof index is inconsistent")
    root_hash = _decode_b64(proof.get("rootHash"), label="inclusion root hash")
    raw_hashes = proof.get("hashes")
    if not isinstance(raw_hashes, list):
        raise BundleVerificationError("Sigstore inclusion proof hashes are missing")
    hashes = [_decode_b64(value, label="inclusion proof hash") for value in raw_hashes]
    inner, border = _decomp_inclusion_proof(proof_index, tree_size)
    if len(hashes) != inner + border:
        raise BundleVerificationError("Sigstore inclusion proof has the wrong size")
    current = _hash_leaf(canonicalized_body)
    for offset, sibling in enumerate(hashes[:inner]):
        current = (
            _hash_children(current, sibling)
            if (proof_index >> offset) & 1 == 0
            else _hash_children(sibling, current)
        )
    for sibling in hashes[inner:]:
        current = _hash_children(sibling, current)
    if current != root_hash:
        raise BundleVerificationError("Sigstore inclusion proof root hash is invalid")


def _verify_checkpoint(
    *,
    entry: Mapping[str, Any],
    key_id: bytes,
    root_hash: bytes,
    public_key: Any,
) -> None:
    proof = entry.get("inclusionProof")
    checkpoint = proof.get("checkpoint") if isinstance(proof, Mapping) else None
    envelope = checkpoint.get("envelope") if isinstance(checkpoint, Mapping) else None
    if (
        not isinstance(envelope, str)
        or envelope.count("\n\n") != 1
    ):
        raise BundleVerificationError("Sigstore checkpoint is malformed")
    header, signature_block = envelope.split("\n\n", 1)
    note = header + "\n"
    header_lines = header.splitlines()
    if len(header_lines) < 3:
        raise BundleVerificationError("Sigstore checkpoint header is malformed")
    checkpoint_size = _transparency_int(
        {"treeSize": header_lines[1]},
        "treeSize",
    )
    proof = entry.get("inclusionProof")
    proof_index = _transparency_int(
        proof if isinstance(proof, Mapping) else {},
        "logIndex",
    )
    if checkpoint_size < proof_index + 1:
        raise BundleVerificationError("Sigstore checkpoint tree size is inconsistent")
    checkpoint_root = _decode_b64(header_lines[2], label="checkpoint root hash")
    if checkpoint_root != root_hash:
        raise BundleVerificationError("Sigstore checkpoint root hash is invalid")
    signatures = [line for line in signature_block.splitlines() if line.strip()]
    for line in signatures:
        match = re.fullmatch(r"— \S+ (\S+)", line)
        if match is None:
            continue
        encoded = _decode_b64(match.group(1), label="checkpoint signature")
        if len(encoded) <= 4 or encoded[:4] != key_id[:4]:
            continue
        _verify_rekor_signature(
            public_key,
            encoded[4:],
            note.encode("utf-8"),
            label="checkpoint",
        )
        return
    raise BundleVerificationError("Sigstore checkpoint has no valid Rekor signature")


def _verify_transparency_entry(
    *,
    entry: Mapping[str, Any],
    integrated_time: int,
    key_id: bytes,
    canonicalized_body: bytes,
    rekor_keys: Mapping[bytes, Any],
) -> None:
    public_key = rekor_keys.get(key_id)
    if public_key is None:
        raise BundleVerificationError("Sigstore transparency log key is not trusted")
    log_index = _transparency_int(entry, "logIndex")
    inclusion = entry["inclusionPromise"]
    signature = _decode_b64(
        inclusion.get("signedEntryTimestamp"),
        label="signed entry timestamp",
    )
    _verify_rekor_signature(
        public_key,
        signature,
        _canonical_transparency_entry(
            body=canonicalized_body,
            integrated_time=integrated_time,
            key_id=key_id,
            log_index=log_index,
        ),
        label="signed entry timestamp",
    )
    _verify_merkle_inclusion(entry=entry, canonicalized_body=canonicalized_body)
    proof = entry["inclusionProof"]
    root_hash = _decode_b64(proof["rootHash"], label="inclusion root hash")
    _verify_checkpoint(
        entry=entry,
        key_id=key_id,
        root_hash=root_hash,
        public_key=public_key,
    )


def _verify_dsse_transparency_body(
    *,
    body: bytes,
    statement_bytes: bytes,
    envelope: Mapping[str, Any],
    certificate: "x509.Certificate",
) -> None:
    try:
        record = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleVerificationError("Sigstore transparency body is not JSON") from error
    if not isinstance(record, Mapping):
        raise BundleVerificationError("Sigstore transparency body is not an object")
    if record.get("kind") == "dsse" and isinstance(record.get("spec"), Mapping):
        content = record["spec"]
        payload_hash_record = content.get("payloadHash")
        signature_records = content.get("signatures")
    elif isinstance(record.get("payloadHash"), Mapping):
        payload_hash_record = record["payloadHash"]
        signature_records = record.get("signatures")
    else:
        content = record.get("content")
        if not isinstance(content, Mapping):
            raise BundleVerificationError("Sigstore transparency DSSE content is missing")
        nested = content.get("envelope")
        if not isinstance(nested, str):
            raise BundleVerificationError("Sigstore transparency DSSE envelope is missing")
        try:
            nested_envelope = json.loads(nested)
        except json.JSONDecodeError as error:
            raise BundleVerificationError(
                "Sigstore transparency DSSE envelope is invalid"
            ) from error
        if not isinstance(nested_envelope, Mapping):
            raise BundleVerificationError("Sigstore transparency DSSE envelope is malformed")
        if (
            nested_envelope.get("payload") != envelope.get("payload")
            or nested_envelope.get("payloadType") != envelope.get("payloadType")
            or nested_envelope.get("signatures") != envelope.get("signatures")
        ):
            raise BundleVerificationError("Sigstore transparency DSSE envelope does not match")
        payload_hash_record = content.get("payloadHash")
        signature_records = None
    if not isinstance(payload_hash_record, Mapping) or payload_hash_record.get(
        "algorithm"
    ) != "sha256":
        raise BundleVerificationError("Sigstore transparency payload hash is not SHA-256")
    if payload_hash_record.get("value") != hashlib.sha256(statement_bytes).hexdigest():
        raise BundleVerificationError("Sigstore transparency payload hash does not match")
    if signature_records is not None:
        if not isinstance(signature_records, list):
            raise BundleVerificationError("Sigstore transparency signatures are missing")
        from cryptography.hazmat.primitives.serialization import Encoding

        expected_verifier = base64.b64encode(
            certificate.public_bytes(Encoding.PEM)
        ).decode("ascii")
        expected = {
            (str(signature.get("sig")), expected_verifier)
            for signature in envelope.get("signatures", [])
            if isinstance(signature, Mapping)
        }
        actual = {
            (str(signature.get("signature")), str(signature.get("verifier")))
            for signature in signature_records
            if isinstance(signature, Mapping)
        }
        if expected != actual:
            raise BundleVerificationError("Sigstore transparency signatures do not match")


def _dsse_pae(payload_type: bytes, payload: bytes) -> bytes:
    return (
        b"DSSEv1 "
        + str(len(payload_type)).encode("ascii")
        + b" "
        + payload_type
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def _verify_dsse_bundle(
    *,
    bundle: Mapping[str, Any],
    payload: bytes,
    certificate: "x509.Certificate",
    chain: Sequence["x509.Certificate"],
    roots: Sequence["x509.Certificate"],
    expected_issuer: str,
    expected_identity: str,
    integrated_time: int,
    transparency_entry: Mapping[str, Any],
    transparency_key_id: bytes,
    canonicalized_body: bytes,
    rekor_keys: Mapping[bytes, Any],
) -> None:
    envelope = bundle.get("dsseEnvelope")
    if not isinstance(envelope, Mapping):
        raise BundleVerificationError("Sigstore DSSE envelope is missing")
    payload_type = envelope.get("payloadType")
    if payload_type != "application/vnd.in-toto+json":
        raise BundleVerificationError("Sigstore DSSE payload type is not in-toto JSON")
    encoded_payload = envelope.get("payload")
    if not isinstance(encoded_payload, str):
        raise BundleVerificationError("Sigstore DSSE payload is missing")
    try:
        statement_bytes = base64.b64decode(encoded_payload, validate=True)
        statement = json.loads(statement_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleVerificationError("Sigstore DSSE payload is invalid") from error
    if not isinstance(statement, Mapping):
        raise BundleVerificationError("Sigstore DSSE statement is not an object")
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or len(subjects) != 1:
        raise BundleVerificationError("Sigstore DSSE subject must contain one manifest")
    subject_digest = subjects[0].get("digest") if isinstance(subjects[0], Mapping) else None
    if not isinstance(subject_digest, Mapping) or subject_digest.get("sha256") != hashlib.sha256(payload).hexdigest():
        raise BundleVerificationError("Sigstore DSSE subject does not bind the manifest")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleVerificationError("signed manifest is not JSON") from error
    if statement.get("predicate") != manifest:
        raise BundleVerificationError("Sigstore DSSE predicate does not contain the manifest")
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != 1:
        raise BundleVerificationError("Sigstore DSSE must contain one signature")
    signature_value = signatures[0].get("sig") if isinstance(signatures[0], Mapping) else None
    signature = _decode_signature(signature_value)
    _verify_transparency_entry(
        entry=transparency_entry,
        integrated_time=integrated_time,
        key_id=transparency_key_id,
        canonicalized_body=canonicalized_body,
        rekor_keys=rekor_keys,
    )
    _verify_chain(
        certificate,
        chain,
        roots,
        verified_at=datetime.fromtimestamp(integrated_time, tz=UTC),
    )
    _verify_identity(certificate, expected_issuer, expected_identity)
    _public_key_verify(
        certificate,
        signature,
        _dsse_pae(payload_type.encode("utf-8"), statement_bytes),
    )
    _verify_dsse_transparency_body(
        body=canonicalized_body,
        statement_bytes=statement_bytes,
        envelope=envelope,
        certificate=certificate,
    )


def _verify_message_signature_bundle(
    *,
    bundle: Mapping[str, Any],
    payload: bytes,
    certificate: "x509.Certificate",
    chain: Sequence["x509.Certificate"],
    roots: Sequence["x509.Certificate"],
    expected_issuer: str,
    expected_identity: str,
    integrated_time: int,
    transparency_entry: Mapping[str, Any],
    transparency_key_id: bytes,
    canonicalized_body: bytes,
    rekor_keys: Mapping[bytes, Any],
) -> None:
    message = bundle.get("messageSignature")
    if not isinstance(message, Mapping):
        raise BundleVerificationError("Sigstore message signature is missing")
    digest = message.get("messageDigest")
    if not isinstance(digest, Mapping) or digest.get("algorithm") != "SHA2_256":
        raise BundleVerificationError("Sigstore message digest must use SHA2_256")
    try:
        expected_digest = base64.b64decode(str(digest.get("digest")), validate=True)
    except (ValueError, binascii.Error) as error:
        raise BundleVerificationError("Sigstore message digest is invalid") from error
    if expected_digest != hashlib.sha256(payload).digest():
        raise BundleVerificationError("Sigstore message digest does not bind the manifest")
    _verify_transparency_entry(
        entry=transparency_entry,
        integrated_time=integrated_time,
        key_id=transparency_key_id,
        canonicalized_body=canonicalized_body,
        rekor_keys=rekor_keys,
    )
    _verify_chain(
        certificate,
        chain,
        roots,
        verified_at=datetime.fromtimestamp(integrated_time, tz=UTC),
    )
    _verify_identity(certificate, expected_issuer, expected_identity)
    _public_key_verify(certificate, _decode_signature(message.get("signature")), payload)

class SigstoreCertificateVerifier:
    """Default verifier: genuine chain, identity, signature, and log verification."""

    def __init__(
        self,
        *,
        trust_roots: Sequence["x509.Certificate"],
        rekor_public_keys: Mapping[bytes, Any],
    ) -> None:
        if not trust_roots:
            raise ReleaseVerifyError("at least one Sigstore trust root is required")
        if not rekor_public_keys:
            raise ReleaseVerifyError("at least one trusted Rekor public key is required")
        self._roots = tuple(trust_roots)
        self._rekor_keys = dict(rekor_public_keys)

    def verify(
        self,
        *,
        payload: bytes,
        bundle: Mapping[str, Any],
        expected_issuer: str,
        expected_identity: str,
    ) -> None:
        if not isinstance(bundle, Mapping):
            raise BundleVerificationError("release bundle must be an object")
        certificate, chain, material = _standard_bundle_material(bundle)
        if "dsseEnvelope" in bundle:
            entry, integrated_time, key_id, canonicalized_body = (
                _require_transparency_material(
                    material,
                    expected_kinds=frozenset({"dsse", "intoto"}),
                )
            )
            _verify_dsse_bundle(
                bundle=bundle,
                payload=payload,
                certificate=certificate,
                chain=chain,
                roots=self._roots,
                expected_issuer=expected_issuer,
                expected_identity=expected_identity,
                integrated_time=integrated_time,
                transparency_entry=entry,
                transparency_key_id=key_id,
                canonicalized_body=canonicalized_body,
                rekor_keys=self._rekor_keys,
            )
            return
        if "messageSignature" in bundle:
            entry, integrated_time, key_id, canonicalized_body = (
                _require_transparency_material(
                    material,
                    expected_kinds=frozenset({"hashedrekord", "hashrekord"}),
                )
            )
            _verify_message_signature_bundle(
                bundle=bundle,
                payload=payload,
                certificate=certificate,
                chain=chain,
                roots=self._roots,
                expected_issuer=expected_issuer,
                expected_identity=expected_identity,
                integrated_time=integrated_time,
                transparency_entry=entry,
                transparency_key_id=key_id,
                canonicalized_body=canonicalized_body,
                rekor_keys=self._rekor_keys,
            )
            return
        raise BundleVerificationError(
            "release bundle must contain a standard Sigstore DSSE envelope"
        )


def load_trust_roots(path: str | Path) -> list["x509.Certificate"]:
    from cryptography import x509

    data = Path(path).expanduser().read_bytes()
    roots = x509.load_pem_x509_certificates(data)
    if not roots:
        raise ReleaseVerifyError("Sigstore trust root bundle is empty")
    return roots


def default_release_verifier(
    trust_roots_path: str | Path | None = None,
    rekor_key_path: str | Path | None = None,
) -> SigstoreCertificateVerifier:
    """Build the production verifier from operator-pinned Sigstore roots."""
    raw_roots = str(
        trust_roots_path or os.environ.get("CITADEL_RELEASE_TRUST_ROOTS", "")
    ).strip()
    if not raw_roots:
        raise ReleaseVerifyError(
            "set CITADEL_RELEASE_TRUST_ROOTS to the Sigstore Fulcio root PEM bundle"
        )
    raw_rekor = str(
        rekor_key_path or os.environ.get("CITADEL_RELEASE_REKOR_PUBLIC_KEY", "")
    ).strip()
    if not raw_rekor:
        raise ReleaseVerifyError(
            "set CITADEL_RELEASE_REKOR_PUBLIC_KEY to the Rekor public key PEM"
        )
    return SigstoreCertificateVerifier(
        trust_roots=load_trust_roots(raw_roots),
        rekor_public_keys=load_rekor_public_keys(raw_rekor),
    )


def verify_release_files(
    *,
    manifest_path: str | Path,
    bundle_path: str | Path,
    platform: str,
    trust_roots_path: str | Path | None = None,
    rekor_key_path: str | Path | None = None,
    expected_version: str | None = None,
    verifier: BundleVerifier | None = None,
) -> VerifiedRelease:
    """Read manifest and bundle from disk and verify them. Fails closed."""
    manifest_bytes = Path(manifest_path).expanduser().read_bytes()
    try:
        bundle = json.loads(Path(bundle_path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseVerifyError("release bundle is not readable JSON") from error
    if not isinstance(bundle, dict):
        raise ReleaseVerifyError("release bundle must be a JSON object")
    active = verifier or default_release_verifier(trust_roots_path, rekor_key_path)
    return verify_release(
        manifest_bytes=manifest_bytes,
        bundle=bundle,
        platform=platform,
        verifier=active,
        expected_version=expected_version,
    )
