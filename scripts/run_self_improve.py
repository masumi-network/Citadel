#!/usr/bin/env python3
"""Scheduled self-improvement pass trigger.

Prefers the web service endpoint (POST /api/learning-agent/optimize) when a
target URL is configured so mesh and audit events land in the live vault;
falls back to an in-process pass otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.stage_loop import run_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _timeout() -> int:
    raw = os.getenv("CITADEL_SELF_IMPROVE_TIMEOUT_SECONDS")
    if not raw:
        return 600
    try:
        return int(raw)
    except ValueError:
        return 600


def _target_endpoint() -> str | None:
    target = (
        os.getenv("CITADEL_SELF_IMPROVE_ENDPOINT")
        or os.getenv("CITADEL_SELF_IMPROVE_TARGET_URL")
        or os.getenv("CITADEL_WEB_URL")
        or os.getenv("CITADEL_BASE_URL")
    )
    if not target:
        return None
    target = target.rstrip("/")
    if target.endswith("/api/learning-agent/optimize"):
        return target
    return f"{target}/api/learning-agent/optimize"


def _access_key() -> str | None:
    return os.getenv("CITADEL_SELF_IMPROVE_ACCESS_KEY") or os.getenv("CITADEL_ADMIN_KEY")


def _post_json(
    url: str,
    *,
    payload: dict[str, Any],
    access_key: str,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "citadel-self-improve-cron",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            return response.status, json.loads(response_body or "{}")
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(response_body or "{}")
        except json.JSONDecodeError:
            parsed = {"detail": response_body}
        return exc.code, parsed
    except URLError as exc:
        return 599, {"detail": str(exc.reason)}
    except TimeoutError as exc:
        # A read-phase timeout is a bare TimeoutError, not a URLError, and
        # would otherwise escape this handler as a raw traceback (#39/#116).
        return 599, {"detail": f"timed out after {timeout}s: {exc}"}


async def _run_local(*, dry_run: bool) -> dict[str, Any]:
    from kb.access import AccessStore
    from kb.mesh import MeshState
    from kb.self_improve import SelfImprovement
    from kb.service import Citadel

    citadel = Citadel.from_env()
    access_store = AccessStore(
        citadel.config.access_store_path,
        max_audit_events=citadel.config.audit_max_events,
    )
    return await SelfImprovement(
        citadel,
        mesh=MeshState(),
        access_store=access_store,
    ).run(dry_run=dry_run)


def _log_result(result: dict[str, Any]) -> None:
    logger.info(
        "Self-improvement pass complete: reviewed=%s optimized=%s llm_used=%s dry_run=%s",
        result.get("reviewed"),
        result.get("optimized"),
        result.get("llm_used"),
        result.get("dry_run"),
    )


async def arun(*, force_in_process: bool = False) -> int:
    """The body of :func:`run`, awaitable on the caller's own event loop.

    The evolve scheduler runs this inside the web process's loop (#88), where
    ``run_async`` is unusable: it falls back to ``asyncio.run``, which raises
    inside a running loop. Blocking HTTP moves to a thread so the web's request
    loop is not stalled.
    """
    dry_run = _bool(os.getenv("CITADEL_SELF_IMPROVE_DRY_RUN"), default=False)
    payload = {"dry_run": dry_run}
    endpoint = None if force_in_process else _target_endpoint()

    if endpoint:
        access_key = _access_key()
        if not access_key:
            logger.error(
                "CITADEL_SELF_IMPROVE_ACCESS_KEY or CITADEL_ADMIN_KEY is required "
                "when a self-improve target URL is set."
            )
            return 1
        logger.info("Starting scheduled self-improvement through %s", endpoint)
        status_code, result = await asyncio.to_thread(
            _post_json,
            endpoint,
            payload=payload,
            access_key=access_key,
            timeout=_timeout(),
        )
        if status_code >= 400:
            logger.error("Self-improvement pass failed with HTTP %s: %s", status_code, result)
            return 1
    else:
        logger.info("Starting scheduled self-improvement in-process")
        result = await _run_local(dry_run=dry_run)

    if result.get("ok") is False:
        logger.error("Self-improvement pass failed: %s", result)
        return 1

    _log_result(result)
    return 0


def run(*, force_in_process: bool = False) -> int:
    """Synchronous entrypoint for the CLI and the standalone cron service.

    run_async shares the evolve chain's single loop so cognee's cached engine
    is not bound to a throwaway loop (#69); standalone it is asyncio.run.
    """
    return run_async(arun(force_in_process=force_in_process))


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
