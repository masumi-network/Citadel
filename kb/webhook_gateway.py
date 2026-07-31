"""Generic outbound webhook delivery for Organization Update Digests.

One adapter that posts to any HTTPS endpoint, which covers Discord, Mattermost,
n8n, Zapier and anything bespoke without writing a provider adapter each.

**Why this does not use ``kb/secure_http.py``.** That module's rules are tuned
for a *credential* travelling to a *known, operator-configured* endpoint
(`LLM_ENDPOINT`, GitHub). It therefore permits plain ``http://`` to loopback and
``*.railway.internal``, and it follows redirects with credentials stripped
cross-origin. Both are right for its callers and wrong here, for one reason: on
a webhook the **payload** is the sensitive thing, not just the header.

- A followed redirect exfiltrates digest content to the redirect target even
  after credential headers are stripped. So redirects are refused outright.
- ``http://localhost`` or ``*.railway.internal`` is exactly the internal-reach
  case a webhook must not enable. So the local-HTTP exception is dropped.

An admin who can set this URL can already reach the network; these rules exist
so a mistyped URL or a stolen admin token cannot become an internal-network
probe, not because the admin is untrusted.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from kb.config import CitadelConfig
from kb.security_scan import redact_secrets

logger = logging.getLogger(__name__)

# Hosts a webhook may never target, whatever the scheme.
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})
_BLOCKED_SUFFIXES = (".railway.internal", ".internal", ".local")


class WebhookConfigError(ValueError):
    """The configured webhook URL is not one we are willing to post to."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    Unlike the credential-stripping handler in ``secure_http``, there is nothing
    safe to strip: the digest body itself is what must not reach an unintended
    origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        raise WebhookConfigError(f"webhook endpoint attempted a {code} redirect")




def _is_forbidden_address(candidate: str) -> bool:
    """True when *candidate* is an IP a webhook must never reach."""
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def resolve_public_address(host: str) -> str:
    """Resolve *host* to one public IP, or raise.

    Returns the address so the caller can CONNECT to that exact IP. Checking a
    hostname and then letting urllib resolve it again is a DNS-rebinding hole:
    an attacker controlling the name answers with a public address for the check
    and a private one microseconds later for the connection. The validated
    address has to be the one dialled, which is why this returns it rather than
    a bool.
    """
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise WebhookConfigError(f"webhook host does not resolve: {host}") from exc
        candidates = [str(info[4][0]) for info in resolved]
    if not candidates:
        raise WebhookConfigError(f"webhook host does not resolve: {host}")
    # EVERY answer must be public. Accepting the first public one would let a
    # host with mixed records steer the connection to the private one.
    for candidate in candidates:
        if _is_forbidden_address(candidate):
            raise WebhookConfigError(
                f"webhook URL resolves to a private or loopback address: {host}"
            )
    return candidates[0]


def require_webhook_url(url: str) -> str:
    """Raise unless *url* is an endpoint a webhook may post vault content to.

    Returns the validated IP to connect to, so the check and the connection
    cannot disagree.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise WebhookConfigError(
            f"webhook URL must be https, got {parsed.scheme or 'no scheme'}"
        )
    host = (parsed.hostname or "").lower().rstrip(".")  # a trailing dot dodges suffixes
    if not host:
        raise WebhookConfigError("webhook URL has no host")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise WebhookConfigError(f"webhook URL may not target an internal host: {host}")
    return resolve_public_address(host)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Dial a pre-validated IP while keeping the hostname for TLS.

    `server_hostname` stays the original host so SNI and certificate validation
    are unchanged; only the address dialled is pinned. That closes the window
    between validating a name and resolving it again to connect.
    """

    def __init__(self, host: str, *, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        context = self._context or ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def https_open(self, req: Any) -> Any:
        def build(host: str, **kwargs: Any) -> _PinnedHTTPSConnection:
            kwargs.pop("context", None)
            return _PinnedHTTPSConnection(host, pinned_ip=self._pinned_ip, **kwargs)

        return self.do_open(build, req)


class WebhookDelivery:
    """Post a digest as JSON to one HTTPS endpoint."""

    def __init__(
        self,
        *,
        url: str,
        token: str | None = None,
        max_message_bytes: int = 30000,
        timeout_seconds: int = 20,
    ) -> None:
        self.pinned_ip = require_webhook_url(url)
        self.url = url
        self.token = token
        self.max_message_bytes = max(1000, max_message_bytes)
        self.timeout_seconds = max(1, timeout_seconds)

    @classmethod
    def from_config(cls, config: CitadelConfig) -> "WebhookDelivery | None":
        """Build from config, or None when unconfigured — the registration idiom.

        A misconfigured URL must not take the whole service down at import time,
        so a bad value disables this gateway loudly rather than raising.
        """
        if not config.webhook_enabled or not config.webhook_url:
            return None
        try:
            return cls(
                url=config.webhook_url,
                token=config.webhook_token,
                max_message_bytes=config.webhook_max_message_bytes,
                timeout_seconds=config.webhook_timeout_seconds,
            )
        except WebhookConfigError as exc:
            logger.error("Webhook gateway disabled: %s", exc)
            return None

    def status(self) -> dict[str, Any]:
        """Sanitised status. The URL is a secret: a webhook path is a bearer
        capability, so only the host is ever reported."""
        return {
            "enabled": True,
            "kind": "webhook",
            "host": urlparse(self.url).hostname,
            "authenticated": bool(self.token),
        }

    def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]:
        # Everything, including payload construction, lives inside the try. The
        # contract is that delivery never raises, and building the body can:
        # a non-str `text` raises on slicing, and a lone UTF-16 surrogate (which
        # any upstream using errors="surrogateescape" can produce) raises on
        # .encode("utf-8"). Both would previously have escaped this method.
        truncated = False
        try:
            body = text[: self.max_message_bytes]
            truncated = len(text) > self.max_message_bytes
            payload = {"text": body}
            if message_id:
                payload["message_id"] = message_id
            request = urllib.request.Request(
                self.url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            if self.token:
                request.add_header("Authorization", f"Bearer {self.token}")
            opener = urllib.request.build_opener(
                _NoRedirect, _PinnedHTTPSHandler(self.pinned_ip)
            )
            with opener.open(request, timeout=self.timeout_seconds) as response:
                status_code = response.status
        except WebhookConfigError as exc:
            logger.error("Webhook delivery refused: %s", redact_secrets(str(exc)))
            return {
                "enabled": True,
                "ok": False,
                "sent": False,
                "status_category": "redirect_refused",
            }
        except urllib.error.HTTPError as exc:
            logger.error("Webhook delivery failed with HTTP %s", exc.code)
            return {
                "enabled": True,
                "ok": False,
                "sent": False,
                "status_category": "http_error",
                "status_code": exc.code,
            }
        except Exception as exc:  # noqa: BLE001 - delivery is best-effort
            logger.error("Webhook delivery failed with %s", exc.__class__.__name__)
            return {
                "enabled": True,
                "ok": False,
                "sent": False,
                "status_category": "delivery_exception",
                "error_type": exc.__class__.__name__,
            }
        return {
            "enabled": True,
            "ok": 200 <= status_code < 300,
            "sent": True,
            "status_code": status_code,
            "truncated": truncated,
        }
