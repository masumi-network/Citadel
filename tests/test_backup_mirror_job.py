from __future__ import annotations

from typing import Any

from scripts import run_backup_mirror


def _clear_mirror_env(monkeypatch: Any) -> None:
    for name in (
        "CITADEL_ADMIN_KEY",
        "CITADEL_BACKUP_MIRROR_ACCESS_KEY",
        "CITADEL_BACKUP_MIRROR_DRY_RUN",
        "CITADEL_BACKUP_MIRROR_ENDPOINT",
        "CITADEL_BACKUP_MIRROR_TARGET_URL",
        "CITADEL_BASE_URL",
        "CITADEL_WEB_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_backup_mirror_job_posts_to_web_endpoint(monkeypatch: Any) -> None:
    _clear_mirror_env(monkeypatch)
    calls: list[dict[str, Any]] = []

    def post_json(
        url: str,
        *,
        payload: dict[str, Any],
        access_key: str,
        timeout: int,
    ) -> tuple[int, dict[str, Any]]:
        calls.append(
            {
                "url": url,
                "payload": payload,
                "access_key": access_key,
                "timeout": timeout,
            }
        )
        return (
            200,
            {
                "ok": True,
                "dry_run": False,
                "written": True,
                "manifest": {
                    "summary": {
                        "tracked_files": 3,
                        "available_files": 2,
                        "total_bytes": 120,
                    }
                },
            },
        )

    monkeypatch.setenv("CITADEL_BACKUP_MIRROR_TARGET_URL", "https://citadel.example")
    monkeypatch.setenv("CITADEL_BACKUP_MIRROR_ACCESS_KEY", "secret")
    monkeypatch.setenv("CITADEL_BACKUP_MIRROR_DRY_RUN", "false")
    monkeypatch.setattr(run_backup_mirror, "_post_json", post_json)

    assert run_backup_mirror.run() == 0
    assert calls == [
        {
            "url": "https://citadel.example/api/backup-mirror/run",
            "payload": {"dry_run": False},
            "access_key": "secret",
            "timeout": 300,
        }
    ]


def test_backup_mirror_job_defaults_to_dry_run(monkeypatch: Any) -> None:
    _clear_mirror_env(monkeypatch)
    calls: list[dict[str, Any]] = []

    def post_json(
        url: str,
        *,
        payload: dict[str, Any],
        access_key: str,
        timeout: int,
    ) -> tuple[int, dict[str, Any]]:
        calls.append({"payload": payload})
        return 200, {"ok": True, "manifest": {"summary": {}}}

    monkeypatch.setenv("CITADEL_BACKUP_MIRROR_TARGET_URL", "https://citadel.example")
    monkeypatch.setenv("CITADEL_BACKUP_MIRROR_ACCESS_KEY", "secret")
    monkeypatch.setattr(run_backup_mirror, "_post_json", post_json)

    assert run_backup_mirror.run() == 0
    assert calls == [{"payload": {"dry_run": True}}]


def test_backup_mirror_job_requires_access_key_for_web_target(monkeypatch: Any) -> None:
    _clear_mirror_env(monkeypatch)
    monkeypatch.setenv("CITADEL_BACKUP_MIRROR_TARGET_URL", "https://citadel.example")

    assert run_backup_mirror.run() == 1


def test_backup_mirror_job_fails_on_remote_http_error(monkeypatch: Any) -> None:
    _clear_mirror_env(monkeypatch)

    def post_json(
        url: str,
        *,
        payload: dict[str, Any],
        access_key: str,
        timeout: int,
    ) -> tuple[int, dict[str, Any]]:
        return 409, {"detail": "disabled"}

    monkeypatch.setenv("CITADEL_BACKUP_MIRROR_TARGET_URL", "https://citadel.example")
    monkeypatch.setenv("CITADEL_BACKUP_MIRROR_ACCESS_KEY", "secret")
    monkeypatch.setattr(run_backup_mirror, "_post_json", post_json)

    assert run_backup_mirror.run() == 1


def test_post_json_converts_read_timeout_to_clean_result(monkeypatch: Any) -> None:
    # #116: a read-phase timeout is a bare TimeoutError, not a URLError, and
    # would otherwise escape this helper's HTTPError/URLError-only handling
    # as a raw traceback (the #39 bug, unbackported to this copy).
    def boom(request: Any, timeout: int) -> None:
        raise TimeoutError("the read operation timed out")

    monkeypatch.setattr(run_backup_mirror, "urlopen", boom)
    status, body = run_backup_mirror._post_json(
        "https://citadel.example/api/backup-mirror/run",
        payload={},
        access_key="secret",
        timeout=5,
    )
    assert status == 599
    assert "timed out" in body["detail"].lower()
