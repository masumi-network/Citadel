from __future__ import annotations

from typing import Any

from scripts import run_github_sync


def _clear_sync_env(monkeypatch: Any) -> None:
    for name in (
        "CITADEL_ADMIN_KEY",
        "CITADEL_BASE_URL",
        "CITADEL_GITHUB_SYNC_ACCESS_KEY",
        "CITADEL_GITHUB_SYNC_DRY_RUN",
        "CITADEL_GITHUB_SYNC_ENDPOINT",
        "CITADEL_GITHUB_SYNC_FORCE",
        "CITADEL_GITHUB_SYNC_OUTPUT_MODE",
        "CITADEL_GITHUB_SYNC_TARGET_URL",
        "CITADEL_ORG_DIGEST_INCLUDE_PREVIEW_IN_CRON_OUTPUT",
        "CITADEL_ORG_DIGEST_POST_TO_CHAT",
        "CITADEL_WEB_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_scheduled_sync_posts_to_web_learning_agent(monkeypatch: Any) -> None:
    _clear_sync_env(monkeypatch)
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
                "ingested": True,
                "improved": True,
                "sources": {
                    "github": {
                        "repos_scanned": 40,
                        "changed_count": 2,
                        "event_count": 3,
                        "commit_count": 4,
                    }
                },
            },
        )

    monkeypatch.setenv("CITADEL_GITHUB_SYNC_TARGET_URL", "https://citadel.example")
    monkeypatch.setenv("CITADEL_GITHUB_SYNC_ACCESS_KEY", "secret")
    monkeypatch.setenv("CITADEL_GITHUB_SYNC_FORCE", "true")
    monkeypatch.setenv("CITADEL_GITHUB_SYNC_DRY_RUN", "false")
    monkeypatch.setattr(run_github_sync, "_post_json", post_json)

    assert run_github_sync.run() == 0
    assert calls == [
        {
            "url": "https://citadel.example/api/learning-agent/run",
            "payload": {
                "force": True,
                "dry_run": False,
                "post_to_chat": True,
                "include_digest_preview": False,
            },
            "access_key": "secret",
            "timeout": 900,
        }
    ]


def test_scheduled_sync_requires_access_key_for_web_target(monkeypatch: Any) -> None:
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("CITADEL_GITHUB_SYNC_TARGET_URL", "https://citadel.example")

    assert run_github_sync.run() == 1


def test_scheduled_sync_redacts_private_metadata_from_default_output(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    _clear_sync_env(monkeypatch)

    def post_json(
        url: str,
        *,
        payload: dict[str, Any],
        access_key: str,
        timeout: int,
    ) -> tuple[int, dict[str, Any]]:
        return (
            200,
            {
                "ok": True,
                "ingested": True,
                "improved": False,
                "dry_run": False,
                "sources": {
                    "github": {
                        "repos_scanned": 40,
                        "changed_count": 2,
                        "event_count": 3,
                        "commit_count": 4,
                        "open_pull_request_count": 1,
                        "merged_pull_request_count": 1,
                        "changed_repositories": [
                            {"full_name": "private-org/private-repo"}
                        ],
                        "recent_commits": [
                            {"message": "private customer incident details"}
                        ],
                        "digest": "private repo digest body",
                    }
                },
                "organization_digest": {
                    "preview": "private organization digest body",
                },
            },
        )

    monkeypatch.setenv("CITADEL_GITHUB_SYNC_TARGET_URL", "https://citadel.example")
    monkeypatch.setenv("CITADEL_GITHUB_SYNC_ACCESS_KEY", "secret")
    monkeypatch.setattr(run_github_sync, "_post_json", post_json)

    assert run_github_sync.run() == 0
    output = capsys.readouterr().out
    assert '"repos_scanned": 40' in output
    assert "private-org" not in output
    assert "private customer" not in output
    assert "private repo digest" not in output
    assert "private organization digest" not in output


def test_scheduled_sync_fails_on_remote_http_error(monkeypatch: Any) -> None:
    _clear_sync_env(monkeypatch)

    def post_json(
        url: str,
        *,
        payload: dict[str, Any],
        access_key: str,
        timeout: int,
    ) -> tuple[int, dict[str, Any]]:
        return 500, {"detail": "boom"}

    monkeypatch.setenv("CITADEL_GITHUB_SYNC_TARGET_URL", "https://citadel.example")
    monkeypatch.setenv("CITADEL_GITHUB_SYNC_ACCESS_KEY", "secret")
    monkeypatch.setattr(run_github_sync, "_post_json", post_json)

    assert run_github_sync.run() == 1


def test_gateway_delivery_summary_prefers_generic_gateways() -> None:
    result = {
        "notifications": {
            "google_chat": {"sent": False, "reason": "compatibility_value"},
            "gateways": {
                "internal_webhook": {"sent": True},
                "google_chat": {"sent": False, "reason": "preview_only"},
            },
        }
    }

    assert (
        run_github_sync._gateway_delivery_summary(result)
        == "google_chat:preview_only,internal_webhook:sent"
    )


def test_gateway_delivery_summary_falls_back_to_google_chat() -> None:
    result = {
        "notifications": {
            "google_chat": {"sent": False, "status_category": "delivery_error"}
        }
    }

    assert (
        run_github_sync._gateway_delivery_summary(result)
        == "google_chat:delivery_error"
    )


def test_post_json_converts_read_timeout_to_clean_result(monkeypatch: Any) -> None:
    # #116: a read-phase timeout is a bare TimeoutError, not a URLError, and
    # would otherwise escape this helper's HTTPError/URLError-only handling
    # as a raw traceback (the #39 bug, unbackported to this copy).
    def boom(request: Any, timeout: int) -> None:
        raise TimeoutError("the read operation timed out")

    monkeypatch.setattr(run_github_sync, "urlopen", boom)
    status, body = run_github_sync._post_json(
        "https://citadel.example/api/learning-agent/run",
        payload={},
        access_key="secret",
        timeout=5,
    )
    assert status == 599
    assert "timed out" in body["detail"].lower()
