import re
import subprocess
import textwrap
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
        "transformers==5.15.0",
        "huggingface-hub==1.27.0",
        "tokenizers==0.22.2",
    }

    assert expected_pins <= set(server_dependencies)
    assert expected_pins <= requirements
    # A global offline pin without the fastembed weight bake froze vector and
    # graph projection in production on 2026-08-12. The bakes and the
    # build-time offline embed proof are unconditional policy (a conditional
    # guard passes vacuously when the ENV and the bake are removed together),
    # and the offline pin must come after all of them.
    # Executable lines of the runtime stage only: a commented-out bake, or a
    # bake moved into the builder stage where it never reaches the image, must
    # not satisfy this guard.
    runtime_stage = dockerfile.split("FROM python", 2)[2].split("FROM runtime AS test", 1)[0]
    executable = "\n".join(
        line for line in runtime_stage.splitlines() if not line.lstrip().startswith("#")
    )
    assert "FASTEMBED_CACHE_PATH=/opt/fastembed-cache" in executable
    assert "from fastembed import TextEmbedding" in executable
    assert "HF_HUB_OFFLINE=1 python -c" in executable
    assert "ENV HF_HUB_OFFLINE=1" in executable
    offline_prefix = executable.split("ENV HF_HUB_OFFLINE=1", 1)[0]
    assert "FASTEMBED_CACHE_PATH=/opt/fastembed-cache" in offline_prefix
    assert "from fastembed import TextEmbedding" in offline_prefix
    assert "HF_HUB_OFFLINE=1 python -c" in offline_prefix

    assert "(version('cognee'), version('ladybug'), version('qdrant-client'))" in dockerfile
    assert "('1.4.1', '0.18.2', '1.19.0')" in dockerfile


def test_runtime_bakes_the_ladybug_json_extension_under_home() -> None:
    # Ladybug resolves its extension directory from $HOME when a Database
    # opens, and it ignores LADYBUG_HOME_DIRECTORY: that name only reaches
    # Ladybug as a connection-level `CALL home_directory`, which cannot run
    # before the database is open. cognee_db_workers' OP_OPEN_DATABASE carries
    # no home_directory field either. Caching the extension under
    # /data/ladybug-home therefore left ladybug.Database() reading an empty
    # $HOME/.lbdb, and graph projection froze at 54 of 289 searchable on
    # 2026-08-12 with "Failed to load library ... libjson.lbug_extension".
    # Executable lines of the runtime stage only, so a commented-out bake or
    # one moved into the builder stage cannot satisfy this guard.
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    runtime_stage = dockerfile.split("FROM python", 2)[2].split("FROM runtime AS test", 1)[0]
    executable = "\n".join(
        line for line in runtime_stage.splitlines() if not line.lstrip().startswith("#")
    )
    assert "INSTALL JSON;" in executable
    assert "/home/citadel/.lbdb" in executable
    # LOAD EXTENSION never installs, so the proof fails the build when the bake
    # is gone. Asserting the .so path keeps the proof from passing vacuously on
    # an empty extension directory.
    assert "libjson.lbug_extension" in executable
    assert "LOAD EXTENSION JSON;" in executable
    assert executable.index("INSTALL JSON;") < executable.index("LOAD EXTENSION JSON;")


def test_ci_proves_the_baked_embedding_engine_offline() -> None:
    # Deleting the smoke step must fail a test, or the offline-bake policy is
    # unenforced (fresh-eyes R6, 2026-08-12).
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_tests = workflow.split("  container-tests:\n", 1)[1].split(
        "\n  container-runtime:\n", 1
    )[0]
    assert "--network none" in container_tests
    assert "CITADEL_EMBEDDING_BAKE_SMOKE=1" in container_tests
    assert "tests/test_embedding_bake_smoke.py" in container_tests
    smoke_step = container_tests.split("Prove baked embedding engine works offline", 1)[1]
    assert "citadel-archive:ci-test" in smoke_step.split("- name:", 1)[0]


def test_compose_does_not_override_the_baked_hf_home() -> None:
    # HF_HOME pointed at the volume made the baked tokenizer unreachable under
    # the offline pin and silently degraded chunk sizing (2026-08-12).
    for name in ("docker-compose.yml", "kb/deploy_assets/docker-compose.yml"):
        compose = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        executable = "\n".join(
            line for line in compose.splitlines() if not line.lstrip().startswith("#")
        )
        assert "HF_HOME" not in executable, name


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
    assert 'docker logs --timestamps "$source_qdrant" > "$log_dir/source-qdrant.log"' in container_tests
    assert (
        'docker logs --timestamps "$restore_success_qdrant" > "$log_dir/restore-success-qdrant.log"'
        in container_tests
    )
    assert (
        'docker logs --timestamps "$restore_rollback_qdrant" '
        '> "$log_dir/restore-rollback-qdrant.log"'
    ) in container_tests
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
    assert (
        'docker compose --project-name "$project" --env-file "$env_file" '
        'logs --timestamps qdrant > "$log_dir/qdrant.log"'
    ) in container_runtime
    assert (
        'docker compose --project-name "$project" --env-file "$env_file" '
        'logs --timestamps citadel > "$log_dir/citadel.log"'
    ) in container_runtime
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


