"""Shared ownership, visibility, concurrency, and audit helpers.

The household applications deliberately treat David's administrator role as a
service role, not as permission to rewrite Diana's saved content.  Saved-content
owners always come from a verified, allowlisted request identity.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Mapping

from flask import current_app, g

from .access_control import IDENTITY_ROLES, normalize_login
from .content_policy import Actor, AuthorizationFacts, decide_transaction_authorization, load_route_policy
from .identity import current_device
from .platform import emit_outbox_event, record_mutation_audit, utcnow


_TEST_IDENTITY_ROLES = {
    "david@example.test": "admin",
    "diana@example.test": "household",
}


def allowed_actor() -> tuple[Actor | None, str]:
    """Return an allowlisted human actor even while central policy is in shadow."""
    identity = current_device()
    principal_id = normalize_login(identity.get("owner_id"))
    role = IDENTITY_ROLES.get(principal_id)
    request_access = getattr(g, "portal_access", None)
    if isinstance(request_access, dict):
        role = request_access.get("role") or role
    if current_app.testing:
        role = _TEST_IDENTITY_ROLES.get(principal_id, role)
    if not principal_id or role not in {"admin", "household"}:
        return None, identity["name"]
    return Actor(principal_id=principal_id, role=role, kind="human"), identity["name"]


def visible_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"({prefix}owner_id = ? OR ({prefix}visibility = 'shared'))"


def row_visible(row: Mapping | None, actor: Actor) -> bool:
    if row is None:
        return False
    owner_id = normalize_login(row["owner_id"])
    return owner_id == actor.principal_id or (row["visibility"] or "shared") == "shared"


def row_mutable_by(row: Mapping | None, principal_id: str | None) -> bool:
    """Return the exact owner/legacy-shared compatibility write scope.

    Callers must establish that ``principal_id`` is an allowlisted human before
    using this display helper. Transactional authorization remains authoritative.
    """
    if row is None:
        return False
    actor_id = normalize_login(principal_id)
    owner_id = normalize_login(row["owner_id"])
    visibility = str(row["visibility"] or "shared").strip().casefold()
    return bool(actor_id and (owner_id == actor_id or (not owner_id and visibility == "shared")))


def mutable_sql(alias: str = "") -> str:
    """SQL predicate matching ``row_mutable_by``; bind the actor id once."""
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}owner_id = ? OR "
        f"(NULLIF(TRIM({prefix}owner_id), '') IS NULL "
        f"AND {prefix}visibility = 'shared'))"
    )


def authorize(route_id: str, actor: Actor, *, owner_id: str | None = None,
              contribution_owner_id: str | None = None,
              personal_owner_id: str | None = None, allow_personal_create: bool = False,
              visibility: str | None = None):
    policy = load_route_policy().by_id[route_id]
    return decide_transaction_authorization(
        policy,
        actor,
        AuthorizationFacts(
            owner_id=owner_id,
            contribution_owner_id=contribution_owner_id,
            personal_owner_id=personal_owner_id,
            allow_personal_state_create=allow_personal_create,
            visibility=visibility,
            legacy=not bool(owner_id),
        ),
    )


def expected_version(data, *, field: str = "version") -> int | None:
    value = data.get(field) if hasattr(data, "get") else None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def validated_visibility(data, *, default: str = "shared") -> str:
    """Return an explicit, valid visibility or preserve the caller's default.

    Update routes pass the row's current visibility as ``default`` so an omitted
    field cannot accidentally publish private content.  If the client does send
    the field, malformed or unknown values fail closed instead of becoming
    shared.
    """
    if not hasattr(data, "get"):
        raise ValueError("Choose who can see this item.")
    if "visibility" not in data:
        return default
    value = data.get("visibility")
    if not isinstance(value, str):
        raise ValueError("Choose who can see this item.")
    normalized = value.strip().casefold()
    if normalized not in {"shared", "private"}:
        raise ValueError("Choose who can see this item.")
    return normalized


def metadata_digest(row: Mapping | None) -> str | None:
    if row is None:
        return None
    keys = set(row.keys())
    payload = {
        "owner_id": row["owner_id"] if "owner_id" in keys else None,
        "visibility": row["visibility"] if "visibility" in keys else None,
        "version": int(row["version"]) if "version" in keys else 0,
        "deleted": bool(row["deleted_at"]) if "deleted_at" in keys else False,
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def audit_mutation(connection, actor: Actor, *, domain: str, object_id: str,
                   action: str, before, after, object_version: int | None = None) -> None:
    """Record content-neutral audit and replay metadata in the caller's transaction."""
    occurred_at = utcnow()
    before_digest = metadata_digest(before)
    after_digest = metadata_digest(after)
    record_mutation_audit(
        connection,
        event_id=uuid.uuid4().hex,
        actor_id=actor.principal_id,
        domain=domain,
        object_id=str(object_id),
        action=action,
        request_id=uuid.uuid4().hex,
        before_digest=before_digest,
        after_digest=after_digest,
        occurred_at=occurred_at,
    )
    if object_version is None and after is not None and "version" in set(after.keys()):
        object_version = int(after["version"])
    if object_version is not None:
        emit_outbox_event(
            connection,
            domain=domain,
            object_id=str(object_id),
            event_type=action,
            object_version=object_version,
            payload_digest=after_digest,
            occurred_at=occurred_at,
        )
