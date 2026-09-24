import json
from pathlib import Path

import pytest

from modules.content_policy import (
    Actor,
    AuthorizationFacts,
    KNOWN_AUTHORIZATIONS,
    PolicyError,
    UnclassifiedMutation,
    compile_route_policy,
    decide_authorization,
    decide_transaction_authorization,
    derive_csrf_contract,
    load_route_policy,
    validate_runtime_routes,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "route-policy.json"
DAVID = Actor("david@example.test", "admin")
DIANA = Actor("diana@example.test", "household")
DEVICE = Actor("phone-device-1", "device", "device")


def document():
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def mutation(raw, route_id):
    return next(item for item in raw["mutations"] if item["id"] == route_id)


def test_source_policy_compiles_and_loader_is_cached_without_claiming_enforcement():
    first = load_route_policy()
    second = load_route_policy()
    assert first is second
    assert first.schema_version == 2
    assert first.enforcement_status == "foundation_only"
    assert first.content_admin_override is False
    assert len(first.mutations) == len(document()["mutations"])
    assert len(first.by_request) == sum(len(item["methods"]) for item in document()["mutations"])


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "methods",
        "route",
        "access",
        "effect",
        "resource",
        "csrf",
        "audit",
        "legacy_handling",
    ],
)
def test_compiler_rejects_every_missing_required_mutation_field(field):
    raw = document()
    raw["mutations"][0].pop(field)
    with pytest.raises(PolicyError, match="missing"):
        compile_route_policy(raw)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("resource", "anything", "unknown resource"),
        ("authorization", "root_can_do_anything", "unknown authorization"),
        ("csrf", "trusted", "unknown CSRF"),
        ("audit", "optional", "unknown audit"),
        ("legacy_handling", "guess_owner", "unknown legacy"),
        ("effect", "maybe_write", "unknown effect"),
    ],
)
def test_compiler_rejects_unknown_policy_vocabulary(field, value, message):
    raw = document()
    raw["mutations"][0][field] = value
    with pytest.raises(PolicyError, match=message):
        compile_route_policy(raw)


def test_compiler_rejects_duplicate_ids_and_duplicate_method_routes():
    raw = document()
    raw["mutations"][1]["id"] = raw["mutations"][0]["id"]
    with pytest.raises(PolicyError, match="invalid or duplicate"):
        compile_route_policy(raw)

    raw = document()
    raw["mutations"][1]["route"] = raw["mutations"][0]["route"]
    raw["mutations"][1]["methods"] = raw["mutations"][0]["methods"]
    with pytest.raises(PolicyError, match="duplicate unsafe route"):
        compile_route_policy(raw)


def test_compiler_requires_exactly_one_authorization_shape_and_known_actions():
    raw = document()
    item = mutation(raw, "note.state.update")
    item["authorization"] = "owner_only"
    with pytest.raises(PolicyError, match="exactly one authorization"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "note.update").pop("authorization")
    with pytest.raises(PolicyError, match="exactly one authorization"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "note.state.update")["authorization_by_action"]["trash"] = "visible_write"
    with pytest.raises(PolicyError, match="unknown authorization"):
        compile_route_policy(raw)


def test_compiler_rejects_visibility_authorized_destruction_in_both_shapes():
    raw = document()
    mutation(raw, "movie.delete")["authorization"] = "shared_collaboration"
    with pytest.raises(PolicyError, match="cannot be visibility-authorized"):
        compile_route_policy(raw)

    raw = document()
    item = mutation(raw, "movie.delete")
    item.pop("authorization")
    item["authorization_by_action"] = {"delete": "visible_read_only"}
    with pytest.raises(PolicyError, match="cannot be visibility-authorized"):
        compile_route_policy(raw)


def test_compiler_rejects_administrator_content_override_and_wrong_audit_class():
    raw = document()
    mutation(raw, "movie.delete")["authorization"] = "system_admin"
    with pytest.raises(PolicyError, match="administrator status as a content override"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "note.create")["authorization"] = "household_service_action"
    with pytest.raises(PolicyError, match="invalid for resource"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "note.update")["audit"] = "security_event"
    with pytest.raises(PolicyError, match="must use audit class"):
        compile_route_policy(raw)