def test_ci_proves_public_skills_from_installed_wheel_and_production_image() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    package_smoke = workflow.split("  package-smoke:\n", 1)[1].split(
        "\n  container-tests:\n", 1
    )[0]
    container_runtime = workflow.split("  container-runtime:\n", 1)[1].split(
        "\n  gate:\n", 1
    )[0]
    public_paths = (
        "/.well-known/citadel.json",
        "/api/state",
        "/skills",
        "/skills/boundary",
        "/skills/connect",
        "/skills/proactive-ingest",
        "/skills/vault",
    )
    skill_names = (
        "citadel-data-boundary",
        "citadel-mcp-connector",
        "citadel-proactive-ingest",
        "citadel-vault",
    )

    assert "--public-routes" in package_smoke
    assert '"$GITHUB_WORKSPACE/scripts/verify_package_artifacts.py"' in package_smoke
    assert '"$GITHUB_WORKSPACE/dist"' in package_smoke
    assert 'HOME="$verifier_home" env -u PYTHONPATH "$venv/bin/python" -I' in package_smoke
    assert 'find "$verifier_home" -mindepth 1 -print -quit' in package_smoke
    assert "Path(sysconfig.get_path(\"purelib\"))" in container_runtime
    assert 'distribution("citadel-archive")' in container_runtime
    assert "locate_file" in container_runtime
    assert "is_file()" in container_runtime
    assert 'name.startswith("docs/adr/")' in container_runtime
    assert "count_adr_records" in container_runtime
    assert 'Path("/src").exists()' in container_runtime
    assert "run_evolve_in_loop" in container_runtime
    assert 'assert row["aliases"] == expected_aliases[slug]' in container_runtime
    assert '"mcp-connector"' in container_runtime
    assert '"public-private"' in container_runtime
    assert 'for alias in row["aliases"]' in container_runtime
    assert 'mount["Type"] == "bind"' in container_runtime
    assert "traceback|exception|died" in container_runtime
    assert "onnxruntime cpuid_info warning: Unknown CPU vendor" in container_runtime
    for path in public_paths:
        assert path in container_runtime
    for name in skill_names:
        assert name in container_runtime


def test_ci_qdrant_containers_mount_volumes_rather_than_exempting_the_storage_warning() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_tests = workflow.split("  container-tests:\n", 1)[1].split(
        "\n  container-runtime:\n", 1
    )[0]

    for container in ("source_qdrant", "restore_success_qdrant", "restore_rollback_qdrant"):
        assert f'--volume "${{{container}}}-storage:/qdrant/storage"' in container_tests
        assert f'--volume "${{{container}}}-snapshots:/qdrant/snapshots"' in container_tests
        assert f'"${{{container}}}-storage" "${{{container}}}-snapshots"' in container_tests
    assert container_tests.count(":/qdrant/storage") == 3
    assert container_tests.count(":/qdrant/snapshots") == 3
    assert "docker volume rm" in container_tests
    # Qdrant's container-filesystem warning reports missing durable storage, so a
    # missing volume has to keep failing this job. Mount the volume; never teach
    # the classifier to read the warning as benign.
    assert "Container filesystem detected" not in container_tests
    assert "potential issue with the filesystem" not in container_tests


def _executable_lines(job: str) -> str:
    return "\n".join(line for line in job.splitlines() if not line.lstrip().startswith("#"))


def test_container_log_classifiers_use_portable_guarded_grep() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_tests = workflow.split("  container-tests:\n", 1)[1].split(
        "\n  container-runtime:\n", 1
    )[0]
    container_runtime = workflow.split("  container-runtime:\n", 1)[1].split(
        "\n  gate:\n", 1
    )[0]

    tests_code = _executable_lines(container_tests)
    runtime_code = _executable_lines(container_runtime)
    hard_failure_pattern = (
        "error|traceback|exception|died|critical|panic|fatal|oom|corrupt|failure|failed"
    )
    for code in (tests_code, runtime_code):
        assert "command -v grep >/dev/null" in code
        assert re.search(r"\brg\b", code) is None
        assert hard_failure_pattern in code
        assert code.index(hard_failure_pattern) < code.index("grep -Evi --")
        assert "grep -Eni" in code
        assert "grep -Evi --" in code
        assert "grep -En" in code
        assert 'grep_statuses=("${PIPESTATUS[@]}")' in code
        assert "grep_statuses[0] > 1 || grep_statuses[1] > 1" in code
        assert "grep_status > 1" in code
    assert "grep -Ev --" in runtime_code
    assert "grep -Fq --" in runtime_code


