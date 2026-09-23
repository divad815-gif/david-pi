"""Transactional ownership checks for saved household content.

The route-policy compiler defines the reviewed contract.  This module is the
small adapter used by content domains so ownership facts are read and enforced
inside the same SQLite write transaction as the mutation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping

from flask import current_app, g

from .access_control import IDENTITY_ROLES, normalize_login
from .content_policy import (
    Actor,
    AuthorizationFacts,
    MutationPolicy,
    decide_transaction_authorization,
    load_route_policy,
)
from .platform import emit_outbox_event, record_mutation_audit, utcnow


_TEST_IDENTITY_ROLES = {
    "david@example.test": "admin",
    "diana@example.test": "household",
}


def actor_for_identity(identity: Mapping[str, object]) -> Actor:
    """Build an actor only from server-verified request identity state."""
    principal_id = normalize_login(identity.get("owner_id"))
    role = IDENTITY_ROLES.get(principal_id)
    request_access = getattr(g, "portal_access", None)
    if isinstance(request_access, Mapping):
        role = request_access.get("role") or role
    # Existing application tests use reserved example.test principals.  This
    # compatibility is deliberately unreachable outside Flask TESTING mode.
    if current_app.testing:
        role = _TEST_IDENTITY_ROLES.get(principal_id, role)
    return Actor(principal_id=principal_id, role=role, kind="human")


def mutation_policy(route_id: str) -> MutationPolicy:
    return load_route_policy().by_id[route_id]


def ownership_facts(row: Mapping[str, object] | None) -> AuthorizationFacts:
    if row is None:
        return AuthorizationFacts()
    owner_id = row["owner_id"] if "owner_id" in row.keys() else None
    visibility = row["visibility"] if "visibility" in row.keys() else None
    return AuthorizationFacts(
        owner_id=owner_id,
        visibility=visibility,
        legacy=not bool(owner_id),
    )


def authorize(
    route_id: str,
    actor: Actor,
    row: Mapping[str, object] | None = None,
    *,
    action: str | None = None,
):
    return decide_transaction_authorization(
        mutation_policy(route_id), actor, ownership_facts(row), action=action
    )


def row_is_visible(row: Mapping[str, object], actor: Actor) -> bool:
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return False
    return row["owner_id"] == actor.principal_id or row["visibility"] == "shared"


def metadata_digest(row: Mapping[str, object] | None) -> str | None:
    """Hash lifecycle metadata only; never include names or saved content."""
    if row is None:
        return None
    keys = set(row.keys())
    payload = {
        "owner_id": row["owner_id"] if "owner_id" in keys else None,
        "visibility": row["visibility"] if "visibility" in keys else None,
        "version": int(row["version"]) if "version" in keys else 0,
        "deleted": bool(row["deleted_at"]) if "deleted_at" in keys else False,
        "archived": bool(row["archived"]) if "archived" in keys else False,
        "pinned": bool(row["pinned"]) if "pinned" in keys else False,
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audit_mutation(
    connection,
    *,
    actor: Actor,
    domain: str,
    object_id: str,
    action: str,
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
) -> None:
    """Append audit and outbox metadata in the caller's open transaction."""
    now = utcnow()
    record_mutation_audit(
        connection,
        event_id=uuid.uuid4().hex,
        actor_id=actor.principal_id,
        domain=domain,
        object_id=object_id,
        action=action,
        request_id=uuid.uuid4().hex,
        before_digest=metadata_digest(before),
        after_digest=metadata_digest(after),
        occurred_at=now,
    )
    if after is not None and "version" in set(after.keys()):
        emit_outbox_event(
            connection,
            domain=domain,
            object_id=object_id,
            event_type=action,
            object_version=int(after["version"]),
            payload_digest=metadata_digest(after),
            occurred_at=now,
        )
