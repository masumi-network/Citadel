from __future__ import annotations

from typing import Any, Mapping, Protocol

from kb.config import CitadelConfig
from kb.google_chat import GoogleChatDelivery
from kb.webhook_gateway import WebhookDelivery


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
    gateways: dict[str, NotificationGateway] = {}
    for name, provider in _GATEWAY_PROVIDERS:
        gateway = provider.from_config(config)
        if gateway is not None:
            gateways[name] = gateway
    return gateways


def gateway_statuses(gateways: GatewayMap) -> dict[str, dict[str, Any]]:
    return {name: gateway.status() for name, gateway in gateways.items()}