def test_container_log_classifier_rejects_grep_errors(tmp_path: Path) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_tests = workflow.split("  container-tests:\n", 1)[1].split(
        "\n  container-runtime:\n", 1
    )[0]
    run_block = textwrap.dedent(
        container_tests.split("        shell: bash\n        run: |\n", 1)[1]
    )
    function_start = run_block.index("assert_clean_container_logs() {")
    function_end = run_block.index("\ncleanup() {", function_start)
    classifier = run_block[function_start:function_end]
    log_file = tmp_path / "qdrant.log"
    log_file.write_text("WARNING injected classifier fault\n", encoding="utf-8")
    script = "\n".join(
        (
            "set -euo pipefail",
            "expected_qdrant_log_pattern='['",
            classifier,
            'assert_clean_container_logs "$1"',
        )
    )

    result = subprocess.run(
        ["bash", "-c", script, "bash", str(log_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Container log classifier grep failed" in result.stdout
    assert "grep:" in result.stderr


def test_container_log_classifiers_reject_fatal_tokens_before_exemptions(
    tmp_path: Path,
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_tests = workflow.split("  container-tests:\n", 1)[1].split(
        "\n  container-runtime:\n", 1
    )[0]
    container_runtime = workflow.split("  container-runtime:\n", 1)[1].split(
        "\n  gate:\n", 1
    )[0]
    classifiers = (
        (container_tests, "assert_clean_container_logs", "container"),
        (container_runtime, "assert_clean_runtime_logs", "runtime"),
    )
    severe_lines = (
        "snapshot upload failed\n",
        "CRITICAL database unavailable\n",
        "WARNING TLS disabled; FATAL corruption\n",
        "WARNING TLS disabled; ERROR request rejected\n",
        "warning Cognee 1.0 changes: ERROR request rejected\n",
        # 'corruption' alone missed the past-tense form until 2026-08-12.
        "corrupted segment detected\n",
    )

    for job, function_name, label in classifiers:
        run_block = textwrap.dedent(job.split("        shell: bash\n        run: |\n", 1)[1])
        function_start = run_block.index(f"{function_name}() {{")
        function_end = run_block.index("\ncleanup() {", function_start)
        classifier = run_block[function_start:function_end]
        for index, line in enumerate(severe_lines):
            log_file = tmp_path / f"{label}-{index}.log"
            log_file.write_text(line, encoding="utf-8")
            script = "\n".join(
                (
                    "set -euo pipefail",
                    "expected_qdrant_log_pattern='TLS (is )?disabled'",
                    "expected_citadel_log_pattern='Cognee 1\\.0 changes:'",
                    'log_dir="$2"',
                    classifier,
                    f'{function_name} "$1"',
                )
            )

            result = subprocess.run(
                ["bash", "-c", script, "bash", str(log_file), str(tmp_path)],
                check=False,
                capture_output=True,
                text=True,
            )

            assert result.returncode != 0, (label, line, result)
            assert "Unexpected fatal" in result.stdout


def test_container_jobs_capture_logs_after_the_fact_instead_of_backgrounding_followers() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_tests = workflow.split("  container-tests:\n", 1)[1].split(
        "\n  container-runtime:\n", 1
    )[0]
    container_runtime = workflow.split("  container-runtime:\n", 1)[1].split(
        "\n  gate:\n", 1
    )[0]

    # Comments are allowed to name the old pattern; executable lines are not.
    tests_code = _executable_lines(container_tests)
    runtime_code = _executable_lines(container_runtime)
    for code in (tests_code, runtime_code):
        # `cmd | tee file &` sets $! to tee, so killing it leaves `docker logs
        # --follow` alive on an idle container, and waiting on that job never
        # returns.
        backgrounded = [
            line
            for line in code.splitlines()
            if line.rstrip().endswith("&") and not line.rstrip().endswith("&&")
        ]
        assert backgrounded == []
        assert "--follow" not in code
        assert "$!" not in code
        assert "stop_log_followers" not in code
        assert "_log_pid" not in code
    # Capture runs inside the trap too, so a failing run still leaves evidence,
    # and it has to precede the teardown that destroys the containers.
    tests_cleanup = tests_code.split("cleanup() {", 1)[1].split("}", 1)[0]
    runtime_cleanup = runtime_code.split("cleanup() {", 1)[1].split("}", 1)[0]
    assert tests_cleanup.index("capture_container_logs") < tests_cleanup.index("docker rm -f")
    assert runtime_cleanup.index("capture_runtime_logs") < runtime_cleanup.index("down --volumes")
    assert "local exit_code=$?" in runtime_cleanup
    assert "tail --lines=200" in runtime_cleanup
    assert 'exit "$exit_code"' in runtime_cleanup
    # Definition, the call in the trap, and the call before classification.
    assert tests_code.count("capture_container_logs") == 3
    assert runtime_code.count("capture_runtime_logs") == 3
    # Capture swallows its own errors, so an empty log file is the one way this
    # rewrite could pass without evidence. Both classifiers reject it.
    for code in (tests_code, runtime_code):
        assert 'if [[ ! -s "$log_file" ]]; then' in code
        assert "::error::No log was captured from $log_file" in code
    # `docker compose logs` resolves from labels today, but it is the only
    # compose call in the job without the env file the rest of them pass.
    assert container_runtime.count('--env-file "$env_file" logs --timestamps') == 2


def _job_blocks(workflow: str) -> dict[str, str]:
    body = workflow.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9-]*):$\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def test_every_job_declares_a_timeout_so_a_hang_cannot_burn_the_default_six_hours() -> None:
    jobs = _job_blocks(WORKFLOW.read_text(encoding="utf-8"))

    assert set(jobs) == {
        "test",
        "audit",
        "plain-requirements-smoke",
        "benchmark",
        "web",
        "package-smoke",
        "container-tests",
        "container-runtime",
        "gate",
    }
    for name, block in jobs.items():
        expected = 30 if name.startswith("container-") else 15
        assert f"timeout-minutes: {expected}\n" in block, name


def _citadel_log_exemptions(container_runtime: str) -> list[str]:
    alternatives: list[str] = []
    for line in container_runtime.splitlines():
        stripped = line.strip()
        if not stripped.startswith("expected_citadel_log_pattern"):
            continue
        value = stripped.split("=", 1)[1].strip()
        assert value[:1] == value[-1:] and value[:1] in ("'", '"'), value
        alternatives.extend(part for part in value[1:-1].split("|") if part)
    return alternatives


def test_runtime_log_classifier_exempts_only_the_measured_benign_citadel_lines() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    container_runtime = workflow.split("  container-runtime:\n", 1)[1].split(
        "\n  gate:\n", 1
    )[0]

    # '^$' can never match a severe line, so it exempted nothing and failed the
    # job on a healthy boot.
    assert "expected_log_pattern='^$'" not in container_runtime
    assert 'expected_log_pattern="$expected_citadel_log_pattern"' in container_runtime
    assert _citadel_log_exemptions(container_runtime) == [
        r"Cognee 1\.0 changes:",
        "IncompleteFieldDefinitionWarning: Field 'lifespan'",
        r"warnings\.warn\(",
        "No nodes found in the database",
        "onnxruntime cpuid_info warning: Unknown CPU vendor",
    ]
    # Measured on an ARM Docker Desktop production boot. Keep one exact
    # executable exemption rather than a broad onnxruntime pattern.
    code = _executable_lines(container_runtime)
    assert code.count("onnxruntime cpuid_info warning: Unknown CPU vendor") == 1
    # Everything outside that list stays fail-closed.
    assert (
        "error|traceback|exception|died|critical|panic|fatal|oom|corrupt|failure|failed"
        in container_runtime
    )
    assert "warn(ing)?|recovery.*(shortfall|failed)" in container_runtime


def test_docker_test_target_disables_the_inherited_runtime_healthcheck() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    runtime_stage = dockerfile.split(" AS runtime\n", 1)[1].split("FROM runtime AS test\n", 1)[0]
    test_stage = dockerfile.split("FROM runtime AS test\n", 1)[1].split(
        "FROM runtime AS production\n", 1
    )[0]
    production = dockerfile.rsplit("FROM runtime AS production\n", 1)[1]

    # Production keeps the probe; test runs never set CITADEL_ADMIN_KEY, so the
    # inherited probe could only ever report the test container unhealthy.
    assert "HEALTHCHECK --interval=15s" in runtime_stage
    assert "CITADEL_ADMIN_KEY" in runtime_stage
    assert "HEALTHCHECK NONE\n" in test_stage
    assert test_stage.count("HEALTHCHECK") == 1
    assert "urlopen" not in test_stage
    assert "HEALTHCHECK" not in production


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
