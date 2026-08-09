from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

from kb.config import CitadelConfig
from kb import notification_gateways as ng
from kb import webhook_gateway as wg
from kb.webhook_gateway import (
    WebhookConfigError,
    WebhookDelivery,
    require_webhook_url,
)


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """Resolve every test hostname to a public address.

    require_webhook_url now RESOLVES, so without this the suite would depend on
    live DNS. IP-literal cases bypass this path and still exercise the real
    check.
    """
    monkeypatch.setattr(
        wg.socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
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
    monkeypatch.setattr(
        "kb.webhook_gateway.urllib.request.build_opener",
        lambda *handlers: type("O", (), {"open": staticmethod(fake_open)})(),
    )

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
    monkeypatch.setattr(
        "kb.webhook_gateway.urllib.request.build_opener",
        lambda *handlers: type("O", (), {"open": staticmethod(fake_open)})(),
    )

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
    monkeypatch.setattr(
        "kb.webhook_gateway.urllib.request.build_opener",
        lambda *handlers: type("O", (), {"open": staticmethod(fake_open)})(),
    )

    result = gateway.post_digest("body")

    assert result["ok"] is False
    assert result["error_type"] == "TimeoutError"


def test_long_digests_are_truncated_and_say_so(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_open(request: Any, timeout: float) -> _Response:
        captured["body"] = json.loads(request.data.decode("utf-8"))["text"]
        return _Response(200)

    gateway = WebhookDelivery(url=GOOD, max_message_bytes=1000)
    monkeypatch.setattr(
        "kb.webhook_gateway.urllib.request.build_opener",
        lambda *handlers: type("O", (), {"open": staticmethod(fake_open)})(),
    )

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

    assert "webhook" in ng.configured_gateways(config)


def test_registry_omits_an_unconfigured_webhook() -> None:
    assert "webhook" not in ng.configured_gateways(CitadelConfig())


def test_a_bad_url_disables_the_gateway_instead_of_crashing_boot() -> None:
    """A misconfigured connector must not take the service down at startup."""
    config = CitadelConfig(webhook_enabled=True, webhook_url="http://169.254.169.254/x")

    assert ng.configured_gateways(config) == {}


# --- findings from an adversarial review, each pinned -----------------------


def test_the_validated_ip_is_the_one_connected_to(monkeypatch) -> None:
    """DNS-rebinding TOCTOU: checking a NAME then letting urllib resolve it
    again lets an attacker answer public for the check and private for the
    connect. The validated address must be the address dialled."""
    gateway = WebhookDelivery(url=GOOD)

    assert gateway.pinned_ip == "93.184.216.34"

    handlers: list[Any] = []
    monkeypatch.setattr(
        "kb.webhook_gateway.urllib.request.build_opener",
        lambda *hs: (
            handlers.extend(hs)
            or type("O", (), {"open": staticmethod(lambda r, timeout: _Response(200))})()
        ),
    )
    gateway.post_digest("body")

    pinned = [h for h in handlers if isinstance(h, wg._PinnedHTTPSHandler)]
    assert pinned, "no pinned-IP handler installed"
    assert pinned[0]._pinned_ip == "93.184.216.34"


def test_a_host_with_any_private_answer_is_refused(monkeypatch) -> None:
    """Accepting the first public answer lets a mixed-record host steer the
    connection to the private one."""
    monkeypatch.setattr(
        wg.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("10.0.0.7", 0)),
        ],
    )
    with pytest.raises(WebhookConfigError, match="private or loopback"):
        require_webhook_url("https://rebind.example.com/hook")


def test_a_trailing_dot_cannot_dodge_the_suffix_blocklist() -> None:
    """`foo.railway.internal.` is the same host with a different string."""
    with pytest.raises(WebhookConfigError, match="internal host"):
        require_webhook_url("https://citadel-archive.railway.internal./hook")


def test_an_unresolvable_host_is_refused_not_allowed(monkeypatch) -> None:
    """Failing open on a resolution error would make DNS downtime a bypass."""

    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("no such host")

    monkeypatch.setattr(wg.socket, "getaddrinfo", boom)
    with pytest.raises(WebhookConfigError, match="does not resolve"):
        require_webhook_url("https://nope.example.com/hook")


def test_no_redirect_handler_actually_refuses(monkeypatch) -> None:
    """Exercise _NoRedirect ITSELF, not post_digest's except-clause.

    The original test patched the opener to raise, so it would have passed with
    _NoRedirect deleted entirely.
    """
    handler = wg._NoRedirect()

    with pytest.raises(WebhookConfigError, match="redirect"):
        handler.redirect_request(
            urllib.request.Request(GOOD), None, 302, "Found", {}, "https://elsewhere.example/"
        )


def test_post_digest_never_raises_on_unencodable_text() -> None:
    """Payload construction used to sit OUTSIDE the try: a lone surrogate raises
    on .encode('utf-8'), which any upstream using errors='surrogateescape' can
    produce."""
    gateway = WebhookDelivery(url=GOOD)

    result = gateway.post_digest("bad \udcff text")

    assert result["ok"] is False
    assert result["sent"] is False
    assert "error_type" in result


def test_post_digest_never_raises_on_non_string_text() -> None:
    gateway = WebhookDelivery(url=GOOD)

    result = gateway.post_digest(None)  # type: ignore[arg-type]

    assert result["ok"] is False
    assert result["error_type"] == "TypeError"


def test_one_broken_provider_does_not_unregister_the_others(monkeypatch) -> None:
    """GoogleChatDelivery.__init__ raises on a space name missing `spaces/`, so
    without isolation a single mistyped env var would also drop a perfectly good
    webhook and take LearningAgent.__init__ with it."""

    def explode(config: Any) -> Any:
        raise ValueError("CITADEL_GOOGLE_CHAT_SPACE_NAME must look like spaces/...")

    monkeypatch.setattr(ng.GoogleChatDelivery, "from_config", staticmethod(explode))
    config = CitadelConfig(webhook_enabled=True, webhook_url=GOOD)

    gateways = ng.configured_gateways(config)

    assert "webhook" in gateways
    assert "google_chat" not in gateways