def test_compiler_rejects_invalid_csrf_exemptions():
    raw = document()
    mutation(raw, "note.create")["csrf"] = "read_only_exempt"
    with pytest.raises(PolicyError, match="invalid read-only"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "note.create")["csrf"] = "bearer_exempt"
    with pytest.raises(PolicyError, match="invalid bearer"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "device.backup_pair")["authorization"] = "system_admin"
    with pytest.raises(PolicyError, match="invalid"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "device.upload.create")["csrf"] = "required"
    with pytest.raises(PolicyError, match="device bearer mutation"):
        compile_route_policy(raw)

    raw = document()
    item = mutation(raw, "device.upload.create")
    item["route"] = "/api/v1/device-backup/arbitrary"
    with pytest.raises(PolicyError, match="approved exact bearer"):
        compile_route_policy(raw)

    raw = document()
    item = mutation(raw, "device.backup_pair")
    item["route"] = "/api/v1/device-backup/arbitrary-pair"
    with pytest.raises(PolicyError, match="invalid pairing-code"):
        compile_route_policy(raw)

    raw = document()
    item = mutation(raw, "collection.membership_state.read")
    item["authorization"] = "shared_collaboration"
    with pytest.raises(PolicyError, match="invalid for effect|read-only CSRF exemption"):
        compile_route_policy(raw)


def test_compiler_requires_safe_retention_contracts_and_valid_restore_links():
    raw = document()
    mutation(raw, "movie.delete").pop("retention")
    with pytest.raises(PolicyError, match="requires retention"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "note.purge")["retention"]["confirmation"] = "none"
    with pytest.raises(PolicyError, match="must require confirmation"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "file.trash")["retention"]["restore_route_id"] = "audiobook.restore"
    with pytest.raises(PolicyError, match="invalid restore route"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "file.restore").pop("retention")
    with pytest.raises(PolicyError, match="restore policy"):
        compile_route_policy(raw)


def test_runtime_validator_rejects_both_unmatched_and_missing_unsafe_routes():
    policy = load_route_policy()
    exact = set(policy.by_request)
    validate_runtime_routes(policy, exact | {("GET", "/safe")})

    missing = exact - {next(iter(exact))}
    with pytest.raises(PolicyError, match="missing="):
        validate_runtime_routes(policy, missing)

    with pytest.raises(PolicyError, match="unmatched="):
        validate_runtime_routes(policy, exact | {("POST", "/api/unclassified")})

    duplicate = [*exact, next(iter(exact))]
    with pytest.raises(PolicyError, match="duplicate unsafe"):
        validate_runtime_routes(policy, duplicate)

    with pytest.raises(UnclassifiedMutation):
        policy.mutation_for("DELETE", "/api/unclassified")


def test_csrf_contract_is_exact_and_retains_legacy_constant_names():
    policy = load_route_policy()
    contract = derive_csrf_contract(policy)
    assert contract.classify("GET", "/anything") == "safe"
    assert contract.classify("POST", "/api/collections/membership-state") == "read_only_exempt"
    assert contract.classify("POST", "/api/v1/device-backup/uploads") == "bearer_exempt"
    assert contract.classify("POST", "/api/v1/device-backup/pair") == "pairing_code_exempt"
    assert contract.classify("POST", "/api/v1/ios-backup/pair") == "pairing_code_exempt"
    assert contract.classify("POST", "/api/notes") == "required"
    with pytest.raises(UnclassifiedMutation):
        contract.classify("POST", "/api/v1/device-backup/not-a-route")

    legacy = contract.legacy_constants()
    assert legacy["SAFE_METHODS"] == {"GET", "HEAD", "OPTIONS"}
    assert legacy["READ_ONLY_POSTS"] == {"/api/collections/membership-state"}
    assert ("POST", "/api/v1/device-backup/pair") not in legacy["EXACT_BEARER_EXEMPT_ROUTES"]
    assert ("POST", "/api/v1/device-backup/pair") in legacy["EXACT_PAIRING_CODE_EXEMPT_ROUTES"]


