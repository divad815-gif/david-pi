"""Compiled, fail-closed policy primitives for saved-content mutations.

This module is deliberately independent of Flask request hooks and storage.  A
domain module can load a mutation policy and evaluate facts it read *inside the
same database transaction* before performing a write.  Merely adding an entry
to ``route-policy.json`` does not enforce it; route integration is a separate,
reviewed migration.

Administrator status is intentionally not a content-superuser capability.
Human content decisions first require a reviewed household role, then compare
stable principal IDs; the administrator role never bypasses those comparisons.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 2
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "route-policy.json"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
KNOWN_EFFECTS = frozenset(
    {
        "activity",
        "create",
        "credential",
        "destructive",
        "read",
        "restore",
        "system",
        "update",
        "upload",
    }
)
KNOWN_RESOURCES = frozenset(
    {
        "assistant_conversation",
        "audiobook",
        "audiobook_progress",
        "chat_conversation",
        "chat_message",
        "chat_read_state",
        "device_backup",
        "device_credential",
        "file",
        "file_folder",
        "game_score",
        "media",
        "media_collection",
        "media_collection_membership",
        "media_device_backup",
        "media_import_from_chat",
        "media_personal_state",
        "media_slideshow",
        "movie",
        "movie_availability",
        "movie_subscription",
        "movie_watched_state",
        "note",
        "place_choice",
        "place_margarita_record",
        "place_restaurant",
        "place_review",
        "push_credential",
        "recipe",
        "recipe_activity",
        "recipe_recommendation",
        "recipe_weekly_plan",
        "system_power",
        "system_settings",
    }
)
KNOWN_AUTHORIZATIONS = frozenset(
    {
        "caller_owned",
        "caller_owned_credential",
        "caller_owned_from_conversation",
        "caller_owned_from_visible_sources",
        "conversation_creator_with_unanimous_active_member_approval",
        "conversation_member_contribution",
        "conversation_member_personal_state",
        "device_owner_or_admin",
        "household_service_action",
        "household_shared_content",
        "message_sender",
        "owner_only",
        "owner_or_contributor_or_legacy_shared",
        "owner_or_contributor",
        "allowlisted_human_and_one_time_pairing_code",
        "paired_device_owner",
        "personal_credential",
        "personal_state",
        "shared_collaboration",
        "system_admin",
        "visible_read_only",
    }
)
KNOWN_CSRF_CLASSES = frozenset(
    {"required", "read_only_exempt", "bearer_exempt", "pairing_code_exempt"}
)
KNOWN_AUDIT_CLASSES = frozenset(
    {"activity_event", "mutation_event", "read_event", "security_event"}
)
KNOWN_LEGACY_HANDLING = frozenset(
    {
        "deny_write_until_owned",
        "device_binding_required",
        "not_applicable",
        "owner_migration_required",
        "preserve_shared_legacy_collaboration",
        "preserve_personal_state",
    }
)
KNOWN_RETENTION_MODES = frozenset(
    {"leave_or_soft_delete", "permanent_delete", "revocation", "soft_delete"}
)
KNOWN_RETENTION_IMPLEMENTATIONS = frozenset({"domain_pending", "enforced"})
KNOWN_ACCESS_CLASSES = frozenset({"public", "portal", "admin", "device_bearer"})
PAIRING_CODE_EXEMPT_ROUTES = frozenset(
    {
        ("POST", "/api/v1/device-backup/pair"),
        ("POST", "/api/v1/ios-backup/pair"),
    }
)
BEARER_EXEMPT_ROUTES = frozenset(
    {
        ("POST", "/api/v1/device-backup/uploads"),
        ("PATCH", "/api/v1/device-backup/uploads/<upload_id>"),
        ("DELETE", "/api/v1/device-backup/uploads/<upload_id>"),
        ("POST", "/api/v1/device-backup/uploads/<upload_id>/complete"),
        ("POST", "/api/v1/ios-backup/upload"),
        ("POST", "/api/v1/ios-backup/upload-file"),
        ("POST", "/api/v1/ios-backup/checkpoint"),
        ("POST", "/api/v1/device-backup/reconcile"),
    }
)
AUTHORIZATION_RESOURCES = {
    "allowlisted_human_and_one_time_pairing_code": frozenset({"device_credential"}),
    "caller_owned": frozenset(
        {
            "audiobook",
            "chat_conversation",
            "file",
            "file_folder",
            "game_score",
            "media",
            "media_collection",
            "movie",
            "note",
            "place_restaurant",
            "recipe",
        }
    ),
    "caller_owned_credential": frozenset({"device_credential"}),
    "caller_owned_from_conversation": frozenset({"media_import_from_chat"}),
    "caller_owned_from_visible_sources": frozenset({"media_slideshow"}),
    "conversation_creator_with_unanimous_active_member_approval": frozenset(
        {"chat_conversation"}
    ),
    "conversation_member_contribution": frozenset({"chat_conversation", "chat_message"}),
    "conversation_member_personal_state": frozenset({"chat_read_state"}),
    "device_owner_or_admin": frozenset({"device_credential"}),
    "household_service_action": frozenset(
        {"movie_availability", "place_choice", "recipe_recommendation", "recipe_weekly_plan"}
    ),
    "household_shared_content": frozenset(
        {
            "assistant_conversation",
            "place_margarita_record",
            "place_restaurant",
            "place_review",
        }
    ),
    "message_sender": frozenset({"chat_message"}),
    "owner_only": frozenset(
        {
            "audiobook",
            "file",
            "media",
            "media_collection",
            "movie",
            "note",
            "place_restaurant",
            "recipe",
        }
    ),
    "owner_or_contributor_or_legacy_shared": frozenset(
        {
            "movie",
            "place_margarita_record",
            "place_restaurant",
            "place_review",
            "recipe",
        }
    ),
    "owner_or_contributor": frozenset({"media_collection_membership", "place_review"}),
    "paired_device_owner": frozenset({"device_backup", "media_device_backup"}),
    "personal_credential": frozenset({"push_credential"}),
    "personal_state": frozenset(
        {
            "audiobook_progress",
            "media_personal_state",
            "movie_subscription",
            "movie_watched_state",
            "recipe_activity",
        }
    ),
    "shared_collaboration": frozenset({"media_collection_membership"}),
    "system_admin": frozenset({"device_credential", "system_power", "system_settings"}),
    "visible_read_only": frozenset({"media", "media_collection_membership"}),
}

# Authorization is not merely resource-specific.  These reviewed matrices keep
# an attacker (or a future typo) from converting an existing write into a
# caller-owned create by changing several policy fields together.
AUTHORIZATION_EFFECTS = {
    "allowlisted_human_and_one_time_pairing_code": frozenset({"credential"}),
    "caller_owned": frozenset({"create", "upload"}),
    "caller_owned_credential": frozenset({"credential"}),
    "caller_owned_from_conversation": frozenset({"create"}),
    "caller_owned_from_visible_sources": frozenset({"create"}),
    "conversation_creator_with_unanimous_active_member_approval": frozenset(
        {"destructive"}
    ),
    "conversation_member_contribution": frozenset({"create", "destructive"}),
    "conversation_member_personal_state": frozenset({"activity"}),
    "device_owner_or_admin": frozenset({"destructive"}),
    "household_service_action": frozenset({"activity", "update"}),
    "household_shared_content": frozenset(
        {"create", "destructive", "restore", "update"}
    ),
    "message_sender": frozenset({"destructive", "update"}),
    "owner_only": frozenset({"destructive", "restore", "update"}),
    "owner_or_contributor_or_legacy_shared": frozenset(
        {"destructive", "restore", "update"}
    ),
    "owner_or_contributor": frozenset({"update"}),
    "paired_device_owner": frozenset({"activity", "create", "update", "upload"}),
    "personal_credential": frozenset({"credential"}),
    "personal_state": frozenset({"activity", "update"}),
    "shared_collaboration": frozenset({"update"}),
    "system_admin": frozenset({"credential", "system"}),
    "visible_read_only": frozenset({"read"}),
}
AUTHORIZATION_LEGACY_HANDLING = {
    "allowlisted_human_and_one_time_pairing_code": frozenset(
        {"device_binding_required"}
    ),
    "caller_owned": frozenset({"not_applicable"}),
    "caller_owned_credential": frozenset({"not_applicable"}),
    "caller_owned_from_conversation": frozenset({"owner_migration_required"}),
    "caller_owned_from_visible_sources": frozenset({"owner_migration_required"}),
    "conversation_creator_with_unanimous_active_member_approval": frozenset(
        {"owner_migration_required"}
    ),
    "conversation_member_contribution": frozenset({"owner_migration_required"}),
    "conversation_member_personal_state": frozenset({"preserve_personal_state"}),
    "device_owner_or_admin": frozenset({"device_binding_required"}),
    "household_service_action": frozenset({"not_applicable"}),
    "household_shared_content": frozenset(
        {"not_applicable", "preserve_shared_legacy_collaboration"}
    ),
    "message_sender": frozenset({"owner_migration_required"}),
    "owner_only": frozenset(
        {"deny_write_until_owned", "owner_migration_required"}
    ),
    "owner_or_contributor_or_legacy_shared": frozenset(
        {"preserve_shared_legacy_collaboration"}
    ),
    "owner_or_contributor": frozenset({"owner_migration_required"}),
    "paired_device_owner": frozenset({"device_binding_required"}),
    "personal_credential": frozenset({"preserve_personal_state"}),
    "personal_state": frozenset({"preserve_personal_state"}),
    "shared_collaboration": frozenset({"owner_migration_required"}),
    "system_admin": frozenset({"not_applicable"}),
    "visible_read_only": frozenset({"owner_migration_required"}),
}

# Exact reviewed saved-content semantics.  New or changed update/destructive/
# restore routes must be consciously added here; broad authorization classes
# are intentionally insufficient for these high-impact writes.
SAVED_CONTENT_WRITE_RESOURCES = frozenset(
    {
        "assistant_conversation",
        "audiobook",
        "chat_conversation",
        "chat_message",
        "file",
        "media",
        "media_collection",
        "media_collection_membership",
        "media_personal_state",
        "movie",
        "movie_subscription",
        "movie_watched_state",
        "note",
        "place_margarita_record",
        "place_restaurant",
        "place_review",
        "recipe",
        "recipe_activity",
    }
)


def _direct_contract(effect: str, legacy: str, authorization: str) -> tuple[str, str, object]:
    return effect, legacy, authorization


def _action_contract(
    effect: str, legacy: str, **actions: str
) -> tuple[str, str, object]:
    return effect, legacy, MappingProxyType(dict(actions))


REVIEWED_SAVED_CONTENT_WRITE_CONTRACTS = MappingProxyType(
    {
        "media.visibility.update": _direct_contract(
            "update", "deny_write_until_owned", "owner_only"
        ),
        "media.favorite.update": _direct_contract(
            "update", "preserve_personal_state", "personal_state"
        ),
        "media.caption.update": _direct_contract(
            "update", "deny_write_until_owned", "owner_only"
        ),
        "media.trash.bulk": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "media.restore.bulk": _direct_contract(
            "restore", "owner_migration_required", "owner_only"
        ),
        "media.restore_all": _direct_contract(
            "restore", "owner_migration_required", "owner_only"
        ),
        "media.purge.bulk": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "media.purge_all": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "media.trash.single": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "mytube.collection.member.put": _direct_contract(
            "update", "owner_migration_required", "owner_or_contributor"
        ),
        "mytube.collection.member.remove": _direct_contract(
            "update", "owner_migration_required", "owner_or_contributor"
        ),
        "mytube.upload.patch": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "mytube.upload.cancel": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "mytube.video.trash": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "mytube.video.restore": _direct_contract(
            "restore", "owner_migration_required", "owner_only"
        ),
        "mytube.media_link.create": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "mytube.media_link.delete": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "collection.update": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "collection.delete": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "collection.membership.update": _direct_contract(
            "update", "owner_migration_required", "shared_collaboration"
        ),
        "collection.photo_membership.put": _direct_contract(
            "update", "owner_migration_required", "shared_collaboration"
        ),
        "collection.photo_membership.add": _direct_contract(
            "update", "owner_migration_required", "shared_collaboration"
        ),
        "collection.photo_membership.remove": _direct_contract(
            "update", "owner_migration_required", "owner_or_contributor"
        ),
        "assistant.conversation.delete": _direct_contract(
            "destructive", "preserve_shared_legacy_collaboration",
            "household_shared_content"
        ),
        "audiobook.update": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "audiobook.trash": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "audiobook.restore": _direct_contract(
            "restore", "owner_migration_required", "owner_only"
        ),
        "chat.conversation.delete": _action_contract(
            "destructive",
            "owner_migration_required",
            leave="conversation_member_contribution",
            delete_for_all=(
                "conversation_creator_with_unanimous_active_member_approval"
            ),
        ),
        "chat.message.update": _direct_contract(
            "update", "owner_migration_required", "message_sender"
        ),
        "chat.message.delete": _direct_contract(
            "destructive", "owner_migration_required", "message_sender"
        ),
        "file.update": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "file.trash": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "file.restore": _direct_contract(
            "restore", "owner_migration_required", "owner_only"
        ),
        "file.purge": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "movie.delete": _direct_contract(
            "destructive", "preserve_shared_legacy_collaboration",
            "owner_or_contributor_or_legacy_shared"
        ),
        "movie.restore": _direct_contract(
            "restore", "preserve_shared_legacy_collaboration",
            "owner_or_contributor_or_legacy_shared"
        ),
        "movie.watched_state.update": _direct_contract(
            "update", "preserve_personal_state", "personal_state"
        ),
        "movie.subscription.update": _direct_contract(
            "update", "preserve_personal_state", "personal_state"
        ),
        "note.update": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "note.state.update": _action_contract(
            "update",
            "owner_migration_required",
            pin="owner_only",
            unpin="owner_only",
            archive="owner_only",
            unarchive="owner_only",
            trash="owner_only",
            restore="owner_only",
        ),
        "note.purge": _direct_contract(
            "destructive", "owner_migration_required", "owner_only"
        ),
        "place.restaurant.update": _direct_contract(
            "update", "preserve_shared_legacy_collaboration",
            "household_shared_content"
        ),
        "place.review.update": _direct_contract(
            "update", "preserve_shared_legacy_collaboration",
            "household_shared_content"
        ),
        "place.restaurant.delete": _direct_contract(
            "destructive", "preserve_shared_legacy_collaboration",
            "household_shared_content"
        ),
        "place.restaurant.restore": _direct_contract(
            "restore", "preserve_shared_legacy_collaboration",
            "household_shared_content"
        ),
        "place.margarita.update": _direct_contract(
            "update", "preserve_shared_legacy_collaboration",
            "household_shared_content"
        ),
        "recipe.update": _direct_contract(
            "update", "preserve_shared_legacy_collaboration",
            "owner_or_contributor_or_legacy_shared"
        ),
        "recipe.quality.review": _direct_contract(
            "update", "owner_migration_required", "owner_only"
        ),
        "recipe.favorite.update": _direct_contract(
            "update", "preserve_personal_state", "personal_state"
        ),
        "recipe.trash": _direct_contract(
            "destructive", "preserve_shared_legacy_collaboration",
            "owner_or_contributor_or_legacy_shared"
        ),
        "recipe.restore": _direct_contract(
            "restore", "preserve_shared_legacy_collaboration",
            "owner_or_contributor_or_legacy_shared"
        ),
    }
)

PAIRING_ROUTE_CONTRACTS = MappingProxyType(
    {
        "device.backup_pair": ("POST", "/api/v1/device-backup/pair"),
        "device.ios_pair": ("POST", "/api/v1/ios-backup/pair"),
    }
)
DEVICE_REVOCATION_ROUTE_ID = "device.revoke"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")


class PolicyError(ValueError):
    """Raised when policy input is incomplete, ambiguous, or unsafe."""


class UnclassifiedMutation(PolicyError):
    """Raised when an unsafe request has no compiled policy."""


def normalize_principal(value: str | None) -> str:
    return str(value or "").strip().casefold()[:320]


@dataclass(frozen=True)
class RetentionPolicy:
    mode: str
    implementation: str
    minimum_days: int
    confirmation: str
    backup_gate: str
    restore_route_id: str | None = None


@dataclass(frozen=True)
class MutationPolicy:
    id: str
    methods: frozenset[str]
    route: str
    access: str
    effect: str
    resource: str
    authorization: str | None
    authorization_by_action: Mapping[str, str]
    csrf: str
    audit: str
    legacy_handling: str
    retention: RetentionPolicy | None = None

    def authorization_for(self, action: str | None = None) -> str:
        """Resolve the exact authorization name, failing closed on actions."""
        if self.authorization is not None:
            if action is not None:
                raise PolicyError(f"{self.id} does not accept an authorization action")
            return self.authorization
        normalized = str(action or "").strip().casefold()
        try:
            return self.authorization_by_action[normalized]
        except KeyError as error:
            raise PolicyError(f"{self.id} has no authorization for action {normalized!r}") from error


@dataclass(frozen=True)
class CompiledPolicy:
    schema_version: int
    default_access: str
    safe_methods: frozenset[str]
    access_classes: Mapping[str, frozenset[str]]
    mutations: tuple[MutationPolicy, ...]
    by_request: Mapping[tuple[str, str], MutationPolicy]
    by_id: Mapping[str, MutationPolicy]
    enforcement_status: str
    content_admin_override: bool

    def mutation_for(self, method: str, route: str) -> MutationPolicy:
        key = (str(method).upper(), str(route))
        if key[0] not in UNSAFE_METHODS:
            raise PolicyError(f"{key[0]} is not an unsafe method")
        try:
            return self.by_request[key]
        except KeyError as error:
            raise UnclassifiedMutation(f"unsafe route is not classified: {key[0]} {key[1]}") from error


@dataclass(frozen=True)
class Actor:
    principal_id: str
    role: str | None = None
    kind: str = "human"

    def __post_init__(self) -> None:
        object.__setattr__(self, "principal_id", normalize_principal(self.principal_id))
        object.__setattr__(self, "role", str(self.role or "").strip().casefold() or None)
        object.__setattr__(self, "kind", str(self.kind or "").strip().casefold())


@dataclass(frozen=True)
class AuthorizationFacts:
    """Facts loaded under the caller's write transaction.

    ``allow_*_create`` flags distinguish a missing row that may be created from
    a missing row that must fail closed.  Callers must not construct facts from
    stale list responses or client-supplied ownership fields.
    """

    owner_id: str | None = None
    creator_id: str | None = None
    contribution_owner_id: str | None = None
    personal_owner_id: str | None = None
    device_owner_id: str | None = None
    visibility: str | None = None
    member_ids: frozenset[str] = field(default_factory=frozenset)
    active_member_ids: frozenset[str] = field(default_factory=frozenset)
    deletion_approval_member_ids: frozenset[str] = field(default_factory=frozenset)
    legacy: bool = False
    sources_visible: bool = False
    allow_personal_state_create: bool = False
    pairing_code_valid: bool = False
    proposal_binding_valid: bool = False

    def __post_init__(self) -> None:
        for name in (
            "owner_id",
            "creator_id",
            "contribution_owner_id",
            "personal_owner_id",
            "device_owner_id",
        ):
            value = normalize_principal(getattr(self, name)) or None
            object.__setattr__(self, name, value)
        for name in (
            "member_ids",
            "active_member_ids",
            "deletion_approval_member_ids",
        ):
            object.__setattr__(
                self,
                name,
                frozenset(
                    filter(
                        None,
                        (normalize_principal(item) for item in getattr(self, name)),
                    )
                ),
            )
        visibility = str(self.visibility or "").strip().casefold() or None
        if visibility not in {None, "private", "shared"}:
            raise ValueError("visibility must be private, shared, or absent")
        object.__setattr__(self, "visibility", visibility)


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str


def _allow(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(True, reason)


def _deny(reason: str) -> AuthorizationDecision:
    return AuthorizationDecision(False, reason)


def decide_authorization(
    authorization: str,
    actor: Actor,
    facts: AuthorizationFacts | None = None,
) -> AuthorizationDecision:
    """Evaluate a named authorization with no implicit administrator override."""
    if authorization not in KNOWN_AUTHORIZATIONS:
        raise PolicyError(f"unknown authorization {authorization!r}")
    details = facts or AuthorizationFacts()
    actor_id = actor.principal_id
    if not actor_id:
        return _deny("missing_verified_principal")

    is_owner = details.owner_id == actor_id
    is_creator = details.creator_id == actor_id
    is_contributor = details.contribution_owner_id == actor_id
    is_member = actor_id in details.member_ids
    is_household = actor.kind == "human" and actor.role in {"admin", "household"}

    if authorization == "system_admin":
        return _allow("system_admin") if actor.kind == "human" and actor.role == "admin" else _deny("system_admin_required")
    if authorization == "device_owner_or_admin":
        if (
            actor.kind == "human"
            and actor.role in {"admin", "household"}
            and details.device_owner_id == actor_id
        ):
            return _allow("device_owner")
        return _allow("device_admin") if actor.kind == "human" and actor.role == "admin" else _deny("device_owner_required")
    if authorization == "paired_device_owner":
        if actor.kind != "device":
            return _deny("paired_device_principal_required")
        return _allow("paired_device_owner") if details.device_owner_id == actor_id else _deny("paired_device_owner_required")
    if authorization == "allowlisted_human_and_one_time_pairing_code":
        if actor.kind != "human" or actor.role not in {"admin", "household"}:
            return _deny("allowlisted_human_required")
        return _allow("allowlisted_human_with_valid_one_time_pairing_code") if details.pairing_code_valid else _deny("valid_one_time_pairing_code_required")
    if actor.kind != "human" or actor.role not in {"admin", "household"}:
        return _deny("allowlisted_human_required")
    if authorization == "caller_owned":
        return _allow("create_for_caller")
    if authorization == "household_shared_content":
        return _allow("household_shared_content")
    if authorization == "caller_owned_credential":
        return _allow("credential_for_caller")
    if authorization == "owner_only":
        return _allow("owner") if is_owner else _deny("owner_required")
    if authorization == "owner_or_contributor_or_legacy_shared":
        if is_owner or is_contributor:
            return _allow("owner_or_contributor")
        if (
            details.legacy
            and details.owner_id is None
            and details.visibility == "shared"
            and is_household
        ):
            return _allow("legacy_shared_household")
        return _deny("owner_contributor_or_legacy_shared_required")
    if authorization == "owner_or_contributor":
        return _allow("owner_or_contributor") if is_owner or is_contributor else _deny("owner_or_contributor_required")
    if authorization == "conversation_creator_with_unanimous_active_member_approval":
        if details.legacy:
            return _deny("nonlegacy_conversation_required")
        if not is_creator:
            return _deny("conversation_creator_required")
        active_members = details.active_member_ids
        if not active_members:
            return _deny("nonempty_active_membership_required")
        if actor_id not in active_members or details.creator_id not in active_members:
            return _deny("active_conversation_creator_required")
        if details.deletion_approval_member_ids != active_members:
            return _deny("unanimous_active_member_approval_required")
        if details.proposal_binding_valid is not True:
            return _deny("current_deletion_proposal_binding_required")
        return _allow("conversation_creator_with_unanimous_active_member_approval")
    if authorization in {"conversation_member_contribution", "caller_owned_from_conversation"}:
        return _allow("conversation_member") if is_member else _deny("conversation_membership_required")
    if authorization == "message_sender":
        return _allow("message_sender") if is_contributor else _deny("message_sender_required")
    if authorization == "conversation_member_personal_state":
        if not is_member:
            return _deny("conversation_membership_required")
        if details.personal_owner_id == actor_id or (
            details.personal_owner_id is None and details.allow_personal_state_create
        ):
            return _allow("member_personal_state")
        return _deny("personal_state_owner_required")
    if authorization in {"personal_state", "personal_credential"}:
        if details.personal_owner_id == actor_id or (
            details.personal_owner_id is None and details.allow_personal_state_create
        ):
            return _allow("personal_state")
        return _deny("personal_state_owner_required")
    if authorization == "caller_owned_from_visible_sources":
        return _allow("visible_sources") if details.sources_visible else _deny("visible_sources_required")
    if authorization == "visible_read_only":
        if is_owner or (details.visibility == "shared" and is_household):
            return _allow("visible_read_only")
        return _deny("visible_resource_required")
    if authorization == "shared_collaboration":
        if is_owner or (details.visibility == "shared" and is_household):
            return _allow("shared_collaboration")
        return _deny("shared_or_owned_resource_required")
    if authorization == "household_service_action":
        return _allow("household_service_action") if is_household else _deny("household_role_required")
    raise AssertionError(f"authorization evaluator missing for {authorization}")


def decide_transaction_authorization(
    mutation: MutationPolicy,
    actor: Actor,
    facts: AuthorizationFacts | None = None,
    *,
    action: str | None = None,
) -> AuthorizationDecision:
    """Resolve a route/action and evaluate facts read in the write transaction."""
    return decide_authorization(mutation.authorization_for(action), actor, facts)


@dataclass(frozen=True)
class CsrfContract:
    safe_methods: frozenset[str]
    required: frozenset[tuple[str, str]]
    read_only_exempt: frozenset[tuple[str, str]]
    bearer_exempt: frozenset[tuple[str, str]]
    pairing_code_exempt: frozenset[tuple[str, str]]

    def classify(self, method: str, route: str) -> str:
        upper = str(method).upper()
        if upper in self.safe_methods:
            return "safe"
        key = (upper, str(route))
        for name in ("required", "read_only_exempt", "bearer_exempt", "pairing_code_exempt"):
            if key in getattr(self, name):
                return name
        raise UnclassifiedMutation(f"unsafe route has no CSRF class: {upper} {route}")

    def legacy_constants(self) -> Mapping[str, object]:
        """Return exact-set equivalents suitable for a future middleware swap.

        The keys retain the names used by ``modules.security``.  Bearer
        exemptions are exact method/route pairs because broad prefixes also
        exempt human pairing endpoints and therefore cannot encode schema v2.
        """
        read_only_posts = frozenset(route for method, route in self.read_only_exempt if method == "POST")
        return MappingProxyType(
            {
                "SAFE_METHODS": self.safe_methods,
                "READ_ONLY_POSTS": read_only_posts,
                "EXACT_BEARER_EXEMPT_ROUTES": self.bearer_exempt,
                "EXACT_PAIRING_CODE_EXEMPT_ROUTES": self.pairing_code_exempt,
            }
        )


def derive_csrf_contract(policy: CompiledPolicy) -> CsrfContract:
    classes: dict[str, set[tuple[str, str]]] = {name: set() for name in KNOWN_CSRF_CLASSES}
    for mutation in policy.mutations:
        for method in mutation.methods:
            classes[mutation.csrf].add((method, mutation.route))
    return CsrfContract(
        safe_methods=policy.safe_methods,
        required=frozenset(classes["required"]),
        read_only_exempt=frozenset(classes["read_only_exempt"]),
        bearer_exempt=frozenset(classes["bearer_exempt"]),
        pairing_code_exempt=frozenset(classes["pairing_code_exempt"]),
    )


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{label} must be a non-empty string")
    return value.strip()


def _compile_retention(raw: Any, route_id: str) -> RetentionPolicy:
    if not isinstance(raw, dict):
        raise PolicyError(f"{route_id}.retention must be an object")
    allowed = {
        "mode",
        "implementation",
        "minimum_days",
        "confirmation",
        "backup_gate",
        "restore_route_id",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise PolicyError(f"{route_id}.retention has unknown fields: {sorted(unknown)}")
    mode = _string(raw.get("mode"), f"{route_id}.retention.mode")
    implementation = _string(raw.get("implementation"), f"{route_id}.retention.implementation")
    confirmation = _string(raw.get("confirmation"), f"{route_id}.retention.confirmation")
    backup_gate = _string(raw.get("backup_gate"), f"{route_id}.retention.backup_gate")
    minimum_days = raw.get("minimum_days")
    restore_route_id = raw.get("restore_route_id")
    if mode not in KNOWN_RETENTION_MODES:
        raise PolicyError(f"{route_id} has unknown retention mode {mode!r}")
    if implementation not in KNOWN_RETENTION_IMPLEMENTATIONS:
        raise PolicyError(f"{route_id} has unknown retention implementation {implementation!r}")
    if not isinstance(minimum_days, int) or isinstance(minimum_days, bool) or not 0 <= minimum_days <= 3650:
        raise PolicyError(f"{route_id}.retention.minimum_days must be an integer from 0 to 3650")
    if confirmation not in {"none", "required"}:
        raise PolicyError(f"{route_id}.retention.confirmation is invalid")
    if backup_gate not in {"not_applicable", "required_before_permanent_delete"}:
        raise PolicyError(f"{route_id}.retention.backup_gate is invalid")
    if restore_route_id is not None:
        restore_route_id = _string(restore_route_id, f"{route_id}.retention.restore_route_id")
    if mode == "permanent_delete" and confirmation != "required":
        raise PolicyError(f"{route_id} permanent deletion must require confirmation")
    if mode == "revocation":
        if backup_gate != "not_applicable" or minimum_days != 0:
            raise PolicyError(f"{route_id} credential revocation cannot be delayed by content retention")
    elif (
        implementation != "domain_pending"
        or minimum_days < 30
        or backup_gate != "required_before_permanent_delete"
    ):
        raise PolicyError(
            f"{route_id} saved-content retention must remain a pending 30-day backup-gated contract"
        )
    return RetentionPolicy(
        mode,
        implementation,
        minimum_days,
        confirmation,
        backup_gate,
        restore_route_id,
    )


def compile_route_policy(document: Mapping[str, Any]) -> CompiledPolicy:
    """Validate and compile schema v2, rejecting ambiguous unsafe behavior."""
    if not isinstance(document, Mapping):
        raise PolicyError("route policy must be an object")
    top_level_fields = {
        "schema_version",
        "enforcement",
        "default_access",
        "safe_methods",
        "principals",
        "access_classes",
        "public_routes",
        "device_bearer_routes",
        "admin_routes",
        "media_read_routes",
        "mutations",
    }
    missing_top_level = top_level_fields - set(document)
    unknown_top_level = set(document) - top_level_fields
    if missing_top_level or unknown_top_level:
        raise PolicyError(
            "top-level policy fields are invalid; "
            f"missing={sorted(missing_top_level)} unknown={sorted(unknown_top_level)}"
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PolicyError(f"route policy schema_version must be {SCHEMA_VERSION}")
    enforcement = document.get("enforcement")
    if not isinstance(enforcement, Mapping):
        raise PolicyError("enforcement metadata is required")
    if set(enforcement) != {"status", "content_admin_override", "notice"}:
        raise PolicyError("enforcement fields must be status, content_admin_override, and notice")
    if enforcement.get("status") != "foundation_only":
        raise PolicyError("schema v2 must remain foundation_only until route integration")
    if enforcement.get("content_admin_override") is not False:
        raise PolicyError("administrator content override must be explicitly false")
    _string(enforcement.get("notice"), "enforcement.notice")

    safe_raw = document.get("safe_methods")
    if not isinstance(safe_raw, list) or not safe_raw:
        raise PolicyError("safe_methods must be a non-empty list")
    safe_methods = frozenset(_string(item, "safe method").upper() for item in safe_raw)
    if len(safe_methods) != len(safe_raw) or safe_methods != {"GET", "HEAD", "OPTIONS"}:
        raise PolicyError("safe_methods must be GET, HEAD, and OPTIONS")

    principals = document.get("principals")
    access_raw = document.get("access_classes")
    if not isinstance(principals, Mapping) or not isinstance(access_raw, Mapping):
        raise PolicyError("principals and access_classes are required")
    if set(principals) != {"missing", "unknown", "david", "diana", "device"}:
        raise PolicyError("principal inventory must contain only the five known principals")
    for name, principal in principals.items():
        if not isinstance(principal, Mapping) or set(principal) != {"kind", "login", "role"}:
            raise PolicyError(f"principal {name!r} has invalid fields")
    expected_principals = {
        "missing": {"kind": "human", "login": None, "role": None},
        "unknown": {"kind": "human", "login": "unknown@example.invalid", "role": None},
        "david": {"kind": "human", "login": "david@example.test", "role": "admin"},
        "diana": {"kind": "human", "login": "diana@example.test", "role": "household"},
        "device": {"kind": "bearer", "login": None, "role": "device"},
    }
    if principals != expected_principals:
        raise PolicyError("principal definitions must match the reviewed identity inventory")
    if principals.get("david") != {
        "kind": "human",
        "login": "david@example.test",
        "role": "admin",
    }:
        raise PolicyError("The synthetic administrator fixture must retain its role")
    if principals.get("diana") != {
        "kind": "human",
        "login": "diana@example.test",
        "role": "household",
    }:
        raise PolicyError("The synthetic member fixture must retain its role")

    access_classes: dict[str, frozenset[str]] = {}
    if set(access_raw) != KNOWN_ACCESS_CLASSES:
        raise PolicyError("access class inventory must contain only reviewed classes")
    for name, raw in access_raw.items():
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"allowed_principals"}
            or not isinstance(raw.get("allowed_principals"), list)
        ):
            raise PolicyError(f"access class {name!r} is invalid")
        allowed = frozenset(_string(item, f"{name} principal") for item in raw["allowed_principals"])
        if len(allowed) != len(raw["allowed_principals"]):
            raise PolicyError(f"access class {name!r} repeats a principal")
        if not allowed <= set(principals):
            raise PolicyError(f"access class {name!r} names unknown principals")
        access_classes[str(name)] = allowed
    expected_access = {
        "public": frozenset({"missing", "unknown", "david", "diana", "device"}),
        "portal": frozenset({"david", "diana"}),
        "admin": frozenset({"david"}),
        "device_bearer": frozenset({"device"}),
    }
    if access_classes != expected_access:
        raise PolicyError("access class principal matrix must match the reviewed least-privilege inventory")
    default_access = _string(document.get("default_access"), "default_access")
    if default_access not in access_classes:
        raise PolicyError("default_access must name a defined access class")

    raw_mutations = document.get("mutations")
    if not isinstance(raw_mutations, list) or not raw_mutations:
        raise PolicyError("mutations must be a non-empty list")
    mutations: list[MutationPolicy] = []
    by_request: dict[tuple[str, str], MutationPolicy] = {}
    by_id: dict[str, MutationPolicy] = {}
    required_fields = {
        "id",
        "methods",
        "route",
        "access",
        "effect",
        "resource",
        "csrf",
        "audit",
        "legacy_handling",
    }
    allowed_fields = required_fields | {"authorization", "authorization_by_action", "retention"}

    for index, raw in enumerate(raw_mutations):
        if not isinstance(raw, Mapping):
            raise PolicyError(f"mutation {index} must be an object")
        missing = required_fields - set(raw)
        unknown = set(raw) - allowed_fields
        if missing or unknown:
            raise PolicyError(f"mutation {index} fields invalid; missing={sorted(missing)} unknown={sorted(unknown)}")
        route_id = _string(raw["id"], f"mutation {index} id")
        if not ID_PATTERN.fullmatch(route_id) or route_id in by_id:
            raise PolicyError(f"mutation id is invalid or duplicate: {route_id!r}")
        route = _string(raw["route"], f"{route_id}.route")
        if not route.startswith("/"):
            raise PolicyError(f"{route_id}.route must be absolute")
        methods_raw = raw["methods"]
        if not isinstance(methods_raw, list) or not methods_raw:
            raise PolicyError(f"{route_id}.methods must be a non-empty list")
        methods = frozenset(_string(item, f"{route_id} method").upper() for item in methods_raw)
        if len(methods) != len(methods_raw) or not methods <= UNSAFE_METHODS:
            raise PolicyError(f"{route_id}.methods must be unique unsafe HTTP methods")
        access = _string(raw["access"], f"{route_id}.access")
        effect = _string(raw["effect"], f"{route_id}.effect")
        resource = _string(raw["resource"], f"{route_id}.resource")
        csrf = _string(raw["csrf"], f"{route_id}.csrf")
        audit = _string(raw["audit"], f"{route_id}.audit")
        legacy_handling = _string(raw["legacy_handling"], f"{route_id}.legacy_handling")
        if access not in access_classes:
            raise PolicyError(f"{route_id} names unknown access class {access!r}")
        if effect not in KNOWN_EFFECTS:
            raise PolicyError(f"{route_id} names unknown effect {effect!r}")
        if resource not in KNOWN_RESOURCES:
            raise PolicyError(f"{route_id} names unknown resource {resource!r}")
        if csrf not in KNOWN_CSRF_CLASSES:
            raise PolicyError(f"{route_id} names unknown CSRF class {csrf!r}")
        if audit not in KNOWN_AUDIT_CLASSES:
            raise PolicyError(f"{route_id} names unknown audit class {audit!r}")
        if legacy_handling not in KNOWN_LEGACY_HANDLING:
            raise PolicyError(f"{route_id} names unknown legacy handling {legacy_handling!r}")

        has_authorization = "authorization" in raw
        has_actions = "authorization_by_action" in raw
        if has_authorization == has_actions:
            raise PolicyError(f"{route_id} must declare exactly one authorization form")
        authorization: str | None = None
        actions: dict[str, str] = {}
        if has_authorization:
            authorization = _string(raw["authorization"], f"{route_id}.authorization")
            if authorization not in KNOWN_AUTHORIZATIONS:
                raise PolicyError(f"{route_id} names unknown authorization {authorization!r}")
        else:
            action_raw = raw["authorization_by_action"]
            if not isinstance(action_raw, Mapping) or not action_raw:
                raise PolicyError(f"{route_id}.authorization_by_action must be a non-empty object")
            for action, name in action_raw.items():
                normalized_action = _string(action, f"{route_id} action").casefold()
                auth_name = _string(name, f"{route_id}.{normalized_action} authorization")
                if normalized_action != action or normalized_action in actions:
                    raise PolicyError(f"{route_id} action names must be unique lowercase strings")
                if auth_name not in KNOWN_AUTHORIZATIONS:
                    raise PolicyError(f"{route_id} names unknown authorization {auth_name!r}")
                actions[normalized_action] = auth_name

        auth_names = {authorization} if authorization else set(actions.values())
        if "system_admin" in auth_names and resource not in {"system_power", "system_settings", "device_credential"}:
            raise PolicyError(f"{route_id} cannot use administrator status as a content override")
        if "device_owner_or_admin" in auth_names and resource != "device_credential":
            raise PolicyError(f"{route_id} cannot use device administration as a content override")
        if effect == "destructive" and any(
            name in {"shared_collaboration", "visible_read_only"} for name in auth_names
        ):
            raise PolicyError(f"{route_id} destructive access cannot be visibility-authorized")
        if any(
            action in {"delete", "delete_for_all", "purge", "trash"}
            and name in {"shared_collaboration", "visible_read_only"}
            for action, name in actions.items()
        ):
            raise PolicyError(f"{route_id} destructive action cannot be visibility-authorized")
        for auth_name in auth_names:
            if resource not in AUTHORIZATION_RESOURCES[auth_name]:
                raise PolicyError(
                    f"{route_id} authorization {auth_name!r} is invalid for resource {resource!r}"
                )
            if effect not in AUTHORIZATION_EFFECTS[auth_name]:
                raise PolicyError(
                    f"{route_id} authorization {auth_name!r} is invalid for effect {effect!r}"
                )
            if legacy_handling not in AUTHORIZATION_LEGACY_HANDLING[auth_name]:
                raise PolicyError(
                    f"{route_id} authorization {auth_name!r} is invalid for legacy handling "
                    f"{legacy_handling!r}"
                )
        if csrf == "read_only_exempt" and not (methods == {"POST"} and effect == "read"):
            raise PolicyError(f"{route_id} has an invalid read-only CSRF exemption")
        if csrf == "read_only_exempt" and auth_names != {"visible_read_only"}:
            raise PolicyError(f"{route_id} read-only CSRF exemption requires visible_read_only")
        if csrf == "bearer_exempt" and not (
            access == "device_bearer" and auth_names == {"paired_device_owner"}
        ):
            raise PolicyError(f"{route_id} has an invalid bearer CSRF exemption")
        if access == "device_bearer" and csrf != "bearer_exempt":
            raise PolicyError(f"{route_id} device bearer mutation must use bearer_exempt")
        if access != "device_bearer" and csrf == "bearer_exempt":
            raise PolicyError(f"{route_id} human route cannot use bearer_exempt")
        request_keys = {(method, route) for method in methods}
        if csrf == "bearer_exempt" and not request_keys <= BEARER_EXEMPT_ROUTES:
            raise PolicyError(f"{route_id} is not an approved exact bearer exemption")
        if csrf == "pairing_code_exempt" and not (
            access == "portal"
            and auth_names == {"allowlisted_human_and_one_time_pairing_code"}
            and request_keys <= PAIRING_CODE_EXEMPT_ROUTES
        ):
            raise PolicyError(f"{route_id} has an invalid pairing-code CSRF exemption")
        if effect == "restore" and not auth_names <= {
            "owner_only", "owner_or_contributor_or_legacy_shared",
            "household_shared_content",
        }:
            raise PolicyError(
                f"{route_id} restore access must use an exact reviewed "
                "saved-content authorization"
            )
        expected_audit = (
            "security_event"
            if effect in {"credential", "system"} or resource == "device_credential"
            else "read_event"
            if effect == "read"
            else "activity_event"
            if effect == "activity"
            else "mutation_event"
        )
        if audit != expected_audit:
            raise PolicyError(f"{route_id} must use audit class {expected_audit!r}")

        retention = _compile_retention(raw["retention"], route_id) if "retention" in raw else None
        if effect == "destructive" and retention is None:
            raise PolicyError(f"{route_id} destructive policy requires retention metadata")
        if effect == "restore" and (
            retention is None or retention.mode not in {"soft_delete", "leave_or_soft_delete"}
        ):
            raise PolicyError(f"{route_id} restore policy requires recoverable retention metadata")

        mutation = MutationPolicy(
            id=route_id,
            methods=methods,
            route=route,
            access=access,
            effect=effect,
            resource=resource,
            authorization=authorization,
            authorization_by_action=MappingProxyType(actions),
            csrf=csrf,
            audit=audit,
            legacy_handling=legacy_handling,
            retention=retention,
        )
        for method in methods:
            key = (method, route)
            if key in by_request:
                raise PolicyError(f"duplicate unsafe route classification: {method} {route}")
            by_request[key] = mutation
        by_id[route_id] = mutation
        mutations.append(mutation)

    for mutation in mutations:
        retention = mutation.retention
        if retention and retention.restore_route_id:
            restore = by_id.get(retention.restore_route_id)
            if restore is None or restore.effect != "restore" or restore.resource != mutation.resource:
                raise PolicyError(f"{mutation.id} references an invalid restore route")

    # Bind every reviewed saved-content update/destructive/restore route to its
    # exact ownership or contribution semantics.  This catches coupled edits
    # that change authorization, effect, and legacy handling together.
    actual_saved_write_ids = {
        mutation.id
        for mutation in mutations
        if mutation.resource in SAVED_CONTENT_WRITE_RESOURCES
        and mutation.effect in {"update", "destructive", "restore"}
    }
    reviewed_saved_write_ids = set(REVIEWED_SAVED_CONTENT_WRITE_CONTRACTS)
    if actual_saved_write_ids != reviewed_saved_write_ids:
        raise PolicyError(
            "saved-content write route inventory differs from reviewed contracts; "
            f"missing={sorted(reviewed_saved_write_ids - actual_saved_write_ids)} "
            f"unreviewed={sorted(actual_saved_write_ids - reviewed_saved_write_ids)}"
        )
    for route_id, (effect, legacy_handling, expected_authorization) in (
        REVIEWED_SAVED_CONTENT_WRITE_CONTRACTS.items()
    ):
        mutation = by_id[route_id]
        if mutation.effect != effect or mutation.legacy_handling != legacy_handling:
            raise PolicyError(
                f"{route_id} differs from its reviewed saved-content effect/legacy contract"
            )
        if isinstance(expected_authorization, str):
            authorization_matches = (
                mutation.authorization == expected_authorization
                and not mutation.authorization_by_action
            )
        else:
            authorization_matches = (
                mutation.authorization is None
                and dict(mutation.authorization_by_action)
                == dict(expected_authorization)
            )
        if not authorization_matches:
            raise PolicyError(
                f"{route_id} differs from its reviewed saved-content authorization contract"
            )

    # Pairing is a two-route human workflow.  Both directions must remain
    # allowlisted portal requests authenticated by the same one-time code;
    # changing both entries together must not silently erase the exception.
    pairing_exemptions = {
        (method, mutation.route)
        for mutation in mutations
        if mutation.csrf == "pairing_code_exempt"
        for method in mutation.methods
    }
    if pairing_exemptions != PAIRING_CODE_EXEMPT_ROUTES:
        raise PolicyError(
            "pair routes must preserve the exact bidirectional pairing-code exemption"
        )
    for route_id, (method, route) in PAIRING_ROUTE_CONTRACTS.items():
        mutation = by_id.get(route_id)
        if mutation is None or (
            mutation.methods != {method}
            or mutation.route != route
            or mutation.access != "portal"
            or mutation.effect != "credential"
            or mutation.resource != "device_credential"
            or mutation.authorization
            != "allowlisted_human_and_one_time_pairing_code"
            or mutation.authorization_by_action
            or mutation.csrf != "pairing_code_exempt"
            or mutation.audit != "security_event"
            or mutation.legacy_handling != "device_binding_required"
            or mutation.retention is not None
        ):
            raise PolicyError(
                f"{route_id} differs from the reviewed human pairing route contract"
            )

    # Content retention never doubles as credential revocation.  Exactly one
    # reviewed device-credential route may use the zero-day revocation mode.
    revocation_ids = {
        mutation.id
        for mutation in mutations
        if mutation.retention is not None
        and mutation.retention.mode == "revocation"
    }
    if revocation_ids != {DEVICE_REVOCATION_ROUTE_ID}:
        raise PolicyError(
            "revocation retention is reserved for the exact reviewed device.revoke route"
        )
    revocation = by_id.get(DEVICE_REVOCATION_ROUTE_ID)
    expected_revocation_retention = RetentionPolicy(
        mode="revocation",
        implementation="enforced",
        minimum_days=0,
        confirmation="required",
        backup_gate="not_applicable",
    )
    if revocation is None or (
        revocation.methods != {"POST"}
        or revocation.route != "/api/device-backup/devices/<device_id>/revoke"
        or revocation.access != "portal"
        or revocation.effect != "destructive"
        or revocation.resource != "device_credential"
        or revocation.authorization != "device_owner_or_admin"
        or revocation.authorization_by_action
        or revocation.csrf != "required"
        or revocation.audit != "security_event"
        or revocation.legacy_handling != "device_binding_required"
        or revocation.retention != expected_revocation_retention
    ):
        raise PolicyError(
            "device.revoke differs from the reviewed credential revocation contract"
        )

    return CompiledPolicy(
        schema_version=SCHEMA_VERSION,
        default_access=default_access,
        safe_methods=safe_methods,
        access_classes=MappingProxyType(access_classes),
        mutations=tuple(mutations),
        by_request=MappingProxyType(by_request),
        by_id=MappingProxyType(by_id),
        enforcement_status="foundation_only",
        content_admin_override=False,
    )


@lru_cache(maxsize=16)
def _load_cached(path: str, modified_ns: int, byte_size: int) -> CompiledPolicy:
    del modified_ns
    if byte_size > 2 * 1024 * 1024:
        raise PolicyError("route policy exceeds the 2 MiB safety limit")
    try:
        raw = Path(path).read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PolicyError(f"cannot load route policy: {error}") from error
    return compile_route_policy(document)


def load_route_policy(path: str | Path = DEFAULT_POLICY_PATH) -> CompiledPolicy:
    """Load and cache a policy by canonical path and file identity metadata."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        stat_result = resolved.stat()
    except OSError as error:
        raise PolicyError(f"cannot stat route policy: {error}") from error
    if not resolved.is_file():
        raise PolicyError("route policy is not a regular file")
    return _load_cached(str(resolved), stat_result.st_mtime_ns, stat_result.st_size)


def clear_policy_cache() -> None:
    _load_cached.cache_clear()


def validate_runtime_routes(
    policy: CompiledPolicy,
    routes: Iterable[tuple[str, str]],
) -> None:
    """Require exact equality between runtime and policy unsafe surfaces."""
    unsafe_routes = [
        (str(method).upper(), str(route))
        for method, route in routes
        if str(method).upper() not in policy.safe_methods
    ]
    runtime = set(unsafe_routes)
    if len(runtime) != len(unsafe_routes):
        raise PolicyError("runtime URL map contains duplicate unsafe method/route rules")
    declared = set(policy.by_request)
    missing = declared - runtime
    unmatched = runtime - declared
    if missing or unmatched:
        raise PolicyError(
            "runtime unsafe route mismatch; "
            f"missing={sorted(missing)} unmatched={sorted(unmatched)}"
        )


def validate_runtime_url_map(policy: CompiledPolicy, url_map: Any) -> None:
    """Validate a Flask/Werkzeug URL map without importing the application."""
    validate_runtime_routes(
        policy,
        (
            (method, rule.rule)
            for rule in url_map.iter_rules()
            for method in set(rule.methods or ())
        ),
    )
