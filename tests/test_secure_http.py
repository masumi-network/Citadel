"""The server-side authenticated transport (M3).

Six server modules send bearer tokens through this. The properties below are
the reason it exists, so each one is asserted directly rather than inferred
from the call sites.
"""

from __future__ import annotations

import urllib.request

import pytest

from kb.secure_http import (
    InsecureEndpointError,
    _CredentialStrippingRedirectHandler,
    is_local_endpoint,
    open_secure,
    require_secure_url,
)


class _FakeFp:
    """Minimal stand-in for the response body urllib hands the handler."""

    def read(self) -> bytes:  # pragma: no cover - never consumed here
        return b""


def _redirect(from_url: str, to_url: str) -> urllib.request.Request | None:
    request = urllib.request.Request(
        from_url,
        headers={
            "Authorization": "Bearer super-secret",
            "Cookie": "session=abc",
            # Not a credential and not a content header, so urllib's own
            # redirect logic keeps it. Proves the strip is targeted.
            "Accept": "application/json",
        },
    )
    return _CredentialStrippingRedirectHandler().redirect_request(
        request, _FakeFp(), 301, "Moved", {}, to_url
    )


# --- scheme -----------------------------------------------------------------


def test_https_is_allowed() -> None:
    require_secure_url("https://openrouter.ai/api/v1/chat/completions")


def test_plain_http_to_the_public_internet_is_refused() -> None:
    """The live risk: LLM_ENDPOINT is operator-set, so http leaks the API key."""
    with pytest.raises(InsecureEndpointError):
        require_secure_url("http://openrouter.ai/api/v1/chat/completions")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/readyz",
        "http://127.0.0.1:8000/search",
        "http://citadel-archive.railway.internal:8080/ingest",
    ],
)
def test_plain_http_is_allowed_where_it_cannot_leave(url: str) -> None:
    """Blanket https-only would break production: loopback and Railway's
    private network are both plain HTTP by design."""
    require_secure_url(url)
    assert is_local_endpoint(url)


def test_a_lookalike_private_host_is_not_treated_as_private() -> None:
    assert not is_local_endpoint("http://railway.internal.evil.example")
    with pytest.raises(InsecureEndpointError):
        require_secure_url("http://railway.internal.evil.example/x")


def test_open_secure_refuses_before_opening_a_socket() -> None:
    request = urllib.request.Request(
        "http://openrouter.ai/v1", headers={"Authorization": "Bearer secret"}
    )
    with pytest.raises(InsecureEndpointError):
        open_secure(request, timeout=1)


# --- redirects --------------------------------------------------------------


def test_credentials_are_stripped_when_the_origin_changes() -> None:
    new = _redirect("https://api.github.com/repos/a/b", "https://evil.example/repos/a/b")

    assert new is not None
    merged = {**new.headers, **new.unredirected_hdrs}
    assert "Authorization" not in merged
    assert "Cookie" not in merged
    # Non-credential headers are not the point and must survive.
    assert merged.get("Accept") == "application/json"


def test_credentials_survive_a_same_origin_redirect() -> None:
    """GitHub 301s a renamed repo. Refusing or stripping there would break
    github_sync for any repository the org renames."""
    new = _redirect(
        "https://api.github.com/repos/old/name", "https://api.github.com/repos/new/name"
    )

    assert new is not None
    merged = {**new.headers, **new.unredirected_hdrs}
    assert merged.get("Authorization") == "Bearer super-secret"


def test_a_port_change_counts_as_a_different_origin() -> None:
    new = _redirect("https://api.example.com/a", "https://api.example.com:8443/a")

    assert new is not None
    merged = {**new.headers, **new.unredirected_hdrs}
    assert "Authorization" not in merged


def test_a_redirect_that_downgrades_to_http_is_refused() -> None:
    with pytest.raises(InsecureEndpointError):
        _redirect("https://api.github.com/x", "http://api.github.com/x")
