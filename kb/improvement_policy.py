"""Scope rules for Cognee improvement passes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import json
from pathlib import Path

from kb.access import CENTRAL_DATASET
from kb.config import CitadelConfig


class ImprovementScopeError(ValueError):
    """An improvement request crossed the Central-only safety boundary."""


class ImprovementEvaluationError(ValueError):
    """Automated improvement has no valid evaluation receipt."""


def central_improvement_dataset(config: CitadelConfig) -> str:
    """Return the configured Central dataset used by improvement."""
    return (config.github_sync_dataset or CENTRAL_DATASET).strip() or CENTRAL_DATASET


def validate_central_improvement_scope(
    config: CitadelConfig,
    *,
    dataset: str | None,
    session_ids: Sequence[str] | None,
) -> str:
    """Reject node and session-scoped improvement before Cognee is called."""
    central = central_improvement_dataset(config)
    target = (dataset or central).strip()
    if target != central:
        raise ImprovementScopeError(
            f"Cognee improvement is Central-only: requested dataset {target!r}, "
            f"Central dataset is {central!r}."
        )
    if session_ids:
        raise ImprovementScopeError(
            "Cognee improvement cannot use session_ids; session traces and seat "
            "sessions stay outside the Central improvement loop."
        )
    return central


_REQUIRED_AUTOMATION_CHECKS = frozenset(
    {
        "projection_chain",
        "seat_isolation",
        "cited_graph_retrieval",
        "rollback",
        "free_model_budget",
    }
)


def automation_evaluation_passed(config: CitadelConfig) -> bool:
    """Return true only for a current, complete Central evaluation receipt."""
    path = Path(getattr(config, "evaluation_gate_path", ".citadel/evaluation_gate.json"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("status") != "passed" or payload.get("scope") != "central":
        return False
    if not isinstance(payload.get("run_id"), str) or not payload["run_id"].strip():
        return False
    evaluated_at = payload.get("evaluated_at")
    if not isinstance(evaluated_at, str) or not evaluated_at.strip():
        return False
    try:
        evaluated = datetime.fromisoformat(evaluated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if evaluated.tzinfo is None:
        return False
    checks = payload.get("checks")
    if not isinstance(checks, dict) or any(
        checks.get(name) is not True for name in _REQUIRED_AUTOMATION_CHECKS
    ):
        return False
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at.strip():
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiry.tzinfo is None or expiry.astimezone(UTC) <= datetime.now(UTC):
        return False
    if evaluated.astimezone(UTC) > datetime.now(UTC):
        return False
    return True


def require_automation_evaluation(config: CitadelConfig) -> None:
    """Fail closed when an automated improvement pass lacks evaluation proof."""
    if not automation_evaluation_passed(config):
        raise ImprovementEvaluationError(
            "automated Cognee improvement is disabled until a current Central "
            "evaluation receipt passes projection, scope, citation, rollback, "
            "and free-model budget checks."
        )