def test_admin_is_not_a_saved_content_superuser_and_diana_keeps_her_content_rights():
    diana_note = AuthorizationFacts(owner_id=DIANA.principal_id, visibility="shared")
    david_note = AuthorizationFacts(owner_id=DAVID.principal_id, visibility="shared")
    assert decide_authorization("owner_only", DIANA, diana_note).allowed
    assert not decide_authorization("owner_only", DAVID, diana_note).allowed
    assert decide_authorization("owner_only", DAVID, david_note).allowed
    assert not decide_authorization("owner_only", DIANA, david_note).allowed

    shared = AuthorizationFacts(owner_id=DAVID.principal_id, visibility="shared")
    private = AuthorizationFacts(owner_id=DAVID.principal_id, visibility="private")
    assert decide_authorization("shared_collaboration", DIANA, shared).allowed
    assert not decide_authorization("shared_collaboration", DIANA, private).allowed
    assert decide_authorization("system_admin", DAVID).allowed
    assert not decide_authorization("system_admin", DIANA).allowed


def test_legacy_unclaimed_media_is_readable_shared_but_immutable():
    legacy = AuthorizationFacts(owner_id=None, visibility="shared", legacy=True)
    assert decide_authorization("visible_read_only", DAVID, legacy).allowed
    assert decide_authorization("visible_read_only", DIANA, legacy).allowed
    assert not decide_authorization("owner_only", DAVID, legacy).allowed
    assert not decide_authorization("owner_only", DIANA, legacy).allowed
    policy = load_route_policy()
    visibility = policy.by_id["media.visibility.update"]
    assert visibility.authorization == "owner_only"
    assert visibility.legacy_handling == "deny_write_until_owned"


def test_legacy_shared_household_compatibility_is_exact_and_does_not_cross_ownership():
    authorization = "owner_or_contributor_or_legacy_shared"
    legacy_shared = AuthorizationFacts(owner_id=None, visibility="shared", legacy=True)
    legacy_private = AuthorizationFacts(owner_id=None, visibility="private", legacy=True)
    diana_owned = AuthorizationFacts(owner_id=DIANA.principal_id, visibility="shared")
    david_owned = AuthorizationFacts(owner_id=DAVID.principal_id, visibility="private")

    assert decide_authorization(authorization, DAVID, legacy_shared).allowed
    assert decide_authorization(authorization, DIANA, legacy_shared).allowed
    assert not decide_authorization(authorization, DAVID, legacy_private).allowed
    assert not decide_authorization(authorization, DIANA, legacy_private).allowed
    assert not decide_authorization(authorization, DAVID, diana_owned).allowed
    assert not decide_authorization(authorization, DIANA, david_owned).allowed
    assert not decide_authorization(
        authorization,
        Actor("outsider@example.test", None),
        legacy_shared,
    ).allowed

    policy = load_route_policy()
    for route_id in (
        "movie.delete",
        "movie.restore",
        "recipe.update",
        "recipe.trash",
        "recipe.restore",
    ):
        mutation_policy = policy.by_id[route_id]
        assert mutation_policy.authorization == authorization
        assert (
            mutation_policy.legacy_handling
            == "preserve_shared_legacy_collaboration"
        )


def test_household_shared_authorization_is_explicit_and_allowlist_bounded():
    authorization = "household_shared_content"
    unrelated_owner = AuthorizationFacts(
        owner_id="someone-else@example.test", visibility="private"
    )
    assert decide_authorization(authorization, DAVID, unrelated_owner).allowed
    assert decide_authorization(authorization, DIANA, unrelated_owner).allowed
    assert not decide_authorization(
        authorization,
        Actor("outsider@example.test", None),
        unrelated_owner,
    ).allowed
    assert not decide_authorization(
        authorization,
        Actor("device-principal", "device", "device"),
        unrelated_owner,
    ).allowed

    policy = load_route_policy()
    routes = {
        "assistant.prompt.create": "not_applicable",
        "assistant.conversation.delete": "preserve_shared_legacy_collaboration",
        "place.restaurant.create": "not_applicable",
        "place.restaurant.update": "preserve_shared_legacy_collaboration",
        "place.review.update": "preserve_shared_legacy_collaboration",
        "place.restaurant.delete": "preserve_shared_legacy_collaboration",
        "place.restaurant.restore": "preserve_shared_legacy_collaboration",
        "place.margarita.update": "preserve_shared_legacy_collaboration",
    }
    for route_id, legacy_handling in routes.items():
        mutation_policy = policy.by_id[route_id]
        assert mutation_policy.authorization == authorization
        assert mutation_policy.legacy_handling == legacy_handling


