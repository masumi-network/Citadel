from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import re
from typing import Iterable
from urllib.parse import urlsplit


SEVERITY_ORDER = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

URL_SHORTENER_DOMAINS = {
    "bit.ly",
    "buff.ly",
    "cutt.ly",
    "goo.gl",
    "is.gd",
    "lnkd.in",
    "ow.ly",
    "rebrand.ly",
    "shorturl.at",
    "t.co",
    "tiny.cc",
    "tinyurl.com",
}

SECRET_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "citadel_access_token",
        "critical",
        re.compile(r"\bctdl_[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "database_connection_url",
        "critical",
        re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s'\"`<>]+"
        ),
    ),
    (
        "github_token",
        "critical",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{30,}\b"),
    ),
    (
        "github_fine_grained_token",
        "critical",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    ),
    (
        "openai_or_llm_key",
        "critical",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    ),
    (
        "aws_access_key",
        "critical",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "stripe_live_secret",
        "critical",
        re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b"),
    ),
    (
        "slack_token",
        "critical",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    ),
    (
        "private_key_marker",
        "critical",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "secret_assignment",
        "high",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*['\"]?([^\s'\"]{8,})"
        ),
    ),
)

# Redaction-only patterns either match the bare secret (no groups) or capture the
# non-secret prefix as group(1); the secret value itself is never kept.
REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ctdl_[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(token[\"'\s:=]+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key[\"'\s:=]+)[A-Za-z0-9._~+/=-]+"),
    *(
        pattern
        for category, _, pattern in SECRET_PATTERNS
        if category != "secret_assignment"
    ),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*['\"]?)[^\s'\"]{4,}"
    ),
)

URL_PATTERN = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)

# A dangerous scheme is only dangerous as a URI, so each alternative requires
# what must actually follow the colon.
#
# The previous pattern matched the bare word ``data:`` anywhere, which is a
# JavaScript/JSON property key far more often than a data URI. That blocked
# seven documentation files whose only offence was ``mockResolvedValue({ data:
# [...] })`` inside a fenced code block, and the block was silent, so nobody
# could see it happening. A data URI is required to carry a MIME type (or the
# ``data:,`` / ``data:;base64,`` degenerate forms), and javascript:/vbscript:
# payloads run straight into their code with no space.
RISKY_SCHEME_PATTERN = re.compile(
    r"(?i)"
    r"\b(?:javascript|vbscript):[^\s<>'\"]"
    r"|\bdata:[a-z0-9.+-]+/[a-z0-9.+-]+"
    r"|\bdata:[;,]"
    r"|file://"
)
BIDI_CONTROL_CODES = {
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
}


class SecretContentError(Exception):
    """Raised when a write is blocked because content carries a blocking-severity secret.

    Carries only redacted, safe metadata (severity + finding summaries) — never the raw
    secret nor the original text — so any caller can surface the block without leaking
    sensitive material.
    """

    def __init__(
        self,
        *,
        dataset: str | None,
        highest_severity: str | None,
        block_severity: str,
        findings: list[dict[str, str | None]],
        message: str | None = None,
    ) -> None:
        self.dataset = dataset
        self.highest_severity = highest_severity
        self.block_severity = block_severity
        self.findings = findings
        self.public_message = message or (
            "Content was blocked: it contains a secret or sensitive value "
            f"(severity {highest_severity or block_severity}) and was not stored."
        )
        super().__init__(self.public_message)


def redact_secrets(value: str, *known_secrets: str | None) -> str:
    """Mask secret-looking material so the text is safe for logs and errors."""
    redacted = value
    for secret in known_secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in REDACTION_PATTERNS:
        redacted = pattern.sub(
            lambda match: f"{match.group(1)}[REDACTED]"
            if match.groups()
            else "[REDACTED]",
            redacted,
        )
    return redacted


@dataclass(frozen=True)
class SecurityScanEntry:
    source: str
    location: str
    text: str


@dataclass(frozen=True)
class SecurityScanFinding:
    severity: str
    category: str
    source: str
    location: str
    summary: str
    fingerprint: str
    evidence: str | None = None

    def public_dict(self) -> dict[str, str | None]:
        return {
            "severity": self.severity,
            "category": self.category,
            "source": self.source,
            "location": self.location,
            "summary": self.summary,
            "fingerprint": self.fingerprint,
            "evidence": self.evidence,
        }


