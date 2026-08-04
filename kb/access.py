from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
from typing import Any
from uuid import uuid4

from kb.capture_config import MAX_APPROVED_CAPTURE_ROOTS, normalize_capture_root_paths
from kb.capture_policy import SeatCapturePolicy, normalize_deny_globs
from kb.promotion_queue import (
    APPROVED_STATUS,
    PENDING_STATUS,
    REJECTED_STATUS,
    PromotionPendingItem,
)

logger = logging.getLogger(__name__)

CENTRAL_DATASET = "masumi-network"
SESSION_TRACES_DATASET = "session-traces"
SEAT_DATASET_PREFIX = "seat:"
SEAT_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

ROLE_ORDER = {"reader": 1, "writer": 2, "admin": 3}
VALID_ROLES = frozenset(ROLE_ORDER)
VALID_PRINCIPAL_KINDS = frozenset({"user", "service_account"})

DEFAULT_SCOPES = {
    "reader": ("kb:read", "kb:search", "sources:read", "obsidian:sync:pull"),
    "writer": (
        "kb:read",
        "kb:search",
        "kb:ingest",
        "kb:feedback",
        "sources:read",
        "obsidian:sync:pull",
        "obsidian:sync:push",
    ),
    "admin": (
        "kb:read",
        "kb:search",
        "kb:ingest",
        "kb:feedback",
        "sources:read",
        "sources:sync",
        "obsidian:sync:pull",
        "obsidian:sync:push",
        "agents:manage",
        "access:manage",
        "audit:read",
    ),
}


@dataclass(frozen=True)
class AccessIdentity:
    role: str
    actor_id: str
    actor_kind: str
    actor_name: str
    source: str
    scopes: tuple[str, ...] = field(default_factory=tuple)
    token_id: str | None = None
    default_dataset: str | None = None
    default_session: str | None = None
    allowed_datasets: tuple[str, ...] = field(default_factory=tuple)
    seat_slug: str | None = None


@dataclass(frozen=True)
class AccessPrincipal:
    id: str
    kind: str
    name: str
    role: str
    scopes: tuple[str, ...]
    team_id: str | None
    created_at: str
    disabled_at: str | None = None
    default_dataset: str | None = None
    default_session: str | None = None
    email: str | None = None
    seat_slug: str | None = None


@dataclass(frozen=True)
class ApiToken:
    id: str
    principal_id: str
    name: str
    token_hash: str
    prefix: str
    role: str
    scopes: tuple[str, ...]
    team_id: str | None
    created_at: str
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    default_dataset: str | None = None
    default_session: str | None = None
    allowed_datasets: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TokenCreation:
    token: str
    api_token: ApiToken
    principal: AccessPrincipal


@dataclass(frozen=True)
class SeatCreation:
    principal: AccessPrincipal
    token: str | None
    api_token: ApiToken | None


@dataclass(frozen=True)
class TokenSession:
    identity: AccessIdentity
    token_hash: str


@dataclass(frozen=True)
class SeatApprovedCaptureRoots:
    paths: tuple[str, ...] = ()
    updated_at: str | None = None
    updated_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


@dataclass(frozen=True)
class AuditEvent:
    id: str
    action: str
    actor_id: str | None
    actor_kind: str | None
    actor_name: str | None
    role: str | None
    success: bool
    created_at: str
    dataset: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_api_token(token: str) -> str:
    return hashlib.sha256(f"citadel-api-token:v1:{token}".encode("utf-8")).hexdigest()


def new_api_token() -> str:
    return f"ctdl_{secrets.token_urlsafe(32)}"


def default_scopes(role: str) -> tuple[str, ...]:
    validate_role(role)
    return DEFAULT_SCOPES[role]


def validate_role_scopes(role: str, scopes: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    validate_role(role)
    normalized = _dedupe(tuple(scopes))
    allowed = set(default_scopes(role))
    extra = sorted(set(normalized) - allowed)
    if extra:
        raise ValueError(
            f"Scopes exceed {role} role: {', '.join(extra)}"
        )
    return normalized


def validate_role(role: str) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"Unsupported role: {role}")