def test_contribution_membership_and_personal_state_are_actor_scoped():
    conversation = AuthorizationFacts(
        creator_id=DAVID.principal_id,
        contribution_owner_id=DIANA.principal_id,
        member_ids=frozenset({DAVID.principal_id, DIANA.principal_id}),
    )
    assert decide_authorization("conversation_member_contribution", DIANA, conversation).allowed
    assert decide_authorization("message_sender", DIANA, conversation).allowed
    assert not decide_authorization("message_sender", DAVID, conversation).allowed

    create_state = AuthorizationFacts(
        member_ids=frozenset({DIANA.principal_id}),
        allow_personal_state_create=True,
    )
    assert decide_authorization("conversation_member_personal_state", DIANA, create_state).allowed
    assert not decide_authorization("conversation_member_personal_state", DAVID, create_state).allowed
    assert decide_authorization(
        "personal_state", DIANA, AuthorizationFacts(personal_owner_id=DIANA.principal_id)
    ).allowed
    assert not decide_authorization(
        "personal_state", DAVID, AuthorizationFacts(personal_owner_id=DIANA.principal_id)
    ).allowed
    assert not decide_authorization("personal_state", DIANA).allowed


def test_chat_delete_for_all_requires_creator_and_current_unanimous_approval():
    policy = load_route_policy()
    deletion = policy.by_id["chat.conversation.delete"]
    assert dict(deletion.authorization_by_action) == {
        "leave": "conversation_member_contribution",
        "delete_for_all": (
            "conversation_creator_with_unanimous_active_member_approval"
        ),
    }

    facts = AuthorizationFacts(
        creator_id=DAVID.principal_id,
        member_ids=frozenset({DAVID.principal_id, DIANA.principal_id}),
        active_member_ids=frozenset({DAVID.principal_id, DIANA.principal_id}),
        deletion_approval_member_ids=frozenset(
            {DAVID.principal_id, DIANA.principal_id}
        ),
        proposal_binding_valid=True,
    )
    assert decide_authorization(
        "conversation_creator_with_unanimous_active_member_approval", DAVID, facts
    ).allowed
    assert decide_transaction_authorization(
        deletion, DAVID, facts, action="delete_for_all"
    ).allowed
    assert decide_transaction_authorization(
        deletion, DIANA, facts, action="leave"
    ).allowed


@pytest.mark.parametrize(
    ("actor", "changes", "reason"),
    [
        (
            DAVID,
            {"deletion_approval_member_ids": frozenset({DAVID.principal_id})},
            "unanimous_active_member_approval_required",
        ),
        (
            DAVID,
            {
                "deletion_approval_member_ids": frozenset(
                    {
                        DAVID.principal_id,
                        DIANA.principal_id,
                        "former-member@example.test",
                    }
                )
            },
            "unanimous_active_member_approval_required",
        ),
        (
            DAVID,
            {
                "active_member_ids": frozenset({DAVID.principal_id}),
                "deletion_approval_member_ids": frozenset(
                    {DAVID.principal_id, DIANA.principal_id}
                ),
            },
            "unanimous_active_member_approval_required",
        ),
        (
            DAVID,
            {"proposal_binding_valid": False},
            "current_deletion_proposal_binding_required",
        ),
        (DIANA, {}, "conversation_creator_required"),
        (
            DAVID,
            {
                "active_member_ids": frozenset(),
                "deletion_approval_member_ids": frozenset(),
            },
            "nonempty_active_membership_required",
        ),
        (
            Actor(DAVID.principal_id, None),
            {},
            "allowlisted_human_required",
        ),
        (DAVID, {"legacy": True}, "nonlegacy_conversation_required"),
        (
            DAVID,
            {
                "active_member_ids": frozenset({DIANA.principal_id}),
                "deletion_approval_member_ids": frozenset({DIANA.principal_id}),
            },
            "active_conversation_creator_required",
        ),
    ],
    ids=[
        "missing-approval",
        "extra-approval",
        "stale-member-approval",
        "proposal-invalid",
        "noncreator",
        "empty-active-set",
        "untrusted-creator",
        "legacy-conversation",
        "inactive-creator",
    ],
)
def test_chat_delete_for_all_fails_closed_for_adversarial_facts(
    actor, changes, reason
):
    values = {
        "creator_id": DAVID.principal_id,
        "member_ids": frozenset({DAVID.principal_id, DIANA.principal_id}),
        "active_member_ids": frozenset({DAVID.principal_id, DIANA.principal_id}),
        "deletion_approval_member_ids": frozenset(
            {DAVID.principal_id, DIANA.principal_id}
        ),
        "proposal_binding_valid": True,
    }
    values.update(changes)
    decision = decide_authorization(
        "conversation_creator_with_unanimous_active_member_approval",
        actor,
        AuthorizationFacts(**values),
    )
    assert not decision.allowed
    assert decision.reason == reason


