#!/usr/bin/env python3
"""Scheduled GitHub sync trigger.

This mirrors the Nori cron shape: run one job, log a compact result, and exit
non-zero when the scheduler should mark the run as failed.
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
    raw = os.getenv("CITADEL_GITHUB_SYNC_TIMEOUT_SECONDS")
    if not raw:
        return 900
    try:
        return int(raw)
    except ValueError:
        return 900


def _output_mode() -> str:
    raw = os.getenv("CITADEL_GITHUB_SYNC_OUTPUT_MODE", "summary").strip().lower()
    if raw in {"none", "summary", "full"}:
        return raw
    return "summary"


def _target_endpoint() -> str | None:
    target = (
        os.getenv("CITADEL_GITHUB_SYNC_ENDPOINT")
        or os.getenv("CITADEL_GITHUB_SYNC_TARGET_URL")
        or os.getenv("CITADEL_WEB_URL")
        or os.getenv("CITADEL_BASE_URL")
    )
    if not target:
        return None
    target = target.rstrip("/")
    if target.endswith("/api/learning-agent/run") or target.endswith("/api/github-sync/run"):
        return target
    return f"{target}/api/learning-agent/run"


def _access_key() -> str | None:
    return os.getenv("CITADEL_GITHUB_SYNC_ACCESS_KEY") or os.getenv("CITADEL_ADMIN_KEY")


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
            "User-Agent": "citadel-github-sync-cron",
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


def _github_result(result: dict[str, Any]) -> dict[str, Any]:
    github = (result.get("sources") or {}).get("github")
    return github if isinstance(github, dict) else result


async def _run_local(
    force: bool,
    dry_run: bool,
    post_to_chat: bool,
    include_digest_preview: bool,
) -> dict[str, Any]:
    from kb.learning_agent import LearningAgent

    return await LearningAgent.from_env().run(
        force=force,
        dry_run=dry_run,
        post_to_chat=post_to_chat,
        include_digest_preview=include_digest_preview,
    )


def _log_result(result: dict[str, Any]) -> None:
    github = _github_result(result)
    security_scan = github.get("security_scan") or {}
    logger.info(
        (
            "GitHub sync complete: repos=%s changed=%s events=%s commits=%s "
            "open_prs=%s merged_prs=%s security_blocked=%s findings=%s "
            "ingested=%s improved=%s gateways=%s"
        ),
        github.get("repos_scanned"),
        github.get("changed_count"),
        github.get("event_count"),
        github.get("commit_count"),
        github.get("open_pull_request_count"),
        github.get("merged_pull_request_count"),
        security_scan.get("blocked"),
        security_scan.get("finding_count"),
        result.get("ingested", github.get("ingested")),
        result.get("improved", github.get("improved")),
        _gateway_delivery_summary(result),
    )


def _gateway_delivery_summary(result: dict[str, Any]) -> str | None:
    notifications = result.get("notifications") or {}
    gateways = notifications.get("gateways")
    if isinstance(gateways, dict) and gateways:
        parts = []
        for name, status in sorted(gateways.items()):
            if not isinstance(status, dict):
                continue
            if status.get("sent") is True:
                outcome = "sent"
            else:
                outcome = status.get("reason") or status.get("status_category") or "not_sent"
            parts.append(f"{name}:{outcome}")
        if parts:
            return ",".join(parts)

    google_chat = notifications.get("google_chat")
    if isinstance(google_chat, dict) and google_chat:
        if google_chat.get("sent") is True:
            return "google_chat:sent"
        outcome = google_chat.get("reason") or google_chat.get("status_category") or "not_sent"
        return f"google_chat:{outcome}"
    return None


def _public_summary(result: dict[str, Any]) -> dict[str, Any]:
    github = _github_result(result)
    notifications = result.get("notifications") or {}
    google_chat = notifications.get("google_chat") or {}
    gateways = notifications.get("gateways") or {}
    security_scan = github.get("security_scan") or {}
    return {
        "ok": result.get("ok"),
        "dry_run": result.get("dry_run", github.get("dry_run")),
        "ingested": result.get("ingested", github.get("ingested")),
        "improved": result.get("improved", github.get("improved")),
        "github": {
            "repos_scanned": github.get("repos_scanned"),
            "changed_count": github.get("changed_count"),
            "event_count": github.get("event_count"),
            "commit_count": github.get("commit_count"),
            "open_pull_request_count": github.get("open_pull_request_count"),
            "merged_pull_request_count": github.get("merged_pull_request_count"),
        },
        "security_scan": {
            "ok": security_scan.get("ok"),
            "blocked": security_scan.get("blocked"),
            "highest_severity": security_scan.get("highest_severity"),
            "finding_count": security_scan.get("finding_count"),
        },
        "google_chat": {
            "sent": google_chat.get("sent"),
            "reason": google_chat.get("reason"),
            "status_category": google_chat.get("status_category"),
        },
        "gateways": {
            name: {
                "sent": status.get("sent"),
                "reason": status.get("reason"),
                "status_category": status.get("status_category"),
            }
            for name, status in sorted(gateways.items())
            if isinstance(status, dict)
        },
    }


def _print_result(result: dict[str, Any]) -> None:
    mode = _output_mode()
    if mode == "none":
        return
    if mode == "full":
        print(json.dumps(result, indent=2, default=str))
        return
    print(json.dumps(_public_summary(result), indent=2, default=str))


async def arun() -> int:
    """The body of :func:`run`, awaitable on the caller's own event loop.

    The evolve scheduler runs this inside the web process's loop (#88), where
    ``run_async`` is unusable: it falls back to ``asyncio.run``, which raises
    inside a running loop. Blocking HTTP moves to a thread so the web's request
    loop is not stalled for the duration of the sync.
    """
    force = _bool(os.getenv("CITADEL_GITHUB_SYNC_FORCE"), default=False)
    dry_run = _bool(os.getenv("CITADEL_GITHUB_SYNC_DRY_RUN"), default=False)
    post_to_chat = _bool(os.getenv("CITADEL_ORG_DIGEST_POST_TO_CHAT"), default=True)
    include_digest_preview = _bool(
        os.getenv("CITADEL_ORG_DIGEST_INCLUDE_PREVIEW_IN_CRON_OUTPUT"),
        default=False,
    )
    payload = {
        "force": force,
        "dry_run": dry_run,
        "post_to_chat": post_to_chat,
        "include_digest_preview": include_digest_preview,
    }
    endpoint = _target_endpoint()
    logger.info("GitHub sync mode: %s", "HTTP endpoint" if endpoint else "in-process")

    if endpoint:
        access_key = _access_key()
        if not access_key:
            logger.error(
                "CITADEL_GITHUB_SYNC_ACCESS_KEY or CITADEL_ADMIN_KEY is required "
                "when CITADEL_GITHUB_SYNC_TARGET_URL is set."
            )
            return 1
        logger.info("Starting scheduled GitHub sync through %s", endpoint)
        status_code, result = await asyncio.to_thread(
            _post_json,
            endpoint,
            payload=payload,
            access_key=access_key,
            timeout=_timeout(),
        )
        if status_code >= 400:
            logger.error(
                "GitHub sync failed with HTTP %s: %s",
                status_code,
                _public_summary(result),
            )
            return 1
    else:
        logger.info("Starting scheduled GitHub sync in-process")
        result = await _run_local(
            force=force,
            dry_run=dry_run,
            post_to_chat=post_to_chat,
            include_digest_preview=include_digest_preview,
        )

    if result.get("ok") is False:
        logger.error("GitHub sync failed: %s", _public_summary(result))
        return 1

    _log_result(result)
    _print_result(result)
    return 0


def run() -> int:
    """Synchronous entrypoint for the CLI and the standalone cron service.

    run_async, not asyncio.run: in the evolve chain this shares the one stage
    loop so cognee's cached engine is not bound to a throwaway loop (#69).
    Standalone (no shared loop) it falls back to asyncio.run.
    """
    return run_async(arun())


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