def scan_text_entries(
    entries: Iterable[SecurityScanEntry],
    *,
    block_severity: str = "high",
) -> dict[str, object]:
    findings: list[SecurityScanFinding] = []
    seen: set[str] = set()
    threshold = _severity_score(block_severity)

    for entry in entries:
        for finding in _scan_entry(entry):
            if finding.fingerprint in seen:
                continue
            seen.add(finding.fingerprint)
            findings.append(finding)

    findings.sort(
        key=lambda item: (_severity_score(item.severity), item.category, item.location),
        reverse=True,
    )
    highest = findings[0].severity if findings else None
    blocked = any(_severity_score(finding.severity) >= threshold for finding in findings)
    return {
        "ok": not blocked,
        "blocked": blocked,
        "block_severity": _normal_severity(block_severity),
        "highest_severity": highest,
        "finding_count": len(findings),
        "findings": [finding.public_dict() for finding in findings[:25]],
    }


def _scan_entry(entry: SecurityScanEntry) -> list[SecurityScanFinding]:
    text = entry.text or ""
    findings: list[SecurityScanFinding] = []
    findings.extend(_scan_secrets(entry, text))
    findings.extend(_scan_urls(entry, text))
    findings.extend(_scan_corruption_markers(entry, text))
    return findings


# Right-hand sides of a `token = ...` assignment that are code or placeholders
# rather than a literal credential. Every one of these was observed blocking a
# real documentation file: `process.env.SOKOSUMI_API_KEY`, `getApiKeyFromEnv()`,
# `os.environ.get("RAILWAY_API_KEY")`, `request.query_params.get('api_key')` and
# `YOUR_KEY`. None of them is a secret; all of them are how you correctly avoid
# hard-coding one.
_CODE_OR_PLACEHOLDER_VALUE = re.compile(
    r"""(?ix)
    ^(?:
        process\.env\b
      | import\.meta\.env\b
      | os\.environ\b
      | os\.getenv\b
      | deno\.env\b
      | system\.getenv\b
      | getenv\b
      | (?:your|my|the|some|a)[_-]
      | x{3,}
      | \.{3}
      | \*{3,}
      | changeme
      | example
      | placeholder
      | redacted
      | <
      | \$
      | \{
      | %
    )
    """
)
# A dotted identifier chain (`process.env.SOKOSUMI_API_KEY;`, `config.api.token`)
# is a reference to a secret, never the secret itself.
_IDENTIFIER_CHAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+[;,]?$")
# A bare SCREAMING_SNAKE name is an env-var placeholder, not a credential.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}[;,]?$")
# Lowercase words joined by _ or -, with no digits: `hashed_token`, `refresh_token`.
# These are identifiers and test fixtures, not credentials.
_WORDY_IDENTIFIER = re.compile(r"^[a-z]+(?:[_-][a-z]+)*$")
# Below this length a wordy all-lowercase value is treated as an identifier.
# Above it, it is more plausibly a passphrase and stays flagged, which is what
# keeps `not-a-real-secret-value` (23 chars) caught.
_WORDY_IDENTIFIER_MAX = 20


def _is_credential_like(value: str) -> bool:
    """Does this assignment's right-hand side look like a literal secret?

    The `secret_assignment` rule fires on any `token|secret|api_key` assignment
    with 8+ non-space characters after it, which also matches ordinary code that
    reads a credential from the environment. Every false positive observed on
    real files was a code expression or a placeholder, so those are what this
    excludes.

    It deliberately does NOT judge how random the value looks. An earlier
    attempt here required a digit, or length plus mixed case, and that let
    `password=not-a-real-secret-value` through — 23 lowercase characters is a
    perfectly ordinary passphrase. Entropy is not the difference between a
    secret and `process.env.API_KEY`; being a literal rather than an expression
    is. The length floor stays at the rule's original 8 so nothing that used to
    be caught stops being caught for a new reason.
    """
    candidate = value.strip().strip("'\"`").rstrip(";,")
    if len(candidate) < 8:
        return False
    # Function calls, indexing and interpolation are code, not literals.
    if any(char in candidate for char in "()[]{}<>$`"):
        return False
    if _CODE_OR_PLACEHOLDER_VALUE.match(candidate):
        return False
    if _IDENTIFIER_CHAIN.match(candidate):
        return False
    if _ENV_NAME.match(candidate):
        return False
    if len(candidate) < _WORDY_IDENTIFIER_MAX and _WORDY_IDENTIFIER.match(candidate):
        # `token: "hashed_token"` in a test mock. The trade-off is explicit: a
        # short all-lowercase passphrase with no digits would now be missed by
        # THIS generic heuristic. Every high-confidence pattern (github_token,
        # aws_access_key, stripe_live_secret, slack_token, private_key_marker,
        # citadel token) is unaffected, and real generated credentials carry
        # digits or mixed case and so never reach this branch.
        return False
    return True


def _scan_secrets(entry: SecurityScanEntry, text: str) -> list[SecurityScanFinding]:
    findings: list[SecurityScanFinding] = []
    for category, severity, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if category == "secret_assignment" and not _is_credential_like(match.group(1)):
                continue
            findings.append(
                _finding(
                    severity=severity,
                    category=category,
                    source=entry.source,
                    location=entry.location,
                    summary="Potential secret found in GitHub metadata; raw value redacted.",
                    evidence=f"pattern={category}",
                    raw=f"{category}:{match.group(0)}",
                )
            )
    return findings


