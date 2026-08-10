import tomllib
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/test.yml"
DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
DOCKERIGNORE = Path(__file__).resolve().parents[1] / ".dockerignore"
COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
DEPLOY_COMPOSE = Path(__file__).resolve().parents[1] / "kb/deploy_assets/docker-compose.yml"
LITE_ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.lite.example"
PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


def test_runtime_dependency_pins_match_the_production_assertion() -> None:
    server_dependencies = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]["server"]
    requirements = {
        line.strip() for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    expected_pins = {
        "cognee[fastembed]==1.4.1",
        "ladybug==0.18.2",
        "qdrant-client==1.19.0",
    }

    assert expected_pins <= set(server_dependencies)
    assert expected_pins <= requirements
    assert "(version('cognee'), version('ladybug'), version('qdrant-client'))" in dockerfile
    assert "('1.4.1', '0.18.2', '1.19.0')" in dockerfile


def test_ci_uses_a_dedicated_docker_test_target_for_qdrant_contracts() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_tests = workflow.split("  container-tests:\n", 1)[1].split(
        "\n  container-runtime:\n", 1
    )[0]
    container_runtime = workflow.split("  container-runtime:\n", 1)[1].split(
        "\n  gate:\n", 1
    )[0]
    gate = workflow.split("  gate:\n", 1)[1]
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()

    assert "FROM runtime AS test" in dockerfile
    assert "USER 10001:10001" in dockerfile
    test_stage = dockerfile.split("FROM runtime AS test\n", 1)[1].split(
        "FROM runtime AS production\n", 1
    )[0]
    assert "apt-get install --no-install-recommends -y git nodejs" in test_stage
    assert '"pytest==9.1.1"' in test_stage
    assert '"pytest-asyncio==1.4.0"' in test_stage
    assert '"ruff==0.15.15"' in test_stage
    stages = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert stages[-1] == "FROM runtime AS production"
    production = dockerfile.rsplit("FROM runtime AS production\n", 1)[1]
    assert "USER 10001:10001" in production
    assert "tests" not in dockerignore
    assert "docker build --target test --tag citadel-archive:ci-test ." in container_tests
    assert container_tests.count("qdrant/qdrant:v1.19.0@sha256:") == 4
    assert 'restore_success_qdrant="${network}-restore-success-qdrant"' in container_tests
    assert 'restore_rollback_qdrant="${network}-restore-rollback-qdrant"' in container_tests
    assert "CITADEL_QDRANT_SERVER_IMAGE" in container_tests
    assert "CITADEL_LIFECYCLE_ENABLED=false" not in container_tests
    assert "docker logs --follow --timestamps" in container_tests
    assert 'tee "$log_dir/source-qdrant.log"' in container_tests
    assert 'tee "$log_dir/restore-success-qdrant.log"' in container_tests
    assert 'tee "$log_dir/restore-rollback-qdrant.log"' in container_tests
    assert "assert_clean_container_logs" in container_tests
    assert "HTTP/[0-9.]+\" [45][0-9][0-9]" in container_tests
    assert "warn(ing)?" in container_tests
    assert "failure" in container_tests
    assert "expected_qdrant_log_pattern" in container_tests
    assert "TLS (is )?disabled" in container_tests
    assert "recovery.*(shortfall|failed)" in container_tests
    assert "python -m ruff check ." in container_tests
    assert 'python -m pytest -q -m "not live"' in container_tests
    assert "tests/test_qdrant_adapter_live.py" in container_tests
    assert "tests/test_cognee_qdrant_sqlite_live.py" in container_tests
    assert "tests/test_generation_backup_live.py::test_generation_backup_restores_fresh_qdrant_and_lite_root" in container_tests
    assert "tests/test_generation_backup_live.py::test_generation_restore_count_mismatch_rolls_back_real_qdrant_and_lite_root" in container_tests
    assert "CITADEL_QDRANT_LIVE_URL=http://$source_qdrant:6333" in container_tests
    assert "CITADEL_QDRANT_RESTORE_URL=http://$restore_success_qdrant:6333" in container_tests
    assert "CITADEL_QDRANT_RESTORE_URL=http://$restore_rollback_qdrant:6333" in container_tests
    assert "docker compose" in container_runtime
    assert "docker compose --project-name \"$project\" --env-file \"$env_file\" up" in container_runtime
    assert "docker compose --project-name \"$project\" logs --follow" in container_runtime
    assert 'tee "$log_dir/qdrant.log"' in container_runtime
    assert 'tee "$log_dir/citadel.log"' in container_runtime
    assert "assert_clean_runtime_logs" in container_runtime
    assert "warn(ing)?" in container_runtime
    assert "failure" in container_runtime
    assert "expected_qdrant_log_pattern" in container_runtime
    assert "TLS (is )?disabled" in container_runtime
    assert "expected_citadel_http_pattern" in container_runtime
    assert "GET /readyz HTTP/1.1\" 401 Unauthorized" in container_runtime
    assert "docker inspect --format '{{.Config.User}}' \"$app\"" in container_runtime
    assert "/readyz" in container_runtime
    assert "container-tests" in gate
    assert "container-runtime" in gate


def test_compose_connection_preflight_is_opt_in_outside_offline_readiness_ci() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_tests = workflow.split("  container-tests:\n", 1)[1].split(
        "\n  container-runtime:\n", 1
    )[0]
    container_runtime = workflow.split("  container-runtime:\n", 1)[1].split(
        "\n  gate:\n", 1
    )[0]
    compose = COMPOSE.read_text(encoding="utf-8")
    deploy_compose = DEPLOY_COMPOSE.read_text(encoding="utf-8")
    lite_env_example = LITE_ENV_EXAMPLE.read_text(encoding="utf-8")
    default = "COGNEE_SKIP_CONNECTION_TEST: ${COGNEE_SKIP_CONNECTION_TEST:-false}"

    assert compose == deploy_compose
    assert compose.count(default) == 1
    assert "COGNEE_SKIP_CONNECTION_TEST=true" not in compose
    assert "COGNEE_SKIP_CONNECTION_TEST=false" in lite_env_example
    assert "COGNEE_SKIP_CONNECTION_TEST=true" not in container_tests
    assert container_runtime.count("COGNEE_SKIP_CONNECTION_TEST=true") == 1


def test_compose_citadel_uses_a_read_only_root_with_declared_writable_paths() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    deploy_compose = DEPLOY_COMPOSE.read_text(encoding="utf-8")
    citadel = compose.split("  citadel:\n", 1)[1].split("\nvolumes:\n", 1)[0]

    assert compose == deploy_compose
    assert "    read_only: true\n" in citadel
    assert "      - citadel-data:/data\n" in citadel
    assert "      - /tmp:size=256m,mode=1777\n" in citadel


def test_compose_qdrant_uses_distinct_pinned_data_and_snapshot_volumes() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    deploy_compose = DEPLOY_COMPOSE.read_text(encoding="utf-8")
    qdrant = compose.split("  qdrant:\n", 1)[1].split("\n  citadel:\n", 1)[0]

    assert compose == deploy_compose
    assert (
        "qdrant/qdrant:v1.19.0@sha256:"
        "057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc"
    ) in qdrant
    assert "      QDRANT__STORAGE__SNAPSHOTS_PATH: /qdrant/snapshots\n" in qdrant
    assert "      - qdrant-data:/qdrant/storage\n" in qdrant
    assert "      - qdrant-snapshots:/qdrant/snapshots\n" in qdrant
    assert "  qdrant-data:\n" in compose
    assert "  qdrant-snapshots:\n" in compose
