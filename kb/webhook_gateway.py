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

import ipaddress
import json
import logging
import socket
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


_OPENER = urllib.request.build_opener(_NoRedirect)


def _is_forbidden_ip(host: str) -> bool:
    """True when *host* is, or resolves to, an address a webhook must not reach."""
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates.append(host)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, None)
        except OSError:
            # Unresolvable: let the request fail normally rather than guessing.
            return False
        candidates.extend(str(info[4][0]) for info in resolved)
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return True
    return False


def require_webhook_url(url: str) -> None:
    """Raise unless *url* is an endpoint a webhook may post vault content to."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise WebhookConfigError(
            f"webhook URL must be https, got {parsed.scheme or 'no scheme'}"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise WebhookConfigError("webhook URL has no host")
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        raise WebhookConfigError(f"webhook URL may not target an internal host: {host}")
    if _is_forbidden_ip(host):
        raise WebhookConfigError(
            f"webhook URL resolves to a private or loopback address: {host}"
        )


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
        require_webhook_url(url)
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
        try:
            with _OPENER.open(request, timeout=self.timeout_seconds) as response:
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
