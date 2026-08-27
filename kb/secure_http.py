"""One authenticated HTTP transport for the server side (M3).

Six server modules build a ``Request`` carrying a bearer token and hand it to
the bare ``urlopen``, which uses the global opener. That opener follows
redirects and replays every header at the target, so a 30x from an upstream
host hands the credential to whatever it points at. ``kb/capture.py`` already
got this right on the CLI side; the server never did.

Two rules, and they are deliberately different:

**Scheme.** Only ``LLM_ENDPOINT`` is operator-configurable, and pointing it at
``http://`` sends ``OPENROUTER_API_KEY`` in cleartext. So plain HTTP is refused,
with an exception for loopback and Railway's private network, because
local service checks use ``http://localhost:8080`` and
``*.railway.internal`` traffic never leaves the project.

**Redirects.** NOT blocked. GitHub 301s a renamed repository, and refusing that
would break `github_sync` for any repo the org renames. Redirects are followed
with the credential headers stripped whenever the origin changes, which is what
browsers and modern HTTP clients do. Same-origin redirects keep their headers so
ordinary API behaviour is untouched.
"""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse

# Headers that must never survive a hop to a different origin.
_CREDENTIAL_HEADERS = ("Authorization", "Cookie", "Proxy-authorization")

# Plain HTTP is only ever acceptable where the bytes cannot leave the host or
# the Railway project private network.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_PRIVATE_SUFFIX = ".railway.internal"


class InsecureEndpointError(ValueError):
    """A credential was about to be sent over an untrusted scheme."""


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return (parsed.scheme, (parsed.hostname or "").lower(), parsed.port)


def is_local_endpoint(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(_PRIVATE_SUFFIX)


def require_secure_url(url: str) -> None:
    """Raise unless *url* is safe to send a credential to."""
    scheme = (urlparse(url).scheme or "").lower()
    if scheme == "https":
        return
    if scheme == "http" and is_local_endpoint(url):
        return
    raise InsecureEndpointError(
        f"refusing to send credentials over {scheme or 'an unknown scheme'}: {url}"
    )


class _CredentialStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry credentials to a new origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        new_request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_request is None:
            return None
        # A redirect to http:// is a downgrade even if the first hop was https.
        require_secure_url(newurl)
        if _origin(newurl) != _origin(req.full_url):
            for header in _CREDENTIAL_HEADERS:
                new_request.remove_header(header)
                # Request.remove_header only checks its own capitalisation, and
                # unredirected_hdrs is where headers set at construction land.
                new_request.unredirected_hdrs.pop(header.capitalize(), None)
        return new_request


_OPENER = urllib.request.build_opener(_CredentialStrippingRedirectHandler)


def open_secure(request: urllib.request.Request, *, timeout: float) -> Any:
    """``urlopen`` for a request that carries a credential.

    Refuses an untrusted scheme up front, then opens through an opener that
    strips credentials on a cross-origin redirect. Returns the same context
    manager ``urlopen`` does, so call sites only change the function name.
    """
    require_secure_url(request.full_url)
    try:
        return _OPENER.open(request, timeout=timeout)  # noqa: S310 - scheme checked above
    except InsecureEndpointError as exc:
        # Raised from inside the redirect handler; urllib would otherwise wrap
        # it somewhere unhelpful. Surface it as a URLError so existing
        # except-chains at the call sites still catch it.
        raise URLError(str(exc)) from exc
