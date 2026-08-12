"""Strict contracts shared by bounded host-side semantic evaluations."""

from __future__ import annotations

from typing import Any

BOUNDED_EVALUATION_SCHEMA_VERSION = 1
BOUNDED_EVALUATION_PURPOSES = frozenset(
    {
        "actor_turn",
        "audience_render",
        "faction_turn",
        "source_interpretation",
        "bounded_ruling",
    }
)
BOUNDED_OUTPUT_CONTRACTS = {
    "actor_turn": "actor-turn-proposal.v1",
    "audience_render": "audience-render-proposal.v1",
    "faction_turn": "faction-turn-proposal.v1",
    "source_interpretation": "source-interpretation-proposal.v1",
    "bounded_ruling": "bounded-ruling-proposal.v1",
}
CLAIM_POSTURES = frozenset(
    {"supported", "inference", "uncertain", "opinion", "nonfactual"}
)
RESOLUTION_KINDS = frozenset(
    {
        "skill_check",
        "opposed_check",
        "sanity_check",
        "luck_check",
        "chase_check",
        "attack",
        "rules_engine",
        "keeper_adjudication",
    }
)
ACTOR_ACTION_KINDS = frozenset(
    {
        "none",
        "gesture",
        "offer",
        "refuse",
        "surrender",
        "move",
        "flee",
        "attack",
        "use_item",
        "exchange_item",
        "scene_transition",
        "observe",
        "interact",
        "follow",
        "wait",
        "other",
    }
)
ACTOR_NARRATIVE_ACTION_KINDS = frozenset(
    {
        "none",
        "gesture",
        "offer",
        "refuse",
        "surrender",
        "move",
        "flee",
        "scene_transition",
        "observe",
        "interact",
        "follow",
        "wait",
    }
)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _strict(value: dict[str, Any], field: str, allowed: set[str]) -> None:
    if unknown := sorted(set(value) - allowed):
        raise ValueError(f"{field} has unknown fields: {unknown}")


def _text(value: Any, field: str, *, required: bool = False, maximum: int = 4_000) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return result


