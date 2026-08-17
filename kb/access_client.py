"""HTTP client for the Access admin API — backs ``citadel seat`` / ``citadel token``.

The admin key is read ONLY from ``CITADEL_ADMIN_KEY`` in the environment — never
a CLI argument, because argv leaks via ``ps`` and shell history. All calls are
HTTPS-only and never follow redirects. A freshly minted token is returned to the
caller exactly once; this module never logs or persists it.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from kb.capture_config import DEFAULT_NODE_URL, load_capture_config

ADMIN_KEY_ENV = "CITADEL_ADMIN_KEY"
_TIMEOUT = 60.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


class AccessClientError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def admin_key() -> str:
    """The admin bootstrap key from the environment, or a clean error."""
    key = (os.getenv(ADMIN_KEY_ENV) or "").strip()
    if not key:
        raise AccessClientError(
            f"missing admin key — set {ADMIN_KEY_ENV} in the environment "
            "(never pass an admin key on the command line)"
        )
    return key


def node_base_url(override: str | None = None) -> str:
    if override:
        return override.rstrip("/")
    try:
        config = load_capture_config()
        if config.node_url:
            return config.node_url.rstrip("/")
    except ValueError:
        pass
    return DEFAULT_NODE_URL.rstrip("/")


def _request(
    method: str,
    path: str,
    *,
    base_url: str,
    key: str,
    payload: dict[str, Any] | None = None,
    timeout: float = _TIMEOUT,
) -> Any:
    if not base_url.lower().startswith("https://"):
        raise AccessClientError("refusing non-HTTPS Node URL")
    url = f"{base_url}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        parsed: Any = raw
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            pass
        detail = parsed.get("detail") if isinstance(parsed, dict) else raw or exc.reason
        raise AccessClientError(str(detail or exc.reason), status=exc.code, body=parsed) from exc
    except urllib.error.URLError as exc:
        raise AccessClientError(str(exc.reason)) from exc
    except TimeoutError as exc:
        # A read-phase timeout surfaces as a bare TimeoutError from http.client's
        # getresponse(), which urllib does NOT wrap in URLError — so it would
        # otherwise escape as a raw traceback (#39).
        raise AccessClientError(
            f"Node request timed out after {timeout:.0f}s — retry or check the Node"
        ) from exc
    except OSError as exc:
        raise AccessClientError(f"could not reach Node: {exc}") from exc
    return json.loads(body) if body else {}


def list_seats(*, base_url: str, key: str | None = None) -> dict[str, Any]:
    return _request("GET", "/api/access/seats", base_url=base_url, key=key or admin_key())


def create_seat(
    *,
    base_url: str,
    name: str,
    slug: str,
    email: str | None = None,
    role: str = "writer",
    issue_token: bool = True,
    token_name: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "slug": slug,
        "role": role,
        "issue_token": issue_token,
    }
    if email:
        payload["email"] = email
    if token_name:
        payload["token_name"] = token_name
    return _request("POST", "/api/access/seats", base_url=base_url, key=key or admin_key(), payload=payload)


def issue_seat_token(
    slug: str, *, base_url: str, token_name: str | None = None, key: str | None = None
) -> dict[str, Any]:
    """Mint a fresh token for an existing seat (POST /api/access/seats/<slug>/tokens)."""
    payload = {"token_name": token_name} if token_name else {}
    return _request(
        "POST",
        f"/api/access/seats/{slug}/tokens",
        base_url=base_url,
        key=key or admin_key(),
        payload=payload,
    )


def create_self_seat_token(
    *,
    base_url: str,
    token: str,
    role: str,
    token_name: str | None = None,
) -> dict[str, Any]:
    """Mint a token for the caller's own seat (POST /api/access/self/tokens).

    Uses the seat token as Bearer. Never reads ``CITADEL_ADMIN_KEY``.
    """
    payload: dict[str, Any] = {"role": role}
    if token_name:
        payload["token_name"] = token_name
    return _request(
        "POST", "/api/access/self/tokens", base_url=base_url, key=token, payload=payload
    )


def create_token(
    *,
    base_url: str,
    name: str,
    role: str = "reader",
    kind: str = "service_account",
    default_dataset: str | None = None,
    default_session: str | None = None,
    allowed_datasets: list[str] | None = None,
    expires_at: str | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "role": role, "kind": kind}
    if default_dataset:
        payload["default_dataset"] = default_dataset
    if default_session:
        payload["default_session"] = default_session
    if allowed_datasets:
        payload["allowed_datasets"] = allowed_datasets
    if expires_at:
        payload["expires_at"] = expires_at
    return _request("POST", "/api/access/tokens", base_url=base_url, key=key or admin_key(), payload=payload)


def revoke_token(token_id: str, *, base_url: str, key: str | None = None) -> dict[str, Any]:
    return _request(
        "POST", f"/api/access/tokens/{token_id}/revoke", base_url=base_url, key=key or admin_key()
    )
