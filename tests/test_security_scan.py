from __future__ import annotations

import json

from kb.security_scan import (
    SecurityScanEntry,
    redact_secrets,
    scan_text_entries,
)

GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
FINE_GRAINED_TOKEN = "github_pat_" + "a1B2" * 12
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
SLACK_TOKEN = "-".join(["xoxb", "1234567890", "abcdefghijklmnop"])
STRIPE_KEY = "sk_live_abcdefghijklmnop1234"
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----"
GENERIC_ASSIGNMENT = 'password = "hunter2-super-secret"'


def entry(text: str, *, location: str = "masumi-network/agent") -> SecurityScanEntry:
    return SecurityScanEntry(source="commit", location=location, text=text)


def scan(text: str) -> dict[str, object]:
    return scan_text_entries([entry(text)])


def categories(result: dict[str, object]) -> set[str]:
    return {finding["category"] for finding in result["findings"]}  # type: ignore[index]


def test_github_token_pattern_blocks_at_critical() -> None:
    result = scan(f"deploy with {GITHUB_TOKEN} now")

    assert result["blocked"] is True
    assert result["highest_severity"] == "critical"
    assert "github_token" in categories(result)


def test_citadel_access_token_is_blocked() -> None:
    token = "ctdl_" + "aB3" * 12
    result = scan(f"paste {token} here")

    assert result["blocked"] is True
    assert "citadel_access_token" in categories(result)


def test_database_connection_url_is_blocked() -> None:
    url = "postgresql://user:secret@db.example.com:5432/app"
    result = scan(f"DATABASE_URL={url}")

    assert result["blocked"] is True
    assert "database_connection_url" in categories(result)


def test_fine_grained_github_token_is_detected() -> None:
    assert "github_fine_grained_token" in categories(scan(f"use {FINE_GRAINED_TOKEN}"))


def test_aws_access_key_is_detected() -> None:
    assert "aws_access_key" in categories(scan(f"creds {AWS_KEY} leaked"))


def test_slack_token_is_detected() -> None:
    assert "slack_token" in categories(scan(f"bot uses {SLACK_TOKEN}"))


def test_stripe_live_secret_is_detected() -> None:
    assert "stripe_live_secret" in categories(scan(f"billing key {STRIPE_KEY}"))


def test_private_key_marker_is_detected() -> None:
    assert "private_key_marker" in categories(scan(f"{PRIVATE_KEY}\nMIIE..."))


def test_generic_secret_assignment_is_detected_at_high() -> None:
    result = scan(GENERIC_ASSIGNMENT)

    assert result["blocked"] is True
    assert "secret_assignment" in categories(result)


def test_findings_never_contain_the_raw_secret() -> None:
    for secret in (GITHUB_TOKEN, AWS_KEY, SLACK_TOKEN, STRIPE_KEY):
        serialized = json.dumps(scan(f"commit message includes {secret}"))
        assert secret not in serialized
        assert "[REDACTED]" not in serialized  # findings carry pattern evidence, not values


def test_duplicate_findings_are_deduped_by_fingerprint() -> None:
    duplicated = [entry(f"leak {AWS_KEY}"), entry(f"leak {AWS_KEY}")]

    result = scan_text_entries(duplicated)

    assert result["finding_count"] == 1


def test_distinct_locations_produce_distinct_fingerprints() -> None:
    entries = [
        entry(f"leak {AWS_KEY}", location="masumi-network/agent"),
        entry(f"leak {AWS_KEY}", location="masumi-network/registry"),
    ]

    result = scan_text_entries(entries)

    assert result["finding_count"] == 2
    fingerprints = {finding["fingerprint"] for finding in result["findings"]}  # type: ignore[index]
    assert len(fingerprints) == 2


def test_benign_text_produces_no_findings() -> None:
    benign = (
        "Bumped version to 1.2.3, refreshed the README, and linked "
        "https://github.com/masumi-network/agent for context. Tokens of appreciation all around."
    )

    result = scan(benign)

    assert result["ok"] is True
    assert result["blocked"] is False
    assert result["finding_count"] == 0


def test_medium_findings_do_not_block_at_high_threshold() -> None:
    result = scan_text_entries(
        [entry("see https://bit.ly/3xyzabc for details")],
        block_severity="high",
    )

    assert result["blocked"] is False
    assert "url_shortener" in categories(result)


def test_redact_secrets_masks_known_and_pattern_matched_values() -> None:
    message = (
        f"Authorization: Bearer ctdl_abc123token bearer {GITHUB_TOKEN} "
        f'api_key=sk-test password: "p4ssw0rd-value"'
    )

    redacted = redact_secrets(message, "explicit-known-secret")

    assert "ctdl_abc123token" not in redacted
    assert GITHUB_TOKEN not in redacted
    assert "sk-test" not in redacted
    assert "p4ssw0rd-value" not in redacted
    assert "[REDACTED]" in redacted


def test_redact_secrets_replaces_explicitly_known_secrets() -> None:
    assert redact_secrets("body with explicit-value", "explicit-value") == "body with [REDACTED]"


def test_redact_secrets_keeps_benign_text_intact() -> None:
    benign = "GitHub sync finished for masumi-network: 12 repos scanned"

    assert redact_secrets(benign) == benign


# --- unsafe_url_scheme: the `data:` false positive -------------------------
#
# On 2026-07-31 the repo-content sync was silently dropping 9 of 69 files.
# Seven were killed by RISKY_SCHEME_PATTERN matching the bare word `data:`
# inside a fenced JavaScript block. No test covered this rule at all, in
# either direction, which is why it shipped.