def _strings(value: Any, field: str, *, maximum: int = 300) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = [_text(item, f"{field}[]", required=True, maximum=maximum) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def _claims(value: Any, field: str = "claims") -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item = _object(raw, f"{field}[{index}]")
        _strict(item, f"{field}[{index}]", {"statement", "basis_refs", "posture"})
        posture = _text(item.get("posture"), f"{field}[{index}].posture", required=True)
        if posture not in CLAIM_POSTURES:
            raise ValueError(f"unsupported claim posture: {posture}")
        basis_refs = _strings(item.get("basis_refs"), f"{field}[{index}].basis_refs")
        if posture in {"supported", "inference", "uncertain"} and not basis_refs:
            raise ValueError(f"{field}[{index}] requires a basis_ref")
        result.append(
            {
                "statement": _text(
                    item.get("statement"),
                    f"{field}[{index}].statement",
                    required=True,
                    maximum=2_000,
                ),
                "basis_refs": basis_refs,
                "posture": posture,
            }
        )
    return result


def _requests(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item = _object(raw, f"{field}[{index}]")
        _strict(item, f"{field}[{index}]", {"kind", "reason", "actor_ids"})
        kind = _text(item.get("kind"), f"{field}[{index}].kind", required=True)
        if kind not in RESOLUTION_KINDS:
            raise ValueError(f"unsupported resolution kind: {kind}")
        result.append(
            {
                "kind": kind,
                "reason": _text(
                    item.get("reason"),
                    f"{field}[{index}].reason",
                    required=True,
                    maximum=1_000,
                ),
                "actor_ids": _strings(item.get("actor_ids"), f"{field}[{index}].actor_ids"),
            }
        )
    return result


def _base(value: Any, purpose: str, allowed: set[str]) -> dict[str, Any]:
    data = _object(value, f"{purpose}.proposal")
    _strict(data, f"{purpose}.proposal", allowed | {"schema_version", "bundle_id", "purpose"})
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError(f"{purpose}.proposal.schema_version must be 1")
    if data.get("purpose") != purpose:
        raise ValueError(f"{purpose}.proposal.purpose must be {purpose!r}")
    return data


def normalize_bounded_proposal(purpose: str, value: Any) -> dict[str, Any]:
    """Normalize exactly one supported proposal; no permissive payload envelope."""

    if purpose not in BOUNDED_EVALUATION_PURPOSES:
        raise ValueError(f"unsupported bounded evaluation purpose: {purpose}")
    common = {
        "schema_version": 1,
        "bundle_id": "",
        "purpose": purpose,
    }
    if purpose == "actor_turn":
        data = _base(
            value,
            purpose,
            {
                "actor_id",
                "intent",
                "proposed_action",
                "claims",
                "resolution_requests",
                "decision_summary",
            },
        )
        action = _object(data.get("proposed_action") or {}, "proposed_action")
        _strict(action, "proposed_action", {"kind", "target_ref", "summary"})
        action_kind = _text(action.get("kind"), "proposed_action.kind", maximum=50) or "none"
        if action_kind not in ACTOR_ACTION_KINDS:
            raise ValueError(f"unsupported actor action kind: {action_kind}")
        requests = _requests(data.get("resolution_requests"), "resolution_requests")
        if action_kind not in ACTOR_NARRATIVE_ACTION_KINDS and not requests:
            raise ValueError(
                f"actor action {action_kind!r} requires an explicit resolution request"
            )
        result = {
            **common,
            "actor_id": _text(data.get("actor_id"), "actor_id", required=True, maximum=100),
            "intent": _text(data.get("intent"), "intent", required=True, maximum=1_000),
            "proposed_action": {
                "kind": action_kind,
                "target_ref": _text(
                    action.get("target_ref"),
                    "proposed_action.target_ref",
                    maximum=300,
                ),
                "summary": _text(
                    action.get("summary"),
                    "proposed_action.summary",
                    maximum=1_000,
                ),
            },
            "claims": _claims(data.get("claims")),
            "resolution_requests": requests,
            "decision_summary": _text(
                data.get("decision_summary"), "decision_summary", maximum=500
            ),
        }
    elif purpose == "audience_render":
        data = _base(
            value,
            purpose,
            {"text", "cited_basis_refs", "omitted_sensitive_refs", "decision_summary"},
        )
        result = {
            **common,
            "text": _text(data.get("text"), "text", required=True, maximum=8_000),
            "cited_basis_refs": _strings(data.get("cited_basis_refs"), "cited_basis_refs"),
            "omitted_sensitive_refs": _strings(
                data.get("omitted_sensitive_refs"), "omitted_sensitive_refs"
            ),
            "decision_summary": _text(
                data.get("decision_summary"), "decision_summary", maximum=500
            ),
        }
    elif purpose == "faction_turn":
        data = _base(
            value,
            purpose,
            {
                "faction_id",
                "intent",
                "proposed_actions",
                "claims",
                "resolution_requests",
                "decision_summary",
            },
        )
        actions = data.get("proposed_actions") or []
        if not isinstance(actions, list):
            raise ValueError("faction_turn.proposal.proposed_actions must be a list")
        normalized_actions: list[dict[str, Any]] = []
        for index, raw in enumerate(actions):
            item = _object(raw, f"proposed_actions[{index}]")
            _strict(
                item,
                f"proposed_actions[{index}]",
                {"kind", "target_ref", "summary", "basis_refs"},
            )
            normalized_actions.append(
                {
                    "kind": _text(
                        item.get("kind"),
                        f"proposed_actions[{index}].kind",
                        required=True,
                        maximum=100,
                    ),
                    "target_ref": _text(
                        item.get("target_ref"),
                        f"proposed_actions[{index}].target_ref",
                        maximum=300,
                    ),
                    "summary": _text(
                        item.get("summary"),
                        f"proposed_actions[{index}].summary",
                        required=True,
                        maximum=1_000,
                    ),
                    "basis_refs": _strings(
                        item.get("basis_refs"),
                        f"proposed_actions[{index}].basis_refs",
                    ),
                }
            )
        result = {
            **common,
            "faction_id": _text(data.get("faction_id"), "faction_id", required=True, maximum=200),
            "intent": _text(data.get("intent"), "intent", required=True, maximum=1_000),
            "proposed_actions": normalized_actions,
            "claims": _claims(data.get("claims")),
            "resolution_requests": _requests(
                data.get("resolution_requests"), "resolution_requests"
            ),
            "decision_summary": _text(
                data.get("decision_summary"), "decision_summary", maximum=500
            ),
        }
    elif purpose == "source_interpretation":
        data = _base(
            value,
            purpose,
            {"question", "interpretation", "claims", "ambiguities", "requires_dm_review"},
        )
        if not isinstance(data.get("requires_dm_review"), bool):
            raise ValueError("requires_dm_review must be boolean")
        claims = _claims(data.get("claims"))
        if not claims or not any(item["basis_refs"] for item in claims):
            raise ValueError(
                "source_interpretation requires at least one evidence-bound claim"
            )
        ambiguities = _strings(
            data.get("ambiguities"), "ambiguities", maximum=1_000
        )
        requires_dm_review = data["requires_dm_review"]
        if (
            ambiguities or any(item["posture"] == "uncertain" for item in claims)
        ) and not requires_dm_review:
            raise ValueError(
                "source_interpretation ambiguities or uncertain claims require DM review"
            )
        result = {
            **common,
            "question": _text(data.get("question"), "question", required=True, maximum=2_000),
            "interpretation": _text(
                data.get("interpretation"),
                "interpretation",
                required=True,
                maximum=6_000,
            ),
            "claims": claims,
            "ambiguities": ambiguities,
            "requires_dm_review": requires_dm_review,
        }
    else:
        data = _base(
            value,
            purpose,
            {"ruling", "claims", "engine_requests", "unresolved", "decision_summary"},
        )
        result = {
            **common,
            "ruling": _text(data.get("ruling"), "ruling", required=True),
            "claims": _claims(data.get("claims")),
            "engine_requests": _requests(data.get("engine_requests"), "engine_requests"),
            "unresolved": _strings(data.get("unresolved"), "unresolved", maximum=1_000),
            "decision_summary": _text(
                data.get("decision_summary"), "decision_summary", maximum=500
            ),
        }
    result["bundle_id"] = _text(data.get("bundle_id"), "bundle_id", required=True, maximum=100)
    return result


def validate_bounded_proposal_refs(
    proposal: dict[str, Any],
    *,
    subject_ref: str,
    allowed_basis_refs: set[str],
    allowed_claim_basis_refs: set[str],
    allowed_target_refs: set[str],
) -> None:
    """Enforce evidence, identity, and target boundaries after normalization."""

    purpose = str(proposal["purpose"])
    cited_basis = {
        str(ref)
        for claim in list(proposal.get("claims") or [])
        for ref in list(dict(claim).get("basis_refs") or [])
    }
    cited_basis.update(str(ref) for ref in proposal.get("cited_basis_refs") or [])
    cited_basis.update(
        str(ref)
        for action in proposal.get("proposed_actions") or []
        for ref in dict(action).get("basis_refs") or []
    )
    if unknown := sorted(cited_basis - allowed_basis_refs):
        raise ValueError(f"proposal cites basis refs outside its bundle: {unknown}")
    claim_basis = {
        str(ref)
        for claim in list(proposal.get("claims") or [])
        for ref in list(dict(claim).get("basis_refs") or [])
    }
    if unknown := sorted(claim_basis - allowed_claim_basis_refs):
        raise ValueError(f"proposal cites decision-only refs as claims: {unknown}")
    cited_targets = {
        str(dict(action).get("target_ref") or "")
        for action in proposal.get("proposed_actions") or []
        if dict(action).get("target_ref")
    }
    proposed_action = dict(proposal.get("proposed_action") or {})
    if proposed_action.get("target_ref"):
        cited_targets.add(str(proposed_action["target_ref"]))
    if unknown := sorted(cited_targets - allowed_target_refs):
        raise ValueError(f"proposal cites target refs outside its bundle: {unknown}")
    if purpose == "actor_turn" and subject_ref != f"actor:{proposal['actor_id']}":
        raise ValueError("actor proposal does not match its signed subject")
    if purpose == "faction_turn" and subject_ref != f"faction:{proposal['faction_id']}":
        raise ValueError("faction proposal does not match its signed subject")
    allowed_actor_ids = {
        ref.removeprefix("actor:")
        for ref in allowed_target_refs
        if ref.startswith("actor:")
    }
    for field in ("resolution_requests", "engine_requests"):
        for request in proposal.get(field) or []:
            if unknown := sorted(set(dict(request).get("actor_ids") or []) - allowed_actor_ids):
                raise ValueError(f"proposal cites actor ids outside its bundle: {unknown}")