def _scan_urls(entry: SecurityScanEntry, text: str) -> list[SecurityScanFinding]:
    findings: list[SecurityScanFinding] = []
    if RISKY_SCHEME_PATTERN.search(text):
        findings.append(
            _finding(
                severity="high",
                category="unsafe_url_scheme",
                source=entry.source,
                location=entry.location,
                summary="Potentially unsafe URL scheme found in GitHub metadata.",
                evidence="scheme=unsafe",
                raw=f"unsafe-scheme:{text}",
            )
        )

    for match in URL_PATTERN.finditer(text):
        url = _strip_url(match.group(0))
        url_findings = _classify_url(url)
        for category, severity, evidence, raw in url_findings:
            findings.append(
                _finding(
                    severity=severity,
                    category=category,
                    source=entry.source,
                    location=entry.location,
                    summary="Potentially risky URL found in GitHub metadata.",
                    evidence=evidence,
                    raw=raw,
                )
            )
    return findings


def _scan_corruption_markers(entry: SecurityScanEntry, text: str) -> list[SecurityScanFinding]:
    findings: list[SecurityScanFinding] = []
    for char in text:
        if char in BIDI_CONTROL_CODES:
            findings.append(
                _finding(
                    severity="high",
                    category="unicode_bidi_control",
                    source=entry.source,
                    location=entry.location,
                    summary="Bidirectional control character found in GitHub metadata.",
                    evidence=f"codepoint=U+{ord(char):04X}",
                    raw=f"bidi:{ord(char):04X}:{entry.location}",
                )
            )
            break
        if char == "\ufffd":
            findings.append(
                _finding(
                    severity="medium",
                    category="unicode_replacement_character",
                    source=entry.source,
                    location=entry.location,
                    summary="Unicode replacement character suggests malformed or corrupted text.",
                    evidence="codepoint=U+FFFD",
                    raw=f"replacement:{entry.location}",
                )
            )
            break
        if ord(char) < 32 and char not in {"\n", "\r", "\t"}:
            findings.append(
                _finding(
                    severity="high",
                    category="control_character",
                    source=entry.source,
                    location=entry.location,
                    summary="Unexpected control character found in GitHub metadata.",
                    evidence=f"codepoint=U+{ord(char):04X}",
                    raw=f"control:{ord(char):04X}:{entry.location}",
                )
            )
            break
    return findings


def _classify_url(url: str) -> list[tuple[str, str, str, str]]:
    findings: list[tuple[str, str, str, str]] = []
    try:
        parts = urlsplit(url)
    except ValueError:
        return [("malformed_url", "medium", "url=malformed", f"malformed:{url}")]

    host = (parts.hostname or "").lower()
    if not host:
        return [("malformed_url", "medium", "host=missing", f"missing-host:{url}")]

    if parts.username or parts.password or "@" in parts.netloc:
        findings.append(
            (
                "credentialed_or_misleading_url",
                "high",
                f"domain={host}",
                f"credentialed:{host}:{parts.path}",
            )
        )
    if "xn--" in host:
        findings.append(("punycode_domain", "medium", f"domain={host}", f"punycode:{host}"))
    if host in URL_SHORTENER_DOMAINS:
        findings.append(("url_shortener", "medium", f"domain={host}", f"shortener:{host}"))
    if _is_ip_literal(host):
        findings.append(("ip_literal_url", "medium", "host=ip_literal", f"ip:{host}"))
    if len(url) > 2048:
        findings.append(("oversized_url", "medium", f"domain={host}", f"oversized:{host}"))
    return findings


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _strip_url(url: str) -> str:
    return url.rstrip(".,;:!?)]}")


def _finding(
    *,
    severity: str,
    category: str,
    source: str,
    location: str,
    summary: str,
    evidence: str | None,
    raw: str,
) -> SecurityScanFinding:
    return SecurityScanFinding(
        severity=_normal_severity(severity),
        category=category,
        source=source,
        location=_short_text(location, 140),
        summary=summary,
        evidence=evidence,
        fingerprint=_fingerprint(category, source, location, raw),
    )


def _fingerprint(category: str, source: str, location: str, raw: str) -> str:
    digest = hashlib.sha256(f"{category}|{source}|{location}|{raw}".encode("utf-8")).hexdigest()
    return digest[:24]


def _normal_severity(value: str) -> str:
    text = (value or "").strip().lower()
    return text if text in SEVERITY_ORDER else "high"


def _severity_score(value: str | None) -> int:
    return SEVERITY_ORDER.get(_normal_severity(value or "high"), SEVERITY_ORDER["high"])


def _short_text(value: str, length: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= length:
        return text
    return f"{text[: length - 1]}."
