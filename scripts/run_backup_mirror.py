#!/usr/bin/env python3
"""Scheduled Vault Backup Mirror manifest export."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from kb.backup_mirror import BackupMirror, BackupMirrorDisabled
from kb.config import CitadelConfig

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
    raw = os.getenv("CITADEL_BACKUP_MIRROR_TIMEOUT_SECONDS")
    if not raw:
        return 300
    try:
        return int(raw)
    except ValueError:
        return 300


def _target_endpoint() -> str | None:
    target = (
        os.getenv("CITADEL_BACKUP_MIRROR_ENDPOINT")
        or os.getenv("CITADEL_BACKUP_MIRROR_TARGET_URL")
        or os.getenv("CITADEL_WEB_URL")
        or os.getenv("CITADEL_BASE_URL")
    )
    if not target:
        return None
    target = target.rstrip("/")
    if target.endswith("/api/backup-mirror/run"):
        return target
    return f"{target}/api/backup-mirror/run"


def _access_key() -> str | None:
    return os.getenv("CITADEL_BACKUP_MIRROR_ACCESS_KEY") or os.getenv("CITADEL_ADMIN_KEY")


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
            "User-Agent": "citadel-backup-mirror-cron",
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


def _run_local(*, dry_run: bool) -> dict[str, Any]:
    return BackupMirror(CitadelConfig.from_env()).run(dry_run=dry_run)


def _log_result(result: dict[str, Any]) -> None:
    manifest = result.get("manifest") or {}
    summary = manifest.get("summary") or {}
    logger.info(
        "Backup mirror complete: dry_run=%s written=%s published=%s tracked=%s available=%s bytes=%s",
        result.get("dry_run"),
        result.get("written"),
        result.get("published"),
        summary.get("tracked_files"),
        summary.get("available_files"),
        summary.get("total_bytes"),
    )


def run() -> int:
    dry_run = _bool(os.getenv("CITADEL_BACKUP_MIRROR_DRY_RUN"), default=True)
    payload = {"dry_run": dry_run}
    endpoint = _target_endpoint()

    if endpoint:
        access_key = _access_key()
        if not access_key:
            logger.error(
                "CITADEL_BACKUP_MIRROR_ACCESS_KEY or CITADEL_ADMIN_KEY is required "
                "when CITADEL_BACKUP_MIRROR_TARGET_URL is set."
            )
            return 1
        logger.info("Starting Vault Backup Mirror export through %s", endpoint)
        status_code, result = _post_json(
            endpoint,
            payload=payload,
            access_key=access_key,
            timeout=_timeout(),
        )
        if status_code >= 400:
            logger.error("Backup mirror export failed with HTTP %s: %s", status_code, result)
            return 1
    else:
        logger.info("Starting Vault Backup Mirror export in-process")
        try:
            result = _run_local(dry_run=dry_run)
        except BackupMirrorDisabled as exc:
            logger.error("Backup mirror export failed: %s", exc)
            return 1

    if result.get("ok") is False:
        logger.error("Backup mirror export failed: %s", result)
        return 1

    _log_result(result)
    print(json.dumps(result, indent=2, default=str))
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
