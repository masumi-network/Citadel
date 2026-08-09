"""HTTP client for Promotion Agent API — used by ``citadel promotion`` CLI."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from kb.capture import capture_token
from kb.capture_config import DEFAULT_NODE_URL, load_capture_config

_TIMEOUT = 120.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


class PromotionClientError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


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
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = _TIMEOUT,
) -> Any:
    if not base_url.lower().startswith("https://"):
        raise PromotionClientError("refusing non-HTTPS Node URL")
    if not token:
        raise PromotionClientError("missing token (set CITADEL_MCP_ACCESS_TOKEN)")
    url = f"{base_url}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers: dict[str, str] = {"Accept": "application/json"}
    headers["Authorization"] = f"Bearer {token}"
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
        raise PromotionClientError(
            str(detail or exc.reason),
            status=exc.code,
            body=parsed,
        ) from exc
    except urllib.error.URLError as exc:
        raise PromotionClientError(str(exc.reason)) from exc
    except TimeoutError as exc:
        # A read-phase timeout surfaces as a bare TimeoutError from http.client's
        # getresponse(), which urllib does NOT wrap in URLError — so it would
        # otherwise escape as a raw traceback (#39).
        raise PromotionClientError(
            f"Node request timed out after {timeout:.0f}s — retry or check the Node"
        ) from exc
    except OSError as exc:
        raise PromotionClientError(f"could not reach Node: {exc}") from exc
    return json.loads(body) if body else {}


def resolve_seat_slug(base_url: str, token: str) -> str | None:
    """Return the seat slug for a seat-bound token, or None for org/admin tokens."""
    session = _request("GET", "/api/session", base_url=base_url, token=token)
    slug = session.get("seat_slug")
    return str(slug) if slug else None


def resolve_seat_dataset(base_url: str, token: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    slug = resolve_seat_slug(base_url, token)
    if not slug:
        raise PromotionClientError("could not resolve seat dataset — pass --dataset seat:<slug>")
    return f"seat:{slug}"


def get_seat_capture_roots(
    slug: str,
    *,
    base_url: str,
    token: str | None = None,
) -> dict[str, Any]:
    token = token or capture_token()
    return _request(
        "GET",
        f"/api/access/seats/{slug}/capture-roots",
        base_url=base_url,
        token=token,
    )


def update_seat_capture_roots(
    slug: str,
    roots: list[str],
    *,
    base_url: str,
    token: str | None = None,
) -> dict[str, Any]:
    token = token or capture_token()
    return _request(
        "PUT",
        f"/api/access/seats/{slug}/capture-roots",
        base_url=base_url,
        token=token,
        payload={"roots": roots},
    )


def list_pending(
    *,
    base_url: str,
    token: str | None = None,
    status: str = "pending",
) -> dict[str, Any]:
    token = token or capture_token()
    return _request(
        "GET",
        f"/api/promotion/pending?status={status}",
        base_url=base_url,
        token=token,
    )


def approve_pending(
    item_id: str,
    *,
    base_url: str,
    token: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    token = token or capture_token()
    payload = {"note": note} if note else None
    return _request(
        "POST",
        f"/api/promotion/pending/{item_id}/approve",
        base_url=base_url,
        token=token,
        payload=payload,
    )


def reject_pending(
    item_id: str,
    *,
    base_url: str,
    token: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    token = token or capture_token()
    payload = {"note": note} if note else None
    return _request(
        "POST",
        f"/api/promotion/pending/{item_id}/reject",
        base_url=base_url,
        token=token,
        payload=payload,
    )


def run_promotion(
    *,
    base_url: str,
    token: str | None = None,
    dataset: str | None = None,
    dry_run: bool = True,
    max_items: int | None = None,
) -> dict[str, Any]:
    token = token or capture_token()
    seat_dataset = resolve_seat_dataset(base_url, token, dataset)
    payload: dict[str, Any] = {"dataset": seat_dataset, "dry_run": dry_run}
    if max_items is not None:
        payload["max_items"] = max_items
    return _request(
        "POST",
        "/api/promote/run",
        base_url=base_url,
        token=token,
        payload=payload,
    )