def validate_principal_kind(kind: str) -> None:
    if kind not in VALID_PRINCIPAL_KINDS:
        raise ValueError(f"Unsupported principal kind: {kind}")


def validate_seat_slug(slug: str) -> str:
    normalized = slug.strip().lower()
    if not SEAT_SLUG_RE.match(normalized):
        raise ValueError(
            "Seat slug must be 2-63 lowercase letters, numbers, or hyphens "
            "and start/end with a letter or number."
        )
    return normalized


def seat_dataset(slug: str) -> str:
    return f"{SEAT_DATASET_PREFIX}{validate_seat_slug(slug)}"


def is_seat_dataset(dataset: str | None) -> bool:
    return bool(dataset and dataset.startswith(SEAT_DATASET_PREFIX))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def validate_expires_at(value: str | None) -> str | None:
    """Reject an expiry the expiry check would not be able to read.

    Stored raw, an unparseable value is indistinguishable from no expiry at all,
    so the token outlives its stated lifetime while the dashboard still labels it
    "Expires". Refusing it at the door is the only place the caller can be told.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _parse_time(text) is None:
        raise ValueError(
            f"expires_at is not an ISO 8601 timestamp: {value!r}"
        )
    return text


def _is_expired(value: str | None) -> bool:
    if not value:
        # No expiry set. Permanent by design.
        return False
    parsed = _parse_time(value)
    if parsed is None:
        # An expiry was set and cannot be read. Previously this returned False
        # and the token authenticated forever. Treat unreadable as expired:
        # validate_expires_at stops new ones being written, and this covers
        # anything already stored, including a value edited in by hand.
        return True
    return parsed <= datetime.now(timezone.utc)


def _dedupe(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _normalize_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolve_token_memory_scope(
    api_token: ApiToken,
    principal: AccessPrincipal,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    default_dataset = (
        api_token.default_dataset
        if api_token.default_dataset is not None
        else principal.default_dataset
    )
    default_session = (
        api_token.default_session
        if api_token.default_session is not None
        else principal.default_session
    )
    return default_dataset, default_session, api_token.allowed_datasets


class AccessStore:
    def __init__(self, path: Path | str, *, max_audit_events: int = 1000) -> None:
        self.path = Path(path)
        self.max_audit_events = max_audit_events

    def has_tokens(self) -> bool:
        return any(token.revoked_at is None for token in self._tokens(self._load()))

    def snapshot(self) -> dict[str, Any]:
        data = self._load()
        return {
            "principals": [asdict(principal) for principal in self._principals(data)],
            "tokens": [self._redact_token(token) for token in self._tokens(data)],
            "audit_events": [asdict(event) for event in self._audit_events(data)],
        }

    def seat_slugs(self) -> list[str]:
        """Return the seat slugs of all principals, in store order.

        Cheaper than ``snapshot()`` for the latency-watched /api/mesh/graph
        presence read: it reads only principals and never materializes the
        (up to ``max_audit_events``) audit list or the token roster.
        """
        return [
            principal.seat_slug
            for principal in self._principals(self._load())
            if principal.seat_slug
        ]

    def authenticate_token(self, token: str) -> TokenSession | None:
        token_hash = hash_api_token(token)
        data = self._load()
        tokens = self._tokens(data)
        for index, api_token in enumerate(tokens):
            if not secrets.compare_digest(api_token.token_hash, token_hash):
                continue
            session = self._session_for_token(data, api_token)
            if not session:
                self._record_token_rejection(data, api_token)
                return None
            tokens[index] = ApiToken(
                **{
                    **asdict(api_token),
                    "last_used_at": now_iso(),
                }
            )
            data["tokens"] = [asdict(token_item) for token_item in tokens]
            self._save(data)
            return session
        return None

    def token_session(self, token_id: str) -> TokenSession | None:
        data = self._load()
        for api_token in self._tokens(data):
            if api_token.id == token_id:
                session = self._session_for_token(data, api_token)
                if not session:
                    self._record_token_rejection(data, api_token)
                return session
        return None

    def create_principal(
        self,
        *,
        name: str,
        kind: str,
        role: str,
        scopes: tuple[str, ...] | list[str] | None = None,
        team_id: str | None = None,
        default_dataset: str | None = None,
        default_session: str | None = None,
        email: str | None = None,
        seat_slug: str | None = None,
    ) -> AccessPrincipal:
        validate_principal_kind(kind)
        validate_role(role)
        principal = AccessPrincipal(
            id=f"principal_{uuid4().hex}",
            kind=kind,
            name=name.strip(),
            role=role,
            scopes=validate_role_scopes(role, tuple(scopes) if scopes else default_scopes(role)),
            team_id=team_id,
            created_at=now_iso(),
            default_dataset=_normalize_optional_str(default_dataset),
            default_session=_normalize_optional_str(default_session),
            email=_normalize_optional_str(email),
            seat_slug=_normalize_optional_str(seat_slug),
        )
        data = self._load()
        data["principals"] = [*data.get("principals", []), asdict(principal)]
        self._save(data)
        return principal

    def create_seat(
        self,
        *,
        name: str,
        slug: str,
        email: str | None = None,
        role: str = "writer",
        issue_token: bool = True,
        token_name: str | None = None,
        central_dataset: str = CENTRAL_DATASET,
    ) -> SeatCreation:
        validate_role(role)
        if role == "admin":
            # A seat is a private-memory boundary for a licensed human; an admin
            # token bypasses the dataset allowlist entirely (see
            # can_bypass_dataset_allowlist) and so dissolves that boundary. Issue
            # admin tokens through create_token, never as a seat.
            raise ValueError("Seats cannot be provisioned with the admin role.")
        normalized_slug = validate_seat_slug(slug)
        node_dataset = seat_dataset(normalized_slug)
        if self._find_seat_by_dataset(node_dataset):
            raise ValueError(f"Seat already exists for slug: {normalized_slug}")
        session_id = f"seat-{normalized_slug}"
        # Derive Central from the caller-supplied dataset (resolved from config at
        # the API layer) so the seat's allowlist can never drift from the value the
        # write/search router actually targets.
        allowed = (node_dataset, central_dataset, SESSION_TRACES_DATASET)
        principal = self.create_principal(
            name=name,
            kind="user",
            role=role,
            default_dataset=node_dataset,
            default_session=session_id,
            email=email,
            seat_slug=normalized_slug,
        )
        if not issue_token:
            return SeatCreation(principal=principal, token=None, api_token=None)
        created = self.create_token(
            principal_id=principal.id,
            name=token_name or f"{name.strip()} writer",
            role=role,
            default_dataset=node_dataset,
            default_session=session_id,
            allowed_datasets=list(allowed),
        )
        return SeatCreation(
            principal=principal,
            token=created.token,
            api_token=created.api_token,
        )

    def issue_seat_token(
        self,
        *,
        slug: str,
        token_name: str | None = None,
        central_dataset: str = CENTRAL_DATASET,
    ) -> TokenCreation:
        """Mint a FRESH token for an EXISTING seat's principal.

        Scoped exactly like the seat's original token (default_dataset =
        seat node, allowlist = seat node + Central), so the new token carries
        seat_slug and routes writes to the seat. Used to re-link a seat whose
        original (shown-once) token was lost or never adopted.
        """
        normalized_slug = validate_seat_slug(slug)
        principal = self.find_seat_by_slug(normalized_slug)
        if principal is None:
            raise KeyError(normalized_slug)
        node_dataset = seat_dataset(normalized_slug)
        session_id = f"seat-{normalized_slug}"
        return self.create_token(
            principal_id=principal.id,
            name=token_name or f"{principal.name} token",
            role=principal.role,
            default_dataset=node_dataset,
            default_session=session_id,
            allowed_datasets=[node_dataset, central_dataset, SESSION_TRACES_DATASET],
        )

    def create_token(
        self,
        *,
        principal_id: str,
        name: str,
        role: str | None = None,
        scopes: tuple[str, ...] | list[str] | None = None,
        team_id: str | None = None,
        expires_at: str | None = None,
        default_dataset: str | None = None,
        default_session: str | None = None,
        allowed_datasets: tuple[str, ...] | list[str] | None = None,
    ) -> TokenCreation:
        data = self._load()
        principal = self._principal(data, principal_id)
        if not principal:
            raise KeyError(principal_id)
        resolved_role = role or principal.role
        validate_role(resolved_role)
        resolved_expires_at = validate_expires_at(expires_at)
        resolved_scopes = (
            validate_role_scopes(resolved_role, scopes)
            if scopes is not None
            else validate_role_scopes(
                resolved_role,
                principal.scopes if resolved_role == principal.role else default_scopes(resolved_role),
            )
        )
        token = new_api_token()
        api_token = ApiToken(
            id=f"token_{uuid4().hex}",
            principal_id=principal.id,
            name=name.strip(),
            token_hash=hash_api_token(token),
            prefix=token[:12],
            role=resolved_role,
            scopes=resolved_scopes,
            team_id=team_id if team_id is not None else principal.team_id,
            created_at=now_iso(),
            expires_at=resolved_expires_at,
            default_dataset=_normalize_optional_str(default_dataset),
            default_session=_normalize_optional_str(default_session),
            allowed_datasets=_dedupe(allowed_datasets or ()),
        )
        data["tokens"] = [*data.get("tokens", []), asdict(api_token)]
        self._save(data)
        return TokenCreation(token=token, api_token=api_token, principal=principal)

    def create_principal_token(
        self,
        *,
        name: str,
        kind: str,
        role: str,
        scopes: tuple[str, ...] | list[str] | None = None,
        team_id: str | None = None,
        expires_at: str | None = None,
        default_dataset: str | None = None,
        default_session: str | None = None,
        allowed_datasets: tuple[str, ...] | list[str] | None = None,
    ) -> TokenCreation:
        # Before create_principal, not after: create_token validates this too,
        # but by then the principal exists and raising would strand it with no
        # token attached.
        validate_expires_at(expires_at)
        principal = self.create_principal(
            name=name,
            kind=kind,
            role=role,
            scopes=scopes,
            team_id=team_id,
            default_dataset=default_dataset,
            default_session=default_session,
        )
        return self.create_token(
            principal_id=principal.id,
            name=name,
            role=role,
            scopes=scopes,
            team_id=team_id,
            expires_at=expires_at,
            default_dataset=default_dataset,
            default_session=default_session,
            allowed_datasets=allowed_datasets,
        )

    def revoke_token(self, token_id: str) -> ApiToken | None:
        data = self._load()
        tokens = self._tokens(data)
        revoked: ApiToken | None = None
        next_tokens: list[ApiToken] = []
        for api_token in tokens:
            if api_token.id == token_id:
                revoked = ApiToken(**{**asdict(api_token), "revoked_at": api_token.revoked_at or now_iso()})
                next_tokens.append(revoked)
            else:
                next_tokens.append(api_token)
        if not revoked:
            return None
        data["tokens"] = [asdict(api_token) for api_token in next_tokens]
        self._save(data)
        return revoked

    def record_event(
        self,
        *,
        action: str,
        actor: AccessIdentity | None,
        success: bool,
        dataset: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=f"audit_{uuid4().hex}",
            action=action,
            actor_id=actor.actor_id if actor else None,
            actor_kind=actor.actor_kind if actor else None,
            actor_name=actor.actor_name if actor else None,
            role=actor.role if actor else None,
            success=success,
            dataset=dataset,
            detail=detail or {},
            created_at=now_iso(),
        )
        data = self._load()
        events = [*data.get("audit_events", []), asdict(event)][-self.max_audit_events :]
        data["audit_events"] = events
        self._save(data)
        return event

    def _rejection_reason(self, data: dict[str, Any], api_token: ApiToken) -> str | None:
        principal = self._principal(data, api_token.principal_id)
        if not principal:
            return "principal_missing"
        if principal.disabled_at:
            return "principal_disabled"
        if api_token.revoked_at:
            return "revoked"
        if _is_expired(api_token.expires_at):
            return "expired"
        return None

    def _record_token_rejection(self, data: dict[str, Any], api_token: ApiToken) -> None:
        reason = self._rejection_reason(data, api_token) or "rejected"
        logger.warning(
            "Rejected token %s for principal %s: %s",
            api_token.id,
            api_token.principal_id,
            reason,
        )

        self.record_event(
            action="access.token.rejected",
            actor=None,
            success=False,
            detail={
                "token_id": api_token.id,
                "principal_id": api_token.principal_id,
                "reason": reason,
            },
        )

    def _session_for_token(self, data: dict[str, Any], api_token: ApiToken) -> TokenSession | None:
        principal = self._principal(data, api_token.principal_id)
        if not principal:
            return None
        if principal.disabled_at or api_token.revoked_at or _is_expired(api_token.expires_at):
            return None
        default_dataset, default_session, allowed_datasets = _resolve_token_memory_scope(
            api_token,
            principal,
        )
        identity = AccessIdentity(
            role=api_token.role,
            actor_id=principal.id,
            actor_kind=principal.kind,
            actor_name=principal.name,
            source="api_token",
            scopes=api_token.scopes,
            token_id=api_token.id,
            default_dataset=default_dataset,
            default_session=default_session,
            allowed_datasets=allowed_datasets,
            # Carry the seat marker straight off this token's own principal so a
            # self-describing scope can never name another seat's slug.
            seat_slug=principal.seat_slug,
        )
        return TokenSession(identity=identity, token_hash=api_token.token_hash)

    def find_seat_by_slug(self, slug: str) -> AccessPrincipal | None:
        normalized = validate_seat_slug(slug)
        for principal in self._principals(self._load()):
            if principal.seat_slug == normalized:
                return principal
        return None

    def get_capture_policy(self, slug: str) -> SeatCapturePolicy:
        normalized = validate_seat_slug(slug)
        data = self._load()
        stored = data.get("capture_policies", {}).get(normalized, {})
        return SeatCapturePolicy(
            deny_globs=tuple(stored.get("deny_globs") or ()),
            updated_at=stored.get("updated_at"),
            updated_by=stored.get("updated_by"),
        )

    def set_capture_policy(
        self,
        slug: str,
        *,
        deny_globs: tuple[str, ...] | list[str],
        actor_id: str | None = None,
    ) -> SeatCapturePolicy:
        normalized = validate_seat_slug(slug)
        if not self.find_seat_by_slug(normalized):
            raise ValueError(f"Seat not found: {normalized}")
        policy = SeatCapturePolicy(
            deny_globs=normalize_deny_globs(deny_globs),
            updated_at=now_iso(),
            updated_by=actor_id,
        )
        data = self._load()
        capture_policies = dict(data.get("capture_policies", {}))
        capture_policies[normalized] = policy.to_dict()
        data["capture_policies"] = capture_policies
        self._save(data)
        return policy

    def get_approved_capture_roots(self, slug: str) -> SeatApprovedCaptureRoots:
        normalized = validate_seat_slug(slug)
        data = self._load()
        stored = data.get("approved_capture_roots", {}).get(normalized, {})
        return SeatApprovedCaptureRoots(
            paths=normalize_capture_root_paths(tuple(stored.get("paths") or ())),
            updated_at=stored.get("updated_at"),
            updated_by=stored.get("updated_by"),
        )

    def set_approved_capture_roots(
        self,
        slug: str,
        *,
        paths: tuple[str, ...] | list[str],
        actor_id: str | None = None,
    ) -> SeatApprovedCaptureRoots:
        normalized = validate_seat_slug(slug)
        if not self.find_seat_by_slug(normalized):
            raise ValueError(f"Seat not found: {normalized}")
        normalized_paths = normalize_capture_root_paths(paths)
        # Enforce the same ceiling the API model does. Without this the store
        # would accept a list its own PUT endpoint refuses, so GET could hand a
        # client roots it could never write back, and the client would retry
        # that rejected write forever.
        if len(normalized_paths) > MAX_APPROVED_CAPTURE_ROOTS:
            raise ValueError(
                f"Too many capture roots for {normalized}: "
                f"{len(normalized_paths)} exceeds the limit of "
                f"{MAX_APPROVED_CAPTURE_ROOTS}."
            )
        roots = SeatApprovedCaptureRoots(
            paths=normalized_paths,
            updated_at=now_iso(),
            updated_by=actor_id,
        )
        data = self._load()
        approved_capture_roots = dict(data.get("approved_capture_roots", {}))
        approved_capture_roots[normalized] = roots.to_dict()
        data["approved_capture_roots"] = approved_capture_roots
        self._save(data)
        return roots

    def list_promotion_pending(
        self,
        *,
        seat_slug: str | None = None,
        status: str | None = PENDING_STATUS,
    ) -> list[PromotionPendingItem]:
        data = self._load()
        items: list[PromotionPendingItem] = []
        normalized_seat = validate_seat_slug(seat_slug) if seat_slug else None
        for raw in data.get("promotion_pending", []):
            try:
                item = PromotionPendingItem.from_dict(raw)
            except ValueError:
                continue
            if status is not None and item.status != status:
                continue
            if normalized_seat is not None and item.seat_slug != normalized_seat:
                continue
            items.append(item)
        items.sort(key=lambda entry: entry.created_at)
        return items

    def get_promotion_pending(self, item_id: str) -> PromotionPendingItem | None:
        data = self._load()
        for raw in data.get("promotion_pending", []):
            try:
                item = PromotionPendingItem.from_dict(raw)
            except ValueError:
                continue
            if item.id == item_id:
                return item
        return None

    def add_promotion_pending(self, item: PromotionPendingItem) -> PromotionPendingItem:
        data = self._load()
        pending: list[PromotionPendingItem] = []
        for raw in data.get("promotion_pending", []):
            try:
                pending.append(PromotionPendingItem.from_dict(raw))
            except ValueError:
                continue
        for existing in pending:
            if (
                existing.status == PENDING_STATUS
                and existing.seat_slug == item.seat_slug
                and existing.candidate_hash == item.candidate_hash
            ):
                return existing
        pending.append(item)
        data["promotion_pending"] = [entry.to_dict() for entry in pending][-500:]
        self._save(data)
        return item

    def is_promotion_rejected(self, seat_slug: str, content_hash: str) -> bool:
        normalized = validate_seat_slug(seat_slug)
        data = self._load()
        for raw in data.get("promotion_pending", []):
            try:
                item = PromotionPendingItem.from_dict(raw)
            except ValueError:
                continue
            if (
                item.seat_slug == normalized
                and item.candidate_hash == content_hash
                and item.status == REJECTED_STATUS
            ):
                return True
        return False

    def decide_promotion_pending(
        self,
        item_id: str,
        *,
        decision: str,
        actor_id: str,
        actor_name: str,
        delegate: bool = False,
    ) -> PromotionPendingItem:
        if decision not in {APPROVED_STATUS, REJECTED_STATUS}:
            raise ValueError(f"Unsupported promotion decision: {decision}")
        data = self._load()
        updated: PromotionPendingItem | None = None
        next_items: list[dict[str, Any]] = []
        for raw in data.get("promotion_pending", []):
            item = PromotionPendingItem.from_dict(raw)
            if item.id != item_id:
                next_items.append(item.to_dict())
                continue
            if item.status != PENDING_STATUS:
                raise ValueError(f"Promotion item is not pending: {item_id}")
            updated = PromotionPendingItem(
                id=item.id,
                seat_slug=item.seat_slug,
                seat_dataset=item.seat_dataset,
                candidate_text=item.candidate_text,
                candidate_hash=item.candidate_hash,
                preview=item.preview,
                reference_status=item.reference_status,
                reference_reason=item.reference_reason,
                repo_hints=item.repo_hints,
                status=decision,
                created_at=item.created_at,
                decided_at=now_iso(),
                decided_by=actor_id,
                decided_by_name=actor_name,
                delegate=delegate,
                score=item.score,
                relevant=item.relevant,
                sensitive=item.sensitive,
            )
            next_items.append(updated.to_dict())
        if updated is None:
            raise ValueError(f"Promotion item not found: {item_id}")
        data["promotion_pending"] = next_items
        self._save(data)
        return updated

    def _find_seat_by_dataset(self, dataset: str) -> AccessPrincipal | None:
        for principal in self._principals(self._load()):
            if principal.default_dataset == dataset:
                return principal
        return None

    def _principal(self, data: dict[str, Any], principal_id: str) -> AccessPrincipal | None:
        for principal in self._principals(data):
            if principal.id == principal_id:
                return principal
        return None

    def _principals(self, data: dict[str, Any]) -> list[AccessPrincipal]:
        return [
            AccessPrincipal(
                **{
                    **item,
                    "scopes": tuple(item.get("scopes", ())),
                    "default_dataset": item.get("default_dataset"),
                    "default_session": item.get("default_session"),
                    "email": item.get("email"),
                    "seat_slug": item.get("seat_slug"),
                }
            )
            for item in data.get("principals", [])
        ]

    def _tokens(self, data: dict[str, Any]) -> list[ApiToken]:
        return [
            ApiToken(
                **{
                    **item,
                    "scopes": tuple(item.get("scopes", ())),
                    "default_dataset": item.get("default_dataset"),
                    "default_session": item.get("default_session"),
                    "allowed_datasets": tuple(item.get("allowed_datasets") or ()),
                }
            )
            for item in data.get("tokens", [])
        ]

    def _audit_events(self, data: dict[str, Any]) -> list[AuditEvent]:
        return [
            AuditEvent(
                **{
                    **item,
                    "detail": item.get("detail", {}),
                }
            )
            for item in data.get("audit_events", [])
        ]

    def recent_audit_events(
        self,
        *,
        action: str | None = None,
        actions: Iterable[str] | None = None,
        actor_id: str | None = None,
        success: bool | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Newest-first audit rows, optionally narrowed.

        ``actions`` accepts a SET of action names because one act can be
        recorded under several of them: a write reaches the store as
        ``ingest``, ``contribute``, ``share_session`` or ``mcp.<tool>``
        depending on the surface it arrived on, and a reader that names one of
        them sees only that surface.
        """
        wanted = {name for name in (actions or ()) if name}
        if action:
            wanted.add(action)
        events = self._audit_events(self._load())
        filtered = [
            asdict(event)
            for event in reversed(events)
            if (not wanted or event.action in wanted)
            and (actor_id is None or event.actor_id == actor_id)
            and (success is None or bool(event.success) is success)
        ]
        return filtered[: max(1, min(limit, 100))]

    def _redact_token(self, api_token: ApiToken) -> dict[str, Any]:
        redacted = asdict(api_token)
        redacted.pop("token_hash", None)
        return redacted

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": 1,
                "principals": [],
                "tokens": [],
                "audit_events": [],
                "capture_policies": {},
                "approved_capture_roots": {},
                "promotion_pending": [],
            }
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {
            "version": data.get("version", 1),
            "principals": data.get("principals", []),
            "tokens": data.get("tokens", []),
            "audit_events": data.get("audit_events", []),
            "capture_policies": data.get("capture_policies", {}),
            "approved_capture_roots": data.get("approved_capture_roots", {}),
            "promotion_pending": data.get("promotion_pending", []),
        }

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temp_path.replace(self.path)
