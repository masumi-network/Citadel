from __future__ import annotations


from kb.access import CENTRAL_DATASET
from kb.config import CitadelConfig


def test_defaults_resolve_to_central_dataset() -> None:
    """The server-level defaults must be the shared Central dataset, not the
    literal "personal" string. Otherwise the mesh creates a phantom "personal"
    dataset node next to Central and /readyz reports tenant "personal"."""
    config = CitadelConfig()
    assert config.tenant_id == CENTRAL_DATASET
    assert config.default_dataset == CENTRAL_DATASET
    assert config.tenant_id != "personal"
    assert config.default_dataset != "personal"


def test_from_env_defaults_to_central_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("CITADEL_TENANT_ID", raising=False)
    monkeypatch.delenv("CITADEL_DEFAULT_DATASET", raising=False)
    config = CitadelConfig.from_env()
    assert config.tenant_id == CENTRAL_DATASET
    assert config.default_dataset == CENTRAL_DATASET


def test_from_env_env_vars_still_override(monkeypatch) -> None:
    """Explicit env vars still win over the Central default."""
    monkeypatch.setenv("CITADEL_TENANT_ID", "explicit-org")
    monkeypatch.setenv("CITADEL_DEFAULT_DATASET", "explicit-dataset")
    config = CitadelConfig.from_env()
    assert config.tenant_id == "explicit-org"
    assert config.default_dataset == "explicit-dataset"


def test_cognify_queue_path_defaults_to_state_root_and_honors_override(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CITADEL_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.delenv("CITADEL_COGNIFY_QUEUE_PATH", raising=False)
    config = CitadelConfig.from_env(env_file=None)
    assert config.cognify_queue_path == str(tmp_path / "state" / "cognify_queue.json")

    explicit = tmp_path / "explicit-queue.json"
    monkeypatch.setenv("CITADEL_COGNIFY_QUEUE_PATH", str(explicit))
    assert CitadelConfig.from_env(env_file=None).cognify_queue_path == str(explicit)


def test_lifecycle_store_path_defaults_to_state_root_and_honors_override(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CITADEL_STATE_DIRECTORY", str(tmp_path / "state"))
    monkeypatch.delenv("CITADEL_LIFECYCLE_STORE_PATH", raising=False)
    config = CitadelConfig.from_env(env_file=None)
    assert config.lifecycle_enabled is True
    assert config.lifecycle_store_path == str(tmp_path / "state" / "lifecycle.sqlite3")

    explicit = tmp_path / "explicit-lifecycle.sqlite3"
    monkeypatch.setenv("CITADEL_LIFECYCLE_STORE_PATH", str(explicit))
    assert CitadelConfig.from_env(env_file=None).lifecycle_store_path == str(explicit)

    monkeypatch.setenv("CITADEL_LIFECYCLE_ENABLED", "false")
    assert CitadelConfig.from_env(env_file=None).lifecycle_enabled is False


def test_repo_content_autojoin_env(monkeypatch) -> None:
    monkeypatch.setenv("CITADEL_REPO_CONTENT_SYNC_AUTOJOIN_ENABLED", "true")
    monkeypatch.setenv("CITADEL_REPO_CONTENT_SYNC_AUTOJOIN_MARKERS", "AGENTS.md, SKILL.md")
    monkeypatch.setenv("CITADEL_REPO_CONTENT_SYNC_AUTOJOIN_MAX_REPOS", "25")
    config = CitadelConfig.from_env(env_file=None)
    assert config.repo_content_sync_autojoin_enabled is True
    assert config.repo_content_sync_autojoin_markers == ("AGENTS.md", "SKILL.md")
    assert config.repo_content_sync_autojoin_max_repos == 25


def test_repo_content_autojoin_defaults_off() -> None:
    config = CitadelConfig()
    assert config.repo_content_sync_autojoin_enabled is False
    assert config.repo_content_sync_autojoin_markers == ()
    assert config.repo_content_sync_autojoin_max_repos == 100
