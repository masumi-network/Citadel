"""Visibility decisions shared by Seat, Node, Central, and projections."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from kb.access import (
    SESSION_TRACES_DATASET,
    AccessIdentity,
    default_scopes,
    is_seat_dataset,
)


def effective_scopes(identity: AccessIdentity) -> tuple[str, ...]:
    if identity.scopes:
        return identity.scopes
    if identity.source == "env":
        return default_scopes(identity.role)
    return ()


def can_bypass_dataset_allowlist(identity: AccessIdentity) -> bool:
    if identity.source == "env":
        return True
    if identity.role == "admin":
        return True
    return "access:manage" in effective_scopes(identity)


def enforce_dataset_allowlist(identity: AccessIdentity, dataset: str) -> None:
    if can_bypass_dataset_allowlist(identity):
        return
    if dataset == SESSION_TRACES_DATASET:
        return
    if dataset in identity.allowed_datasets:
        return
    if is_seat_dataset(dataset):
        raise HTTPException(status_code=403, detail=f"Dataset not allowed: {dataset}.")
    if not identity.allowed_datasets:
        return
    raise HTTPException(status_code=403, detail=f"Dataset not allowed: {dataset}.")


def dataset_visible_to(identity: AccessIdentity, dataset: str) -> bool:
    """Return whether a dataset may appear in a scoped projection."""
    try:
        enforce_dataset_allowlist(identity, dataset)
    except HTTPException:
        return False
    return True


def owner_visible_to(
    identity: AccessIdentity,
    owner_actor_id: str | None,
    owner_seat_slug: str | None = None,
) -> bool:
    """Return whether an actor-owned record may appear in a projection."""
    if can_bypass_dataset_allowlist(identity):
        return True
    return bool(
        (owner_actor_id and owner_actor_id == identity.actor_id)
        or (owner_seat_slug and owner_seat_slug == identity.seat_slug)
    )


def conflict_visible_to(identity: AccessIdentity, conflict: dict[str, Any]) -> bool:
    """Scope a Knowledge Conflict by its recorded dataset."""
    if can_bypass_dataset_allowlist(identity):
        return True
    dataset = conflict.get("dataset")
    return isinstance(dataset, str) and dataset_visible_to(identity, dataset)


def event_visible_to(identity: AccessIdentity, event: dict[str, Any]) -> bool:
    """Scope activity events while failing closed for unscoped sensitive events."""
    if can_bypass_dataset_allowlist(identity):
        return True
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    dataset = details.get("dataset")
    if not isinstance(dataset, str):
        dataset = event.get("dataset")
    if event.get("type") in {"conflict", "error"} and not isinstance(dataset, str):
        return False
    return not isinstance(dataset, str) or dataset_visible_to(identity, dataset)