def test_unanimous_approval_fact_principals_are_normalized():
    facts = AuthorizationFacts(
        active_member_ids=frozenset(
            {" david@example.test ", "diana@example.test"}
        ),
        deletion_approval_member_ids=frozenset(
            {"david@example.test", " diana@example.test "}
        ),
    )
    assert facts.active_member_ids == facts.deletion_approval_member_ids
    assert facts.active_member_ids == {
        DAVID.principal_id,
        DIANA.principal_id,
    }


def test_device_and_pairing_authorizations_are_distinct_and_fail_closed():
    bound = AuthorizationFacts(device_owner_id=DEVICE.principal_id)
    assert decide_authorization("paired_device_owner", DEVICE, bound).allowed
    assert not decide_authorization("paired_device_owner", DAVID, bound).allowed
    assert not decide_authorization(
        "paired_device_owner", Actor("other-device", "device", "device"), bound
    ).allowed
    assert decide_authorization(
        "allowlisted_human_and_one_time_pairing_code",
        DIANA,
        AuthorizationFacts(pairing_code_valid=True),
    ).allowed
    assert not decide_authorization(
        "allowlisted_human_and_one_time_pairing_code", DIANA
    ).allowed
    assert not decide_authorization(
        "allowlisted_human_and_one_time_pairing_code",
        Actor("unknown@example.test"),
        AuthorizationFacts(pairing_code_valid=True),
    ).allowed
    assert not decide_authorization(
        "allowlisted_human_and_one_time_pairing_code",
        DEVICE,
        AuthorizationFacts(pairing_code_valid=True),
    ).allowed
    assert decide_authorization("device_owner_or_admin", DAVID, bound).allowed
    assert not decide_authorization("device_owner_or_admin", DIANA, bound).allowed


def test_phone_setup_credentials_are_caller_owned_for_each_allowlisted_person():
    policy = load_route_policy()
    for route_id in (
        "device.pairing_token.create",
        "device.ios_credential_file.create",
        "device.ios_shortcut.create",
    ):
        mutation = policy.by_id[route_id]
        assert mutation.authorization == "caller_owned_credential"
        assert mutation.resource == "device_credential"
    assert decide_authorization("caller_owned_credential", DIANA).allowed
    assert decide_authorization("caller_owned_credential", DAVID).allowed
    assert not decide_authorization(
        "caller_owned_credential", Actor("unknown@example.test")
    ).allowed
    assert not decide_authorization("caller_owned_credential", DEVICE).allowed


def test_action_specific_routes_fail_closed_and_transaction_helper_resolves_action():
    policy = load_route_policy()
    note_state = policy.by_id["note.state.update"]
    diana_note = AuthorizationFacts(owner_id=DIANA.principal_id)
    assert decide_transaction_authorization(note_state, DIANA, diana_note, action="trash").allowed
    assert not decide_transaction_authorization(note_state, DAVID, diana_note, action="trash").allowed
    with pytest.raises(PolicyError, match="no authorization"):
        decide_transaction_authorization(note_state, DIANA, diana_note, action="share")
    with pytest.raises(PolicyError, match="does not accept"):
        decide_transaction_authorization(policy.by_id["note.update"], DIANA, diana_note, action="save")