def test_javascript_object_key_named_data_is_not_a_url_scheme() -> None:
    """`data:` as a JS/JSON property key must not read as a data URI."""
    doc = (
        "## Test plan\n\n"
        "```ts\n"
        "listAdminTasksMock.mockResolvedValue({\n"
        "  data: [\n"
        '    { id: "task_1" },\n'
        "  ],\n"
        "  meta: { timestamp: now },\n"
        "});\n"
        "```\n"
    )

    result = scan(doc)

    assert "unsafe_url_scheme" not in categories(result)
    assert result["blocked"] is False


def test_prose_mentioning_data_and_javascript_is_not_blocked() -> None:
    doc = "Input data: the user record. Written in javascript: see the appendix."

    assert "unsafe_url_scheme" not in categories(scan(doc))


def test_real_data_uri_is_still_blocked() -> None:
    """The rule must still fire on an actual data URI."""
    result = scan("<img src='data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=='>")

    assert "unsafe_url_scheme" in categories(result)
    assert result["blocked"] is True


def test_degenerate_data_uri_forms_are_still_blocked() -> None:
    assert "unsafe_url_scheme" in categories(scan("data:;base64,PHN2Zz4="))
    assert "unsafe_url_scheme" in categories(scan("data:,Hello%2C%20World"))


def test_javascript_and_vbscript_payloads_are_still_blocked() -> None:
    assert "unsafe_url_scheme" in categories(scan("<a href='javascript:alert(1)'>x</a>"))
    assert "unsafe_url_scheme" in categories(scan("<a href='vbscript:msgbox(1)'>x</a>"))


def test_file_scheme_is_still_blocked() -> None:
    assert "unsafe_url_scheme" in categories(scan("see file:///etc/passwd for details"))


# --- secret_assignment: code that reads a secret is not a secret ------------
#
# The same sync dropped Sokosumi-MCP/docs/DEBUG_CONNECTION.md (5 findings) and
# sokosumi-cli/AGENTS.md (2). Every one was code correctly avoiding a
# hard-coded credential. Each string below is taken verbatim from those files.


def test_env_lookups_are_not_secret_assignments() -> None:
    for snippet in (
        "const apiKey = process.env.SOKOSUMI_API_KEY;",
        "const apiKey = getApiKeyFromEnv();",
        'api_key = os.environ.get("RAILWAY_API_KEY")',
        "api_key = request.query_params.get('api_key')",
        "https://your-app.up.railway.app?api_key=YOUR_KEY",
    ):
        assert "secret_assignment" not in categories(scan(snippet)), snippet


def test_short_and_placeholder_values_are_not_secret_assignments() -> None:
    for snippet in (
        "token = xxxxxxxxxxxx",
        "api_key = <your-key-here>",
        "password = changeme-please",
        "secret = ${VAULT_SECRET}",
        "token = config.auth.token",
    ):
        assert "secret_assignment" not in categories(scan(snippet)), snippet


def test_literal_credentials_are_still_secret_assignments() -> None:
    """Narrowing the rule must not let a real hard-coded credential through."""
    for snippet in (
        GENERIC_ASSIGNMENT,
        # Synthetic throughout. Never paste a real credential into a fixture:
        # it lands in git history, and the value is burned the moment it does.
        'api_key = "abcd1234efgh5678ijkl9012mnop3456"',
        "token: Tok3nLikeAGeneratedValue0123456789",
        # all-alpha but long and mixed case, like a generated DB password
        'password = "QwertyuiopAsdfghjklZxcvbn"',
    ):
        assert "secret_assignment" in categories(scan(snippet)), snippet


def test_documentation_files_that_were_wrongly_blocked_now_pass() -> None:
    """End-to-end shape of the two real files, as one scan."""
    doc = (
        "# Sokosumi CLI - Agent Guidelines\n\n"
        "```ts\n"
        "const apiKey = getApiKeyFromEnv();\n"
        "// Bad - Direct process.env access\n"
        "const apiKey = process.env.SOKOSUMI_API_KEY;\n"
        "```\n\n"
        "```py\n"
        'api_key = os.environ.get("RAILWAY_API_KEY")\n'
        "if api_key:\n"
        '    api_keys["current"] = api_key\n'
        "```\n\n"
        "```ts\n"
        "mockResolvedValue({ data: [] });\n"
        "```\n"
    )

    result = scan(doc)

    assert result["blocked"] is False, result["findings"]


def test_identifier_valued_assignments_in_test_fixtures_are_not_secrets() -> None:
    """`token: "hashed_token"` in a mock is a fixture identifier, not a secret."""
    for snippet in (
        'oauthAccessTokenFindUniqueMock.mockResolvedValue({ token: "hashed_token" })',
        "const secret = refresh_token",
        "api_key = access-token",
    ):
        assert "secret_assignment" not in categories(scan(snippet)), snippet


def test_long_lowercase_passphrases_are_still_secrets() -> None:
    """The identifier carve-out is length-bounded and must not swallow these."""
    assert "secret_assignment" in categories(scan("password=not-a-real-secret-value"))
    assert "secret_assignment" in categories(scan("token = correct-horse-battery-staple"))


def test_high_confidence_patterns_are_untouched_by_the_carve_outs() -> None:
    """Narrowing the generic rule must not weaken the specific detectors."""
    for value, expected in (
        (GITHUB_TOKEN, "github_token"),
        (AWS_KEY, "aws_access_key"),
        (STRIPE_KEY, "stripe_live_secret"),
        (SLACK_TOKEN, "slack_token"),
        (PRIVATE_KEY, "private_key_marker"),
    ):
        result = scan(f"token = {value}")
        assert expected in categories(result), value
        assert result["blocked"] is True
