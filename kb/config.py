from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
from typing import Iterable

from kb.access import CENTRAL_DATASET

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only before dependencies are installed.

    def load_dotenv(*args: object, **kwargs: object) -> bool:
        return False


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float(value: str | None, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _state_root() -> str:
    return (
        os.getenv("CITADEL_STATE_DIRECTORY")
        or os.getenv("SYSTEM_ROOT_DIRECTORY")
        or ("/data/.citadel" if Path("/data").exists() else ".citadel")
    )


def _github_state_path(value: str | None) -> str:
    if value:
        return value
    return str(Path(_state_root()) / "github_sync_state.json")


def _repo_content_sync_state_path(value: str | None) -> str:
    if value:
        return value
    return str(Path(_state_root()) / "repo_content_sync_state.json")


def _evolve_state_path(value: str | None) -> str:
    if value:
        return value
    # Beside the sync state, which on Railway is the mounted volume. It has to
    # outlive the container: the whole point is that a redeploy resumes the
    # interval instead of restarting it (#153).
    return str(Path(_state_root()) / "evolve_state.json")


def _repair_journal_path(value: str | None) -> str:
    if value:
        return value
    return str(Path(_state_root()) / "repair_journal.jsonl")


def _cognify_queue_path(value: str | None) -> str:
    if value:
        return value
    return str(Path(_state_root()) / "cognify_queue.json")


def _repo_stats_state_path(value: str | None) -> str:
    if value:
        return value
    # Lives with the other sync state, which on Railway is the mounted volume.
    # That matters: the cache is what a redeploy reads before the first refresh
    # lands, so on the volume the public numbers survive a restart.
    return str(Path(_state_root()) / "repo_stats.json")


def _access_store_path(value: str | None) -> str:
    if value:
        return value
    root = (
        os.getenv("CITADEL_STATE_DIRECTORY")
        or os.getenv("SYSTEM_ROOT_DIRECTORY")
        or ("/data/.citadel" if Path("/data").exists() else ".citadel")
    )
    return str(Path(root) / "access.json")


def _contact_store_path(value: str | None) -> str:
    if value:
        return value
    root = (
        os.getenv("CITADEL_STATE_DIRECTORY")
        or os.getenv("SYSTEM_ROOT_DIRECTORY")
        or ("/data/.citadel" if Path("/data").exists() else ".citadel")
    )
    return str(Path(root) / "contacts.json")


def _obsidian_sync_state_path(value: str | None) -> str:
    if value:
        return value
    root = (
        os.getenv("CITADEL_STATE_DIRECTORY")
        or os.getenv("SYSTEM_ROOT_DIRECTORY")
        or ("/data/.citadel" if Path("/data").exists() else ".citadel")
    )
    return str(Path(root) / "obsidian_sync_state.json")


def _conflicts_store_path(value: str | None) -> str:
    if value:
        return value
    root = (
        os.getenv("CITADEL_STATE_DIRECTORY")
        or os.getenv("SYSTEM_ROOT_DIRECTORY")
        or ("/data/.citadel" if Path("/data").exists() else ".citadel")
    )
    return str(Path(root) / "conflicts.json")


def _linear_sync_state_path(value: str | None) -> str:
    if value:
        return value
    root = (
        os.getenv("CITADEL_STATE_DIRECTORY")
        or os.getenv("SYSTEM_ROOT_DIRECTORY")
        or ("/data/.citadel" if Path("/data").exists() else ".citadel")
    )
    return str(Path(root) / "linear_sync_state.json")


def _linear_user_map(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(item) for key, item in parsed.items() if item}


def _backup_mirror_root_path(value: str | None) -> str:
    if value:
        return value
    root = (
        os.getenv("CITADEL_STATE_DIRECTORY")
        or os.getenv("SYSTEM_ROOT_DIRECTORY")
        or ("/data/.citadel" if Path("/data").exists() else ".citadel")
    )
    return str(Path(root) / "backup_mirror")


@dataclass(frozen=True)
class CitadelConfig:
    tenant_id: str = CENTRAL_DATASET
    user_id: str = "local"
    admin_key: str | None = None
    reader_keys: tuple[str, ...] = field(default_factory=tuple)
    writer_keys: tuple[str, ...] = field(default_factory=tuple)
    access_store_path: str = ".citadel/access.json"
    contact_store_path: str = ".citadel/contacts.json"
    obsidian_sync_state_path: str = ".citadel/obsidian_sync_state.json"
    conflicts_store_path: str = ".citadel/conflicts.json"
    conflicts_max_records: int = 500
    mesh_graph_max_nodes: int = 200
    audit_max_events: int = 1000
    default_dataset: str = CENTRAL_DATASET
    search_default_dataset: str | None = None
    # Read-path bounds (#44/#50): a per-request search budget so a slow cognee
    # recall degrades to empty-fast instead of a 100s+ hang, and a concurrency cap
    # that yields a 429 + Retry-After backpressure contract instead of silent fails.
    search_timeout_seconds: float = 20.0
    search_max_concurrency: int = 8
    # The Knowledge Mesh graph read is the DEFAULT dashboard view, so a ~15-seat
    # login burst must not compete with /search for the same budget (both would
    # 429). It gets its own dedicated cap. graph_data() is TTL-cached +
    # single-flight, so concurrent mesh reads mostly hit cache and are cheap —
    # a small cpu-based cap is plenty.
    mesh_graph_max_concurrency: int = field(default_factory=lambda: max(4, os.cpu_count() or 4))
    default_session: str = "personal-session"
    default_tags: tuple[str, ...] = field(default_factory=tuple)
    min_chars: int = 3
    exclude_patterns: tuple[str, ...] = (
        ".git/*",
        ".venv/*",
        "__pycache__/*",
        "node_modules/*",
    )
    auto_improve: bool = False
    build_global_context_index: bool = False
    github_org: str = "masumi-network"
    github_sync_dataset: str = "masumi-network"
    github_sync_session: str = "masumi-github-daily"
    github_sync_state_path: str = ".citadel/github_sync_state.json"
    github_sync_max_repos: int = 100
    github_sync_max_events: int = 50
    github_sync_max_commits_per_repo: int = 5
    github_sync_max_pull_requests_per_repo: int = 5
    github_sync_include_commits: bool = True
    github_sync_run_improve: bool = True
    github_sync_ingest_unchanged: bool = True
    github_sync_include_private: bool = True
    github_sync_repo_allowlist: tuple[str, ...] = field(default_factory=tuple)
    github_sync_repo_denylist: tuple[str, ...] = field(default_factory=tuple)
    github_sync_security_scan_enabled: bool = True
    github_sync_security_block_severity: str = "high"
    github_webhook_enabled: bool = False
    github_webhook_secret: str = ""
    content_scan_enabled: bool = True
    content_scan_block_severity: str = "high"
    promotion_enabled: bool = False
    promotion_dry_run: bool = True
    promotion_relevance_threshold: float = 0.7
    promotion_max_items: int = 20
    evolve_scheduler_enabled: bool = False
    evolve_interval_seconds: int = 21600
    github_token: str | None = None
    # Live repo figures for the public pages. On by default: it is one GitHub
    # request a day, it degrades to the cached value, and the alternative is
    # numbers that are wrong and say nothing about it.
    repo_stats_enabled: bool = True
    repo_stats_repo: str = "masumi-network/Citadel"
    repo_stats_state_path: str = ".citadel/repo_stats.json"
    repo_stats_interval_seconds: int = 86400
    repo_content_sync_enabled: bool = True
    repo_content_sync_dataset: str = "masumi-network"
    repo_content_sync_session: str = "masumi-repo-content"
    repo_content_sync_state_path: str = ".citadel/repo_content_sync_state.json"
    evolve_state_path: str = ".citadel/evolve_state.json"
    repair_journal_path: str = ".citadel/repair_journal.jsonl"
    cognify_queue_path: str = ".citadel/cognify_queue.json"
    repo_content_sync_repos: tuple[str, ...] = field(default_factory=tuple)
    repo_content_sync_root_paths: tuple[str, ...] = field(default_factory=tuple)
    repo_content_sync_tree_prefixes: tuple[str, ...] = field(default_factory=tuple)
    repo_content_sync_tree_extensions: tuple[str, ...] = field(default_factory=tuple)
    repo_content_sync_max_files_per_repo: int = 40
    repo_content_sync_max_bytes_per_file: int = 120_000
    repo_content_sync_run_improve: bool = True
    repo_content_sync_autojoin_enabled: bool = False
    repo_content_sync_autojoin_markers: tuple[str, ...] = field(default_factory=tuple)
    repo_content_sync_autojoin_max_repos: int = 100
    contribute_run_improve: bool = False
    organization_digest_enabled: bool = True
    organization_digest_window_hours: int = 24
    organization_digest_max_items: int = 6
    organization_digest_llm_enabled: bool = True
    organization_digest_llm_allow_private: bool = False
    organization_digest_post_on_no_updates: bool = False
    # Generic outbound webhook. One adapter covers Discord, Mattermost, n8n,
    # Zapier and bespoke receivers; see kb/webhook_gateway.py for why it applies
    # a stricter URL policy than kb/secure_http.py.
    webhook_enabled: bool = False
    webhook_url: str | None = None
    webhook_token: str | None = None
    webhook_max_message_bytes: int = 30000
    webhook_timeout_seconds: int = 20

    google_chat_enabled: bool = False
    google_chat_space_name: str | None = None
    google_chat_service_account_json: str | None = None
    google_chat_service_account_file: str | None = None
    google_chat_thread_key: str = "citadel-org-digest"
    google_chat_message_prefix: str = "citadel-org-digest"
    google_chat_max_message_bytes: int = 30000
    google_chat_timeout_seconds: int = 20
    google_chat_retry_count: int = 2
    backup_mirror_repo: str = "masumi-network/Vault-Backup-Mirror"
    backup_mirror_enabled: bool = False
    backup_mirror_push_enabled: bool = False
    backup_mirror_branch: str = "main"
    backup_mirror_root_path: str = ".citadel/backup_mirror"
    backup_mirror_token: str | None = None
    linear_api_key: str | None = None
    linear_sync_dataset: str = "masumi-network"
    linear_sync_session: str = "masumi-linear"
    linear_sync_state_path: str = ".citadel/linear_sync_state.json"
    linear_sync_max_issues: int = 200
    linear_sync_run_improve: bool = False
    linear_user_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, env_file: str | None = ".env") -> "CitadelConfig":
        if env_file:
            load_dotenv(env_file, override=False)

        return cls(
            tenant_id=os.getenv("CITADEL_TENANT_ID", CENTRAL_DATASET),
            user_id=os.getenv("CITADEL_USER_ID", "local"),
            admin_key=os.getenv("CITADEL_ADMIN_KEY") or None,
            reader_keys=tuple(_csv(os.getenv("CITADEL_READER_KEYS"))),
            writer_keys=tuple(_csv(os.getenv("CITADEL_WRITER_KEYS"))),
            access_store_path=_access_store_path(os.getenv("CITADEL_ACCESS_STORE_PATH")),
            contact_store_path=_contact_store_path(os.getenv("CITADEL_CONTACT_STORE_PATH")),
            obsidian_sync_state_path=_obsidian_sync_state_path(
                os.getenv("CITADEL_OBSIDIAN_SYNC_STATE_PATH")
            ),
            conflicts_store_path=_conflicts_store_path(os.getenv("CITADEL_CONFLICTS_STORE_PATH")),
            conflicts_max_records=_int(
                os.getenv("CITADEL_CONFLICTS_MAX_RECORDS"),
                default=500,
            ),
            mesh_graph_max_nodes=_int(
                os.getenv("CITADEL_MESH_GRAPH_MAX_NODES"),
                default=200,
            ),
            audit_max_events=_int(os.getenv("CITADEL_AUDIT_MAX_EVENTS"), default=1000),
            default_dataset=os.getenv("CITADEL_DEFAULT_DATASET", CENTRAL_DATASET),
            search_default_dataset=os.getenv("CITADEL_SEARCH_DEFAULT_DATASET") or None,
            search_timeout_seconds=_float(
                os.getenv("CITADEL_SEARCH_TIMEOUT_SECONDS"), default=20.0
            ),
            search_max_concurrency=_int(os.getenv("CITADEL_SEARCH_MAX_CONCURRENCY"), default=8),
            mesh_graph_max_concurrency=_int(
                os.getenv("CITADEL_MESH_GRAPH_MAX_CONCURRENCY"),
                default=max(4, os.cpu_count() or 4),
            ),
            default_session=os.getenv("CITADEL_DEFAULT_SESSION", "personal-session"),
            default_tags=tuple(_csv(os.getenv("CITADEL_DEFAULT_TAGS"))),
            min_chars=int(os.getenv("CITADEL_MIN_CHARS", "3")),
            exclude_patterns=tuple(
                _csv(os.getenv("CITADEL_EXCLUDE_PATTERNS"))
                or [".git/*", ".venv/*", "__pycache__/*", "node_modules/*"]
            ),
            auto_improve=_bool(os.getenv("CITADEL_AUTO_IMPROVE")),
            build_global_context_index=_bool(os.getenv("CITADEL_BUILD_GLOBAL_CONTEXT_INDEX")),
            github_org=os.getenv("CITADEL_GITHUB_ORG", "masumi-network"),
            github_sync_dataset=os.getenv("CITADEL_GITHUB_SYNC_DATASET", "masumi-network"),
            github_sync_session=os.getenv("CITADEL_GITHUB_SYNC_SESSION", "masumi-github-daily"),
            github_sync_state_path=_github_state_path(os.getenv("CITADEL_GITHUB_SYNC_STATE_PATH")),
            github_sync_max_repos=_int(os.getenv("CITADEL_GITHUB_SYNC_MAX_REPOS"), default=100),
            github_sync_max_events=_int(os.getenv("CITADEL_GITHUB_SYNC_MAX_EVENTS"), default=50),
            github_sync_max_commits_per_repo=_int(
                os.getenv("CITADEL_GITHUB_SYNC_MAX_COMMITS_PER_REPO"),
                default=5,
            ),
            github_sync_max_pull_requests_per_repo=_int(
                os.getenv("CITADEL_GITHUB_SYNC_MAX_PULL_REQUESTS_PER_REPO"),
                default=5,
            ),
            github_sync_include_commits=_bool(
                os.getenv("CITADEL_GITHUB_SYNC_INCLUDE_COMMITS"),
                default=True,
            ),
            github_sync_run_improve=_bool(
                os.getenv("CITADEL_GITHUB_SYNC_RUN_IMPROVE"), default=True
            ),
            github_sync_ingest_unchanged=_bool(
                os.getenv("CITADEL_GITHUB_SYNC_INGEST_UNCHANGED"),
                default=True,
            ),
            github_sync_include_private=_bool(
                os.getenv("CITADEL_GITHUB_SYNC_INCLUDE_PRIVATE"),
                default=True,
            ),
            github_sync_repo_allowlist=tuple(_csv(os.getenv("CITADEL_GITHUB_SYNC_REPO_ALLOWLIST"))),
            github_sync_repo_denylist=tuple(_csv(os.getenv("CITADEL_GITHUB_SYNC_REPO_DENYLIST"))),
            github_sync_security_scan_enabled=_bool(
                os.getenv("CITADEL_GITHUB_SYNC_SECURITY_SCAN_ENABLED"),
                default=True,
            ),
            github_sync_security_block_severity=os.getenv(
                "CITADEL_GITHUB_SYNC_SECURITY_BLOCK_SEVERITY",
                "high",
            ),
            github_webhook_enabled=_bool(
                os.getenv("CITADEL_GITHUB_WEBHOOK_ENABLED"),
                default=False,
            ),
            github_webhook_secret=os.getenv("CITADEL_GITHUB_WEBHOOK_SECRET", ""),
            content_scan_enabled=_bool(
                os.getenv("CITADEL_CONTENT_SCAN_ENABLED"),
                default=True,
            ),
            content_scan_block_severity=os.getenv(
                "CITADEL_CONTENT_SCAN_BLOCK_SEVERITY",
                "high",
            ),
            promotion_enabled=_bool(
                os.getenv("CITADEL_PROMOTION_ENABLED"),
                default=False,
            ),
            promotion_dry_run=_bool(
                os.getenv("CITADEL_PROMOTION_DRY_RUN"),
                default=True,
            ),
            promotion_relevance_threshold=_float(
                os.getenv("CITADEL_PROMOTION_RELEVANCE_THRESHOLD"),
                default=0.7,
            ),
            promotion_max_items=_int(
                os.getenv("CITADEL_PROMOTION_MAX_ITEMS"),
                default=20,
            ),
            evolve_scheduler_enabled=_bool(
                os.getenv("CITADEL_EVOLVE_SCHEDULER_ENABLED"),
                default=False,
            ),
            evolve_interval_seconds=_int(
                os.getenv("CITADEL_EVOLVE_INTERVAL_SECONDS"),
                default=21600,
            ),
            github_token=os.getenv("CITADEL_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or None,
            repo_stats_enabled=_bool(
                os.getenv("CITADEL_REPO_STATS_ENABLED"),
                default=True,
            ),
            repo_stats_repo=os.getenv("CITADEL_REPO_STATS_REPO", "masumi-network/Citadel"),
            repo_stats_state_path=_repo_stats_state_path(
                os.getenv("CITADEL_REPO_STATS_STATE_PATH")
            ),
            repo_stats_interval_seconds=_int(
                os.getenv("CITADEL_REPO_STATS_INTERVAL_SECONDS"),
                default=86400,
            ),
            repo_content_sync_enabled=_bool(
                os.getenv("CITADEL_REPO_CONTENT_SYNC_ENABLED"),
                default=True,
            ),
            repo_content_sync_dataset=os.getenv(
                "CITADEL_REPO_CONTENT_SYNC_DATASET",
                os.getenv("CITADEL_GITHUB_SYNC_DATASET", "masumi-network"),
            ),
            repo_content_sync_session=os.getenv(
                "CITADEL_REPO_CONTENT_SYNC_SESSION",
                "masumi-repo-content",
            ),
            repo_content_sync_state_path=_repo_content_sync_state_path(
                os.getenv("CITADEL_REPO_CONTENT_SYNC_STATE_PATH")
            ),
            evolve_state_path=_evolve_state_path(os.getenv("CITADEL_EVOLVE_STATE_PATH")),
            repair_journal_path=_repair_journal_path(os.getenv("CITADEL_REPAIR_JOURNAL_PATH")),
            cognify_queue_path=_cognify_queue_path(os.getenv("CITADEL_COGNIFY_QUEUE_PATH")),
            repo_content_sync_repos=tuple(_csv(os.getenv("CITADEL_REPO_CONTENT_SYNC_REPOS"))),
            repo_content_sync_root_paths=tuple(
                _csv(os.getenv("CITADEL_REPO_CONTENT_SYNC_ROOT_PATHS"))
            ),
            repo_content_sync_tree_prefixes=tuple(
                _csv(os.getenv("CITADEL_REPO_CONTENT_SYNC_TREE_PREFIXES"))
            ),
            repo_content_sync_tree_extensions=tuple(
                _csv(os.getenv("CITADEL_REPO_CONTENT_SYNC_TREE_EXTENSIONS"))
            ),
            repo_content_sync_max_files_per_repo=_int(
                os.getenv("CITADEL_REPO_CONTENT_SYNC_MAX_FILES_PER_REPO"),
                default=40,
            ),
            repo_content_sync_max_bytes_per_file=_int(
                os.getenv("CITADEL_REPO_CONTENT_SYNC_MAX_BYTES_PER_FILE"),
                default=120_000,
            ),
            repo_content_sync_run_improve=_bool(
                os.getenv("CITADEL_REPO_CONTENT_SYNC_RUN_IMPROVE"),
                default=True,
            ),
            repo_content_sync_autojoin_enabled=_bool(
                os.getenv("CITADEL_REPO_CONTENT_SYNC_AUTOJOIN_ENABLED"),
                default=False,
            ),
            repo_content_sync_autojoin_markers=tuple(
                _csv(os.getenv("CITADEL_REPO_CONTENT_SYNC_AUTOJOIN_MARKERS"))
            ),
            repo_content_sync_autojoin_max_repos=_int(
                os.getenv("CITADEL_REPO_CONTENT_SYNC_AUTOJOIN_MAX_REPOS"),
                default=100,
            ),
            contribute_run_improve=_bool(
                os.getenv("CITADEL_CONTRIBUTE_RUN_IMPROVE"),
                default=False,
            ),
            organization_digest_enabled=_bool(
                os.getenv("CITADEL_ORG_DIGEST_ENABLED"),
                default=True,
            ),
            organization_digest_window_hours=_int(
                os.getenv("CITADEL_ORG_DIGEST_WINDOW_HOURS"),
                default=24,
            ),
            organization_digest_max_items=_int(
                os.getenv("CITADEL_ORG_DIGEST_MAX_ITEMS"),
                default=6,
            ),
            organization_digest_llm_enabled=_bool(
                os.getenv("CITADEL_ORG_DIGEST_LLM_ENABLED"),
                default=True,
            ),
            organization_digest_llm_allow_private=_bool(
                os.getenv("CITADEL_ORG_DIGEST_LLM_ALLOW_PRIVATE"),
                default=False,
            ),
            organization_digest_post_on_no_updates=_bool(
                os.getenv("CITADEL_ORG_DIGEST_POST_ON_NO_UPDATES"),
                default=False,
            ),
            webhook_enabled=_bool(os.getenv("CITADEL_WEBHOOK_ENABLED"), default=False),
            webhook_url=os.getenv("CITADEL_WEBHOOK_URL") or None,
            webhook_token=os.getenv("CITADEL_WEBHOOK_TOKEN") or None,
            webhook_max_message_bytes=_int(
                os.getenv("CITADEL_WEBHOOK_MAX_MESSAGE_BYTES"), default=30000
            ),
            webhook_timeout_seconds=_int(os.getenv("CITADEL_WEBHOOK_TIMEOUT_SECONDS"), default=20),
            google_chat_enabled=_bool(os.getenv("CITADEL_GOOGLE_CHAT_ENABLED"), default=False),
            google_chat_space_name=os.getenv("CITADEL_GOOGLE_CHAT_SPACE_NAME") or None,
            google_chat_service_account_json=(
                os.getenv("CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON") or None
            ),
            google_chat_service_account_file=(
                os.getenv("CITADEL_GOOGLE_CHAT_SERVICE_ACCOUNT_FILE") or None
            ),
            google_chat_thread_key=os.getenv(
                "CITADEL_GOOGLE_CHAT_THREAD_KEY",
                "citadel-org-digest",
            ),
            google_chat_message_prefix=os.getenv(
                "CITADEL_GOOGLE_CHAT_MESSAGE_PREFIX",
                "citadel-org-digest",
            ),
            google_chat_max_message_bytes=_int(
                os.getenv("CITADEL_GOOGLE_CHAT_MAX_MESSAGE_BYTES"),
                default=30000,
            ),
            google_chat_timeout_seconds=_int(
                os.getenv("CITADEL_GOOGLE_CHAT_TIMEOUT_SECONDS"),
                default=20,
            ),
            google_chat_retry_count=_int(
                os.getenv("CITADEL_GOOGLE_CHAT_RETRY_COUNT"),
                default=2,
            ),
            backup_mirror_repo=os.getenv(
                "CITADEL_BACKUP_MIRROR_REPO",
                "masumi-network/Vault-Backup-Mirror",
            ),
            backup_mirror_enabled=_bool(os.getenv("CITADEL_BACKUP_MIRROR_ENABLED")),
            backup_mirror_push_enabled=_bool(os.getenv("CITADEL_BACKUP_MIRROR_PUSH_ENABLED")),
            backup_mirror_branch=os.getenv("CITADEL_BACKUP_MIRROR_BRANCH", "main"),
            backup_mirror_root_path=_backup_mirror_root_path(
                os.getenv("CITADEL_BACKUP_MIRROR_ROOT_PATH")
            ),
            backup_mirror_token=(
                os.getenv("CITADEL_BACKUP_MIRROR_TOKEN")
                or os.getenv("CITADEL_BACKUP_MIRROR_GITHUB_TOKEN")
                or None
            ),
            linear_api_key=os.getenv("CITADEL_LINEAR_API_KEY")
            or os.getenv("LINEAR_API_KEY")
            or None,
            linear_sync_dataset=os.getenv("CITADEL_LINEAR_SYNC_DATASET", "masumi-network"),
            linear_sync_session=os.getenv("CITADEL_LINEAR_SYNC_SESSION", "masumi-linear"),
            linear_sync_state_path=_linear_sync_state_path(
                os.getenv("CITADEL_LINEAR_SYNC_STATE_PATH")
            ),
            linear_sync_max_issues=_int(os.getenv("CITADEL_LINEAR_SYNC_MAX_ISSUES"), default=200),
            linear_sync_run_improve=_bool(os.getenv("CITADEL_LINEAR_SYNC_RUN_IMPROVE")),
            linear_user_map=_linear_user_map(os.getenv("CITADEL_LINEAR_USER_MAP")),
        )

    def with_tags(self, tags: Iterable[str]) -> "CitadelConfig":
        merged = tuple(dict.fromkeys([*self.default_tags, *tags]))
        return replace(self, default_tags=merged)
