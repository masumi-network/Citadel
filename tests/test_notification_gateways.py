from kb import notification_gateways as ng


class _FakeGateway:
    def __init__(self, name: str):
        self._name = name

    def status(self) -> dict:
        return {"gateway": self._name, "ok": True}

    def post_digest(self, text: str, *, message_id: str | None = None) -> dict:
        return {"delivered": True, "message_id": message_id}


def test_configured_gateways_includes_google_chat_when_available(monkeypatch):
    fake = _FakeGateway("google_chat")
    monkeypatch.setattr(
        ng.GoogleChatDelivery, "from_config", staticmethod(lambda config: fake)
    )
    assert ng.configured_gateways(config=object()) == {"google_chat": fake}


def test_configured_gateways_empty_when_delivery_unavailable(monkeypatch):
    monkeypatch.setattr(
        ng.GoogleChatDelivery, "from_config", staticmethod(lambda config: None)
    )
    assert ng.configured_gateways(config=object()) == {}


def test_gateway_statuses_maps_every_gateway():
    gateways = {"a": _FakeGateway("a"), "b": _FakeGateway("b")}
    assert ng.gateway_statuses(gateways) == {
        "a": {"gateway": "a", "ok": True},
        "b": {"gateway": "b", "ok": True},
    }


def test_gateway_statuses_empty_map():
    assert ng.gateway_statuses({}) == {}