def test_every_known_authorization_has_an_evaluator_branch():
    actor = Actor("actor@example.test", "household")
    facts = AuthorizationFacts(
        owner_id=actor.principal_id,
        creator_id=actor.principal_id,
        contribution_owner_id=actor.principal_id,
        personal_owner_id=actor.principal_id,
        device_owner_id=actor.principal_id,
        visibility="shared",
        member_ids=frozenset({actor.principal_id}),
        legacy=True,
        sources_visible=True,
        allow_personal_state_create=True,
        pairing_code_valid=True,
    )
    for name in KNOWN_AUTHORIZATIONS:
        decision = decide_authorization(name, actor, facts)
        assert isinstance(decision.allowed, bool), name


def test_actor_and_fact_identifiers_are_normalized_before_comparison():
    actor = Actor("  diana@example.test  ", "HOUSEHOLD")
    facts = AuthorizationFacts(
        owner_id="diana@example.test",
        member_ids=frozenset({" diana@example.test "}),
    )
    assert actor.principal_id == "diana@example.test"
    assert decide_authorization("owner_only", actor, facts).allowed
    assert decide_authorization("conversation_member_contribution", actor, facts).allowed


@pytest.mark.parametrize(
    "authorization",
    [
        "caller_owned",
        "household_shared_content",
        "owner_only",
        "owner_or_contributor_or_legacy_shared",
        "personal_state",
        "owner_or_contributor",
        "conversation_member_contribution",
        "message_sender",
        "shared_collaboration",
    ],
)
@pytest.mark.parametrize("role", [None, "unknown", "device"])
def test_untrusted_human_role_cannot_mutate_even_when_ids_match(authorization, role):
    actor = Actor("unknown@example.test", role, "human")
    facts = AuthorizationFacts(
        owner_id=actor.principal_id,
        contribution_owner_id=actor.principal_id,
        personal_owner_id=actor.principal_id,
        visibility="shared",
        member_ids=frozenset({actor.principal_id}),
        allow_personal_state_create=True,
    )
    assert not decide_authorization(authorization, actor, facts).allowed


def test_compiler_rejects_unknown_container_fields_and_undefined_default_access():
    raw = document()
    raw["typo"] = True
    with pytest.raises(PolicyError, match="top-level policy fields"):
        compile_route_policy(raw)

    raw = document()
    raw["enforcement"]["typo"] = True
    with pytest.raises(PolicyError, match="enforcement fields"):
        compile_route_policy(raw)

    raw = document()
    raw["principals"]["david"]["typo"] = True
    with pytest.raises(PolicyError, match="principal 'david' has invalid fields"):
        compile_route_policy(raw)

    raw = document()
    raw["access_classes"]["portal"]["typo"] = True
    with pytest.raises(PolicyError, match="access class 'portal' is invalid"):
        compile_route_policy(raw)

    raw = document()
    raw["access_classes"]["extra"] = {"allowed_principals": ["david"]}
    with pytest.raises(PolicyError, match="access class inventory"):
        compile_route_policy(raw)

    raw = document()
    raw["access_classes"]["portal"]["allowed_principals"].append("unknown")
    with pytest.raises(PolicyError, match="least-privilege inventory"):
        compile_route_policy(raw)

    raw = document()
    raw["default_access"] = "typo"
    with pytest.raises(PolicyError, match="default_access"):
        compile_route_policy(raw)

    raw = document()
    raw["safe_methods"].append("GET")
    with pytest.raises(PolicyError, match="safe_methods"):
        compile_route_policy(raw)

    raw = document()
    raw["access_classes"]["portal"]["allowed_principals"].append("david")
    with pytest.raises(PolicyError, match="repeats a principal"):
        compile_route_policy(raw)


def test_saved_content_retention_is_truthfully_pending_for_thirty_day_gate():
    policy = load_route_policy()
    for mutation_policy in policy.mutations:
        retention = mutation_policy.retention
        if retention is None or retention.mode == "revocation":
            continue
        assert retention.implementation == "domain_pending", mutation_policy.id
        assert retention.minimum_days == 30, mutation_policy.id
        assert retention.backup_gate == "required_before_permanent_delete", mutation_policy.id


