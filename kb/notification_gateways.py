from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol

from kb.config import CitadelConfig
from kb.google_chat import GoogleChatDelivery
from kb.webhook_gateway import WebhookDelivery

logger = logging.getLogger(__name__)


class NotificationGateway(Protocol):
    """Outbound delivery adapter for organization update digests."""

    def status(self) -> dict[str, Any]:
        """Return sanitized gateway status suitable for API responses and logs."""
        ...

    def post_digest(self, text: str, *, message_id: str | None = None) -> dict[str, Any]:
        """Deliver one formatted organization update digest."""
        ...


GatewayMap = Mapping[str, NotificationGateway]


# Registering a new connector is one entry here and nothing else: every consumer
# already iterates the map rather than naming a provider, and each provider's
# ``from_config`` returns None when it is unconfigured.
#
# This holds the provider CLASSES, not bound ``from_config`` references, so the
# lookup happens per call. Capturing the functions here would freeze them at
# import time, which makes the registry untestable — patching the class would
# have no effect on what this tuple already holds.
_GATEWAY_PROVIDERS: tuple[tuple[str, Any], ...] = (
    ("webhook", WebhookDelivery),
    ("google_chat", GoogleChatDelivery),
)


def configured_gateways(config: CitadelConfig) -> dict[str, NotificationGateway]:
    """Build every configured gateway. One bad provider must not cost the others.

    Providers are not uniformly defensive: `GoogleChatDelivery.__init__` raises
    on a space name missing its `spaces/` prefix, so a single mistyped env var
    would otherwise abort this whole function — un-registering a perfectly good
    webhook and taking `LearningAgent.__init__` with it. Isolate per provider so
    a misconfiguration disables one connector rather than all of them.
    """
    gateways: dict[str, NotificationGateway] = {}
    for name, provider in _GATEWAY_PROVIDERS:
        try:
            gateway = provider.from_config(config)
        except Exception as exc:  # noqa: BLE001 - one bad connector must not break the rest
            logger.error(
                "Notification gateway %r disabled by a configuration error: %s",
                name,
                exc.__class__.__name__,
            )
            continue
        if gateway is not None:
            gateways[name] = gateway
    return gateways


def gateway_statuses(gateways: GatewayMap) -> dict[str, dict[str, Any]]:
    return {name: gateway.status() for name, gateway in gateways.items()}
