from __future__ import annotations

import json
from typing import Any

import pytest

from kb.config import CitadelConfig
from kb.notification_gateways import configured_gateways
from kb.webhook_gateway import (
    WebhookConfigError,
    WebhookDelivery,
    require_webhook_url,
)

GOOD = "https://hooks.example.com/services/T000/B000/xxxx"


# --- URL policy -------------------------------------------------------------
#
# These are the reason this adapter does not reuse kb/secure_http.py. That
# module permits http:// to loopback and *.railway.internal and FOLLOWS
# redirects, both correct for a credential going to a known operator-configured
# endpoint. On a webhook the payload is the sensitive thing, so the rules invert.


def test_https_is_required() -> None:
    with pytest.raises(WebhookConfigError, match="must be https"):
        require_webhook_url("http://hooks.example.com/x")


def test_loopback_is_refused_even_over_https() -> None:
    """secure_http ALLOWS http to localhost. A webhook must not."""
    for url in (
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
    ):
        with pytest.raises(WebhookConfigError):
            require_webhook_url(url)


def test_railway_private_network_is_refused() -> None:
    """Also explicitly allowed by secure_http, and wrong here."""
    with pytest.raises(WebhookConfigError, match="internal host"):
        require_webhook_url("https://citadel-archive.railway.internal/hook")


def test_private_and_link_local_addresses_are_refused() -> None:
    for host in ("10.0.0.5", "192.168.1.10", "172.16.0.1", "169.254.169.254"):
        with pytest.raises(WebhookConfigError, match="private or loopback"):
            require_webhook_url(f"https://{host}/hook")


def test_cloud_metadata_address_is_refused() -> None:
    """The canonical SSRF target gets its own test so it can never regress."""
    with pytest.raises(WebhookConfigError):
        require_webhook_url("https://169.254.169.254/latest/meta-data/")


def test_a_normal_public_https_endpoint_is_accepted() -> None:
    require_webhook_url(GOOD)  # must not raise


# --- delivery ---------------------------------------------------------------


class _Response:
    def __init__(self, status: int = 204) -> None:
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def test_post_digest_sends_json_and_reports_status(monkeypatch) -> None:
    sent: dict[str, Any] = {}

    def fake_open(request: Any, timeout: float) -> _Response:
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data.decode("utf-8"))
        sent["auth"] = request.get_header("Authorization")
        return _Response(204)

    gateway = WebhookDelivery(url=GOOD, token="s3cret-token-value")
    monkeypatch.setattr("kb.webhook_gateway._OPENER.open", fake_open)

    result = gateway.post_digest("daily digest body", message_id="m-1")

    assert result["ok"] is True
    assert result["sent"] is True
    assert result["status_code"] == 204
    assert sent["body"] == {"text": "daily digest body", "message_id": "m-1"}
    assert sent["auth"] == "Bearer s3cret-token-value"


def test_a_redirect_is_refused_rather_than_followed(monkeypatch) -> None:
    """secure_http follows redirects with credentials stripped. Here the digest
    body itself must not reach an unintended origin, so there is nothing safe to
    strip and the request is abandoned."""

    def fake_open(request: Any, timeout: float) -> _Response:
        raise WebhookConfigError("webhook endpoint attempted a 302 redirect")

    gateway = WebhookDelivery(url=GOOD)
    monkeypatch.setattr("kb.webhook_gateway._OPENER.open", fake_open)

    result = gateway.post_digest("body")

    assert result["ok"] is False
    assert result["sent"] is False
    assert result["status_category"] == "redirect_refused"


def test_delivery_failure_is_best_effort_and_never_raises(monkeypatch) -> None:
    """ADR-0002's best-effort constraint survives its withdrawal: a delivery
    failure must not fail the digest run."""

    def fake_open(request: Any, timeout: float) -> _Response:
        raise TimeoutError("upstream slow")

    gateway = WebhookDelivery(url=GOOD)
    monkeypatch.setattr("kb.webhook_gateway._OPENER.open", fake_open)

    result = gateway.post_digest("body")

    assert result["ok"] is False
    assert result["error_type"] == "TimeoutError"


def test_long_digests_are_truncated_and_say_so(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_open(request: Any, timeout: float) -> _Response:
        captured["body"] = json.loads(request.data.decode("utf-8"))["text"]
        return _Response(200)

    gateway = WebhookDelivery(url=GOOD, max_message_bytes=1000)
    monkeypatch.setattr("kb.webhook_gateway._OPENER.open", fake_open)

    result = gateway.post_digest("x" * 5000)

    assert len(captured["body"]) == 1000
    assert result["truncated"] is True


def test_status_never_leaks_the_url_or_token() -> None:
    """A webhook path is a bearer capability — anyone holding it can post."""
    status = WebhookDelivery(url=GOOD, token="s3cret-token-value").status()

    blob = json.dumps(status)
    assert "s3cret-token-value" not in blob
    assert "T000/B000/xxxx" not in blob
    assert status["host"] == "hooks.example.com"
    assert status["authenticated"] is True


# --- registration -----------------------------------------------------------


def test_registry_picks_up_a_configured_webhook() -> None:
    config = CitadelConfig(webhook_enabled=True, webhook_url=GOOD)

    assert "webhook" in configured_gateways(config)


def test_registry_omits_an_unconfigured_webhook() -> None:
    assert "webhook" not in configured_gateways(CitadelConfig())


def test_a_bad_url_disables_the_gateway_instead_of_crashing_boot() -> None:
    """A misconfigured connector must not take the service down at startup."""
    config = CitadelConfig(webhook_enabled=True, webhook_url="http://169.254.169.254/x")

    assert configured_gateways(config) == {}
