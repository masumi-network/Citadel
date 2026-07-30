"""Sync local Approved Capture Roots to the Node for server-side share enforcement."""

from __future__ import annotations

from dataclasses import dataclass

from kb.capture import capture_token
from kb.capture_config import (
    MAX_APPROVED_CAPTURE_ROOTS,
    CaptureConfig,
    normalize_capture_root_paths,
)
from kb.promotion_client import (
    PromotionClientError,
    get_seat_capture_roots,
    node_base_url,
    resolve_seat_slug,
    update_seat_capture_roots,
)


@dataclass(frozen=True)
class CaptureRootsSyncResult:
    ok: bool
    status: str
    detail: str
    seat_slug: str | None = None
    merged_count: int = 0
    # True when retrying cannot possibly help: a rejected payload stays rejected.
    permanent: bool = False


def merge_capture_root_paths(
    server_paths: tuple[str, ...] | list[str],
    local_paths: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Union server + local paths (order-preserving, normalized, deduped)."""
    return normalize_capture_root_paths((*server_paths, *local_paths))


def _sync_local_capture_roots_once(
    config: CaptureConfig,
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> CaptureRootsSyncResult:
    local_paths = tuple(root.path for root in config.roots)
    if not local_paths:
        return CaptureRootsSyncResult(
            ok=True,
            status="skipped",
            detail="no local capture roots to sync",
        )

    token = (token or capture_token()).strip()
    if not token:
        return CaptureRootsSyncResult(
            ok=True,
            status="skipped",
            detail="no seat token in environment — set CITADEL_MCP_ACCESS_TOKEN",
        )

    resolved_base = (base_url or config.node_url or node_base_url()).rstrip("/")
    try:
        seat_slug = resolve_seat_slug(resolved_base, token)
    except PromotionClientError as exc:
        return CaptureRootsSyncResult(
            ok=False,
            status="failed",
            detail=str(exc),
        )

    if not seat_slug:
        return CaptureRootsSyncResult(
            ok=True,
            status="skipped",
            detail="token is not seat-bound — server capture roots unchanged",
        )

    try:
        current = get_seat_capture_roots(seat_slug, base_url=resolved_base, token=token)
        server_paths = tuple(current.get("roots") or ())
        merged = merge_capture_root_paths(server_paths, local_paths)
        if merged == normalize_capture_root_paths(server_paths):
            return CaptureRootsSyncResult(
                ok=True,
                status="unchanged",
                detail="server already has these capture roots",
                seat_slug=seat_slug,
                merged_count=len(merged),
            )
        if len(merged) > MAX_APPROVED_CAPTURE_ROOTS:
            # Refuse locally instead of sending a payload the Node will reject.
            # The merge is a union of server and local roots, so it only grows,
            # and the write never lands: merged stays different from the server
            # list, so the "unchanged" short-circuit above never fires and every
            # subsequent sync re-sends the same rejected request. That is the
            # 422 loop, and it cannot end on its own.
            return CaptureRootsSyncResult(
                ok=False,
                status="failed",
                detail=(
                    f"{len(merged)} capture roots exceeds the Node limit of "
                    f"{MAX_APPROVED_CAPTURE_ROOTS}. Remove some roots from "
                    "capture config, or from the seat on the Node, and re-run."
                ),
                seat_slug=seat_slug,
                merged_count=len(merged),
                permanent=True,
            )
        update_seat_capture_roots(
            seat_slug,
            list(merged),
            base_url=resolved_base,
            token=token,
        )
    except PromotionClientError as exc:
        # A 4xx means the Node understood and refused. Sending it again changes
        # nothing except the error rate.
        status = getattr(exc, "status", None)
        return CaptureRootsSyncResult(
            ok=False,
            status="failed",
            detail=str(exc),
            seat_slug=seat_slug,
            permanent=bool(status is not None and 400 <= status < 500),
        )

    return CaptureRootsSyncResult(
        ok=True,
        status="synced",
        detail=f"synced {len(merged)} approved capture root(s) to Node",
        seat_slug=seat_slug,
        merged_count=len(merged),
    )


def sync_local_capture_roots_to_server(
    config: CaptureConfig,
    *,
    base_url: str | None = None,
    token: str | None = None,
) -> CaptureRootsSyncResult:
    """Merge local roots into the seat's server-approved list (best-effort).

    Non-seat tokens and offline Nodes are skipped with a warning — local setup
    must still succeed when sync cannot run. TRANSIENT Node errors are retried
    once; a rejection is not retried, because the Node refusing a payload it
    understood will refuse the identical payload again. Production showed the
    cost of not making that distinction: ten consecutive
    PUT .../capture-roots 422s, two per invocation, none of which could ever
    have succeeded.
    """
    result = _sync_local_capture_roots_once(
        config,
        base_url=base_url,
        token=token,
    )
    if result.status == "failed" and not result.permanent:
        result = _sync_local_capture_roots_once(
            config,
            base_url=base_url,
            token=token,
        )
    return result


def sync_warning_message(result: CaptureRootsSyncResult) -> str | None:
    """Human-readable warning when sync failed or was skipped unexpectedly."""
    if result.status == "failed":
        seat = f" for seat {result.seat_slug}" if result.seat_slug else ""
        return f"Could not sync capture roots to Node{seat}: {result.detail}"
    if result.status == "skipped" and "no seat token" in result.detail:
        return (
            "Capture roots saved locally only — set CITADEL_MCP_ACCESS_TOKEN "
            "and re-run `citadel setup` to sync share enforcement to the Node."
        )
    return None
