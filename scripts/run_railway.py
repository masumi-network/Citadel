#!/usr/bin/env python3
"""Railway run-mode dispatcher for Citadel services.

Modes (via ``CITADEL_RUN_MODE``):

- ``web`` (default): the FastAPI Organization Vault service.
- ``github-sync`` / ``learning-agent``: the single GitHub org learning job.
- ``backup-mirror``: the single Vault Backup Mirror export job.
- ``cognify`` / ``cognify-verify``: re-cognify already-added data in a dataset
  (``CITADEL_COGNIFY_DATASET``, default dataset otherwise) to recover data that
  was added but never cognified; ``cognify-verify`` also ingests a unique marker
  and confirms it lands in the graph.
- ``pipeline`` (also ``all``/``cron``): the full scheduled pipeline —
  GitHub org sync, skills catalog refresh, self-improvement pass, and backup
  mirror export. Each stage is toggleable via env, logs a per-stage summary
  line, and a failed stage never stops the stages after it. The process exits
  nonzero only when every enabled stage fails.
- ``evolve``: the 6h self-evolving cycle (ADR-0005 step 3) — GitHub org sync,
  repo-content sync, self-improvement, selective seat->Central promotion, and
  cognify. Same per-stage ``CITADEL_EVOLVE_*`` toggles + fail-soft semantics as
  ``pipeline``; the 6h cadence is an operator Railway-cron step, not code.
- ``linear-sync``: sync the Linear workspace to Central and mirror assignee
  issues into seat Nodes (requires ``CITADEL_LINEAR_API_KEY``).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable, Sequence
from typing import Callable

from scripts.stage_loop import run_async, stage_loop

logger = logging.getLogger("citadel.pipeline")


def _bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def web_command(*, port: str) -> list[str]:
    return [
        "python",
        "-m",
        "uvicorn",
        "kb.server:app",
        "--host",
        "0.0.0.0",
        "--port",
        port,
    ]


def _github_sync_stage() -> int:
    from scripts.run_github_sync import run as run_github_sync

    return run_github_sync(force_in_process=True)


async def _github_sync_stage_async() -> int:
    from scripts.run_github_sync import arun

    return await arun(force_in_process=True)


def _skills_refresh_stage() -> int:
    from kb.skills import refresh_skill_catalog

    result = refresh_skill_catalog()
    logger.info(
        "Skills catalog refreshed: skills=%s changed=%s added=%s removed=%s",
        result["skills"],
        ",".join(result["changed"]) or "none",
        ",".join(result["added"]) or "none",
        ",".join(result["removed"]) or "none",
    )
    return 0


def _self_improve_stage() -> int:
    from scripts.run_self_improve import run as run_self_improve

    return run_self_improve(force_in_process=True)


async def _self_improve_stage_async() -> int:
    from scripts.run_self_improve import arun

    return await arun(force_in_process=True)


def _backup_mirror_stage() -> int:
    from scripts.run_backup_mirror import run as run_backup_mirror

    return run_backup_mirror()


async def _repo_content_sync_stage_async() -> int:
    from kb.repo_content_sync import RepoContentSyncer
    from kb.service import Citadel

    result = await RepoContentSyncer(Citadel.from_env()).run()
    if not result.get("ok"):
        return 1
    if result.get("enabled") is False:
        logger.info("Repo content sync skipped: %s", result.get("reason"))
        return 0
    logger.info(
        "Repo content sync finished: repos=%s ingested=%s skipped=%s improved=%s",
        result.get("repos_scanned"),
        result.get("files_ingested"),
        result.get("files_skipped"),
        result.get("improved"),
    )
    return 0


def _repo_content_sync_stage() -> int:
    return run_async(_repo_content_sync_stage_async())


def _cognify_mode(*, verify: bool) -> int:
    from kb.service import Citadel

    dataset = os.getenv("CITADEL_COGNIFY_DATASET") or None
    result = run_async(Citadel.from_env().cognify_dataset(dataset=dataset, verify=verify))
    logger.info(
        "Cognify finished: dataset=%s graph_before=%s graph_after=%s grew=%s verify=%s",
        result.get("dataset"),
        result.get("graph_before"),
        result.get("graph_after"),
        result.get("graph_grew"),
        (result.get("verification") or {}).get("ok") if verify else result.get("verify"),
    )
    if verify and not (result.get("verification") or {}).get("ok"):
        logger.error("Cognify verification failed: %s", result.get("verification"))
        return 1
    return 0


def _cognify_stage() -> int:
    url = os.getenv("CITADEL_COGNIFY_TARGET_URL")
    if url:
        logger.error(
            "Refusing Cognify over the user API. Remove CITADEL_COGNIFY_TARGET_URL "
            "and use the in-process evolve scheduler."
        )
        return 1
    # Two OS processes must never write Kuzu at once (#47). If the web's
    # in-process evolve scheduler is enabled, the web owns the single Kuzu
    # writer. An external Cognify process would collide with that lock.
    if _bool(os.getenv("CITADEL_EVOLVE_SCHEDULER_ENABLED")):
        logger.error(
            "Refusing in-process cognify: CITADEL_EVOLVE_SCHEDULER_ENABLED is set, so the "
            "web process owns the Kuzu writer. Let that scheduler run Cognify in its own loop."
        )
        return 1
    return _cognify_mode(verify=False)


async def _promotion_stage_async() -> int:
    """Selective seat-node -> Central promotion across every seat (ADR-0005 step 3).

    Reuses :class:`kb.promotion.PromotionEngine`, honoring its opt-in
    (``CITADEL_PROMOTION_ENABLED``) and dry-run (``CITADEL_PROMOTION_DRY_RUN``,
    default on) config. Each seat node is promoted independently. A failure on one
    seat does not abort the others, but it does fail the stage because Central is
    then incomplete for that scheduled pass.
    """
    from kb.access import AccessStore, is_seat_dataset
    from kb.improvement_policy import automation_evaluation_passed
    from kb.learning import LearningProcess
    from kb.promotion import PromotionEngine
    from kb.service import Citadel

    citadel = Citadel.from_env()
    config = citadel.config
    if not config.promotion_enabled:
        logger.info("Promotion stage skipped: disabled via CITADEL_PROMOTION_ENABLED")
        return 0
    if not automation_evaluation_passed(config):
        logger.info("Promotion stage skipped: Central evaluation gate has not passed")
        return 0

    access_store = AccessStore(config.access_store_path)
    seats = sorted(
        {
            principal.get("default_dataset")
            for principal in access_store.snapshot()["principals"]
            if principal.get("seat_slug") and is_seat_dataset(principal.get("default_dataset"))
        }
    )
    if not seats:
        logger.info("Promotion stage: no seat nodes to promote from")
        return 0

    engine = PromotionEngine(citadel, LearningProcess(citadel), access_store, config)

    async def _run() -> tuple[int, int]:
        promoted = 0
        failures = 0
        for seat in seats:
            try:
                result = await engine.run(seat, dry_run=config.promotion_dry_run)
            except Exception as exc:
                logger.error(
                    "Promotion failed for %s: %s: %s",
                    seat,
                    exc.__class__.__name__,
                    exc,
                )
                failures += 1
                continue
            promoted += result.get("promoted") or 0
        return promoted, failures

    promoted, failures = await _run()
    logger.info(
        "Promotion stage finished: seats=%s promoted=%s failures=%s dry_run=%s",
        len(seats),
        promoted,
        failures,
        config.promotion_dry_run,
    )
    return 1 if failures else 0


def _promotion_stage() -> int:
    return run_async(_promotion_stage_async())


def pipeline_stages() -> list[tuple[str, bool, Callable[[], int]]]:
    """(name, enabled, runner) for every pipeline stage, in execution order."""
    return [
        (
            "github_sync",
            _bool(os.getenv("CITADEL_PIPELINE_GITHUB_SYNC_ENABLED"), default=True),
            _github_sync_stage,
        ),
        (
            "repo_content_sync",
            _bool(os.getenv("CITADEL_PIPELINE_REPO_CONTENT_SYNC_ENABLED"), default=True),
            _repo_content_sync_stage,
        ),
        (
            "skills_refresh",
            _bool(os.getenv("CITADEL_PIPELINE_SKILLS_REFRESH_ENABLED"), default=True),
            _skills_refresh_stage,
        ),
        (
            "self_improve",
            _bool(os.getenv("CITADEL_SELF_IMPROVE_ENABLED"), default=False),
            _self_improve_stage,
        ),
        (
            "backup_mirror",
            _bool(os.getenv("CITADEL_PIPELINE_BACKUP_MIRROR_ENABLED"), default=True),
            _backup_mirror_stage,
        ),
    ]


async def _linear_sync_stage_async() -> int:
    """Sync the Linear workspace into Central (+ seat mirrors) for the evolve cron.

    No-op (exit 0) when ``CITADEL_LINEAR_API_KEY`` is unset, so the stage is safe
    to leave enabled. The Central write lands in shared Postgres/pgvector; the
    evolve cognify stage then folds it into the graph. Incremental
    (``force=False``, #90): issues whose ``updatedAt`` predates the stored
    cursor are skipped — the explicit ``CITADEL_RUN_MODE=linear-sync`` job
    stays a forced full sync.
    """
    from kb.access import AccessStore
    from kb.linear_sync import LinearSyncer
    from kb.service import Citadel

    async def _run() -> int:
        citadel = Citadel.from_env()
        if not citadel.config.linear_api_key:
            logger.info("Linear sync stage skipped: CITADEL_LINEAR_API_KEY not set")
            return 0
        access_store = AccessStore(citadel.config.access_store_path)
        result = await LinearSyncer(citadel, access_store=access_store).run(force=False)
        if not result.get("ok"):
            # Surface the actual failure reason + detail, not just the stage name in
            # the _run_stages `failed=linear_sync` summary (#46).
            logger.error(
                "Linear sync stage failed: reason=%s error=%s",
                result.get("reason"),
                result.get("error"),
            )
            return 1
        logger.info(
            "Linear sync stage finished: issues=%s written=%s skipped_unchanged=%s mirrored=%s members=%s auto_mapped=%s unresolved=%s",
            result.get("issue_count"),
            result.get("written_count"),
            result.get("skipped_unchanged"),
            result.get("mirrored_count"),
            result.get("auto_map_members_fetched"),
            result.get("auto_mapped_assignees"),
            result.get("unresolved_assignee_count"),
        )
        return 0

    return await _run()


def _linear_sync_stage() -> int:
    return run_async(_linear_sync_stage_async())


def evolve_stages() -> list[tuple[str, bool, Callable[[], int]]]:
    """(name, enabled, runner) for the 6h evolve cron, in execution order.

    Mirrors :func:`pipeline_stages` (per-stage env toggles) but chains the
    self-evolving cycle: github sync -> repo-content sync -> self-improve ->
    promotion -> linear sync -> cognify. The 6h cadence is an operator
    Railway-cron / in-process scheduler step, not code. Each stage carries its own
    ``CITADEL_EVOLVE_*`` toggle so an operator can disable any link without
    touching the others.
    """
    return [
        (
            "github_sync",
            _bool(os.getenv("CITADEL_EVOLVE_GITHUB_SYNC_ENABLED"), default=True),
            _github_sync_stage,
        ),
        (
            "repo_content_sync",
            _bool(os.getenv("CITADEL_EVOLVE_REPO_CONTENT_SYNC_ENABLED"), default=True),
            _repo_content_sync_stage,
        ),
        (
            "self_improve",
            _bool(os.getenv("CITADEL_EVOLVE_SELF_IMPROVE_ENABLED"), default=True),
            _self_improve_stage,
        ),
        (
            "promotion",
            _bool(os.getenv("CITADEL_EVOLVE_PROMOTION_ENABLED"), default=True),
            _promotion_stage,
        ),
        (
            "linear_sync",
            _bool(os.getenv("CITADEL_EVOLVE_LINEAR_SYNC_ENABLED"), default=True),
            _linear_sync_stage,
        ),
        (
            "cognify",
            _bool(os.getenv("CITADEL_EVOLVE_COGNIFY_ENABLED"), default=True),
            _cognify_stage,
        ),
    ]


def evolve_stages_async() -> list[tuple[str, bool, Callable[[], Awaitable[int]]]]:
    """The evolve stages as awaitables, for running inside the web loop (#88).

    Mirrors :func:`evolve_stages` name-for-name and toggle-for-toggle;
    ``test_evolve_stage_lists_stay_in_sync`` pins that. ``cognify`` is absent on
    purpose: the scheduler runs it itself afterwards as Phase 2, which is what
    the old subprocess expressed by setting CITADEL_EVOLVE_COGNIFY_ENABLED=false.
    """
    return [
        (
            "github_sync",
            _bool(os.getenv("CITADEL_EVOLVE_GITHUB_SYNC_ENABLED"), default=True),
            _github_sync_stage_async,
        ),
        (
            "repo_content_sync",
            _bool(os.getenv("CITADEL_EVOLVE_REPO_CONTENT_SYNC_ENABLED"), default=True),
            _repo_content_sync_stage_async,
        ),
        (
            "self_improve",
            _bool(os.getenv("CITADEL_EVOLVE_SELF_IMPROVE_ENABLED"), default=True),
            _self_improve_stage_async,
        ),
        (
            "promotion",
            _bool(os.getenv("CITADEL_EVOLVE_PROMOTION_ENABLED"), default=True),
            _promotion_stage_async,
        ),
        (
            "linear_sync",
            _bool(os.getenv("CITADEL_EVOLVE_LINEAR_SYNC_ENABLED"), default=True),
            _linear_sync_stage_async,
        ),
    ]


async def run_evolve_in_loop(
    *,
    capture_run_id: str | None = None,
    stages: Sequence[str] | None = None,
) -> int:
    """Run selected evolve stages on the caller's event loop.

    ``capture_run_id`` identifies the source-sync watermark for callers that
    record accepted lifecycle revisions. Stage selection lets the web scheduler
    place projection barriers between source and consumer stages.
    """
    if capture_run_id is not None and (
        not isinstance(capture_run_id, str) or not capture_run_id.strip()
    ):
        raise ValueError("capture_run_id must be a non-empty string when provided")

    available = evolve_stages_async()
    if stages is None:
        selected = available
    else:
        requested = tuple(stages)
        if any(not isinstance(name, str) or not name.strip() for name in requested):
            raise ValueError("stages must contain only non-empty strings")
        available_names = {name for name, _, _ in available}
        unknown = sorted(set(requested) - available_names)
        if unknown:
            raise ValueError(f"unknown evolve stages: {', '.join(unknown)}")
        requested_names = set(requested)
        selected = [
            stage for stage in available if stage[0] in requested_names
        ]

    if capture_run_id is not None:
        logger.info("Evolve capture run: %s", capture_run_id)
    if capture_run_id is None:
        return await _run_stages_async(selected, label="Evolve")
    from kb.service import capture_run_scope

    with capture_run_scope(capture_run_id):
        return await _run_stages_async(selected, label="Evolve")


def _run_stages(stages: list[tuple[str, bool, Callable[[], int]]], *, label: str) -> int:
    """Run every enabled stage; continue past failures.

    Every enabled stage runs regardless of what failed before it — that is
    delivered by the ``continue`` in the loop below, not by the return value.
    The exit code is a separate question: it reports whether the pass was
    clean, so any failed stage makes it nonzero (#89).

    It used to return 0 unless *every* stage failed, which meant the hourly
    evolve cycle reported success while github_sync and linear_sync failed on
    the Kuzu lock every single time (#88, #46) and nothing ever surfaced it.

    Callers that must not retry a partial failure need
    ``restartPolicyType = "NEVER"``; see docs/operations.md.
    """
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            stream=sys.stdout,
        )

    succeeded: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    for name, enabled, runner in stages:
        if not enabled:
            skipped.append(name)
            logger.info("%s stage %s: skipped (disabled via env)", label, name)
            continue
        logger.info("%s stage %s: starting", label, name)
        try:
            code = runner()
        except Exception as exc:
            logger.error(
                "%s stage %s: FAILED with %s: %s",
                label,
                name,
                exc.__class__.__name__,
                exc,
            )
            failed.append(name)
            continue
        if code == 0:
            succeeded.append(name)
            logger.info("%s stage %s: ok", label, name)
        else:
            failed.append(name)
            logger.error("%s stage %s: FAILED with exit code %s", label, name, code)

    return _stage_verdict(label, succeeded, failed, skipped)


def _stage_verdict(
    label: str, succeeded: list[str], failed: list[str], skipped: list[str]
) -> int:
    """Log the pass summary and return its exit code.

    Shared by the sync and in-loop stage runners so the #89 rule (any failed
    stage means a failed pass) cannot drift between the two.
    """
    logger.info(
        "%s finished: succeeded=%s failed=%s skipped=%s",
        label,
        ",".join(succeeded) or "none",
        ",".join(failed) or "none",
        ",".join(skipped) or "none",
    )
    return 1 if failed else 0


async def _run_stages_async(
    stages: list[tuple[str, bool, Callable[[], Awaitable[int]]]], *, label: str
) -> int:
    """Run every enabled stage on the CALLER's event loop; continue past failures.

    The in-loop twin of :func:`_run_stages`, used by the web process's evolve
    scheduler (#88). It cannot reuse the sync version: those runners funnel
    through ``run_async``, which falls back to ``asyncio.run`` and raises inside
    a running loop. Running here instead of in a subprocess is the fix for the
    Kuzu lock, because cognee holds an exclusive OS file lock on the graph for
    the lifetime of whichever process opens it, and in this deployment that is
    always the web.
    """
    succeeded: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    for name, enabled, runner in stages:
        if not enabled:
            skipped.append(name)
            logger.info("%s stage %s: skipped (disabled via env)", label, name)
            continue
        logger.info("%s stage %s: starting", label, name)
        try:
            code = await runner()
        except Exception as exc:
            logger.error(
                "%s stage %s: FAILED with %s: %s",
                label,
                name,
                exc.__class__.__name__,
                exc,
            )
            failed.append(name)
            continue
        if code == 0:
            succeeded.append(name)
            logger.info("%s stage %s: ok", label, name)
        else:
            failed.append(name)
            logger.error("%s stage %s: FAILED with exit code %s", label, name, code)

    return _stage_verdict(label, succeeded, failed, skipped)


def run_pipeline() -> int:
    return _run_stages(pipeline_stages(), label="Pipeline")


def run_evolve() -> int:
    # One shared event loop for the whole chain: cognee caches its async DB engine
    # on the first loop and raises "got Future attached to a different loop" on any
    # later loop, so every cognee-touching stage (github_sync, repo_content_sync,
    # self_improve, promotion, linear_sync) must run on the SAME loop or all but the
    # first silently fail while the pass still exits 0 (#69). Pipeline mode keeps its
    # per-stage loops. It is not the in-loop evolve scheduler and does not suppress
    # inline Cognify, so a shared loop there could let one stage's background work
    # straddle into the next stage.
    with stage_loop():
        return _run_stages(evolve_stages(), label="Evolve")


def run(mode: str | None = None) -> int:
    resolved_mode = (mode or os.getenv("CITADEL_RUN_MODE") or "web").strip() or "web"
    if resolved_mode == "web":
        os.execvp("python", web_command(port=os.getenv("PORT", "8000")))
        raise RuntimeError("os.execvp returned unexpectedly.")
    if resolved_mode in {"github-sync", "learning-agent"}:
        from scripts.run_github_sync import run as run_github_sync

        return run_github_sync(force_in_process=True)
    if resolved_mode == "backup-mirror":
        from scripts.run_backup_mirror import run as run_backup_mirror

        return run_backup_mirror()
    if resolved_mode in {"cognify", "cognify-verify"}:
        return _cognify_mode(verify=resolved_mode == "cognify-verify")
    if resolved_mode in {"pipeline", "all", "cron"}:
        return run_pipeline()
    if resolved_mode == "evolve":
        return run_evolve()
    if resolved_mode == "linear-sync":
        from kb.access import AccessStore
        from kb.linear_sync import LinearSyncer
        from kb.service import Citadel

        async def _run() -> int:
            citadel = Citadel.from_env()
            access_store = AccessStore(citadel.config.access_store_path)
            # Standalone forced sync: AWAIT the coalesced cognify so a manual run
            # actually indexes the issues (the scheduled background cognify would be
            # cancelled when this asyncio.run loop tears down at process exit) (#46).
            result = await LinearSyncer(
                citadel,
                access_store=access_store,
            ).run(force=True, await_cognify=True)
            if not result.get("ok"):
                logger.error(
                    "Linear sync failed: reason=%s error=%s",
                    result.get("reason"),
                    result.get("error"),
                )
                return 1
            logger.info(
                "Linear sync finished: issues=%s mirrored=%s members=%s auto_mapped=%s unresolved=%s",
                result.get("issue_count"),
                result.get("mirrored_count"),
                result.get("auto_map_members_fetched"),
                result.get("auto_mapped_assignees"),
                result.get("unresolved_assignee_count"),
            )
            return 0

        return run_async(_run())
    print(f"Unsupported CITADEL_RUN_MODE: {resolved_mode}", file=sys.stderr)
    return 1


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