def test_compiler_binds_chat_delete_actions_to_reviewed_semantics():
    raw = document()
    mutation(raw, "chat.conversation.delete")["authorization_by_action"] = {
        "delete_for_all": (
            "conversation_creator_with_unanimous_active_member_approval"
        )
    }
    with pytest.raises(PolicyError, match="reviewed saved-content authorization"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "chat.conversation.delete")["authorization_by_action"][
        "delete_for_all"
    ] = "conversation_member_contribution"
    with pytest.raises(PolicyError, match="reviewed saved-content authorization"):
        compile_route_policy(raw)


def test_compiler_enforces_authorization_effect_and_legacy_matrix():
    raw = document()
    mutation(raw, "note.update")["authorization"] = "caller_owned"
    with pytest.raises(PolicyError, match="invalid for effect"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "note.create")["legacy_handling"] = "owner_migration_required"
    with pytest.raises(PolicyError, match="invalid for legacy handling"):
        compile_route_policy(raw)

    raw = document()
    item = mutation(raw, "note.update")
    item.update(
        {
            "effect": "create",
            "authorization": "caller_owned",
            "legacy_handling": "not_applicable",
        }
    )
    with pytest.raises(PolicyError, match="reviewed contracts"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "collection.photo_membership.remove")[
        "authorization"
    ] = "shared_collaboration"
    with pytest.raises(PolicyError, match="reviewed saved-content authorization"):
        compile_route_policy(raw)


def test_revocation_retention_is_exactly_device_revoke_only():
    policy = load_route_policy()
    revocations = {
        item.id
        for item in policy.mutations
        if item.retention is not None and item.retention.mode == "revocation"
    }
    assert revocations == {"device.revoke"}
    assert policy.by_id["device.revoke"].resource == "device_credential"

    raw = document()
    mutation(raw, "movie.delete")["retention"] = {
        "mode": "revocation",
        "implementation": "enforced",
        "minimum_days": 0,
        "confirmation": "required",
        "backup_gate": "not_applicable",
    }
    with pytest.raises(PolicyError, match="revocation retention is reserved"):
        compile_route_policy(raw)

    raw = document()
    mutation(raw, "device.revoke")["retention"] = {
        "mode": "soft_delete",
        "implementation": "domain_pending",
        "minimum_days": 30,
        "confirmation": "required",
        "backup_gate": "required_before_permanent_delete",
    }
    with pytest.raises(PolicyError, match="revocation retention is reserved"):
        compile_route_policy(raw)


def test_pair_routes_are_an_exact_bidirectional_human_contract():
    raw = document()
    for route_id in ("device.backup_pair", "device.ios_pair"):
        mutation(raw, route_id)["csrf"] = "required"
    with pytest.raises(PolicyError, match="bidirectional pairing-code"):
        compile_route_policy(raw)

    raw = document()
    for route_id in ("device.backup_pair", "device.ios_pair"):
        item = mutation(raw, route_id)
        item["csrf"] = "required"
        item["authorization"] = "system_admin"
        item["legacy_handling"] = "not_applicable"
    with pytest.raises(PolicyError, match="bidirectional pairing-code"):
        compile_route_policy(raw)

    raw = document()
    item = mutation(raw, "device.backup_pair")
    item.pop("authorization")
    item["authorization_by_action"] = {
        "pair": "allowlisted_human_and_one_time_pairing_code"
    }
    with pytest.raises(PolicyError, match="reviewed human pairing route"):
        compile_route_policy(raw)

    raw = document()
    raw["mutations"] = [
        item for item in raw["mutations"] if item["id"] != "device.ios_pair"
    ]
    with pytest.raises(PolicyError, match="bidirectional pairing-code"):
        compile_route_policy(raw)


def test_policy_document_mutation_after_compile_does_not_change_compiled_maps():
    raw = document()
    policy = compile_route_policy(raw)
    raw["mutations"][0]["route"] = "/tampered"
    assert policy.by_id["system.shutdown"].route == "/api/system/shutdown"
    with pytest.raises(TypeError):
        policy.by_id["other"] = policy.by_id["system.shutdown"]
