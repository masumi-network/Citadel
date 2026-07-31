from kb import notification_gateways as ng


class _FakeGateway:
    def __init__(self, name: str):
        self._name = name

    def status(self) -> dict:
        return {"gateway": self._name, "ok": True}

    def post_digest(self, text: str, *, message_id: str | None = None) -> dict:
        return {"delivered": True, "message_id": message_id}


def _stub_factories(monkeypatch, **returns):
    """Patch every registered factory so the registry can be exercised with a
    stand-in config.

    These tests are about the registry mapping names to gateways, not about any
    provider's configuration. Passing a bare ``object()`` only worked while one
    factory existed; each additional connector reads its own config fields, so
    every factory has to be stubbed for the stand-in to hold.
    """
    for name, provider in ng._GATEWAY_PROVIDERS:
        monkeypatch.setattr(
            provider, "from_config", staticmethod(lambda config, _n=name: returns.get(_n))
        )


def test_configured_gateways_includes_google_chat_when_available(monkeypatch):
    fake = _FakeGateway("google_chat")
    _stub_factories(monkeypatch, google_chat=fake)
    assert ng.configured_gateways(config=object()) == {"google_chat": fake}


def test_configured_gateways_includes_every_configured_connector(monkeypatch):
    chat = _FakeGateway("google_chat")
    hook = _FakeGateway("webhook")
    _stub_factories(monkeypatch, google_chat=chat, webhook=hook)
    assert ng.configured_gateways(config=object()) == {
        "google_chat": chat,
        "webhook": hook,
    }


def test_configured_gateways_empty_when_delivery_unavailable(monkeypatch):
    _stub_factories(monkeypatch)
    assert ng.configured_gateways(config=object()) == {}


def test_gateway_statuses_maps_every_gateway():
    gateways = {"a": _FakeGateway("a"), "b": _FakeGateway("b")}
    assert ng.gateway_statuses(gateways) == {
        "a": {"gateway": "a", "ok": True},
        "b": {"gateway": "b", "ok": True},
    }


def test_gateway_statuses_empty_map():
    assert ng.gateway_statuses({}) == {}
