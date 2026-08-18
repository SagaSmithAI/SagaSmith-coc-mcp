"""MCP surface for SagaSmith Call of Cthulhu 7e."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import secrets
import time
from copy import deepcopy
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import TextContent
from sagasmith_coc.content_packages import (
    build_module_content_package,
    build_rule_content_package,
    validate_coc_content_package,
)
from sagasmith_coc.engine.character_state import (
    change_inventory,
    change_money,
    settle_source_study,
)
from sagasmith_coc.engine.chase_state import (
    end_chase as close_chase_state,
)
from sagasmith_coc.engine.chase_state import (
    resolve_chase_turn_action,
    start_chase_with_speed_checks,
)
from sagasmith_coc.engine.checks.chase import (
    resolve_chase_action,
    resolve_chase_speed_check,
)
from sagasmith_coc.engine.checks.combat import resolve_melee_attack, resolve_ranged_attack
from sagasmith_coc.engine.checks.sanity import resolve_sanity_check, resolve_sanity_loss
from sagasmith_coc.engine.checks.skill import (
    group_luck_candidates,
    resolve_opposed_check,
    resolve_skill_check,
)
from sagasmith_coc.engine.combat_resolution import (
    combat_attack_profile,
    resolve_combat_attack,
)
from sagasmith_coc.engine.combat_state import (
    advance_turn as advance_combat_turn,
)
from sagasmith_coc.engine.combat_state import (
    combat_distance_feet,
    move_combatant,
)
from sagasmith_coc.engine.combat_state import (
    join_combat as join_combat_state,
)
from sagasmith_coc.engine.combat_state import (
    start_combat as build_combat_state,
)
from sagasmith_coc.engine.development import (
    development_query as query_development,
)
from sagasmith_coc.engine.development import (
    resolve_luck_development,
    settle_development,
)
from sagasmith_coc.engine.dice.rolls import roll_d100, roll_dice_expression
from sagasmith_coc.engine.health import apply_damage, apply_healing
from sagasmith_coc.engine.investigation import (
    resolve_investigation_check,
    spend_luck_on_investigation,
)
from sagasmith_coc.engine.sheet import (
    development_skill_eligible,
    exact_sheet_value,
)
from sagasmith_coc.module_profile import CocModuleProfile
from sagasmith_coc.random_stream import (
    CampaignRandomStream,
    initial_random_stream,
    use_random_stream,
)
from sagasmith_coc.retrieval import COC7E_QUERY_HINTS
from sagasmith_coc.statblocks import coc7e_statblock_readiness, validate_coc7e_statblock
from sagasmith_coc.system import validate_investigator_sheet
from sagasmith_core import (
    AccessService,
    ActorKnowledgeService,
    ActorLifecycleService,
    BranchService,
    CampaignService,
    CharacterService,
    CharacterStateUpdate,
    ContinuityCommitService,
    ContinuityService,
    EventService,
    IdempotencyService,
    IdempotencyWrite,
    ImportJobService,
    InitialActorGrant,
    MemoryService,
    ModuleService,
    RevisionService,
    RulePackService,
    RuleService,
    SnapshotService,
    StateMutationService,
    apply_document_page_revisions,
    default_local_principal,
    extract_pdf_page_text,
    normalize_document,
    normalized_document_page_text,
    render_pdf_page,
    validate_subject_context_fact,
)
from sagasmith_core.access import LOCAL_SYSTEM_PRINCIPAL_ID
from sagasmith_core.integrity import canonical_json
from sagasmith_core.modules import MarkdownModuleParser
from sagasmith_core.visibility import PLAYER_MODULE_VISIBILITY_SCOPES

from .bounded_evaluations import (
    BOUNDED_EVALUATION_PURPOSES,
    BOUNDED_OUTPUT_CONTRACTS,
    normalize_bounded_proposal,
    validate_bounded_proposal_refs,
)
from .config import McpConfig
from .exposure import Exposure, ExposureError, ExposureRegistry
from .npc_conversations import (
    NPC_CONVERSATION_CONTRACT,
    NPC_CONVERSATION_SCHEMA_VERSION,
    ConversationStore,
    normalize_audience_facts,
)
from .receipt_signing import sign_receipt, verify_receipt_signature
from .skills import SkillCatalog
from .storage import SagaSmithStorage
from .tool_profiles import (
    CORE_TOOLS,
    HOST_PRIVATE_TOOLS,
    PROFILE_COMBAT,
    PROFILE_LOBBY,
    PROFILE_PLAY,
    PROFILES,
    policy_for_tool,
    tool_catalog,
    tools_for_phase,
    validate_profile_coverage,
)


def _preload_optional_pdf_runtime() -> None:
    """Load PDF native dependencies before the stdio host starts worker threads."""

    try:
        importlib.import_module("pypdfium2")
    except ModuleNotFoundError as exc:
        if exc.name != "pypdfium2":
            raise


class SessionExposureFastMCP(FastMCP):
    """Filter native tools/list by one server-owned MCP-session exposure."""

    def __init__(
        self,
        *args: Any,
        exposure_registry: ExposureRegistry,
        phase_lookup: Any,
        allowed_tools_lookup: Any,
        scope_validator: Any,
        context_binding_factory: Any,
        authorization_fingerprint_lookup: Any,
        bound_principal_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.exposure_registry = exposure_registry
        self._phase_lookup = phase_lookup
        self._allowed_tools_lookup = allowed_tools_lookup
        self._scope_validator = scope_validator
        self._context_binding_factory = context_binding_factory
        self._authorization_fingerprint_lookup = authorization_fingerprint_lookup
        self._bound_principal_id = bound_principal_id.strip() if bound_principal_id else None
        self._sessions: WeakValueDictionary[str, Any] = WeakValueDictionary()
        self._exposure_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        super().__init__(*args, **kwargs)
        original_initialization_options = self._mcp_server.create_initialization_options

        def initialization_options(
            notification_options: NotificationOptions | None = None,
            experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        ):
            return original_initialization_options(
                notification_options
                or NotificationOptions(
                    tools_changed=True,
                    prompts_changed=False,
                    resources_changed=False,
                ),
                experimental_capabilities,
            )

        self._mcp_server.create_initialization_options = initialization_options  # type: ignore[method-assign]

    def _request_session(self) -> tuple[str, Any] | None:
        try:
            session = self.get_context().session
        except (LookupError, ValueError):
            return None
        key = getattr(session, "_sagasmith_exposure_session_key", None)
        if key is None:
            key = f"mcp:{uuid4().hex}"
            setattr(session, "_sagasmith_exposure_session_key", key)
        self._sessions[key] = session
        return key, session

    def _exposure_lock(self, exposure_id: str) -> asyncio.Lock:
        return self._exposure_locks.setdefault(exposure_id, asyncio.Lock())

    def _bind_principal(
        self, exposure: Exposure, tool_id: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(arguments)
        tool = self._tool_manager.get_tool(tool_id)
        properties = dict((tool.parameters if tool else {}).get("properties") or {})
        if "principal_id" not in properties:
            return result
        supplied = result.get("principal_id")
        if supplied is not None and supplied != exposure.principal_id:
            raise ExposureError("tool principal does not match the exposure principal")
        result["principal_id"] = exposure.principal_id
        return result

    def _bind_configured_principal(self, tool_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = dict(arguments)
        if self._bound_principal_id is None:
            return result
        tool = self._tool_manager.get_tool(tool_id)
        properties = dict((tool.parameters if tool else {}).get("properties") or {})
        if "principal_id" in properties:
            result["principal_id"] = self._bound_principal_id
        return result

    async def _refresh(self, session_key: str, campaign_id: str | None = None) -> bool:
        changed_session_keys: set[str] = set()
        exposure_items = (
            [(session_key, self.exposure_registry.active(session_key))]
            if campaign_id is None
            else list(self.exposure_registry.active_items(campaign_id))
        )
        for key, exposure in exposure_items:
            if exposure is None or exposure.campaign_id is None:
                continue
            phase = self._phase_lookup(exposure.campaign_id)
            if self.exposure_registry.refresh_phase(
                exposure,
                phase,
                allowed_tools=self._allowed_tools_lookup(exposure, phase),
            ):
                changed_session_keys.add(key)
            fingerprint = self._authorization_fingerprint_lookup(
                exposure.campaign_id, exposure.principal_id
            )
            if self.exposure_registry.refresh_authorization(exposure, fingerprint):
                changed_session_keys.add(key)
        for key in changed_session_keys:
            session = self._sessions.get(key)
            if session is not None:
                await session.send_tool_list_changed()
        return bool(changed_session_keys)

    async def list_tools(self):  # type: ignore[override]
        public_tools = [
            tool for tool in await super().list_tools() if tool.name not in HOST_PRIVATE_TOOLS
        ]
        request = self._request_session()
        if request is None:
            return public_tools
        session_key, _ = request
        await self._refresh(session_key)
        visible = self.exposure_registry.visible_tools(self.exposure_registry.active(session_key))
        return [tool for tool in public_tools if tool.name in visible]

    async def call_tool(self, name: str, arguments: dict[str, Any]):  # type: ignore[override]
        request = self._request_session()
        if request is None:
            return await super().call_tool(name, arguments)
        session_key, _ = request
        await self._refresh(session_key)
        arguments = self._bind_configured_principal(name, arguments)
        if name in HOST_PRIVATE_TOOLS:
            return await super().call_tool(name, arguments)
        exposure = self.exposure_registry.active(session_key)
        if name not in CORE_TOOLS and exposure is None:
            raise ExposureError("Open and load a session exposure before calling domain tools.")
        bound = arguments
        if exposure is not None and name != "exposure":
            self.exposure_registry.require_tool(exposure, name)
            bound = self._bind_principal(exposure, name, bound)
            self._scope_validator(exposure, name, bound)
        result = await super().call_tool(name, bound)
        if name == "exposure" and bound.get("action") in {"open", "set"}:
            session = self._sessions.get(session_key)
            if session is not None:
                await session.send_tool_list_changed()
        campaign_id = str(bound.get("campaign_id") or "") or None
        campaign_id = campaign_id or (exposure.campaign_id if exposure else None)
        if campaign_id:
            await self._refresh(session_key, campaign_id)
            principal_id = str(bound.get("principal_id") or "").strip()
            principal_id = (
                principal_id
                or (exposure.principal_id if exposure is not None else "")
                or self._bound_principal_id
                or LOCAL_SYSTEM_PRINCIPAL_ID
            )
            binding = self._context_binding_factory(campaign_id, principal_id, bound)
            result = self._attach_host_context_binding(result, binding)
        return result

    @staticmethod
    def _attach_host_context_binding(result: Any, binding: dict[str, str] | None) -> Any:
        if binding is None or not (isinstance(result, tuple) and len(result) == 2):
            return result
        content, structured = result

        def attach(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            updated = deepcopy(value)
            payload = updated.get("result")
            if isinstance(payload, dict):
                payload["host_context_binding"] = deepcopy(binding)
            else:
                updated["host_context_binding"] = deepcopy(binding)
            return updated

        updated_content = []
        for item in content:
            if not isinstance(item, TextContent):
                updated_content.append(item)
                continue
            try:
                decoded = json.loads(item.text)
            except json.JSONDecodeError:
                updated_content.append(item)
                continue
            updated_content.append(
                item.model_copy(
                    update={
                        "text": json.dumps(
                            attach(decoded), ensure_ascii=False, separators=(",", ":")
                        )
                    }
                )
            )
        # FastMCP validates structuredContent against each tool's declared
        # return annotation after this override. Keep that value unchanged and
        # carry the Host-only envelope in the JSON text representation.
        return updated_content, structured


def create_server(config: McpConfig | None = None) -> FastMCP:
    _preload_optional_pdf_runtime()
    config = config or McpConfig.from_environment()
    storage = SagaSmithStorage(config)
    storage.migrate()
    campaigns = CampaignService(storage.database)
    branches = BranchService(storage.database)
    characters = CharacterService(storage.database)
    actor_lifecycle = ActorLifecycleService(storage.database)
    access = AccessService(storage.database)
    memories = MemoryService(storage.database)
    knowledge = ActorKnowledgeService(storage.database)
    events = EventService(storage.database)
    continuity = ContinuityService(storage.database)
    continuity_commits = ContinuityCommitService(storage.database)
    modules = ModuleService(storage.database)
    rules = RuleService(storage.database)
    rule_packs = RulePackService(storage.database)
    import_jobs = ImportJobService(storage.database)
    snapshots = SnapshotService(storage.database)
    revisions = RevisionService(storage.database)
    idempotency = IdempotencyService(storage.database)
    npc_conversations = ConversationStore(config.npc_conversations_dir)
    default_local_principal(storage.database)
    parser = MarkdownModuleParser(profile=CocModuleProfile())
    skills = SkillCatalog(
        coc_root=config.coc_skills_dir,
        modulegen_root=config.modulegen_skills_dir,
    )
    exposures = ExposureRegistry()
    bounded_receipt_secret = uuid4().bytes + uuid4().bytes
    bounded_receipt_ttl_ns = 10 * 60 * 1_000_000_000

    def principal_fingerprint(principal_id: str) -> str:
        return hashlib.sha256(principal_id.encode("utf-8")).hexdigest()

    def authoritative_host_context_binding(
        campaign_id: str,
        principal_id: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        membership = access.membership(campaign_id, principal_id)
        branch = branches.current(campaign_id)
        if membership is None or branch is None:
            return None
        values = dict(arguments or {})
        data = values.get("data")
        if isinstance(data, dict):
            values = {**data, **values}
        requested_audience = str(values.get("audience") or "").strip().casefold()
        if membership.role not in {"owner", "dm"}:
            audience = "player"
        elif requested_audience in {"dm", "player"}:
            audience = requested_audience
        else:
            audience = "dm"
        value = {
            "domain": "sagasmith-coc",
            "campaign_id": campaign_id,
            "principal_fingerprint": principal_fingerprint(principal_id),
            "authorization_fingerprint": access.authorization_fingerprint(
                campaign_id, principal_id
            ),
            "role": membership.role,
            "audience": audience,
            "branch_id": branch.id,
            "memory_policy": "domain_authoritative",
        }
        epoch_fields = {
            key: value[key]
            for key in (
                "domain",
                "campaign_id",
                "principal_fingerprint",
                "authorization_fingerprint",
                "role",
                "audience",
                "branch_id",
            )
        }
        return {
            **value,
            "context_epoch": hashlib.sha256(
                canonical_json(epoch_fields).encode("utf-8")
            ).hexdigest(),
        }

    def bounded_context_digest(value: dict[str, Any]) -> str:
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

    def authoritative_phase(campaign_id: str) -> str:
        from .tool_profiles import campaign_phase

        return campaign_phase(campaigns.get(campaign_id).state)

    def require_dm(campaign_id: str, principal_id: str) -> None:
        access.require_campaign(campaign_id, principal_id, roles={"owner", "dm"})

    def is_dm(campaign_id: str, principal_id: str) -> bool:
        return access.require_campaign(campaign_id, principal_id).role in {"owner", "dm"}

    def validate_scope(exposure: Exposure, tool_id: str, arguments: dict[str, Any]) -> None:
        policy = policy_for_tool(tool_id)
        if policy is not None:
            if exposure.phase not in policy.phases:
                raise ExposureError(f"Tool {tool_id!r} is unavailable during {exposure.phase!r}.")
            if policy.requires_campaign and exposure.campaign_id is None:
                raise ExposureError(f"Tool {tool_id!r} requires a campaign-bound exposure.")
            roles = policy.roles(exposure.phase)
            if roles:
                if exposure.campaign_id is None:
                    raise ExposureError(f"Tool {tool_id!r} requires a campaign role.")
                access.require_campaign(
                    exposure.campaign_id,
                    exposure.principal_id,
                    roles=set(roles),
                )
        if exposure.campaign_id is None:
            return
        campaign_id = arguments.get("campaign_id")
        if campaign_id and str(campaign_id) != exposure.campaign_id:
            raise ExposureError("tool target does not match the exposure campaign")
        for key in ("character_id", "actor_id"):
            value = arguments.get(key)
            if not value:
                continue
            character = characters.get(str(value))
            if character.campaign_id != exposure.campaign_id:
                raise ExposureError("actor target does not match the exposure campaign")

    def allowed_tools_for_exposure(exposure: Exposure, phase: str) -> set[str]:
        allowed = set(tools_for_phase(phase))
        membership = (
            access.membership(exposure.campaign_id, exposure.principal_id)
            if exposure.campaign_id is not None
            else None
        )
        for tool_id in tuple(allowed):
            policy = policy_for_tool(tool_id)
            if policy is None:
                continue
            if policy.local_only and exposure.principal_id != LOCAL_SYSTEM_PRINCIPAL_ID:
                allowed.discard(tool_id)
                continue
            if policy.requires_campaign and membership is None:
                allowed.discard(tool_id)
                continue
            roles = policy.roles(phase)
            if roles and (membership is None or membership.role not in roles):
                allowed.discard(tool_id)
        return allowed

    mcp = SessionExposureFastMCP(
        "SagaSmith CoC",
        instructions=(
            "Call of Cthulhu 7e campaign runtime. Engine tools return deterministic "
            "resolution data; character state changes remain explicit writes."
        ),
        exposure_registry=exposures,
        phase_lookup=authoritative_phase,
        allowed_tools_lookup=allowed_tools_for_exposure,
        scope_validator=validate_scope,
        context_binding_factory=authoritative_host_context_binding,
        authorization_fingerprint_lookup=access.authorization_fingerprint,
        bound_principal_id=config.bound_principal_id,
        host=config.http_host,
        port=config.http_port,
        streamable_http_path=config.http_path,
    )

    def visible_character(character: Any, principal_id: str) -> dict[str, Any]:
        value = asdict(character)
        if character.campaign_id is None or is_dm(character.campaign_id, principal_id):
            return value
        try:
            access.require_actor(character.campaign_id, character.id, principal_id, private=True)
        except PermissionError:
            return {
                key: value[key]
                for key in (
                    "id",
                    "system_id",
                    "campaign_id",
                    "character_type",
                    "name",
                    "summary",
                    "revision",
                )
            }
        return value

    def actor_access(campaign_id: str, actor_id: str, principal_id: str, *, control=False):
        return access.require_actor(
            campaign_id, actor_id, principal_id, control=control, private=not control
        )

    def can_control_actor(campaign_id: str, actor_id: str, principal_id: str) -> bool:
        try:
            actor_access(campaign_id, actor_id, principal_id, control=True)
        except (LookupError, PermissionError):
            return False
        return True

    def combat_participant(
        campaign_id: str,
        raw: dict[str, Any],
        *,
        positioning_mode: str,
    ) -> dict[str, Any]:
        actor_id = str(raw.get("actor_id") or "").strip()
        side = str(raw.get("side") or "").strip()
        if not actor_id or not side:
            raise ValueError("each combat participant requires actor_id and side")
        actor = characters.get(actor_id)
        if actor.campaign_id != campaign_id:
            raise ValueError("every combat participant must belong to the target campaign")
        sheet = validate_investigator_sheet(dict(actor.sheet))
        conditions = dict(sheet.get("conditions") or {})
        if conditions.get("dead"):
            raise ValueError(f"dead actor cannot enter combat: {actor_id}")
        value = {
            "actor_id": actor_id,
            "name": actor.name,
            "side": side,
            "dex": int(sheet["characteristics"]["dex"]),
            "ready_firearm": bool(raw.get("ready_firearm", False)),
            "attacks_per_round": int(sheet.get("attacks_per_round", 1)),
        }
        if positioning_mode == "grid":
            value["position"] = raw.get("position")
        elif "position" in raw:
            raise ValueError("agent positioning mode must not accept coordinates")
        return value

    def active_combat(campaign_id: str) -> tuple[Any, dict[str, Any]]:
        campaign = campaigns.get(campaign_id)
        combat = dict(campaign.state.get("combat") or {})
        if not combat.get("active"):
            raise ValueError("campaign has no active combat")
        return campaign, combat

    def active_chase(campaign_id: str) -> tuple[Any, dict[str, Any]]:
        campaign = campaigns.get(campaign_id)
        chase = dict(campaign.state.get("chase") or {})
        if not chase.get("active"):
            raise ValueError("campaign has no active chase")
        return campaign, chase

    def player_pending_choice(
        campaign_id: str,
        principal_id: str,
        pending: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Return only the choice fields needed by an authorized responder."""

        if not pending:
            return None
        responder_ids = {
            str(pending.get(key) or "")
            for key in ("actor_id", "target_actor_id", "responder_actor_id")
            if str(pending.get(key) or "")
        }
        if not any(
            can_control_actor(campaign_id, actor_id, principal_id)
            for actor_id in responder_ids
        ):
            return None
        allowed = {
            "id",
            "kind",
            "actor_id",
            "attacker_id",
            "target_actor_id",
            "responder_actor_id",
            "attacker_name",
            "target_name",
            "response_options",
            "range_band",
            "distance_feet",
        }
        return {key: deepcopy(value) for key, value in pending.items() if key in allowed}

    def chase_audience_state(
        campaign_id: str,
        principal_id: str,
        chase: dict[str, Any],
    ) -> dict[str, Any]:
        if is_dm(campaign_id, principal_id):
            return deepcopy(chase)
        participants = {
            str(actor_id): {
                key: deepcopy(value)
                for key, value in dict(item).items()
                if key
                in {
                    "actor_id",
                    "name",
                    "role",
                    "participant_kind",
                    "position",
                    "action_points",
                    "action_points_remaining",
                    "status",
                }
            }
            for actor_id, item in dict(chase.get("participants") or {}).items()
        }
        route = [
            {
                key: deepcopy(value)
                for key, value in dict(item).items()
                if key in {"id", "title", "index", "kind"}
            }
            for item in list(chase.get("route") or [])
            if isinstance(item, dict)
        ]
        value = {
            key: deepcopy(chase[key])
            for key in (
                "schema",
                "active",
                "round",
                "turn_index",
                "current_actor_id",
                "order",
                "outcome",
            )
            if key in chase
        }
        value["participants"] = participants
        value["route"] = route
        pending = player_pending_choice(
            campaign_id,
            principal_id,
            dict(chase.get("pending_choice") or {}),
        )
        if pending is not None:
            value["pending_choice"] = pending
        value["audience_redacted"] = True
        return value

    def combat_audience_state(
        campaign_id: str,
        principal_id: str,
        combat: dict[str, Any],
    ) -> dict[str, Any]:
        if is_dm(campaign_id, principal_id):
            return deepcopy(combat)
        participants = {
            str(actor_id): {
                key: deepcopy(value)
                for key, value in dict(item).items()
                if key
                in {
                    "actor_id",
                    "name",
                    "side",
                    "position",
                    "available_from_round",
                }
            }
            for actor_id, item in dict(combat.get("participants") or {}).items()
        }
        value = {
            key: deepcopy(combat[key])
            for key in (
                "schema",
                "active",
                "positioning_mode",
                "grid_metric",
                "grid_unit_feet",
                "round",
                "turn_index",
                "current_actor_id",
                "order",
                "outcome",
            )
            if key in combat
        }
        value["participants"] = participants
        pending = player_pending_choice(
            campaign_id,
            principal_id,
            dict(combat.get("pending_choice") or {}),
        )
        if pending is not None:
            value["pending_choice"] = pending
        value["audience_redacted"] = True
        return value

    def chase_view(campaign_id: str, principal_id: str) -> dict[str, Any]:
        campaign, chase = active_chase(campaign_id)
        current_actor_id = str(chase.get("current_actor_id") or "")
        actions: list[str] = []
        if current_actor_id and can_control_actor(campaign_id, current_actor_id, principal_id):
            actions.extend(["move", "check", "speed_check", "end_turn"])
        if is_dm(campaign_id, principal_id):
            actions.append("end")
        return {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision,
            "phase": PROFILE_PLAY,
            "chase": chase_audience_state(campaign_id, principal_id, chase),
            "available_actions": actions,
        }

    def combat_view(campaign_id: str, principal_id: str) -> dict[str, Any]:
        campaign, combat = active_combat(campaign_id)
        current_actor_id = str(combat.get("current_actor_id") or "")
        pending = dict(combat.get("pending_choice") or {})
        actions: list[str] = []
        if pending:
            target_id = str(pending.get("target_actor_id") or "")
            if target_id and can_control_actor(campaign_id, target_id, principal_id):
                actions.append("react")
        elif current_actor_id and can_control_actor(campaign_id, current_actor_id, principal_id):
            actions.extend(["move", "attack", "end_turn"])
        if is_dm(campaign_id, principal_id):
            actions.extend(["join", "end"])
        return {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision,
            "phase": PROFILE_COMBAT,
            "combat": combat_audience_state(campaign_id, principal_id, combat),
            "available_actions": list(dict.fromkeys(actions)),
        }

    def campaign_audience_view(campaign_id: str, principal_id: str) -> dict[str, Any]:
        """Project persisted campaign state without exposing Keeper-only ledgers."""

        access.require_campaign(campaign_id, principal_id)
        campaign = campaigns.get(campaign_id)
        value = asdict(campaign)
        value["effective_game_phase"] = authoritative_phase(campaign_id)
        if is_dm(campaign_id, principal_id):
            return value
        state = dict(value.get("state") or {})
        safe_state: dict[str, Any] = {
            "game_phase": str(state.get("game_phase") or PROFILE_LOBBY),
        }
        combat = dict(state.get("combat") or {})
        if combat.get("active"):
            safe_state["combat"] = combat_audience_state(
                campaign_id,
                principal_id,
                combat,
            )
        elif combat:
            safe_state["combat"] = {"active": False}
        chase = dict(state.get("chase") or {})
        if chase.get("active"):
            safe_state["chase"] = chase_audience_state(
                campaign_id,
                principal_id,
                chase,
            )
        elif chase:
            safe_state["chase"] = {"active": False}
        value["state"] = safe_state
        value["state_redacted"] = True
        return value

    def resolution_rolls(resolution_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[int] = set()

        def visit(value: Any) -> None:
            if len(normalized) >= 32:
                return
            if isinstance(value, dict):
                total = value.get("total")
                dice: list[int] | None = None
                expression = str(value.get("expression") or "")
                kept: list[int] | None = None
                if isinstance(value.get("all_tens"), list) and isinstance(
                    value.get("unit_die"), int
                ):
                    dice = [*list(value["all_tens"]), int(value["unit_die"])]
                    kept = [int(total)] if isinstance(total, int) else None
                    expression = expression or "d100"
                elif isinstance(value.get("rolls"), list):
                    dice = list(value["rolls"])
                    expression = expression or "dice"
                if (
                    dice
                    and all(isinstance(item, int) and not isinstance(item, bool) for item in dice)
                    and isinstance(total, int)
                    and not isinstance(total, bool)
                ):
                    identity = id(value)
                    if identity not in seen:
                        seen.add(identity)
                        normalized.append(
                            {
                                "roll_id": f"{resolution_id}:roll:{len(normalized) + 1}",
                                "expression": expression,
                                "dice": dice,
                                "kept": kept or list(dice),
                                "modifier": 0,
                                "total": int(total),
                            }
                        )
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(result)
        return normalized

    def resolution_outcome(result: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "success",
            "success_level",
            "critical",
            "fumble",
            "difficulty",
            "outcome",
            "damage",
            "san_loss",
            "major_wound",
            "unconscious",
            "dead",
        }
        return {
            key: deepcopy(value)
            for key, value in result.items()
            if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
        }

    def resolution_presentation_event(
        campaign_id: str,
        resolution_id: str,
    ) -> dict[str, Any]:
        state = dict(campaigns.get(campaign_id).state or {})
        for item in list(state.get("resolution_presentation_log") or []):
            if isinstance(item, dict) and str(item.get("id") or "") == resolution_id:
                return deepcopy(item)
        ledger = investigation_ledger(state)
        candidates = [
            *list(dict(ledger.get("pending") or {}).values()),
            *list(ledger.get("history") or []),
        ]
        matching = [
            item
            for item in candidates
            if isinstance(item, dict) and str(item.get("id") or "") == resolution_id
        ]
        if len(matching) == 1:
            item = deepcopy(matching[0])
            status_value = str(item.get("status") or "pending")
            available_actions = (
                investigation_actions(campaigns.get(campaign_id), item)
                if status_value == "pending"
                else []
            )
            return {
                "id": resolution_id,
                "thread_id": str(item.get("thread_id") or resolution_id),
                "event_sequence": int(item.get("event_sequence") or 1),
                "operation": str(item.get("operation") or "investigation_check"),
                "status": status_value,
                "audience": dict(
                    item.get("audience")
                    or {
                        "scope": "actors",
                        "actor_refs": [str(item.get("actor_id") or "")],
                        "disclosure": "private",
                    }
                ),
                "branch_id": item.get("branch_id"),
                "campaign_revision": item.get("campaign_revision"),
                "result": {
                    "roll": deepcopy(item.get("roll")),
                    **deepcopy(dict(item.get("outcome") or {})),
                },
                "pending_choice": (
                    {
                        "id": resolution_id,
                        "kind": "investigation_check",
                        "available_actions": available_actions,
                    }
                    if status_value == "pending"
                    else None
                ),
                "random_stream_receipt": deepcopy(item.get("random_stream_receipt")),
            }
        combat = dict(state.get("combat") or {})
        pending = dict(combat.get("pending_choice") or {})
        if str(pending.get("id") or "") == resolution_id:
            actor_refs = [
                str(pending.get("attacker_id") or ""),
                str(pending.get("target_actor_id") or ""),
            ]
            actor_refs = [value for value in actor_refs if value]
            return {
                "id": resolution_id,
                "thread_id": resolution_id,
                "event_sequence": 1,
                "operation": "combat_attack",
                "status": "pending",
                "audience": {
                    "scope": "actors",
                    "actor_refs": actor_refs,
                    "disclosure": "private",
                },
                "branch_id": pending.get("branch_id"),
                "campaign_revision": pending.get("campaign_revision"),
                "result": {},
                "pending_choice": {
                    "id": resolution_id,
                    "kind": "combat_attack_response",
                    "available_actions": list(pending.get("response_options") or []),
                },
                "random_stream_receipt": None,
            }
        combat_events = [
            item
            for item in list(combat.get("events") or [])
            if isinstance(item, dict) and str(item.get("pending_id") or "") == resolution_id
        ]
        if combat_events:
            item = deepcopy(combat_events[-1])
            event_type = str(item.get("type") or "")
            actor_refs = [str(value) for value in item.get("actor_refs") or [] if str(value)]
            if event_type == "attack_opened":
                status_value = "pending"
                event_sequence = 1
            elif event_type == "attack_aborted":
                status_value = "aborted"
                event_sequence = 2
            else:
                status_value = "settled"
                event_sequence = 2
            mechanics = dict(item.get("resolution") or {})
            damage = mechanics.get("damage") or mechanics.get("counterattack")
            safe_result = {
                "attack_roll": deepcopy(item.get("attack_roll")),
                "defense_roll": deepcopy(item.get("defense_roll")),
                "con_roll": deepcopy(item.get("con_roll")),
                "success": bool(
                    mechanics.get("success")
                    or mechanics.get("attacker_wins")
                    or damage is not None
                ),
                "outcome": str(
                    mechanics.get("outcome")
                    or mechanics.get("winner")
                    or event_type.removeprefix("attack_")
                ),
                "damage": (
                    int(dict(damage).get("total") or 0)
                    if isinstance(damage, dict)
                    else None
                ),
            }
            return {
                "id": resolution_id,
                "thread_id": resolution_id,
                "event_sequence": event_sequence,
                "operation": "combat_attack",
                "status": status_value,
                "audience": {
                    "scope": "actors",
                    "actor_refs": actor_refs,
                    "disclosure": "private",
                },
                "branch_id": item.get("branch_id"),
                "campaign_revision": item.get("campaign_revision"),
                "result": safe_result,
                "pending_choice": None,
                "random_stream_receipt": deepcopy(item.get("random_stream_receipt")),
            }
        raise LookupError("resolution presentation not found")

    def resolution_presentation_view(
        campaign_id: str,
        resolution_id: str,
        principal_id: str,
    ) -> dict[str, Any]:
        membership = access.require_campaign(campaign_id, principal_id)
        campaign = campaigns.get(campaign_id)
        event = resolution_presentation_event(campaign_id, resolution_id)
        audience = dict(event.get("audience") or {})
        actor_refs = [str(item) for item in audience.get("actor_refs") or [] if str(item)]
        scope = str(audience.get("scope") or ("actors" if actor_refs else "dm"))
        if scope == "dm" and membership.role not in {"owner", "dm"}:
            raise LookupError("resolution presentation not found")
        if scope == "principal" and (
            membership.role not in {"owner", "dm"}
            and str(event.get("principal_id") or "") != principal_id
        ):
            raise LookupError("resolution presentation not found")
        if scope == "actors" and membership.role not in {"owner", "dm"}:
            authorized = False
            for actor_ref in actor_refs:
                try:
                    actor_access(campaign_id, actor_ref, principal_id)
                except (LookupError, PermissionError):
                    continue
                authorized = True
                break
            if not authorized:
                raise LookupError("resolution presentation not found")
        result = deepcopy(dict(event.get("result") or {}))
        pending_choice = deepcopy(event.get("pending_choice"))
        if isinstance(pending_choice, dict) and scope == "actors":
            pending_choice["available_actions"] = list(
                pending_choice.get("available_actions") or []
            )
        return {
            "schema": "sagasmith.resolution-presentation/v1",
            "resolution_id": resolution_id,
            "thread_id": str(event.get("thread_id") or resolution_id),
            "event_sequence": int(event.get("event_sequence") or 1),
            "system_id": "coc7e",
            "campaign_id": campaign_id,
            "branch_id": event.get("branch_id"),
            "operation": str(event.get("operation") or "resolution"),
            "status": str(event.get("status") or "settled"),
            "audience": {
                "scope": scope,
                "actor_refs": actor_refs if scope == "actors" else [],
                "disclosure": str(
                    audience.get("disclosure") or ("private" if scope == "actors" else "hidden")
                ),
            },
            "actor_refs": actor_refs,
            "rolls": resolution_rolls(resolution_id, result),
            "outcome": resolution_outcome(result),
            "pending_choice": pending_choice,
            "campaign_revision": int(event.get("campaign_revision") or campaign.revision),
            "random_stream_receipt": {
                key: deepcopy(value)
                for key, value in dict(event.get("random_stream_receipt") or {}).items()
                if key
                in {
                    "operation",
                    "position_before",
                    "position_after",
                    "draw_count",
                    "receipt_digest",
                }
            },
        }

    def require_lobby(campaign_id: str, operation: str) -> None:
        phase = authoritative_phase(campaign_id)
        if phase != PROFILE_LOBBY:
            raise ValueError(
                f"{operation} is available only during lobby; current phase is {phase}"
            )

    def require_write_contract(
        expected_revision: int | None, idempotency_key: str | None
    ) -> tuple[int, str]:
        if expected_revision is None:
            raise ValueError("expected_revision is required for this mutation")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required for this mutation")
        return int(expected_revision), key

    def import_job_handle(job: Any) -> dict[str, Any]:
        result = dict(job.result or {})
        finalized = dict(result.get("finalized_package") or {})
        return {
            "job_id": job.id,
            "state": job.state,
            "resumable": not bool(finalized),
            "artifact": job.artifact,
            "artifact_checksum": job.artifact_checksum,
            "source_key": str(dict(job.payload or {}).get("source_key") or job.artifact),
            "title": str(dict(job.payload or {}).get("title") or ""),
            "module_id": job.module_id or "",
            "revision": job.revision,
            "pack_decision_fields": sorted(dict(result.get("pack_draft") or {})),
            "finalized_artifact": str(finalized.get("artifact") or ""),
            "finalized_pack_id": str(dict(finalized.get("summary") or {}).get("id") or ""),
        }

    def import_job_view(job: Any) -> dict[str, Any]:
        value = asdict(job)
        finalized = dict(dict(value.get("result") or {}).get("finalized_package") or {})
        if finalized:
            finalized.pop("package", None)
            value["result"] = {
                **dict(value.get("result") or {}),
                "finalized_package": finalized,
            }
        return value

    def require_module_job(campaign_id: str, job_id: str) -> Any:
        job = import_jobs.get(job_id)
        if job.campaign_id != campaign_id or job.kind != "module":
            raise LookupError(job_id)
        return job

    def require_rule_job(campaign_id: str, job_id: str) -> Any:
        job = import_jobs.get(job_id)
        if job.campaign_id != campaign_id or job.kind != "rulebook":
            raise LookupError(job_id)
        return job

    def import_page_revisions(job: Any) -> list[dict[str, Any]]:
        revisions = dict(job.inspection or {}).get("page_revisions", [])
        if not isinstance(revisions, list):
            raise RuntimeError("module inspection page_revisions must be an array")
        return [deepcopy(dict(item)) for item in revisions if isinstance(item, dict)]

    def advance_module_draft(job: Any, key: str, principal_id: str) -> dict[str, Any]:
        """Resume the mechanical first pass after any committed intermediate step."""

        request = {
            "operation": "advance_module_draft",
            "job_id": job.id,
            "artifact_checksum": job.artifact_checksum,
        }
        scope = f"module-draft-advance:{job.campaign_id}:{job.id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        if dict(job.result or {}).get("finalized_package") or job.state == "compiled":
            raise ValueError("a finalized module draft is immutable")
        if job.state == "imported":
            response = {
                "job": import_job_view(job),
                "inspection": deepcopy(job.inspection),
                "validation": deepcopy(job.validation),
                "module_id": job.module_id,
                "status": "editing",
            }
            return remember_response(
                scope,
                key,
                request,
                response,
                campaign_id=job.campaign_id,
            )

        values = dict(job.payload or {})
        source = storage.artifact_module_path(job.artifact)
        if job.state in {"staged", "failed"}:
            inspect_scope = f"{scope}:inspect"
            inspect_key = f"{key}:inspect"
            inspected = replay_response(inspect_scope, inspect_key, request)
            if inspected is None:
                inspection = modules.preview_path(
                    source,
                    parser=parser,
                    document_cache_dir=config.normalized_modules_dir,
                    expected_checksum=job.artifact_checksum,
                )
                job = import_jobs.record_inspection(
                    job.id,
                    inspection,
                    expected_revision=job.revision,
                    idempotency_key=inspect_key,
                    idempotency_write=IdempotencyWrite(
                        scope=inspect_scope,
                        payload=request,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_module_job(job.campaign_id, str(inspected["job_id"]))

        if job.state == "inspected":
            inspection = deepcopy(job.inspection)
            validation = {
                "valid": bool(inspection.get("valid", not inspection.get("errors"))),
                "errors": list(inspection.get("errors") or []),
                "warnings": list(inspection.get("warnings") or []),
            }
            if not validation["valid"]:
                public_validation = deepcopy(validation)
                failed = import_jobs.record_validation(
                    job.id,
                    validation,
                    state="failed",
                    expected_revision=job.revision,
                    idempotency_key=key,
                    idempotency_write=IdempotencyWrite(
                        scope=scope,
                        payload=request,
                        response=lambda value: {
                            "job": import_job_view(value),
                            "inspection": deepcopy(value.inspection),
                            "validation": deepcopy(public_validation),
                            "module_id": value.module_id,
                            "status": "needs_repair",
                        },
                    ),
                )
                return {
                    "job": import_job_view(failed),
                    "inspection": inspection,
                    "validation": validation,
                    "module_id": failed.module_id,
                    "status": "needs_repair",
                }
            validate_scope = f"{scope}:validate"
            validate_key = f"{key}:validate"
            validated = replay_response(validate_scope, validate_key, request)
            if validated is None:
                job = import_jobs.record_validation(
                    job.id,
                    validation,
                    expected_revision=job.revision,
                    idempotency_key=validate_key,
                    idempotency_write=IdempotencyWrite(
                        scope=validate_scope,
                        payload=request,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_module_job(job.campaign_id, str(validated["job_id"]))

        if job.state == "validated":
            ingest_scope = f"{scope}:ingest"
            ingest_key = f"{key}:ingest"
            ingested = replay_response(ingest_scope, ingest_key, request)
            if ingested is None:
                imported = modules.ingest_path(
                    campaign_id=job.campaign_id,
                    path=source,
                    source_key=str(values.get("source_key") or job.artifact),
                    logical_source_key=str(values.get("source_key") or job.artifact),
                    title=str(values.get("title") or job.artifact),
                    parser=parser,
                    activate=False,
                    document_cache_dir=config.normalized_modules_dir,
                    expected_checksum=job.artifact_checksum,
                    page_revisions=import_page_revisions(job),
                    idempotency_key=ingest_key,
                    idempotency_write=IdempotencyWrite(
                        scope=ingest_scope,
                        payload=request,
                        response=lambda value: {
                            "module_id": value.module_id,
                            "scenes": value.scenes,
                            "chunks": value.chunks,
                        },
                    ),
                )
                mechanical_import = {
                    "module_id": imported.module_id,
                    "scenes": imported.scenes,
                    "chunks": imported.chunks,
                }
            else:
                mechanical_import = dict(ingested)
            public_import = deepcopy(mechanical_import)
            updated = import_jobs.record_result(
                job.id,
                {
                    **dict(job.result or {}),
                    "mechanical_import": mechanical_import,
                    "pack_draft": {},
                    "pack_edit_history": [],
                },
                state="imported",
                module_id=str(mechanical_import["module_id"]),
                expected_revision=job.revision,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=lambda value: {
                        "job": import_job_view(value),
                        "inspection": deepcopy(value.inspection),
                        "validation": deepcopy(value.validation),
                        "module_id": public_import["module_id"],
                        "status": "editing",
                    },
                ),
            )
            return {
                "job": import_job_view(updated),
                "inspection": deepcopy(updated.inspection),
                "validation": deepcopy(updated.validation),
                "module_id": updated.module_id,
                "status": "editing",
            }
        raise ValueError(f"module draft cannot advance from state {job.state!r}")

    def replay_response(scope: str, key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        replay = idempotency.lookup(scope, key, payload)
        if replay is None:
            return None
        if replay.response is None:
            raise RuntimeError("idempotency replay has no stored response")
        return dict(replay.response)

    def remember_response(
        scope: str,
        key: str,
        payload: dict[str, Any],
        response: dict[str, Any],
        *,
        campaign_id: str,
    ) -> dict[str, Any]:
        remembered = idempotency.remember(
            scope,
            key,
            payload,
            response,
            campaign_id=campaign_id,
        )
        if remembered.response is None:
            raise RuntimeError("idempotency write has no stored response")
        return dict(remembered.response)

    def current_branch_id(campaign_id: str) -> str:
        return branches.current(campaign_id).id

    def require_no_active_npc_conversation(campaign_id: str, operation: str) -> None:
        active = npc_conversations.active_ids(
            campaign_id=campaign_id,
            branch_id=current_branch_id(campaign_id),
        )
        if active:
            raise ValueError(
                f"close or abort active NPC conversation(s) before {operation}: "
                + ", ".join(active)
            )

    def readable_branch_id(campaign_id: str, branch_id: str | None, principal_id: str) -> str:
        """Players may read only the checked-out timeline."""

        current = current_branch_id(campaign_id)
        if not is_dm(campaign_id, principal_id) and branch_id not in {None, current}:
            raise PermissionError("players may inspect only the current branch")
        return current if branch_id is None else str(branch_id)

    def writable_branch_id(campaign_id: str, branch_id: str | None) -> str:
        """All live writes target the checked-out branch."""

        current = current_branch_id(campaign_id)
        if branch_id not in {None, current}:
            raise ValueError("branch_id must match the campaign's active branch")
        return current

    def readable_scope_id(campaign_id: str, scope_id: str, principal_id: str) -> str:
        """Keep split-party scene progress inside an owned actor scope."""

        value = str(scope_id or "party").strip() or "party"
        if is_dm(campaign_id, principal_id) or value == "party":
            return value
        if value.startswith("player:"):
            actor_id = value.split(":", 1)[1]
            actor_access(campaign_id, actor_id, principal_id)
            return value
        raise PermissionError("players may read only party or an owned player scene scope")

    def investigation_ledger(state: dict[str, Any]) -> dict[str, Any]:
        raw = dict(state.get("investigation_checks") or {})
        pending = dict(raw.get("pending") or {})
        history = list(raw.get("history") or [])
        return {"schema_version": 1, "pending": pending, "history": history}

    def investigation_actions(campaign: Any, pending: dict[str, Any]) -> list[str]:
        outcome = dict(pending.get("outcome") or {})
        actions = ["settle"]
        if (
            bool(campaign.settings.get("spending_luck", False))
            and outcome.get("luck_options")
            and not pending.get("decision")
        ):
            actions.append("spend_luck")
        if bool(outcome.get("push_eligible")) and not pending.get("decision"):
            actions.append("push")
        return actions

    def require_investigation_play(campaign_id: str) -> Any:
        campaign = campaigns.get(campaign_id)
        if authoritative_phase(campaign_id) != PROFILE_PLAY:
            raise ValueError("investigation checks are available only during play")
        if bool(dict(campaign.state.get("chase") or {}).get("active", False)):
            raise ValueError("use chase_action for checks while a chase is active")
        return campaign

    def require_campaign_revision(campaign_id: str, expected_revision: int) -> Any:
        campaign = campaigns.get(campaign_id)
        if campaign.revision != expected_revision:
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        return campaign

    def require_active_branch(campaign_id: str, expected_branch_id: str) -> str:
        current = current_branch_id(campaign_id)
        if current != expected_branch_id:
            raise ValueError(
                f"active branch conflict: expected {expected_branch_id}, found {current}"
            )
        return current

    def history_cursor(campaign_id: str) -> int:
        applied = next((item for item in revisions.history(campaign_id) if item.applied), None)
        return int(applied.sequence) if applied is not None else 0

    def module_archive(
        campaign_id: str, module_id: str
    ) -> tuple[dict[str, Any], dict[str, bytes], dict[str, Any]]:
        matches = [
            item
            for item in modules.list_assets(campaign_id, module_id)
            if str(dict(item.get("metadata") or {}).get("asset_kind") or "")
            == "content_package_archive"
        ]
        if len(matches) != 1:
            if not matches:
                raise LookupError(f"module {module_id} has no finalized Pack archive")
            raise ValueError("module has multiple authoritative content Pack archives")
        artifact = str(dict(matches[0].get("metadata") or {}).get("content_archive_artifact") or "")
        if not artifact:
            raise ValueError("module content Pack archive metadata is incomplete")
        package, blobs = storage.read_content_archive(artifact=artifact)
        return (
            validate_coc_content_package(package),
            blobs,
            storage.write_content_archive(package, blobs),
        )

    def authoritative_random_resolution(
        *,
        campaign_id: str,
        principal_id: str,
        operation: str,
        payload: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
        resolve: Any,
    ) -> dict[str, Any]:
        """Resolve and persist one random operation as a single audited write."""

        if not str(idempotency_key or "").strip():
            raise ValueError("idempotency_key is required for random resolution")
        access.require_campaign(campaign_id, principal_id)
        campaign = campaigns.get(campaign_id)
        branch_id = branches.current(campaign_id).id
        request = {
            "operation": operation,
            "campaign_id": campaign_id,
            "principal_id": principal_id,
            "branch_id": branch_id,
            "expected_revision": expected_revision,
            "payload": payload,
        }
        scope = f"coc-random:{campaign_id}:{branch_id}:{principal_id}:{operation}"
        replay = idempotency.lookup(scope, idempotency_key, request)
        if replay is not None:
            if replay.response is None:
                raise RuntimeError("random resolution replay has no stored response")
            return replay.response
        stream = CampaignRandomStream.from_campaign_state(
            campaign_id,
            campaign.state,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        with use_random_stream(stream):
            result = resolve()
        if stream.draw_count == 0:
            return {"resolution": result, "campaign_revision": campaign.revision}
        resolution_id = "resolution-" + hashlib.sha256(
            f"{campaign_id}:{branch_id}:{operation}:{idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]
        membership = access.require_campaign(campaign_id, principal_id)
        audience = (
            {"scope": "dm", "actor_refs": [], "disclosure": "hidden"}
            if membership.role in {"owner", "dm"}
            else {"scope": "principal", "actor_refs": [], "disclosure": "private"}
        )
        receipt = stream.receipt()
        next_state = {**dict(campaign.state), "random_stream": stream.persisted_state()}
        next_state["resolution_presentation_log"] = [
            *list(next_state.get("resolution_presentation_log") or []),
            {
                "id": resolution_id,
                "thread_id": resolution_id,
                "event_sequence": 1,
                "operation": operation,
                "status": "settled",
                "audience": audience,
                "principal_id": principal_id if audience["scope"] == "principal" else None,
                "branch_id": branch_id,
                "campaign_revision": campaign.revision + 1,
                "result": deepcopy(dict(result)),
                "random_stream_receipt": receipt,
            },
        ][-200:]
        response = {
            "resolution": result,
            "resolution_id": resolution_id,
            "thread_id": resolution_id,
            "event_sequence": 1,
            "campaign_revision": campaign.revision + 1,
            "random_stream_receipt": receipt,
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            expected_campaign_revision=expected_revision,
            operation=operation,
            actor=principal_id,
            branch_id=branch_id or None,
            idempotency_key=idempotency_key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        stream.mark_persisted()
        return response

    @mcp.tool()
    def server_capabilities() -> dict[str, Any]:
        return {
            "server": "sagasmith-coc-mcp",
            "version": "0.1.0",
            "system": "coc7e",
            "phases": list(PROFILES),
            "progressive_exposure": True,
            "native_dynamic_tools_required": True,
            "actor_knowledge": "branch-scoped and actor-authorized",
            "resolution_boundary": (
                "random draws and their stream receipt commit atomically; "
                "pure calculations do not mutate state"
            ),
            "npc_conversations": {
                "schema_version": NPC_CONVERSATION_SCHEMA_VERSION,
                "contract": NPC_CONVERSATION_CONTRACT,
                "proposal_contract": "npc-conversation-proposal.v4",
                "public_tool": "npc_conversation",
                "host_transport": "private_authenticated_unlisted",
                "host_transport_tool": "npc_conversation_transport",
                "stable_memory_candidate_ids": True,
                "symmetric_heard_statement_candidates": True,
                "actor_safe_transcript_recall": True,
                "terminal_journal_compaction": True,
            },
            "content_pack": {
                "format": "sagasmith.content-package",
                "schema_version": 2,
                "kinds": ["module", "core_rules"],
                "draft_stages": [
                    "module_draft(start)",
                    "module_draft(edit:advance)",
                    "module_draft(evidence)",
                    "module_draft(edit:source_text|content|statblock|asset|actor)",
                    "module_draft(edit:package)",
                    "module_draft(finalize)",
                    "rulebook_draft(start|evidence|finalize)",
                    "content_pack(import)",
                    "content_pack(activate)",
                ],
                "finalization": "explicit Agent confirmation; finalized archive is immutable",
            },
            "tool_catalog": tool_catalog(),
        }

    @mcp.tool()
    def storage_status() -> dict[str, Any]:
        return storage.status()

    @mcp.tool()
    def resolution_presentation(
        campaign_id: str,
        resolution_id: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Return one audience-safe, authoritative resolution bubble projection."""

        return resolution_presentation_view(campaign_id, resolution_id, principal_id)

    @mcp.tool()
    def campaign_query(
        action: Literal["list", "get"],
        campaign_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        if action == "list":
            allowed = access.accessible_campaign_ids(principal_id)
            return {
                "campaigns": [
                    campaign_audience_view(item.id, principal_id)
                    for item in campaigns.list(system_id="coc7e")
                    if item.id in allowed
                ]
            }
        if campaign_id is None:
            raise ValueError("campaign_id is required")
        return campaign_audience_view(campaign_id, principal_id)

    @mcp.tool()
    def game_phase(campaign_id: str, principal_id: str = "system:local") -> dict[str, str]:
        access.require_campaign(campaign_id, principal_id)
        return {"campaign_id": campaign_id, "phase": authoritative_phase(campaign_id)}

    @mcp.tool()
    def campaign_change(
        action: Literal[
            "create", "set_phase", "grant_campaign", "revoke_campaign", "grant_actor"
        ],
        data: dict[str, Any],
        campaign_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        if action == "create":
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("data.name is required")
            created = campaigns.create_owned(
                system_id="coc7e",
                name=name,
                principal_id=principal_id,
                idempotency_key=str(data.get("idempotency_key") or uuid4().hex),
                description=str(data.get("description") or ""),
                settings=dict(data.get("settings") or {}),
                state={
                    "game_phase": PROFILE_LOBBY,
                    "random_stream": initial_random_stream(f"sagasmith-coc:{uuid4().hex}"),
                    **dict(data.get("state") or {}),
                },
            )
            return asdict(created)
        if campaign_id is None:
            raise ValueError("campaign_id is required")
        require_dm(campaign_id, principal_id)
        if action == "set_phase":
            phase = str(data.get("phase") or "")
            if phase not in {PROFILE_LOBBY, PROFILE_PLAY}:
                raise ValueError(
                    "phase must be lobby or play; combat is derived from combat.active"
                )
            current = campaigns.get(campaign_id)
            if phase != authoritative_phase(campaign_id):
                require_no_active_npc_conversation(campaign_id, "changing phase")
            if phase == PROFILE_LOBBY and investigation_ledger(dict(current.state))["pending"]:
                raise ValueError(
                    "settle or abort pending investigation checks before returning to lobby"
                )
            state = {**dict(current.state), "game_phase": phase}
            return asdict(
                campaigns.update(
                    campaign_id,
                    state=state,
                    expected_revision=int(data["expected_revision"]),
                )
            )
        target = str(data.get("target_principal_id") or "")
        if not target:
            raise ValueError("data.target_principal_id is required")
        if action == "revoke_campaign":
            return asdict(access.revoke_campaign(campaign_id, target))
        access.ensure_principal(target, display_name=str(data.get("display_name") or ""))
        if action == "grant_campaign":
            return asdict(
                access.grant_campaign(campaign_id, target, role=str(data.get("role") or "player"))
            )
        actor_id = str(data.get("actor_id") or "")
        if not actor_id:
            raise ValueError("data.actor_id is required")
        return asdict(
            access.grant_actor(
                campaign_id,
                target,
                actor_id,
                can_control=bool(data.get("can_control", True)),
                can_view_private=bool(data.get("can_view_private", True)),
            )
        )

    @mcp.tool()
    def character_query(
        action: Literal["list", "get"],
        campaign_id: str,
        character_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        access.require_campaign(campaign_id, principal_id)
        if action == "list":
            return {
                "characters": [
                    visible_character(item, principal_id)
                    for item in characters.list(system_id="coc7e", campaign_id=campaign_id)
                ]
            }
        if character_id is None:
            raise ValueError("character_id is required")
        item = characters.get(character_id)
        if item.campaign_id != campaign_id:
            raise LookupError(character_id)
        return visible_character(item, principal_id)

    @mcp.tool()
    def character_change(
        action: Literal["create", "instantiate", "update"],
        campaign_id: str,
        data: dict[str, Any],
        character_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        membership = access.require_campaign(campaign_id, principal_id)
        key = str(data.get("idempotency_key") or "").strip()
        if not key:
            raise ValueError("data.idempotency_key is required")
        if action == "instantiate":
            require_dm(campaign_id, principal_id)
            template_id = str(data.get("template_id") or "").strip()
            if not template_id:
                raise ValueError("data.template_id is required")
            template = characters.get(template_id)
            if template.system_id != "coc7e" or template.character_type != "investigator":
                raise ValueError("template must be a CoC investigator")
            instance_name = (
                str(data["name"]) if data.get("name") is not None else template.name
            )
            created = actor_lifecycle.create(
                campaign_id,
                system_id="coc7e",
                template_id=template_id,
                name=instance_name,
                character_type=template.character_type,
                player_name=(
                    str(data["player_name"]) if data.get("player_name") is not None else None
                ),
                summary=template.summary,
                sheet=validate_investigator_sheet(dict(template.sheet)),
                notes=deepcopy(template.notes),
                principal_id=principal_id,
                idempotency_key=key,
                initial_grants=(InitialActorGrant(principal_id),),
                expected_campaign_revision=int(data["expected_campaign_revision"]),
                operation="character.instantiate",
                actor=principal_id,
            )
            return asdict(created.character)
        if action == "create":
            character_type = str(data.get("character_type") or "investigator")
            if character_type not in {"investigator", "npc", "creature"}:
                raise ValueError("character_type must be investigator, npc, or creature")
            if character_type != "investigator" and membership.role not in {"owner", "dm"}:
                raise PermissionError("only the Keeper may create NPCs or creatures")
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("data.name is required")
            created = actor_lifecycle.create(
                campaign_id,
                system_id="coc7e",
                name=name,
                character_type=character_type,
                player_name=data.get("player_name"),
                summary=str(data.get("summary") or ""),
                sheet=validate_investigator_sheet(dict(data.get("sheet") or {})),
                notes=dict(data.get("notes") or {}),
                principal_id=principal_id,
                idempotency_key=key,
                initial_grants=(InitialActorGrant(principal_id),),
                expected_campaign_revision=int(data["expected_campaign_revision"]),
                operation="character.create",
                actor=principal_id,
            )
            return asdict(created.character)
        if character_id is None:
            raise ValueError("character_id is required")
        actor_access(campaign_id, character_id, principal_id, control=True)
        if authoritative_phase(campaign_id) == PROFILE_COMBAT and membership.role not in {
            "owner",
            "dm",
        }:
            raise PermissionError("combat character mutations require Keeper authority")
        current = characters.get(character_id)
        if "expected_revision" not in data:
            raise ValueError("data.expected_revision is required")
        updated = {
            **asdict(current),
            "name": str(data.get("name", current.name)),
            "player_name": data.get("player_name", current.player_name),
            "summary": str(data.get("summary", current.summary)),
            "sheet": (
                validate_investigator_sheet(dict(data["sheet"]))
                if "sheet" in data
                else deepcopy(current.sheet)
            ),
            "notes": (
                dict(data["notes"]) if "notes" in data else deepcopy(current.notes)
            ),
            "revision": current.revision + 1,
        }
        payload = {
            "action": "update",
            "campaign_id": campaign_id,
            "character_id": character_id,
            "data": deepcopy(data),
        }
        scope = f"character-update:{campaign_id}:{current_branch_id(campaign_id)}:{principal_id}"
        replay = idempotency.lookup(scope, key, payload)
        if replay is not None and replay.response is not None:
            return dict(replay.response)
        StateMutationService(storage.database).replace(
            campaign_id,
            character_updates=[
                CharacterStateUpdate(
                    character_id=character_id,
                    name=updated["name"],
                    player_name=updated["player_name"],
                    summary=updated["summary"],
                    sheet=updated["sheet"],
                    notes=updated["notes"],
                    expected_revision=int(data["expected_revision"]),
                )
            ],
            operation="character.update",
            actor=principal_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=payload,
                response=updated,
            ),
        )
        return updated

    @mcp.tool()
    def inventory_change(
        action: Literal["add", "update", "remove", "consume"],
        campaign_id: str,
        actor_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_character_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Atomically mutate one stable CoC inventory item."""

        actor_access(campaign_id, actor_id, principal_id, control=True)
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": f"inventory_change.{action}",
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "data": deepcopy(data),
            "expected_revision": int(expected_revision),
            "expected_character_revision": int(expected_character_revision),
            "branch_id": branch_id,
        }
        scope = f"coc-inventory:{campaign_id}:{branch_id}:{actor_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_campaign_revision(campaign_id, int(expected_revision))
        actor = characters.get(actor_id)
        if actor.campaign_id != campaign_id:
            raise ValueError("actor must belong to the target campaign")
        if actor.revision != int(expected_character_revision):
            raise ValueError(
                "character revision conflict: "
                f"expected {expected_character_revision}, found {actor.revision}"
            )
        next_sheet, receipt = change_inventory(
            dict(actor.sheet),
            action=action,
            item=(dict(data["item"]) if isinstance(data.get("item"), dict) else None),
            item_id=(str(data["item_id"]) if data.get("item_id") is not None else None),
            quantity=(int(data["quantity"]) if data.get("quantity") is not None else None),
        )
        response = {
            "campaign_revision": campaign.revision + 1,
            "character_revision": actor.revision + 1,
            "actor_id": actor_id,
            "receipt": receipt,
            "inventory": deepcopy(next_sheet["inventory"]),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=dict(campaign.state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=next_sheet,
                    notes=dict(actor.notes),
                    expected_revision=actor.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation=f"coc.inventory.{action}",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(scope=scope, payload=request, response=response),
        )
        return response

    @mcp.tool()
    def wallet_change(
        action: Literal["set", "adjust"],
        campaign_id: str,
        actor_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_character_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Atomically set or adjust a campaign-defined monetary field."""

        actor_access(campaign_id, actor_id, principal_id, control=True)
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": f"wallet_change.{action}",
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "data": deepcopy(data),
            "expected_revision": int(expected_revision),
            "expected_character_revision": int(expected_character_revision),
            "branch_id": branch_id,
        }
        scope = f"coc-wallet:{campaign_id}:{branch_id}:{actor_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_campaign_revision(campaign_id, int(expected_revision))
        actor = characters.get(actor_id)
        if actor.campaign_id != campaign_id:
            raise ValueError("actor must belong to the target campaign")
        if actor.revision != int(expected_character_revision):
            raise ValueError(
                "character revision conflict: "
                f"expected {expected_character_revision}, found {actor.revision}"
            )
        next_sheet, receipt = change_money(
            dict(actor.sheet),
            action=action,
            field=str(data.get("field") or ""),
            amount=data.get("amount"),
            value=data.get("value"),
        )
        response = {
            "campaign_revision": campaign.revision + 1,
            "character_revision": actor.revision + 1,
            "actor_id": actor_id,
            "receipt": receipt,
            "monetary": deepcopy(next_sheet["monetary"]),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=dict(campaign.state),
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=next_sheet,
                    notes=dict(actor.notes),
                    expected_revision=actor.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation=f"coc.wallet.{action}",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(scope=scope, payload=request, response=response),
        )
        return response

    @mcp.tool()
    def long_term_change(
        action: Literal["luck_recovery", "therapy", "aging", "source_study"],
        campaign_id: str,
        actor_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_character_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Settle source-bound downtime, aging, Luck, tome, and spell changes."""

        require_dm(campaign_id, principal_id)
        require_lobby(campaign_id, f"long_term_change({action})")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        source = " ".join(str(data.get("source") or "").split()).strip()
        if not source or len(source) > 1000:
            raise ValueError("data.source must contain 1 to 1000 characters")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": f"long_term_change.{action}",
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "data": deepcopy(data),
            "expected_revision": int(expected_revision),
            "expected_character_revision": int(expected_character_revision),
            "branch_id": branch_id,
        }
        scope = f"coc-long-term:{campaign_id}:{branch_id}:{actor_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_campaign_revision(campaign_id, int(expected_revision))
        actor = characters.get(actor_id)
        if actor.campaign_id != campaign_id or actor.revision != int(expected_character_revision):
            raise ValueError("actor campaign or character revision conflict")
        sheet = validate_investigator_sheet(dict(actor.sheet))
        stream = CampaignRandomStream.from_campaign_state(
            campaign_id,
            campaign.state,
            operation=f"long_term_change.{action}",
            idempotency_key=key,
        )
        with use_random_stream(stream):
            if action == "luck_recovery":
                if not bool(campaign.settings.get("luck_recovery", False)):
                    raise ValueError("campaign settings do not enable optional Luck recovery")
                receipt = resolve_luck_development(int(sheet["luck"]))
                sheet["luck"] = int(receipt["new_value"])
            elif action == "therapy":
                amount_fields = [field for field in ("amount", "expression") if field in data]
                if len(amount_fields) != 1:
                    raise ValueError("therapy requires exactly one of amount or expression")
                rolled = None
                if amount_fields[0] == "expression":
                    rolled = roll_dice_expression(str(data["expression"]))
                    amount = int(rolled["total"])
                else:
                    amount = int(data["amount"])
                if amount < 0:
                    raise ValueError("therapy SAN recovery must be non-negative")
                before = int(sheet["san"])
                sheet["san"] = min(int(sheet["san_max"]), before + amount)
                receipt = {
                    "source": source,
                    "roll": rolled,
                    "san": {"before": before, "gain": amount, "after": sheet["san"]},
                }
            elif action == "aging":
                changes = data.get("characteristic_changes")
                if not isinstance(changes, dict) or not changes:
                    raise ValueError("aging requires source-reviewed characteristic_changes")
                before = deepcopy(sheet["characteristics"])
                for name, delta in changes.items():
                    key_name = str(name).casefold()
                    if (
                        key_name not in before
                        or isinstance(delta, bool)
                        or not isinstance(delta, int)
                    ):
                        raise ValueError("aging characteristic changes must be integer core deltas")
                    sheet["characteristics"][key_name] = before[key_name] + delta
                sheet = validate_investigator_sheet(sheet)
                receipt = {
                    "source": source,
                    "before": before,
                    "after": deepcopy(sheet["characteristics"]),
                }
            else:
                sheet, receipt = settle_source_study(
                    sheet,
                    kind=str(data.get("kind") or ""),
                    source_id=str(data.get("source_id") or ""),
                    title=str(data.get("title") or ""),
                    sanity_loss=int(data.get("sanity_loss", 0)),
                    mythos_gain=int(data.get("mythos_gain", 0)),
                    spell=(dict(data["spell"]) if isinstance(data.get("spell"), dict) else None),
                )
                receipt["source"] = source
        next_state = {**dict(campaign.state), "random_stream": stream.persisted_state()}
        response = {
            "campaign_revision": campaign.revision + 1,
            "character_revision": actor.revision + 1,
            "actor_id": actor_id,
            "action": action,
            "receipt": receipt,
            "random_stream_receipt": stream.receipt(),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_investigator_sheet(sheet),
                    notes=dict(actor.notes),
                    expected_revision=actor.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation=f"coc.long_term.{action}",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(scope=scope, payload=request, response=response),
        )
        stream.mark_persisted()
        return response

    @mcp.tool()
    def rulebook_draft(
        action: Literal["start", "get", "evidence", "finalize"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Create, review, and finalize one private CoC rules Pack draft."""

        require_dm(campaign_id, principal_id)
        require_lobby(campaign_id, f"rulebook_draft({action})")
        data = dict(data or {})
        if action == "get":
            if data.get("job_id"):
                return {"job": import_job_view(require_rule_job(campaign_id, str(data["job_id"])))}
            return {
                "order": "newest_first",
                "jobs": [
                    import_job_handle(item)
                    for item in import_jobs.list(campaign_id, kind="rulebook")
                ],
            }

        if action == "start":
            key = str(idempotency_key or "").strip()
            source_path = str(data.get("source_path") or "").strip()
            if not key or not source_path:
                raise ValueError("rulebook start requires source_path and idempotency_key")
            staged = storage.stage_rule(source_path)
            title = str(data.get("title") or Path(source_path).stem).strip()
            source_key = str(data.get("source_key") or Path(source_path).name).strip()
            request = {
                "operation": "start_rulebook_draft",
                "artifact": staged["artifact"],
                "checksum": staged["checksum"],
                "title": title,
                "source_key": source_key,
            }
            scope = f"rulebook-draft-start:{campaign_id}:{principal_id}"
            replay = replay_response(scope, key, request)
            if replay is not None:
                return replay
            create_scope = f"rulebook-draft-job:{campaign_id}:{principal_id}:create"
            create_key = f"{key}:create"
            created = replay_response(create_scope, create_key, request)
            if created is None:
                job = import_jobs.create(
                    campaign_id=campaign_id,
                    kind="rulebook",
                    artifact=str(staged["artifact"]),
                    artifact_checksum=str(staged["checksum"]),
                    payload={"title": title, "source_key": source_key},
                    idempotency_key=create_key,
                    idempotency_write=IdempotencyWrite(
                        scope=create_scope,
                        payload=request,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_rule_job(campaign_id, str(created["job_id"]))
            source = storage.artifact_rule_path(job.artifact)
            inspection = rules.inspect_path(
                source,
                document_cache_dir=config.normalized_rules_dir,
                expected_checksum=job.artifact_checksum,
            )
            inspection_scope = f"rulebook-draft-job:{campaign_id}:{job.id}:inspect"
            inspection_key = f"{key}:inspect"
            inspected = replay_response(inspection_scope, inspection_key, inspection)
            if inspected is None:
                job = import_jobs.record_inspection(
                    job.id,
                    inspection,
                    expected_revision=job.revision,
                    idempotency_key=inspection_key,
                    idempotency_write=IdempotencyWrite(
                        scope=inspection_scope,
                        payload=inspection,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_rule_job(campaign_id, str(inspected["job_id"]))
            validation = {
                "valid": bool(inspection.get("sections")) and bool(inspection.get("chunks")),
                "errors": (
                    []
                    if inspection.get("sections") and inspection.get("chunks")
                    else ["rule source did not produce indexed sections and chunks"]
                ),
                "warnings": list(inspection.get("warnings") or []),
            }
            validation_scope = f"rulebook-draft-job:{campaign_id}:{job.id}:validate"
            validation_key = f"{key}:validate"
            validated = replay_response(validation_scope, validation_key, validation)
            if validated is None:
                job = import_jobs.record_validation(
                    job.id,
                    validation,
                    expected_revision=job.revision,
                    idempotency_key=validation_key,
                    idempotency_write=IdempotencyWrite(
                        scope=validation_scope,
                        payload=validation,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_rule_job(campaign_id, str(validated["job_id"]))
            if not validation["valid"]:
                response = {"job": import_job_view(job), "status": "needs_repair"}
                return remember_response(scope, key, request, response, campaign_id=campaign_id)
            ingest_scope = f"rulebook-draft-job:{campaign_id}:{job.id}:ingest"
            ingest_key = f"{key}:ingest"
            ingest_request = {"job_id": job.id, "source_key": source_key, "title": title}
            imported = replay_response(ingest_scope, ingest_key, ingest_request)
            if imported is None:
                result = rules.ingest_path(
                    system_id="coc7e",
                    path=source,
                    source_key=source_key,
                    title=title,
                    locale=str(data.get("locale") or "en"),
                    edition="7e",
                    version=str(data.get("version") or ""),
                    publication_id=str(data.get("publication_id") or ""),
                    authority=str(data.get("authority") or "primary"),
                    document_cache_dir=config.normalized_rules_dir,
                    expected_checksum=job.artifact_checksum,
                    idempotency_campaign_id=campaign_id,
                    idempotency_key=ingest_key,
                    idempotency_write=IdempotencyWrite(
                        scope=ingest_scope,
                        payload=ingest_request,
                        response=lambda value: {"source_id": value["result"].source_id},
                    ),
                )
                imported = {"source_id": result.source_id}
            record_scope = f"rulebook-draft-job:{campaign_id}:{job.id}:record-import"
            record_key = f"{key}:record-import"
            recorded = replay_response(record_scope, record_key, imported)
            if recorded is None:
                job = import_jobs.record_result(
                    job.id,
                    {"source_id": imported["source_id"], "draft_edit_history": []},
                    state="imported",
                    expected_revision=job.revision,
                    idempotency_key=record_key,
                    idempotency_write=IdempotencyWrite(
                        scope=record_scope,
                        payload=imported,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_rule_job(campaign_id, str(recorded["job_id"]))
            response = {
                "job": import_job_view(job),
                "source_id": str(imported["source_id"]),
                "inspection": inspection,
                "validation": validation,
                "status": "editing",
            }
            return remember_response(scope, key, request, response, campaign_id=campaign_id)

        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("data.job_id is required")
        job = require_rule_job(campaign_id, job_id)
        source_id = str(dict(job.result or {}).get("source_id") or "")
        if not source_id:
            raise ValueError("rulebook draft has no indexed source")
        if action == "evidence":
            query = str(data.get("query") or "").strip()
            if query:
                return {
                    "hits": [
                        asdict(hit)
                        for hit in rules.search(
                            system_id="coc7e",
                            query=query,
                            query_hints=COC7E_QUERY_HINTS,
                            source_ids=[source_id],
                            top_k=max(1, min(20, int(data.get("top_k", 8)))),
                        )
                    ]
                }
            return {"source": rules.source(source_id), "chunks": rules.source_chunks(source_id)}
        if action != "finalize":
            raise ValueError(f"unsupported rulebook_draft action: {action}")
        revision, key = require_write_contract(expected_revision, idempotency_key)
        if revision != job.revision:
            raise ValueError(
                f"rulebook draft revision conflict: expected {revision}, found {job.revision}"
            )
        confirmation = data.get("confirmation")
        if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
            raise ValueError("rulebook finalization requires explicit Agent confirmation")
        note = str(confirmation.get("note") or "").strip()
        if not note or len(note) > 2000:
            raise ValueError("rulebook finalization confirmation note is required")
        package_id = str(data.get("package_id") or "").strip()
        version = str(data.get("version") or "1.0.0").strip()
        title = str(data.get("title") or dict(job.payload or {}).get("title") or "").strip()
        request = {
            "operation": "finalize_rulebook_draft",
            "job_id": job.id,
            "expected_revision": revision,
            "package_id": package_id,
            "version": version,
            "title": title,
            "confirmation": {"confirmed": True, "note": note},
        }
        scope = f"rulebook-draft-finalize:{campaign_id}:{job.id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        exported = rules.export_content_source(source_id)
        package, blobs = build_rule_content_package(
            package_id=package_id,
            version=version,
            title=title,
            exported_sources=[exported],
            metadata={
                "agent_finalization": {
                    "confirmed": True,
                    "reviewer": principal_id,
                    "note": note,
                }
            },
            dependencies=list(data.get("dependencies") or []),
            artifacts=list(data.get("artifacts") or []),
            mechanics=list(data.get("mechanics") or []),
        )
        stored = storage.write_content_archive(package, blobs)
        finalized = {
            "artifact": stored["artifact"],
            "summary": {
                "id": package["id"],
                "version": package["version"],
                "checksum": package["checksum"],
                "sources": len(package["sources"]),
            },
            "confirmation": package["metadata"]["agent_finalization"],
        }
        updated = import_jobs.record_result(
            job.id,
            {**dict(job.result or {}), "finalized_package": finalized},
            state="compiled",
            expected_revision=revision,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=lambda value: {"job": import_job_view(value), **finalized},
            ),
        )
        return {"job": import_job_view(updated), **finalized}

    @mcp.tool()
    def rule_query(
        action: Literal["sources", "search", "expand", "effective"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read indexed CoC rules and the branch-locked effective ruleset."""

        access.require_campaign(campaign_id, principal_id)
        data = dict(data or {})
        if action == "sources":
            return {"sources": rules.sources(system_id="coc7e")}
        if action == "search":
            query = str(data.get("query") or "").strip()
            if not query:
                raise ValueError("rule search requires data.query")
            return {
                "hits": [
                    asdict(hit)
                    for hit in rules.search(
                        system_id="coc7e",
                        query=query,
                        query_hints=COC7E_QUERY_HINTS,
                        edition="7e",
                        top_k=max(1, min(20, int(data.get("top_k", 8)))),
                    )
                ]
            }
        if action == "expand":
            return rules.expand(str(data.get("chunk_id") or ""))
        if action == "effective":
            return asdict(rule_packs.effective_ruleset(campaign_id))
        raise ValueError(f"unsupported rule_query action: {action}")

    @mcp.tool()
    def module_draft(
        action: Literal["start", "get", "evidence", "edit", "finalize"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Create, inspect, evidence-review, edit, and finalize one CoC Module Pack draft."""

        require_dm(campaign_id, principal_id)
        require_lobby(campaign_id, f"module_draft({action})")
        data = dict(data or {})
        if action == "get":
            if data.get("job_id"):
                job = require_module_job(campaign_id, str(data["job_id"]))
                return {"job": import_job_view(job)}
            return {
                "order": "newest_first",
                "jobs": [
                    import_job_handle(item) for item in import_jobs.list(campaign_id, kind="module")
                ],
            }

        if action == "start":
            key = str(idempotency_key or "").strip()
            if not key:
                raise ValueError("idempotency_key is required to start a module draft")
            source_path = data.get("source_path")
            generated_fields = {"name", "content"}.intersection(data)
            if source_path is not None and generated_fields:
                raise ValueError("start accepts either source_path or name+content, not both")
            if source_path is None and generated_fields != {"name", "content"}:
                raise ValueError("start requires source_path or both name and content")
            request = {
                "operation": "start_module_draft",
                "source_path": str(source_path or ""),
                "name": str(data.get("name") or ""),
                "content": str(data.get("content") or ""),
                "title": str(data.get("title") or ""),
                "source_key": str(data.get("source_key") or ""),
            }
            scope = f"module-draft-start:{campaign_id}:{principal_id}"
            replay = replay_response(scope, key, request)
            if replay is not None:
                return replay
            if source_path is not None:
                stored = storage.stage_module(str(source_path))
                default_name = Path(str(source_path)).name
            else:
                stored = storage.stage_text_module(str(data["name"]), str(data["content"]))
                default_name = str(data["name"])
            artifact = str(stored["artifact"])
            title = str(data.get("title") or Path(default_name).stem).strip()
            source_key = str(data.get("source_key") or default_name).strip()
            if not title or not source_key:
                raise ValueError("module title and source_key must not be empty")

            create_payload = {
                "artifact": artifact,
                "checksum": stored["checksum"],
                "title": title,
                "source_key": source_key,
            }
            create_scope = f"module-draft-job:{campaign_id}:{principal_id}:create"
            create_key = f"{key}:create"
            created_replay = replay_response(create_scope, create_key, create_payload)
            if created_replay is None:
                job = import_jobs.create(
                    campaign_id=campaign_id,
                    kind="module",
                    artifact=artifact,
                    artifact_checksum=str(stored["checksum"]),
                    payload={"title": title, "source_key": source_key},
                    idempotency_key=create_key,
                    idempotency_write=IdempotencyWrite(
                        scope=create_scope,
                        payload=create_payload,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_module_job(campaign_id, str(created_replay["job_id"]))

            source = storage.artifact_module_path(job.artifact)
            inspection = modules.preview_path(
                source,
                parser=parser,
                document_cache_dir=config.normalized_modules_dir,
                expected_checksum=job.artifact_checksum,
            )
            inspect_payload = {
                "job_id": job.id,
                "artifact_checksum": job.artifact_checksum,
                "parser_profile": inspection.get("parser_profile"),
                "parser_version": inspection.get("parser_version"),
            }
            inspect_scope = f"module-draft-job:{campaign_id}:{job.id}:inspect"
            inspect_key = f"{key}:inspect"
            inspected_replay = replay_response(inspect_scope, inspect_key, inspect_payload)
            if inspected_replay is None:
                job = import_jobs.record_inspection(
                    job.id,
                    inspection,
                    expected_revision=job.revision,
                    idempotency_key=inspect_key,
                    idempotency_write=IdempotencyWrite(
                        scope=inspect_scope,
                        payload=inspect_payload,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_module_job(campaign_id, str(inspected_replay["job_id"]))

            validation = {
                "valid": not bool(inspection.get("errors")),
                "errors": list(inspection.get("errors") or []),
                "warnings": list(inspection.get("warnings") or []),
            }
            validate_payload = {"job_id": job.id, "inspection": inspection}
            validate_scope = f"module-draft-job:{campaign_id}:{job.id}:validate"
            validate_key = f"{key}:validate"
            validated_replay = replay_response(validate_scope, validate_key, validate_payload)
            if validated_replay is None:
                job = import_jobs.record_validation(
                    job.id,
                    validation,
                    expected_revision=job.revision,
                    idempotency_key=validate_key,
                    idempotency_write=IdempotencyWrite(
                        scope=validate_scope,
                        payload=validate_payload,
                        response=lambda value: {"job_id": value.id},
                    ),
                )
            else:
                job = require_module_job(campaign_id, str(validated_replay["job_id"]))

            if validation["valid"]:
                ingest_payload = {
                    "job_id": job.id,
                    "artifact_checksum": job.artifact_checksum,
                    "source_key": source_key,
                    "title": title,
                }
                ingest_scope = f"module-draft-job:{campaign_id}:{job.id}:ingest"
                ingest_key = f"{key}:ingest"
                ingest_replay = replay_response(ingest_scope, ingest_key, ingest_payload)
                if ingest_replay is None:
                    imported = modules.ingest_path(
                        campaign_id=campaign_id,
                        path=source,
                        source_key=source_key,
                        logical_source_key=source_key,
                        title=title,
                        parser=parser,
                        activate=False,
                        document_cache_dir=config.normalized_modules_dir,
                        expected_checksum=job.artifact_checksum,
                        idempotency_key=ingest_key,
                        idempotency_write=IdempotencyWrite(
                            scope=ingest_scope,
                            payload=ingest_payload,
                            response=lambda value: {
                                "module_id": value.module_id,
                                "scenes": value.scenes,
                                "chunks": value.chunks,
                            },
                        ),
                    )
                    import_result = {
                        "module_id": imported.module_id,
                        "scenes": imported.scenes,
                        "chunks": imported.chunks,
                    }
                else:
                    import_result = ingest_replay
                record_payload = {"job_id": job.id, **import_result}
                record_scope = f"module-draft-job:{campaign_id}:{job.id}:record-import"
                record_key = f"{key}:record-import"
                record_replay = replay_response(record_scope, record_key, record_payload)
                if record_replay is None:
                    job = import_jobs.record_result(
                        job.id,
                        {
                            "mechanical_import": deepcopy(import_result),
                            "pack_draft": {},
                            "pack_edit_history": [],
                        },
                        state="imported",
                        module_id=str(import_result["module_id"]),
                        expected_revision=job.revision,
                        idempotency_key=record_key,
                        idempotency_write=IdempotencyWrite(
                            scope=record_scope,
                            payload=record_payload,
                            response=lambda value: {"job_id": value.id},
                        ),
                    )
                else:
                    job = require_module_job(campaign_id, str(record_replay["job_id"]))
                response = {
                    "job_id": job.id,
                    "job": import_job_view(job),
                    "inspection": inspection,
                    "validation": validation,
                    "module_id": job.module_id,
                    "status": "editing",
                }
            else:
                response = {
                    "job_id": job.id,
                    "job": import_job_view(job),
                    "inspection": inspection,
                    "validation": validation,
                    "status": "needs_repair",
                }
            return remember_response(scope, key, request, response, campaign_id=campaign_id)

        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("data.job_id is required")
        job = require_module_job(campaign_id, job_id)
        if action == "evidence":
            if not job.module_id:
                raise ValueError("module evidence requires a mechanically imported draft")
            evidence_kind = str(
                data.get("kind") or ("page" if data.get("page_number") else "chunks")
            )
            if evidence_kind == "page":
                page_number = data.get("page_number")
                if (
                    isinstance(page_number, bool)
                    or not isinstance(page_number, int)
                    or page_number < 1
                ):
                    raise ValueError("data.page_number must be a positive integer")
                source = storage.artifact_module_path(job.artifact)
                if source.suffix.casefold() != ".pdf":
                    raise ValueError("page evidence requires a staged PDF")
                scale = float(data.get("scale", 1.5))
                rendered = render_pdf_page(source, page_number, scale=scale)
                if rendered.source_checksum != job.artifact_checksum:
                    raise RuntimeError("rendered PDF no longer matches the staged checksum")
                document = normalize_document(
                    source,
                    cache_dir=config.normalized_modules_dir,
                    expected_checksum=job.artifact_checksum,
                )
                document = apply_document_page_revisions(document, import_page_revisions(job))
                normalized_text = normalized_document_page_text(document, page_number)
                native_text = extract_pdf_page_text(source, page_number)
                target = storage.store_rendered_module_page(
                    module_id=job.module_id,
                    source_checksum=rendered.source_checksum,
                    page_number=rendered.page_number,
                    scale=rendered.scale,
                    checksum=rendered.checksum,
                    content=rendered.content,
                )
                asset = modules.register_asset(
                    campaign_id=campaign_id,
                    module_id=job.module_id,
                    source_path=str(target),
                    media_type=rendered.media_type,
                    checksum=rendered.checksum,
                    metadata={
                        "kind": "rendered_page",
                        "asset_kind": "rendered_page",
                        "source_checksum": rendered.source_checksum,
                        "source_page": rendered.page_number,
                        "page_count": rendered.page_count,
                        "width": rendered.width,
                        "height": rendered.height,
                        "scale": rendered.scale,
                    },
                )
                page_receipts = []
                source_key = str(dict(job.payload or {}).get("source_key") or job.artifact)
                for item in modules.list_chunks(campaign_id, job.module_id):
                    page_start = item.get("page_start")
                    page_end = item.get("page_end")
                    if (
                        isinstance(page_start, int)
                        and isinstance(page_end, int)
                        and page_start <= page_number <= page_end
                    ):
                        page_receipts.append(
                            {
                                "chunk_id": item["id"],
                                "source_ref": {
                                    "source_key": source_key,
                                    "page": page_number,
                                    "chunk_hash": str(item.get("content_hash") or ""),
                                    "note": "Agent-reviewed source evidence from rendered page",
                                },
                            }
                        )
                return {
                    "campaign_id": campaign_id,
                    "job_id": job.id,
                    "module_id": job.module_id,
                    "artifact": job.artifact,
                    "source_checksum": rendered.source_checksum,
                    "page_number": rendered.page_number,
                    "page_count": rendered.page_count,
                    "width": rendered.width,
                    "height": rendered.height,
                    "scale": rendered.scale,
                    "image": {
                        "asset_id": asset["id"],
                        "managed_path": str(target),
                        "media_type": rendered.media_type,
                        "checksum": rendered.checksum,
                    },
                    "normalized": {
                        "text": normalized_text[:50000],
                        "text_sha256": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
                        "truncated": len(normalized_text) > 50000,
                    },
                    "native_text": {
                        "text": native_text[:50000],
                        "text_sha256": hashlib.sha256(native_text.encode("utf-8")).hexdigest(),
                        "truncated": len(native_text) > 50000,
                    },
                    "citation_candidates": page_receipts,
                }
            if evidence_kind not in {"chunks", "assets", "reviews"}:
                raise ValueError("data.kind must be page, chunks, assets, or reviews")
            if evidence_kind == "assets":
                return {
                    "job_id": job.id,
                    "assets": modules.list_assets(campaign_id, job.module_id),
                }
            if evidence_kind == "reviews":
                return {
                    "job_id": job.id,
                    "reviews": modules.list_content_reviews(campaign_id, job.module_id),
                }
            chunks = modules.list_chunks(
                campaign_id,
                job.module_id,
                scene_id=(str(data["scene_id"]) if data.get("scene_id") else None),
            )
            query = str(data.get("query") or "").strip().casefold()
            page = data.get("page")
            if page is not None and (
                isinstance(page, bool) or not isinstance(page, int) or page < 1
            ):
                raise ValueError("data.page must be a positive integer")
            if query:
                chunks = [
                    item
                    for item in chunks
                    if query
                    in "\n".join(
                        [
                            *[str(value) for value in item.get("heading_path") or []],
                            str(item.get("content") or ""),
                        ]
                    ).casefold()
                ]
            if page is not None:
                chunks = [
                    item
                    for item in chunks
                    if isinstance(item.get("page_start"), int)
                    and isinstance(item.get("page_end"), int)
                    and int(item["page_start"]) <= page <= int(item["page_end"])
                ]
            limit = data.get("limit", 100)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
                raise ValueError("data.limit must be an integer between 1 and 500")
            source_key = str(dict(job.payload or {}).get("source_key") or job.artifact)
            evidence = []
            for item in chunks[:limit]:
                note_subject = " / ".join(str(value) for value in item.get("heading_path") or [])
                evidence.append(
                    {
                        **deepcopy(item),
                        "source_ref": {
                            "source_key": source_key,
                            "page": item.get("page_start"),
                            "chunk_hash": str(item.get("content_hash") or ""),
                            "note": f"Agent-reviewed source evidence: {note_subject or 'module'}",
                        },
                    }
                )
            return {"job_id": job.id, "evidence": evidence}

        if action == "edit" and str(data.get("operation") or "").strip() == "advance":
            advance_key = str(idempotency_key or "").strip()
            if not advance_key:
                raise ValueError("idempotency_key is required to advance a module draft")
            return advance_module_draft(job, advance_key, principal_id)

        revision, key = require_write_contract(expected_revision, idempotency_key)
        if action == "edit":
            operation = str(data.get("operation") or "").strip()
            if operation == "source_text":
                request = {
                    "operation": "edit_module_source_text",
                    "job_id": job.id,
                    "expected_revision": revision,
                    "data": {
                        field: deepcopy(value)
                        for field, value in data.items()
                        if field not in {"job_id", "operation"}
                    },
                }
                scope = f"module-draft-edit:{campaign_id}:{job.id}:{principal_id}"
                replay = replay_response(scope, key, request)
                if replay is not None:
                    return replay
                if dict(job.result or {}).get("finalized_package") or job.state == "compiled":
                    raise ValueError("a finalized module draft is immutable")
                inspect_scope = f"{scope}:source-text-inspect"
                inspect_key = f"{key}:inspect"
                inspected_replay = replay_response(inspect_scope, inspect_key, request)
                if inspected_replay is None:
                    if job.state not in {"imported", "failed"} or not job.module_id:
                        raise ValueError(
                            "source text review requires an imported or failed PDF draft"
                        )
                    if job.revision != revision:
                        raise ValueError(
                            f"import job revision conflict: expected {revision}, "
                            f"found {job.revision}"
                        )
                    source = storage.artifact_module_path(job.artifact)
                    if source.suffix.casefold() != ".pdf":
                        raise ValueError("source_text review requires a staged PDF")
                    page_number = data.get("page_number")
                    if (
                        isinstance(page_number, bool)
                        or not isinstance(page_number, int)
                        or page_number < 1
                    ):
                        raise ValueError("data.page_number must be a positive integer")
                    rationale = str(data.get("rationale") or "").strip()
                    if not 1 <= len(rationale) <= 2000:
                        raise ValueError("data.rationale must contain 1 to 2000 characters")
                    evidence_basis = str(data.get("evidence_basis") or "")
                    if evidence_basis not in {"agent_context", "rendered_page"}:
                        raise ValueError(
                            "data.evidence_basis must be agent_context or rendered_page"
                        )
                    review_method = str(data.get("review_method") or "agent")
                    if review_method not in {"agent", "human"}:
                        raise ValueError("data.review_method must be agent or human")
                    document = normalize_document(
                        source,
                        cache_dir=config.normalized_modules_dir,
                        expected_checksum=job.artifact_checksum,
                    )
                    current_revisions = import_page_revisions(job)
                    document = apply_document_page_revisions(document, current_revisions)
                    page_text = normalized_document_page_text(document, page_number)
                    base_checksum = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
                    if str(data.get("base_text_sha256") or "") != base_checksum:
                        raise ValueError(
                            "data.base_text_sha256 does not match the current normalized page"
                        )
                    raw_replacements = data.get("replacements")
                    if (
                        not isinstance(raw_replacements, list)
                        or not 1 <= len(raw_replacements) <= 128
                    ):
                        raise ValueError("data.replacements must contain 1 to 128 entries")
                    replacements: list[dict[str, str]] = []
                    empty_recovery = (
                        not page_text.strip()
                        and len(raw_replacements) == 1
                        and isinstance(raw_replacements[0], dict)
                        and str(raw_replacements[0].get("old", "")) == ""
                    )
                    for index, raw in enumerate(raw_replacements):
                        if not isinstance(raw, dict) or set(raw) != {"old", "new"}:
                            raise ValueError(
                                f"data.replacements[{index}] must contain exactly old and new"
                            )
                        old = str(raw["old"])
                        new = str(raw["new"])
                        maximum_new = 50000 if empty_recovery else 500
                        if (
                            (not old and not empty_recovery)
                            or not new
                            or old == new
                            or len(old) > 500
                            or len(new) > maximum_new
                        ):
                            raise ValueError(f"data.replacements[{index}] is invalid")
                        if not empty_recovery and page_text.count(old) != 1:
                            raise ValueError(
                                f"data.replacements[{index}].old must match exactly once"
                            )
                        if evidence_basis == "agent_context":
                            if re.findall(r"\d+", old) != re.findall(r"\d+", new):
                                raise ValueError(
                                    "agent_context transcription repair cannot alter numbers"
                                )
                            old_key = re.sub(r"\s+", "", old).casefold()
                            new_key = re.sub(r"\s+", "", new).casefold()
                            if (
                                not old_key
                                or not new_key
                                or SequenceMatcher(None, old_key, new_key).ratio() < 0.8
                            ):
                                raise ValueError(
                                    "agent_context repair exceeds the bounded text correction"
                                )
                        replacements.append({"old": old, "new": new})
                    evidence: dict[str, Any] = {
                        "basis": evidence_basis,
                        "normalized_text_sha256": base_checksum,
                        "native_text_sha256": hashlib.sha256(
                            extract_pdf_page_text(source, page_number).encode("utf-8")
                        ).hexdigest(),
                    }
                    if evidence_basis == "rendered_page":
                        scale = float(data.get("scale", 1.5))
                        rendered = render_pdf_page(source, page_number, scale=scale)
                        if data.get("rendered_image_checksum") != rendered.checksum:
                            raise ValueError(
                                "data.rendered_image_checksum does not match rendered evidence"
                            )
                        evidence["rendered_image_checksum"] = rendered.checksum
                        evidence["render_scale"] = rendered.scale
                    source_revision = {
                        "source_checksum": job.artifact_checksum,
                        "page_number": page_number,
                        "base_text_sha256": base_checksum,
                        "replacements": replacements,
                        "reviewer": principal_id,
                        "review_method": review_method,
                        "rationale": rationale,
                        "evidence": evidence,
                    }
                    page_revisions = [*current_revisions, source_revision]
                    inspection = modules.preview_path(
                        source,
                        parser=parser,
                        document_cache_dir=config.normalized_modules_dir,
                        expected_checksum=job.artifact_checksum,
                        page_revisions=page_revisions,
                    )
                    inspection["page_revisions"] = deepcopy(page_revisions)
                    job = import_jobs.record_inspection(
                        job.id,
                        inspection,
                        expected_revision=revision,
                        idempotency_key=inspect_key,
                        idempotency_write=IdempotencyWrite(
                            scope=inspect_scope,
                            payload=request,
                            response=lambda value: {"job_id": value.id},
                        ),
                    )
                else:
                    job = require_module_job(campaign_id, str(inspected_replay["job_id"]))
                    inspection = deepcopy(job.inspection)
                    page_revisions = import_page_revisions(job)
                    source_revision = deepcopy(page_revisions[-1])

                validation = {
                    "valid": bool(inspection.get("valid", not inspection.get("errors"))),
                    "errors": list(inspection.get("errors") or []),
                    "warnings": list(inspection.get("warnings") or []),
                }
                validate_scope = f"{scope}:source-text-validate"
                validate_key = f"{key}:validate"
                validated_replay = replay_response(validate_scope, validate_key, request)
                if validated_replay is None:
                    if not validation["valid"]:
                        failed = import_jobs.record_validation(
                            job.id,
                            validation,
                            state="failed",
                            expected_revision=job.revision,
                            idempotency_key=key,
                            idempotency_write=IdempotencyWrite(
                                scope=scope,
                                payload=request,
                                response=lambda value: {
                                    "job": import_job_view(value),
                                    "review": deepcopy(source_revision),
                                    "inspection": deepcopy(inspection),
                                    "validation": deepcopy(validation),
                                    "status": "needs_repair",
                                },
                            ),
                        )
                        return {
                            "job": import_job_view(failed),
                            "review": source_revision,
                            "inspection": inspection,
                            "validation": validation,
                            "status": "needs_repair",
                        }
                    job = import_jobs.record_validation(
                        job.id,
                        validation,
                        expected_revision=job.revision,
                        idempotency_key=validate_key,
                        idempotency_write=IdempotencyWrite(
                            scope=validate_scope,
                            payload=request,
                            response=lambda value: {"job_id": value.id},
                        ),
                    )
                else:
                    job = require_module_job(campaign_id, str(validated_replay["job_id"]))

                ingest_scope = f"{scope}:source-text-ingest"
                ingest_key = f"{key}:ingest"
                ingested_replay = replay_response(ingest_scope, ingest_key, request)
                if ingested_replay is None:
                    values = dict(job.payload or {})
                    imported = modules.ingest_path(
                        campaign_id=campaign_id,
                        path=storage.artifact_module_path(job.artifact),
                        source_key=str(values.get("source_key") or job.artifact),
                        logical_source_key=str(values.get("source_key") or job.artifact),
                        title=str(values.get("title") or job.artifact),
                        parser=parser,
                        activate=False,
                        document_cache_dir=config.normalized_modules_dir,
                        expected_checksum=job.artifact_checksum,
                        page_revisions=page_revisions,
                        idempotency_key=ingest_key,
                        idempotency_write=IdempotencyWrite(
                            scope=ingest_scope,
                            payload=request,
                            response=lambda value: {
                                "module_id": value.module_id,
                                "scenes": value.scenes,
                                "chunks": value.chunks,
                            },
                        ),
                    )
                    mechanical_import = {
                        "module_id": imported.module_id,
                        "scenes": imported.scenes,
                        "chunks": imported.chunks,
                    }
                else:
                    mechanical_import = ingested_replay
                prior = dict(job.result or {})
                result_value = {
                    **prior,
                    "mechanical_import": deepcopy(mechanical_import),
                    "source_text_revisions": deepcopy(page_revisions),
                    "pack_draft": {},
                    "pack_edit_history": [],
                    "content_review_ids": [],
                    "asset_ids": [],
                    "actor_binding_ids": [],
                    "draft_edit_history": [
                        *list(prior.get("draft_edit_history") or []),
                        {
                            "revision": job.revision + 1,
                            "editor": principal_id,
                            "operation": "source_text",
                            "note": str(source_revision["rationale"]),
                            "invalidated_fields": [
                                "pack_draft",
                                "content_reviews",
                                "assets",
                                "actor_bindings",
                            ],
                        },
                    ],
                }
                public = {
                    "review": source_revision,
                    "inspection": inspection,
                    "validation": validation,
                    "module_id": mechanical_import["module_id"],
                    "status": "editing",
                }
                public_response = deepcopy(public)
                updated = import_jobs.record_result(
                    job.id,
                    result_value,
                    state="imported",
                    module_id=str(mechanical_import["module_id"]),
                    expected_revision=job.revision,
                    idempotency_key=key,
                    idempotency_write=IdempotencyWrite(
                        scope=scope,
                        payload=request,
                        response=lambda value: {
                            "job": import_job_view(value),
                            **deepcopy(public_response),
                        },
                    ),
                )
                return {"job": import_job_view(updated), **public}

            if job.state != "imported" or not job.module_id:
                raise ValueError("module edits require a mechanically imported draft")
            if dict(job.result or {}).get("finalized_package"):
                raise ValueError("a finalized module draft is immutable")
            if operation not in {"content", "statblock", "asset", "actor", "package"}:
                raise ValueError(
                    "data.operation must be content, statblock, asset, actor, or package"
                )
            request = {
                "operation": f"edit_module_{operation}",
                "job_id": job.id,
                "expected_revision": revision,
                "data": {
                    field: deepcopy(value)
                    for field, value in data.items()
                    if field not in {"job_id", "operation"}
                },
            }
            scope = f"module-draft-edit:{campaign_id}:{job.id}:{principal_id}"
            replay = replay_response(scope, key, request)
            if replay is not None:
                return replay
            prior = dict(job.result or {})
            edit_record = {
                "revision": job.revision + 1,
                "editor": principal_id,
                "operation": operation,
                "note": str(data.get("note") or data.get("observation") or "").strip(),
            }
            operation_history = [
                *list(prior.get("draft_edit_history") or []),
                edit_record,
            ]

            if operation == "statblock":
                raw_statblock = data.get("statblock")
                if not isinstance(raw_statblock, dict):
                    raise ValueError("data.statblock must be an object")
                statblock = validate_coc7e_statblock(raw_statblock)
                readiness = coc7e_statblock_readiness(statblock)
                service_payload = {
                    "module_id": job.module_id,
                    "scene_id": str(data.get("scene_id") or ""),
                    "content_key": str(data.get("content_key") or ""),
                    "content_kind": "coc7e_statblock",
                    "normalized_content": json.dumps(
                        statblock,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "source_asset_id": data.get("source_asset_id"),
                    "page_number": data.get("page_number"),
                    "source_chunk_ids": list(data.get("source_chunk_ids") or []),
                    "observation": str(data.get("observation") or ""),
                    "metadata": {
                        **dict(data.get("metadata") or {}),
                        "statblock_schema": "sagasmith.coc7e-statblock.v1",
                        "runtime_readiness": readiness,
                    },
                }
                service_scope = f"{scope}:statblock"
                service_key = f"{key}:statblock"
                saved = replay_response(service_scope, service_key, service_payload)
                if saved is None:
                    review = modules.review_content(
                        campaign_id=campaign_id,
                        reviewer=principal_id,
                        idempotency_key=service_key,
                        idempotency_write=IdempotencyWrite(
                            scope=service_scope,
                            payload=service_payload,
                            response=lambda value: {"review": value},
                        ),
                        **service_payload,
                    )
                else:
                    review = dict(saved["review"])
                review_ids = list(prior.get("content_review_ids") or [])
                if review["id"] not in review_ids:
                    review_ids.append(review["id"])
                result_value = {
                    **prior,
                    "content_review_ids": review_ids,
                    "draft_edit_history": operation_history,
                }
                public = {
                    "review": review,
                    "statblock": statblock,
                    "runtime_readiness": readiness,
                }
            elif operation == "content":
                allowed_kinds = {
                    "clue",
                    "handout",
                    "map_transcription",
                    "scenario_table",
                    "tome",
                    "spell",
                    "custom",
                }
                content_kind = str(data.get("content_kind") or "custom").strip()
                if content_kind not in allowed_kinds:
                    raise ValueError(
                        "data.content_kind is not a supported CoC review kind; "
                        "use operation=statblock for CoC statblocks"
                    )
                service_payload = {
                    "module_id": job.module_id,
                    "scene_id": str(data.get("scene_id") or ""),
                    "content_key": str(data.get("content_key") or ""),
                    "content_kind": content_kind,
                    "normalized_content": str(data.get("normalized_content") or ""),
                    "source_asset_id": data.get("source_asset_id"),
                    "page_number": data.get("page_number"),
                    "source_chunk_ids": list(data.get("source_chunk_ids") or []),
                    "observation": str(data.get("observation") or ""),
                    "metadata": dict(data.get("metadata") or {}),
                }
                service_scope = f"{scope}:content"
                service_key = f"{key}:content"
                saved = replay_response(service_scope, service_key, service_payload)
                if saved is None:
                    review = modules.review_content(
                        campaign_id=campaign_id,
                        reviewer=principal_id,
                        idempotency_key=service_key,
                        idempotency_write=IdempotencyWrite(
                            scope=service_scope,
                            payload=service_payload,
                            response=lambda value: {"review": value},
                        ),
                        **service_payload,
                    )
                else:
                    review = dict(saved["review"])
                review_ids = list(prior.get("content_review_ids") or [])
                if review["id"] not in review_ids:
                    review_ids.append(review["id"])
                result_value = {
                    **prior,
                    "content_review_ids": review_ids,
                    "draft_edit_history": operation_history,
                }
                public = {"review": review}
            elif operation == "asset":
                source_path = str(data.get("source_path") or "").strip()
                asset_kind = str(data.get("asset_kind") or "").strip()
                if not source_path or not 1 <= len(asset_kind) <= 80:
                    raise ValueError("asset edit requires source_path and asset_kind")
                scene_id = str(data.get("scene_id") or "").strip() or None
                if scene_id:
                    scene = modules.read_scene(campaign_id, scene_id)
                    if scene["module_id"] != job.module_id:
                        raise ValueError("asset scene_id does not belong to the draft module")
                staged = storage.stage_module_asset(job.module_id, source_path)
                service_payload = {
                    "module_id": job.module_id,
                    "source_checksum": staged["checksum"],
                    "asset_kind": asset_kind,
                    "scene_id": scene_id,
                    "location_key": data.get("location_key"),
                    "title": data.get("title"),
                    "metadata": dict(data.get("metadata") or {}),
                }
                service_scope = f"{scope}:asset"
                service_key = f"{key}:asset"
                saved = replay_response(service_scope, service_key, service_payload)
                if saved is None:
                    asset_metadata = {
                        **service_payload["metadata"],
                        "kind": asset_kind,
                        "asset_kind": asset_kind,
                        "source_name": Path(source_path).name,
                        **({"scene_id": scene_id} if scene_id else {}),
                        **(
                            {"location_key": str(data["location_key"])}
                            if data.get("location_key")
                            else {}
                        ),
                        **({"title": str(data["title"])} if data.get("title") else {}),
                    }
                    asset = modules.register_asset(
                        campaign_id=campaign_id,
                        module_id=job.module_id,
                        source_path=staged["path"],
                        media_type=staged["media_type"],
                        checksum=staged["checksum"],
                        metadata=asset_metadata,
                        idempotency_key=service_key,
                        idempotency_write=IdempotencyWrite(
                            scope=service_scope,
                            payload=service_payload,
                            response=lambda value: {"asset": value},
                        ),
                    )
                else:
                    asset = dict(saved["asset"])
                asset_ids = list(prior.get("asset_ids") or [])
                if asset["id"] not in asset_ids:
                    asset_ids.append(asset["id"])
                result_value = {
                    **prior,
                    "asset_ids": asset_ids,
                    "draft_edit_history": operation_history,
                }
                public = {
                    "asset": asset,
                    "artifact": {
                        field: value for field, value in staged.items() if field != "path"
                    },
                }
            elif operation == "actor":
                character_id = str(data.get("character_id") or "").strip()
                actor_card_id = str(data.get("actor_card_id") or "").strip()
                binding_kind = str(data.get("binding_kind") or "").strip()
                if not character_id or not actor_card_id or not binding_kind:
                    raise ValueError(
                        "actor edit requires character_id, actor_card_id, and binding_kind"
                    )
                character = characters.get(character_id)
                if character.system_id != "coc7e" or character.character_type not in {
                    "investigator",
                    "npc",
                    "creature",
                }:
                    raise ValueError("module actor must be a valid CoC actor")
                binding = modules.bind_actor(
                    campaign_id=campaign_id,
                    module_id=job.module_id,
                    character_id=character_id,
                    actor_card_id=actor_card_id,
                    binding_kind=binding_kind,
                    role=str(data.get("role") or ""),
                    scene_id=(str(data["scene_id"]) if data.get("scene_id") else None),
                    metadata=dict(data.get("metadata") or {}),
                )
                binding_ids = list(prior.get("actor_binding_ids") or [])
                if binding["id"] not in binding_ids:
                    binding_ids.append(binding["id"])
                result_value = {
                    **prior,
                    "actor_binding_ids": binding_ids,
                    "draft_edit_history": operation_history,
                }
                public = {"binding": binding}
            else:
                allowed = {
                    "catalogs",
                    "dependencies",
                    "manifest",
                    "metadata",
                    "narrative",
                    "version",
                }
                decisions = {field: deepcopy(data[field]) for field in allowed if field in data}
                unsupported = sorted(set(data) - allowed - {"job_id", "operation", "note"})
                if unsupported:
                    raise ValueError(
                        "module Pack edit has unsupported fields: " + ", ".join(unsupported)
                    )
                if not decisions:
                    raise ValueError("module Pack edit requires at least one decision field")
                draft = {**dict(prior.get("pack_draft") or {}), **decisions}
                package_history = [
                    *list(prior.get("pack_edit_history") or []),
                    {
                        "revision": job.revision + 1,
                        "editor": principal_id,
                        "note": str(data.get("note") or "").strip(),
                        "fields": sorted(decisions),
                    },
                ]
                result_value = {
                    **prior,
                    "pack_draft": draft,
                    "pack_edit_history": package_history,
                    "draft_edit_history": operation_history,
                }
                public = {"pack_draft": draft}

            public_response = deepcopy(public)
            updated = import_jobs.record_result(
                job.id,
                result_value,
                state="imported",
                module_id=job.module_id,
                expected_revision=revision,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=lambda value: {
                        "job": import_job_view(value),
                        **deepcopy(public_response),
                    },
                ),
            )
            return {"job": import_job_view(updated), **public}

        if action != "finalize":
            raise ValueError(f"unsupported module_draft action: {action}")
        saved = dict(dict(job.result or {}).get("pack_draft") or {})
        final_data = {**saved, **{key: deepcopy(value) for key, value in data.items()}}
        confirmation = final_data.get("confirmation")
        if not isinstance(confirmation, dict) or set(confirmation) != {"confirmed", "note"}:
            raise ValueError("module draft confirmation requires exactly confirmed and note")
        note = str(confirmation.get("note") or "").strip()
        if confirmation.get("confirmed") is not True or not note or len(note) > 2000:
            raise ValueError("the Agent must explicitly confirm finalization with a note")
        request = {
            "operation": "finalize_module_draft",
            "job_id": job.id,
            "expected_revision": revision,
            **{key: value for key, value in final_data.items()},
        }
        scope = f"module-draft-finalize:{campaign_id}:{job.id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        if job.state != "imported" or not job.module_id:
            raise ValueError("module draft must complete mechanical import before finalization")
        metadata = {
            **dict(final_data.get("metadata") or {}),
            "agent_finalization": {"confirmed": True, "reviewer": principal_id, "note": note},
            "authoring_review": {
                "schema_version": 1,
                "draft_kind": "module",
                "draft_revision": job.revision,
                "package_edit_history": deepcopy(
                    dict(job.result or {}).get("pack_edit_history") or []
                ),
            },
        }
        package_id = str(final_data.get("package_id") or final_data.get("id") or "").strip()
        if not package_id:
            raise ValueError("data.package_id is required")
        archive_blobs: dict[str, bytes] = {}
        descriptor = modules.export_content_descriptor(
            campaign_id,
            job.module_id,
            package_id=package_id,
            version=str(final_data.get("version") or "1.0.0"),
            metadata=metadata,
            dependencies=list(final_data.get("dependencies") or []),
            manifest=dict(final_data.get("manifest") or {}),
            catalogs=dict(final_data.get("catalogs") or {}),
            narrative=dict(final_data.get("narrative") or {}),
            asset_loader=storage.read_managed_asset,
            blob_sink=lambda checksum, content: archive_blobs.__setitem__(checksum, content),
        )
        package, blobs = build_module_content_package(descriptor, archive_blobs)
        stored = storage.write_content_archive(package, blobs)
        finalized = {
            "artifact": stored["artifact"],
            "summary": {
                "id": package["id"],
                "version": package["version"],
                "checksum": package["checksum"],
                "scenes": len(package["content"]["scene_atlas"]),
                "actors": len(package["actors"]),
                "assets": len(package["assets"]),
            },
            "confirmation": metadata["agent_finalization"],
        }
        public_finalized = {
            **finalized,
            **({"package": package} if final_data.get("include_package") is True else {}),
        }
        updated = import_jobs.record_result(
            job.id,
            {**dict(job.result or {}), "finalized_package": finalized},
            state="compiled",
            module_id=job.module_id,
            expected_revision=revision,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=lambda value: {"job": import_job_view(value), **public_finalized},
            ),
        )
        return {"job": import_job_view(updated), **public_finalized}

    def _content_pack(
        action: Literal["list", "get", "import", "export", "activate", "deactivate", "remove"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Inspect and manage finalized CoC Module and rules Pack archives."""

        require_dm(campaign_id, principal_id)
        data = dict(data or {})
        if action not in {"list", "get"}:
            require_lobby(campaign_id, f"content_pack({action})")
        if action == "list":
            return {
                "packs": [
                    item
                    for item in modules.list(campaign_id, include_retired=True)
                    if str(item.get("parser_profile") or "") == "content-package"
                ],
                "finalized_drafts": [
                    import_job_handle(item)
                    for kind in ("module", "rulebook")
                    for item in import_jobs.list(campaign_id, kind=kind)
                    if dict(item.result or {}).get("finalized_package")
                ],
                "rule_packs": [asdict(item) for item in rule_packs.list_versions()],
            }
        if action == "get":
            choices = [name for name in ("artifact", "source_path") if data.get(name)]
            if choices:
                if len(choices) != 1:
                    raise ValueError("provide exactly one of data.artifact or data.source_path")
                package, _blobs = storage.read_content_archive(
                    artifact=(str(data["artifact"]) if choices[0] == "artifact" else None),
                    source_path=(data["source_path"] if choices[0] == "source_path" else None),
                )
                return {"package": validate_coc_content_package(package)}
            module_id = str(data.get("module_id") or "").strip()
            if not module_id:
                raise ValueError("data.module_id is required")
            package, _blobs, artifact = module_archive(campaign_id, module_id)
            return {
                "module": next(
                    item
                    for item in modules.list(campaign_id, include_retired=True)
                    if str(item.get("id") or item.get("module_id") or "") == module_id
                ),
                "artifact": artifact,
                **({"package": package} if data.get("include_package") is True else {}),
            }
        if action == "export":
            module_id = str(data.get("module_id") or "").strip()
            if not module_id:
                raise ValueError("data.module_id is required")
            package, _blobs, artifact = module_archive(campaign_id, module_id)
            return {
                **artifact,
                "summary": {
                    "id": package["id"],
                    "version": package["version"],
                    "scenes": len(package["content"]["scene_atlas"]),
                    "actors": len(package["actors"]),
                    "assets": len(package["assets"]),
                },
                **({"package": package} if data.get("include_package") is True else {}),
            }

        revision, key = require_write_contract(expected_revision, idempotency_key)
        if action == "import":
            choices = [name for name in ("artifact", "source_path") if data.get(name)]
            if len(choices) != 1:
                raise ValueError("provide exactly one of data.artifact or data.source_path")
            request = {
                "operation": "import_content_pack",
                "artifact": str(data.get("artifact") or ""),
                "source_path": str(data.get("source_path") or ""),
                "expected_revision": revision,
            }
            scope = f"content-pack-import:{campaign_id}:{principal_id}"
            replay = replay_response(scope, key, request)
            if replay is not None:
                return replay
            require_campaign_revision(campaign_id, revision)
            package, blobs = storage.read_content_archive(
                artifact=(str(data["artifact"]) if choices[0] == "artifact" else None),
                source_path=(data["source_path"] if choices[0] == "source_path" else None),
            )
            package = validate_coc_content_package(package)
            if package["kind"] not in {"module", "core_rules"} or package["system_id"] != "coc7e":
                raise ValueError("content package must be a coc7e module or core_rules Pack")
            managed = storage.write_content_archive(package, blobs)
            assets_by_key = {str(item["asset_key"]): item for item in package["assets"]}
            if package["kind"] == "core_rules":
                assets = assets_by_key
                source_map: dict[str, str] = {}
                existing_sources = {
                    str(item["source_key"]): item for item in rules.sources(system_id="coc7e")
                }
                for source in package["sources"]:
                    source_key = str(source["source_key"])
                    existing = existing_sources.get(source_key)
                    expected_source_checksum = str(
                        dict(source.get("metadata") or {}).get("source_checksum") or ""
                    )
                    if (
                        existing is not None
                        and str(existing["checksum"]) == expected_source_checksum
                    ):
                        source_map[source_key] = str(existing["id"])
                        continue
                    asset = assets[str(source["normalized_document_asset_key"])]
                    imported_source = rules.import_content_source(
                        source,
                        blobs[str(asset["checksum"])],
                        system_id="coc7e",
                    )
                    source_map[source_key] = str(imported_source["source_id"])
                content = dict(package["content"])
                manifest = {
                    "id": package["id"],
                    "version": package["version"],
                    "system_id": "coc7e",
                    "title": str(package["manifest"].get("title") or package["id"]),
                    "namespace": package["id"],
                    "editions": list(content.get("editions") or ["7e"]),
                    "dependencies": [
                        {
                            "id": str(item["id"]),
                            "version": str(item.get("version") or ""),
                            "checksum": str(item.get("checksum") or ""),
                        }
                        for item in package.get("dependencies") or []
                        if str(item.get("kind") or "") in {"addon", "core_rules"}
                    ],
                    "conflicts": list(content.get("conflicts") or []),
                    "capabilities": sorted(
                        {
                            str(item.get("event") or "")
                            for item in content.get("mechanics") or []
                            if str(item.get("event") or "")
                        }
                    ),
                    "native_mechanic_refs": [],
                    "native_provider_locks": [],
                }
                draft = rule_packs.save_draft(
                    manifest=manifest,
                    artifacts=list(content.get("artifacts") or []),
                    mechanics=list(content.get("mechanics") or []),
                    provenance={
                        "content_package_id": package["id"],
                        "content_package_version": package["version"],
                        "content_package_checksum": package["checksum"],
                        "content_archive_artifact": managed["artifact"],
                        "source_map": source_map,
                    },
                )
                installed = rule_packs.install(draft.pack_id, draft.version)
                response = {
                    "pack_id": installed.pack_id,
                    "version": installed.version,
                    "status": installed.status,
                    "source_map": source_map,
                    "artifact": managed,
                    "activated": False,
                }
                return remember_response(scope, key, request, response, campaign_id=campaign_id)
            imported = modules.import_content_package(
                campaign_id,
                package,
                blobs,
                activate=False,
                asset_writer=storage.store_content_module_asset,
            )
            modules.register_asset(
                campaign_id=campaign_id,
                module_id=str(imported["module_id"]),
                source_path=str((config.content_packages_dir / managed["artifact"]).resolve()),
                media_type="application/vnd.sagasmith.content-package+zip",
                checksum=str(managed["archive_checksum"]),
                metadata={
                    "asset_kind": "content_package_archive",
                    "content_package_id": package["id"],
                    "content_package_version": package["version"],
                    "content_package_checksum": package["checksum"],
                    "content_archive_artifact": managed["artifact"],
                },
            )
            actor_map: dict[str, str] = {}
            binding_ids: list[str] = []
            module_key = str(package["sources"][0]["source_key"])
            for actor in package["actors"]:
                bindings = list(actor.get("bindings") or [])
                preset = any(
                    str(binding.get("binding_kind") or "") == "preset_pc" for binding in bindings
                )
                character = characters.import_content_actor(
                    actor,
                    campaign_id=None if preset else campaign_id,
                    assets_by_key=assets_by_key,
                    principal_id=principal_id,
                    idempotency_key=f"{key}:actor:{actor['id']}",
                )
                actor_map[str(actor["id"])] = character.id
                effective = bindings or [
                    {
                        "kind": "module",
                        "module_key": module_key,
                        "binding_kind": "preset_pc" if preset else "cast",
                        "role": "",
                    }
                ]
                for binding in effective:
                    scene_key = str(binding.get("scene_key") or "")
                    saved = modules.bind_actor(
                        campaign_id=campaign_id,
                        module_id=str(imported["module_id"]),
                        character_id=character.id,
                        actor_card_id=str(actor["id"]),
                        binding_kind=str(binding.get("binding_kind") or "cast"),
                        role=str(binding.get("role") or ""),
                        scene_id=(str(imported["scene_map"][scene_key]) if scene_key else None),
                        metadata={
                            **dict(binding.get("metadata") or {}),
                            "content_package_checksum": package["checksum"],
                            "content_actor_version": actor["version"],
                            "content_actor_provenance": deepcopy(actor.get("provenance") or {}),
                        },
                    )
                    binding_ids.append(str(saved["id"]))
            response = {
                **{key: value for key, value in imported.items() if key != "actors"},
                "actor_map": actor_map,
                "actor_binding_ids": binding_ids,
                "artifact": managed,
                "activated": False,
            }
            return remember_response(scope, key, request, response, campaign_id=campaign_id)

        pack_id = str(data.get("pack_id") or "").strip()
        if pack_id:
            version = str(data.get("version") or "").strip()
            if not version:
                raise ValueError("rules Pack management requires data.version")
            request = {
                "operation": f"{action}_rule_pack",
                "pack_id": pack_id,
                "version": version,
                "expected_revision": revision,
            }
            scope = f"content-pack-{action}-rules:{campaign_id}:{principal_id}"
            replay = replay_response(scope, key, request)
            if replay is not None:
                return replay
            require_campaign_revision(campaign_id, revision)
            if action in {"activate", "deactivate"}:
                activation = rule_packs.set_activation(
                    campaign_id,
                    pack_id=pack_id,
                    version=version,
                    enabled=action == "activate",
                    expected_campaign_revision=revision,
                    idempotency_key=key,
                    idempotency_write=IdempotencyWrite(
                        scope=scope,
                        payload=request,
                        response=lambda value: {"activation": asdict(value["activation"])},
                    ),
                )
                return {"activation": asdict(activation)}
            if action == "remove":
                rule_packs.remove_version(pack_id, version)
                response = {"removed": True, "pack_id": pack_id, "version": version}
                return remember_response(scope, key, request, response, campaign_id=campaign_id)
            raise ValueError(f"unsupported rules Pack action: {action}")

        module_id = str(data.get("module_id") or "").strip()
        if not module_id:
            raise ValueError("data.module_id is required")
        if action == "remove":
            remove_request = {
                "operation": "remove_content_module",
                "module_id": module_id,
                "expected_revision": revision,
            }
            remove_scope = f"content-pack-remove:{campaign_id}:{principal_id}"
            replay = replay_response(remove_scope, key, remove_request)
            if replay is not None:
                return replay
        module = next(
            (
                item
                for item in modules.list(campaign_id, include_retired=True)
                if str(item.get("id") or item.get("module_id") or "") == module_id
            ),
            None,
        )
        if module is None:
            raise LookupError(module_id)
        if str(module.get("parser_profile") or "") != "content-package":
            raise ValueError("only a module imported from a finalized Pack may be managed here")
        if action == "activate":
            raw_remaps = data.get("progress_remaps") or []
            if not isinstance(raw_remaps, list):
                raise ValueError("data.progress_remaps must be an array")
            scene_map = {
                str(item.get("stable_key") or ""): str(item.get("scene_id") or "")
                for item in modules.scene_index(campaign_id, module_id=module_id)
            }
            remap_targets: dict[str, str] = {}
            rulings: list[dict[str, str]] = []
            for index, raw in enumerate(raw_remaps):
                if not isinstance(raw, dict) or set(raw) != {
                    "from_scene_id",
                    "to_scene_key",
                    "reason",
                }:
                    raise ValueError(
                        f"progress_remaps[{index}] requires exactly from_scene_id, "
                        "to_scene_key, reason"
                    )
                source_id = str(raw["from_scene_id"]).strip()
                target_key = str(raw["to_scene_key"]).strip()
                reason = str(raw["reason"]).strip()
                if not source_id or target_key not in scene_map or not reason or len(reason) > 1000:
                    raise ValueError(f"progress_remaps[{index}] is not a valid Agent remap")
                if source_id in remap_targets:
                    raise ValueError("progress_remaps contains duplicate from_scene_id values")
                remap_targets[source_id] = scene_map[target_key]
                rulings.append(
                    {
                        "from_scene_id": source_id,
                        "to_scene_key": target_key,
                        "to_scene_id": scene_map[target_key],
                        "reason": reason,
                        "resolver": "agent",
                    }
                )
            request = {
                "operation": "activate_content_module",
                "module_id": module_id,
                "expected_revision": revision,
                "progress_remaps": rulings,
            }
            scope = f"content-pack-activate:{campaign_id}:{principal_id}"
            replay = replay_response(scope, key, request)
            if replay is not None:
                return replay
            require_campaign_revision(campaign_id, revision)
            activation = modules.activate_candidate(
                campaign_id,
                module_id,
                progress_remaps=remap_targets,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=lambda value: {
                        "activation": {
                            **dict(value),
                            "progress_remap_rulings": deepcopy(rulings),
                        }
                    },
                ),
            )
            return {
                "activation": {
                    **dict(activation),
                    "progress_remap_rulings": rulings,
                }
            }
        if action == "deactivate":
            request = {
                "operation": "deactivate_content_module",
                "module_id": module_id,
                "expected_revision": revision,
            }
            scope = f"content-pack-deactivate:{campaign_id}:{principal_id}"
            replay = replay_response(scope, key, request)
            if replay is not None:
                return replay
            require_campaign_revision(campaign_id, revision)
            deactivation = modules.deactivate_candidate(
                campaign_id,
                module_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=lambda value: {"deactivation": dict(value)},
                ),
            )
            return {"deactivation": deactivation}
        if action == "remove":
            if bool(module.get("active")):
                raise ValueError("deactivate or replace the active module before removal")
            require_campaign_revision(campaign_id, revision)
            return modules.delete_candidate(
                campaign_id,
                module_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=remove_scope,
                    payload=remove_request,
                    response=lambda value: dict(value),
                ),
            )
        raise ValueError(f"unsupported content_pack action: {action}")

    @mcp.tool()
    def content_pack(
        action: Literal["list", "get", "import", "export", "activate", "deactivate", "remove"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Inspect and atomically manage finalized CoC Module and rules Pack archives."""

        arguments = {
            "action": action,
            "campaign_id": campaign_id,
            "data": data,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
            "principal_id": principal_id,
        }
        if action == "import":
            with storage.database.transaction():
                return _content_pack(**arguments)
        return _content_pack(**arguments)

    @mcp.tool()
    def module_query(
        action: Literal["list", "index", "current", "progress", "search"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        access.require_campaign(campaign_id, principal_id)
        data = dict(data or {})
        keeper = is_dm(campaign_id, principal_id)
        if action == "list":
            return {"modules": modules.list(campaign_id)}
        if action == "index":
            values = modules.scene_index(campaign_id, module_id=data.get("module_id"))
            return {
                "scenes": values
                if keeper
                else [
                    item for item in values if item["visibility"] in PLAYER_MODULE_VISIBILITY_SCOPES
                ]
            }
        if action == "current":
            value = modules.current_scene(
                campaign_id, scope_id=str(data.get("scope_id") or "party")
            )
            if (
                value
                and not keeper
                and value.get("visibility") not in PLAYER_MODULE_VISIBILITY_SCOPES
            ):
                return {"scene": None}
            return {"scene": value}
        if action == "progress":
            return {
                "progress": modules.scene_progress_index(
                    campaign_id,
                    scope_id=str(data.get("scope_id") or "party"),
                    module_id=data.get("module_id"),
                )
            }
        if not data.get("query"):
            raise ValueError("data.query is required")
        hits = modules.search(
            campaign_id=campaign_id,
            query=str(data["query"]),
            query_hints=COC7E_QUERY_HINTS,
        )
        values = [asdict(item) for item in hits]
        return {
            "hits": values
            if keeper
            else [
                item
                for item in values
                if item.get("metadata", {}).get("visibility") in PLAYER_MODULE_VISIBILITY_SCOPES
            ]
        }

    @mcp.tool()
    def module_change(
        action: Literal["set_progress"],
        campaign_id: str,
        data: dict[str, Any],
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        require_dm(campaign_id, principal_id)
        require_no_active_npc_conversation(campaign_id, "module scene progress")
        scene_id = str(data.get("scene_id") or "")
        if not scene_id:
            raise ValueError("data.scene_id is required")
        return modules.set_scene_progress(
            campaign_id=campaign_id,
            scene_id=scene_id,
            status=str(data.get("status") or "current"),
            progress=data.get("progress"),
            current_location_key=data.get("current_location_key"),
            state=dict(data["state"]) if "state" in data else None,
            scope_id=str(data.get("scope_id") or "party"),
        )

    @mcp.tool()
    def memory_query(
        action: Literal["list", "search"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Read the Keeper's objective, branch-scoped continuity ledger."""

        require_dm(campaign_id, principal_id)
        data = dict(data or {})
        branch_id = readable_branch_id(campaign_id, data.get("branch_id"), principal_id)
        values = (
            memories.search(
                campaign_id,
                str(data.get("query") or " "),
                limit=int(data.get("limit", 8)),
                branch_id=branch_id,
                include_inactive=bool(data.get("include_inactive", False)),
            )
            if action == "search"
            else memories.list(
                campaign_id,
                kind=data.get("kind"),
                branch_id=branch_id,
                include_inactive=bool(data.get("include_inactive", False)),
            )
        )
        return {"memories": [asdict(item) for item in values]}

    @mcp.tool()
    def memory_change(
        action: Literal["add", "upsert", "revise", "commit"],
        campaign_id: str,
        data: dict[str, Any],
        principal_id: str = "system:local",
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Write objective facts or atomically settle one investigation outcome."""

        require_dm(campaign_id, principal_id)
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required for memory writes")
        data = deepcopy(dict(data or {}))
        branch_id = writable_branch_id(campaign_id, data.get("branch_id"))

        if action == "commit":
            event = data.get("event")
            facts = data.get("facts") or []
            actor_knowledge = data.get("actor_knowledge") or []
            snapshot = data.get("snapshot")
            if not isinstance(event, dict):
                raise ValueError("data.event must be an object")
            if not isinstance(facts, list) or not all(isinstance(item, dict) for item in facts):
                raise ValueError("data.facts must be a list of objects")
            if not isinstance(actor_knowledge, list) or not all(
                isinstance(item, dict) for item in actor_knowledge
            ):
                raise ValueError("data.actor_knowledge must be a list of objects")
            if snapshot is not None and not isinstance(snapshot, dict):
                raise ValueError("data.snapshot must be an object")
            event_data = deepcopy(event)
            facts_data = [deepcopy(item) for item in facts]
            knowledge_data = [deepcopy(item) for item in actor_knowledge]
            if not str(event_data.get("summary") or "").strip():
                raise ValueError("data.event.summary is required")
            if (
                str(event_data.get("audience_scope") or "dm") == "actor"
                and not event_data.get("participants")
                and not knowledge_data
            ):
                raise ValueError(
                    "actor-scoped continuity events require participants or actor knowledge"
                )
            request = {
                "action": action,
                "branch_id": branch_id,
                "event": event_data,
                "facts": facts_data,
                "actor_knowledge": knowledge_data,
                "snapshot": snapshot,
            }
            scope = f"continuity-commit:{campaign_id}:{branch_id}:{principal_id}"
            replay = replay_response(scope, key, request)
            if replay is not None:
                return replay
            if expected_revision is not None:
                require_campaign_revision(campaign_id, int(expected_revision))
            current_facts = {
                item.fact_key: item
                for item in memories.list(
                    campaign_id,
                    branch_id=branch_id,
                    include_inactive=True,
                )
            }
            for index, fact in enumerate(facts_data):
                validate_subject_context_fact(
                    kind=fact.get("kind"), subject_ref=fact.get("subject_ref")
                )
                fact_action = str(fact.get("action") or "upsert")
                if fact_action == "upsert" and str(fact.get("fact_key") or "") in current_facts:
                    if not fact.get("expected_revision_id"):
                        raise ValueError(
                            f"data.facts[{index}].expected_revision_id is required "
                            "when upsert revises a fact"
                        )
                if fact_action == "revise" and not fact.get("expected_revision_id"):
                    raise ValueError(
                        f"data.facts[{index}].expected_revision_id is required for revisions"
                    )
            for index, item in enumerate(knowledge_data):
                if str(item.get("action") or "add") == "revise" and not item.get(
                    "expected_revision_id"
                ):
                    raise ValueError(
                        "data.actor_knowledge"
                        f"[{index}].expected_revision_id is required for revisions"
                    )
            return continuity_commits.commit(
                campaign_id,
                event=event_data,
                facts=facts_data,
                actor_knowledge=knowledge_data,
                snapshot=deepcopy(snapshot),
                branch_id=branch_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=lambda result: result,
                ),
            )

        content = str(data.get("content") or "").strip()
        if not content:
            raise ValueError("data.content is required")
        if action in {"add", "upsert"}:
            validate_subject_context_fact(
                kind=data.get("kind") or "fact",
                subject_ref=data.get("subject_ref") or "",
            )
        request = {**data, "action": action, "branch_id": branch_id}
        scope = f"memory-change:{campaign_id}:{branch_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        if expected_revision is not None:
            require_campaign_revision(campaign_id, int(expected_revision))
        atomic_write = IdempotencyWrite(
            scope=scope,
            payload=request,
            response=lambda result: asdict(result),
        )
        if action == "add":
            return asdict(
                memories.add(
                    campaign_id,
                    content=content,
                    kind=str(data.get("kind") or "fact"),
                    subject=str(data.get("subject") or ""),
                    metadata=dict(data.get("metadata") or {}),
                    branch_id=branch_id,
                    fact_key=data.get("fact_key"),
                    subject_ref=str(data.get("subject_ref") or ""),
                    predicate=str(data.get("predicate") or ""),
                    status=str(data.get("status") or "active"),
                    source_event_ids=list(data.get("source_event_ids") or []),
                    importance=int(data.get("importance", 3)),
                    disclosure_scope=data.get("disclosure_scope"),
                    idempotency_key=key,
                    idempotency_write=atomic_write,
                )
            )
        if action == "upsert":
            return asdict(
                memories.upsert(
                    campaign_id,
                    fact_key=str(data.get("fact_key") or ""),
                    content=content,
                    kind=(str(data["kind"]) if data.get("kind") is not None else None),
                    subject=(str(data["subject"]) if data.get("subject") is not None else None),
                    subject_ref=(
                        str(data["subject_ref"]) if data.get("subject_ref") is not None else None
                    ),
                    predicate=(
                        str(data["predicate"]) if data.get("predicate") is not None else None
                    ),
                    metadata=(dict(data["metadata"]) if data.get("metadata") is not None else None),
                    branch_id=branch_id,
                    expected_revision_id=data.get("expected_revision_id"),
                    status=str(data.get("status") or "active"),
                    source_event_ids=(
                        list(data["source_event_ids"])
                        if data.get("source_event_ids") is not None
                        else None
                    ),
                    importance=int(data.get("importance", 3)),
                    disclosure_scope=data.get("disclosure_scope"),
                    idempotency_key=key,
                    idempotency_write=atomic_write,
                )
            )
        return asdict(
            memories.revise(
                str(data.get("memory_id") or ""),
                content=content,
                metadata=(dict(data["metadata"]) if data.get("metadata") is not None else None),
                branch_id=branch_id,
                expected_revision_id=data.get("expected_revision_id"),
                status=data.get("status"),
                source_event_ids=(
                    list(data["source_event_ids"])
                    if data.get("source_event_ids") is not None
                    else None
                ),
                importance=(
                    int(data["importance"]) if data.get("importance") is not None else None
                ),
                disclosure_scope=data.get("disclosure_scope"),
                idempotency_key=key,
                idempotency_write=atomic_write,
            )
        )

    @mcp.tool()
    def campaign_event(
        action: Literal["add", "list"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append or read the branch-visible chronology with explicit audiences."""

        data = deepcopy(dict(data or {}))
        membership = access.require_campaign(campaign_id, principal_id)
        if action == "list":
            branch_id = readable_branch_id(campaign_id, data.get("branch_id"), principal_id)
            actor_id = str(data.get("actor_id") or "").strip() or None
            if actor_id is not None:
                actor_access(campaign_id, actor_id, principal_id)
            audience = "dm" if membership.role in {"owner", "dm"} else "player"
            values = events.list_for_audience(
                campaign_id,
                audience=audience,
                actor_id=actor_id,
                limit=int(data.get("limit", 50)),
                branch_id=branch_id,
            )
            return {"events": [asdict(item) for item in values]}

        require_dm(campaign_id, principal_id)
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required for event writes")
        branch_id = writable_branch_id(campaign_id, data.get("branch_id"))
        summary = str(data.get("summary") or "").strip()
        if not summary:
            raise ValueError("data.summary is required")
        audience_scope = str(data.get("audience_scope") or "dm")
        participants = data.get("participants") or []
        known_by_actor_ids = [str(item) for item in data.get("known_by_actor_ids") or []]
        if audience_scope == "actor" and not participants and not known_by_actor_ids:
            raise ValueError("actor-scoped events require participants or known_by_actor_ids")
        if known_by_actor_ids and (
            not str(data.get("knowledge_key") or "").strip()
            or not str(data.get("knowledge_proposition") or "").strip()
        ):
            raise ValueError(
                "knowledge_key and knowledge_proposition are required for known actors"
            )
        request = {
            "summary": summary,
            "event_type": str(data.get("event_type") or "narrative"),
            "payload": deepcopy(dict(data.get("payload") or {})),
            "audience_scope": audience_scope,
            "participants": deepcopy(participants),
            "known_by_actor_ids": known_by_actor_ids,
            "knowledge_key": data.get("knowledge_key"),
            "knowledge_proposition": data.get("knowledge_proposition"),
            "knowledge_disclosure_scope": str(data.get("knowledge_disclosure_scope") or "owner"),
            "branch_id": branch_id,
        }
        scope = f"campaign-event:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        atomic_write = IdempotencyWrite(
            scope=scope,
            payload=request,
            response=lambda result: (
                {**asdict(result[0]), "actor_knowledge_ids": result[1]}
                if isinstance(result, tuple)
                else {**asdict(result), "actor_knowledge_ids": []}
            ),
        )
        if known_by_actor_ids:
            created, knowledge_ids = events.add_with_actor_knowledge(
                campaign_id,
                summary=summary,
                actor_ids=known_by_actor_ids,
                knowledge_key=str(data["knowledge_key"]),
                proposition=str(data["knowledge_proposition"]),
                event_type=request["event_type"],
                payload=request["payload"],
                audience_scope=audience_scope,
                disclosure_scope=request["knowledge_disclosure_scope"],
                participants=deepcopy(participants),
                branch_id=branch_id,
                idempotency_key=key,
                idempotency_write=atomic_write,
            )
            return {**asdict(created), "actor_knowledge_ids": knowledge_ids}
        created = events.add(
            campaign_id,
            summary=summary,
            event_type=request["event_type"],
            payload=request["payload"],
            audience_scope=audience_scope,
            participants=deepcopy(participants),
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=atomic_write,
        )
        return {**asdict(created), "actor_knowledge_ids": []}

    @mcp.tool()
    def continuity_context(
        campaign_id: str,
        query: str = "",
        actor_id: str | None = None,
        scope_id: str = "party",
        audience: Literal["dm", "player"] = "dm",
        branch_id: str | None = None,
        limit: int = 8,
        budget_chars: int = 12_000,
        related_refs: list[str] | None = None,
        purpose: Literal[
            "actor_turn",
            "audience_render",
            "faction_turn",
            "source_interpretation",
            "bounded_ruling",
        ]
        | None = None,
        subject_ref: str | None = None,
        interlocutor_actor_ids: list[str] | None = None,
        evaluation_target_refs: list[str] | None = None,
        stimulus: dict[str, Any] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Retrieve one audience-safe, branch-scoped investigation context bundle."""

        membership = access.require_campaign(campaign_id, principal_id)
        resolved_branch = readable_branch_id(campaign_id, branch_id, principal_id)
        resolved_scope = readable_scope_id(campaign_id, scope_id, principal_id)
        if membership.role not in {"owner", "dm"}:
            audience = "player"
        if actor_id is not None:
            actor_access(campaign_id, actor_id, principal_id)
        result = continuity.context(
            campaign_id,
            query=str(query or ""),
            actor_id=actor_id,
            scope_id=resolved_scope,
            audience=audience,
            branch_id=resolved_branch,
            limit=int(limit),
            budget_chars=int(budget_chars),
            related_refs=list(related_refs or []),
        )
        if purpose is None:
            return result
        if purpose not in BOUNDED_EVALUATION_PURPOSES:
            raise ValueError(f"unsupported bounded evaluation purpose: {purpose}")
        if purpose in {"actor_turn", "faction_turn", "source_interpretation", "bounded_ruling"}:
            require_dm(campaign_id, principal_id)
        if purpose in {"actor_turn", "audience_render", "faction_turn"} and authoritative_phase(
            campaign_id
        ) not in {PROFILE_PLAY, PROFILE_COMBAT}:
            raise ValueError(f"{purpose} context is available only during Play or Combat")
        normalized_interlocutors = [str(item) for item in interlocutor_actor_ids or []]
        for interlocutor_id in normalized_interlocutors:
            item = characters.get(interlocutor_id)
            if item.campaign_id != campaign_id:
                raise ValueError("bounded context interlocutors must belong to the campaign")
        subject: dict[str, Any]
        resolved_subject_ref = str(subject_ref or "").strip()
        actor_revision = None
        if purpose in {"actor_turn", "audience_render"}:
            if not actor_id:
                raise ValueError(f"actor_id is required for {purpose}")
            actor = characters.get(actor_id)
            if actor.campaign_id != campaign_id:
                raise ValueError("bounded context actor must belong to the campaign")
            if purpose == "actor_turn" and actor.character_type == "investigator":
                raise ValueError("actor_turn cannot replace a human-owned investigator decision")
            if purpose == "audience_render" and audience != "player":
                raise ValueError("audience_render requires audience='player'")
            resolved_subject_ref = f"actor:{actor.id}"
            actor_revision = actor.revision
            subject = {"kind": "actor", "id": actor.id, "name": actor.name}
        elif purpose == "faction_turn":
            if not resolved_subject_ref.startswith("faction:"):
                raise ValueError("faction_turn requires subject_ref='faction:<id>'")
            faction_facts = [
                item
                for item in memories.list(
                    campaign_id,
                    branch_id=resolved_branch,
                    include_inactive=False,
                )
                if item.subject_ref == resolved_subject_ref
            ]
            if not faction_facts:
                raise ValueError(f"{resolved_subject_ref} has no faction_state")
            subject = {
                "kind": "faction",
                "id": resolved_subject_ref.removeprefix("faction:"),
                "name": resolved_subject_ref.removeprefix("faction:"),
            }
        else:
            resolved_subject_ref = resolved_subject_ref or f"campaign:{campaign_id}"
            subject = {"kind": purpose, "id": resolved_subject_ref, "name": resolved_subject_ref}
        target_refs = sorted(
            {
                resolved_subject_ref,
                *(f"actor:{item}" for item in normalized_interlocutors),
                *(str(item) for item in evaluation_target_refs or []),
            }
        )
        context_request = {
            "query": str(query or ""),
            "actor_id": actor_id,
            "scope_id": resolved_scope,
            "audience": audience,
            "branch_id": resolved_branch,
            "limit": int(limit),
            "budget_chars": int(budget_chars),
            "related_refs": list(related_refs or []),
        }
        campaign = campaigns.get(campaign_id)
        bundle_id = f"bounded:{uuid4().hex}"
        issued_ns = time.monotonic_ns()
        receipt_payload = {
            "schema_version": 1,
            "bundle_id": bundle_id,
            "purpose": purpose,
            "campaign_id": campaign_id,
            "branch_id": resolved_branch,
            "campaign_revision": campaign.revision,
            "actor_revision": actor_revision,
            "subject_ref": resolved_subject_ref,
            "principal_fingerprint": principal_fingerprint(principal_id),
            "allowed_basis_refs": [],
            "allowed_claim_basis_refs": [],
            "allowed_target_refs": target_refs,
            "context_request": context_request,
            "context_digest": bounded_context_digest(result),
            "issued_monotonic_ns": issued_ns,
            "expires_monotonic_ns": issued_ns + bounded_receipt_ttl_ns,
        }
        return {
            "schema_version": 1,
            "bundle_id": bundle_id,
            "purpose": purpose,
            "subject": subject,
            "stimulus": deepcopy(stimulus),
            "context": result,
            "constraints": {
                "allowed_basis_refs": [],
                "allowed_target_refs": target_refs,
                "may_roll_dice": False,
                "may_call_tools": False,
                "may_write_state": False,
                "output_contract": BOUNDED_OUTPUT_CONTRACTS[purpose],
            },
            "delegation": {
                "schema_version": 1,
                "task": f"propose_{purpose}",
                "execution": "awaited_fresh_context",
                "inherit_agent_history": False,
                "tools_exposed": False,
                "persist_worker_session": False,
                "authoritative_result": False,
            },
            "bundle_receipt": sign_receipt(receipt_payload, bounded_receipt_secret),
        }

    @mcp.tool()
    def bounded_evaluation(
        action: Literal["validate"],
        campaign_id: str,
        proposal: dict[str, Any],
        bundle_receipt: dict[str, Any],
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Validate one tool-free semantic proposal without changing authoritative state."""

        if action != "validate":
            raise ValueError("bounded_evaluation action must be validate")
        membership = access.require_campaign(campaign_id, principal_id)
        receipt = verify_receipt_signature(
            bundle_receipt,
            bounded_receipt_secret,
            missing_error="bounded evaluation bundle_receipt is required",
            invalid_error="bounded evaluation bundle_receipt signature is invalid",
        )
        purpose = str(receipt.get("purpose") or "")
        if receipt.get("schema_version") != 1 or purpose not in BOUNDED_EVALUATION_PURPOSES:
            raise ValueError("bounded evaluation receipt has the wrong purpose or schema")
        if str(receipt.get("campaign_id") or "") != campaign_id:
            raise ValueError("bounded evaluation receipt belongs to another campaign")
        if receipt.get("principal_fingerprint") != principal_fingerprint(principal_id):
            raise ValueError("bounded evaluation receipt belongs to another principal")
        if membership.role not in {"owner", "dm"} and purpose != "audience_render":
            raise PermissionError("only the Keeper may validate this bounded purpose")
        if time.monotonic_ns() > int(receipt.get("expires_monotonic_ns") or 0):
            raise ValueError("bounded evaluation receipt expired; read continuity_context again")
        campaign = campaigns.get(campaign_id)
        if campaign.revision != int(receipt.get("campaign_revision", -1)):
            raise ValueError("bounded evaluation receipt is stale at the campaign revision")
        if current_branch_id(campaign_id) != str(receipt.get("branch_id") or ""):
            raise ValueError("bounded evaluation receipt is stale after branch change")
        subject_ref_value = str(receipt.get("subject_ref") or "")
        if subject_ref_value.startswith("actor:"):
            actor = characters.get(subject_ref_value.removeprefix("actor:"))
            expected_actor_revision = receipt.get("actor_revision")
            if expected_actor_revision is not None and actor.revision != int(
                expected_actor_revision
            ):
                raise ValueError("bounded evaluation receipt is stale at the actor revision")
        context_request = dict(receipt.get("context_request") or {})
        refreshed = continuity.context(
            campaign_id,
            query=str(context_request.get("query") or ""),
            actor_id=context_request.get("actor_id"),
            scope_id=str(context_request.get("scope_id") or "party"),
            audience=str(context_request.get("audience") or "dm"),
            branch_id=str(context_request.get("branch_id") or ""),
            limit=int(context_request.get("limit", 8)),
            budget_chars=int(context_request.get("budget_chars", 12_000)),
            related_refs=list(context_request.get("related_refs") or []),
        )
        if bounded_context_digest(refreshed) != str(receipt.get("context_digest") or ""):
            raise ValueError("bounded evaluation receipt is stale after continuity changed")
        normalized = normalize_bounded_proposal(purpose, proposal)
        if normalized["bundle_id"] != str(receipt.get("bundle_id") or ""):
            raise ValueError("bounded proposal does not match its signed bundle")
        validate_bounded_proposal_refs(
            normalized,
            subject_ref=subject_ref_value,
            allowed_basis_refs={str(item) for item in receipt.get("allowed_basis_refs") or []},
            allowed_claim_basis_refs={
                str(item) for item in receipt.get("allowed_claim_basis_refs") or []
            },
            allowed_target_refs={str(item) for item in receipt.get("allowed_target_refs") or []},
        )
        response = {
            "validated": True,
            "authoritative_state_changed": False,
            "purpose": purpose,
            "proposal": normalized,
            "validation_receipt": sign_receipt(
                {
                    "schema_version": 1,
                    "bundle_id": normalized["bundle_id"],
                    "purpose": purpose,
                    "campaign_id": campaign_id,
                    "principal_fingerprint": principal_fingerprint(principal_id),
                    "proposal_digest": bounded_context_digest(normalized),
                    "validated_at_ns": time.time_ns(),
                },
                bounded_receipt_secret,
            ),
        }
        if purpose == "audience_render":
            response["publication"] = {
                "text": normalized["text"],
                "cited_basis_refs": list(normalized.get("cited_basis_refs") or []),
            }
        return response

    def npc_actor_visible_transcript(
        transcript: list[dict[str, Any]], actor_id: str
    ) -> list[dict[str, Any]]:
        projected: list[dict[str, Any]] = []
        for raw in transcript:
            item = deepcopy(raw)
            overall = dict(item.get("audience_facts") or {})
            segments = list(item.get("utterance_segments") or [])
            segment_facts = list(item.get("segment_audience_facts") or [])
            visible_parts: list[str] = []
            if segments and len(segments) == len(segment_facts):
                visible_segments = []
                for segment, facts in zip(segments, segment_facts, strict=True):
                    understood = set(facts.get("understood_actor_ids") or [])
                    perceived = set(facts.get("perceived_actor_ids") or [])
                    partial = dict(facts.get("partial_renditions") or {})
                    if actor_id in understood:
                        text = str(segment.get("text") or "")
                    elif actor_id in partial:
                        text = str(partial[actor_id])
                    elif actor_id in perceived:
                        text = "[Perceived but not understood]"
                    else:
                        continue
                    visible_parts.append(text)
                    visible_segments.append({"text": text})
                item["utterance_segments"] = visible_segments
            else:
                understood = set(overall.get("understood_actor_ids") or [])
                perceived = set(overall.get("perceived_actor_ids") or [])
                partial = dict(overall.get("partial_renditions") or {})
                if actor_id in understood:
                    visible_parts.append(str(item.get("content") or ""))
                elif actor_id in partial:
                    visible_parts.append(str(partial[actor_id]))
                elif actor_id in perceived:
                    visible_parts.append("[Perceived but not understood]")
            content = " ".join(part for part in visible_parts if part).strip()
            if not content:
                continue
            item["content"] = content
            item.pop("audience_facts", None)
            item.pop("segment_audience_facts", None)
            projected.append(item)
        return projected

    def npc_actor_continuity(context: dict[str, Any], actor_id: str) -> dict[str, Any]:
        projected = deepcopy(context)
        actor_ref = f"actor:{actor_id}"
        projected["facts"] = [
            item
            for item in list(projected.get("facts") or [])
            if item.get("disclosure_scope") == "public"
            or (
                item.get("subject_ref") == actor_ref
                and item.get("kind") == "actor_state"
                and item.get("predicate") in {"relationship_to", "goal", "commitment"}
            )
        ]
        visible_events = []
        for event in list(projected.get("events") or []):
            participants = {
                str(item.get("actor_id") or "") for item in event.get("participants") or []
            }
            if event.get("event_type") == "npc_conversation":
                transcript = npc_actor_visible_transcript(
                    list(dict(event.get("payload") or {}).get("transcript") or []),
                    actor_id,
                )
                if not transcript:
                    continue
                event = deepcopy(event)
                event["payload"] = {
                    "schema_version": 1,
                    "transcript": transcript,
                }
                event["retrieval_text"] = "\n".join(
                    str(item.get("content") or "") for item in transcript
                )
                visible_events.append(event)
            elif actor_id in participants or event.get("audience_scope") in {"party", "public"}:
                visible_events.append(event)
        projected["events"] = visible_events
        projected["module_evidence"] = [
            {
                **dict(item),
                "context_role": "keeper_portrayal_context",
                "disclosure_policy": "not_speakable_without_actor_basis",
            }
            for item in list(projected.get("module_evidence") or [])
        ]
        return projected

    def npc_conversation_context_lock(
        campaign_id: str,
        branch_id: str,
        actor_id: str,
    ) -> dict[str, dict[str, str]]:
        actor_facts = memories.list_for_subject_refs(
            campaign_id,
            subject_refs={f"actor:{actor_id}"},
            predicates={"relationship_to", "goal", "commitment"},
            kinds={"actor_state"},
            branch_id=branch_id,
        )
        actor_knowledge = knowledge.list(
            campaign_id,
            actor_id=actor_id,
            branch_id=branch_id,
            include_inactive=False,
        )
        return {
            "fact_heads": {item.id: item.revision_id for item in actor_facts},
            "knowledge_heads": {item.id: item.revision_id for item in actor_knowledge},
        }

    def npc_private_context(
        campaign_id: str,
        branch_id: str,
        scope_id: str,
        actor: Any,
        participant_ids: list[str],
        query: str,
    ) -> dict[str, Any]:
        context_request = {
            "query": query,
            "actor_id": actor.id,
            "scope_id": scope_id,
            "audience": "dm",
            "branch_id": branch_id,
            "limit": 12,
            "budget_chars": 16_000,
            "related_refs": [f"actor:{item}" for item in participant_ids],
        }
        context = npc_actor_continuity(
            continuity.context(campaign_id, **context_request),
            actor.id,
        )
        commitments = [
            asdict(item)
            for item in memories.list_for_subject_refs(
                campaign_id,
                subject_refs={f"actor:{actor.id}"},
                predicates={"commitment"},
                kinds={"actor_state"},
                branch_id=branch_id,
            )
        ]
        existing_fact_ids = {str(item.get("id") or "") for item in context["facts"]}
        context["facts"].extend(
            item for item in commitments if str(item.get("id") or "") not in existing_fact_ids
        )
        allowed_basis_refs = sorted(
            {
                *(
                    f"fact:{item['id']}:{item['revision_id']}"
                    for item in context["facts"]
                    if item.get("id") and item.get("revision_id")
                ),
                *(
                    f"knowledge:{item['id']}:{item['revision_id']}"
                    for item in context.get("actor_knowledge") or []
                    if item.get("id") and item.get("revision_id")
                ),
                *(
                    f"event:{item['id']}"
                    for item in context.get("events") or []
                    if item.get("id")
                ),
            }
        )
        bundle = {
            "schema_version": 1,
            "purpose": "npc_conversation",
            "authority": {
                "campaign_id": campaign_id,
                "branch_id": branch_id,
                "actor_revision": actor.revision,
            },
            "actor": {
                "id": actor.id,
                "name": actor.name,
                "character_type": actor.character_type,
                "summary": actor.summary,
            },
            "continuity": context,
            "constraints": {
                "allowed_basis_refs": allowed_basis_refs,
                "allowed_target_actor_ids": participant_ids,
                "may_call_tools": False,
                "may_roll_dice": False,
                "may_write_state": False,
                "output_contract": "npc-conversation-proposal.v4",
            },
            "delegation": {
                "schema_version": 1,
                "task": "propose_npc_conversation_turn",
                "execution": "persistent_actor_worker",
                "inherit_agent_history": False,
                "tools_exposed": False,
                "persist_worker_session": True,
                "authoritative_result": False,
            },
            "context_request": context_request,
        }
        return bundle

    def npc_conversation_require_fresh(session: dict[str, Any]) -> None:
        if session.get("status") == "stale":
            raise ValueError("SESSION_STALE: abort and open a fresh NPC conversation")
        if session.get("status") != "open":
            raise ValueError(f"conversation is not open: {session.get('status')}")
        campaign_id = str(session["campaign_id"])
        reasons: list[str] = []
        if current_branch_id(campaign_id) != str(session["branch_id"]):
            reasons.append("branch")
        if authoritative_phase(campaign_id) != PROFILE_PLAY:
            reasons.append("phase")
        campaign = campaigns.get(campaign_id)
        if dict(campaign.state.get("combat") or {}).get("active"):
            reasons.append("combat")
        if dict(campaign.state.get("chase") or {}).get("active"):
            reasons.append("chase")
        authority = deepcopy(dict(session.get("authority") or {}))
        expected_revisions = dict(authority.get("actor_revisions") or {})
        refreshed_actor_ids: list[str] = []
        for actor_id, runtime in dict(session.get("actor_runtimes") or {}).items():
            try:
                actor = characters.get(str(actor_id))
            except LookupError:
                reasons.append(f"actor:{actor_id}:missing")
                continue
            lock = dict(dict(authority.get("actor_context_locks") or {}).get(actor_id) or {})
            current_lock = npc_conversation_context_lock(
                campaign_id,
                str(session["branch_id"]),
                str(actor_id),
            )
            fact_changed = dict(lock.get("fact_heads") or {}) != current_lock["fact_heads"]
            knowledge_changed = (
                dict(lock.get("knowledge_heads") or {}) != current_lock["knowledge_heads"]
            )
            if (
                actor.revision != int(expected_revisions.get(actor_id, -1))
                or fact_changed
                or knowledge_changed
            ):
                old_runtime_id = str(runtime["actor_runtime_id"])
                request = dict(dict(runtime.get("context") or {}).get("context_request") or {})
                runtime["context"] = npc_private_context(
                    campaign_id,
                    str(session["branch_id"]),
                    str(session["scope_id"]),
                    actor,
                    list(session["participant_ids"]),
                    str(request.get("query") or ""),
                )
                runtime["actor_runtime_id"] = (
                    f"{session['conversation_id']}:{actor_id}:r{actor.revision}:"
                    f"c{int(session['conversation_revision']) + 1}"
                )
                runtime["working_state_revision"] = int(
                    runtime.get("working_state_revision", 0)
                ) + 1
                for activation in session["activations"].values():
                    if activation["actor_id"] == actor_id and activation["status"] in {
                        "pending",
                        "claimed",
                    }:
                        activation["status"] = "invalidated"
                        activation["lease"] = None
                invalidated_activation_ids = {
                    str(activation["activation_id"])
                    for activation in session["activations"].values()
                    if activation["actor_id"] == actor_id
                    and str(activation.get("actor_runtime_id") or "") == old_runtime_id
                }
                for publication in session.get("publications") or []:
                    if (
                        publication.get("status") == "pending_audience"
                        and str(publication.get("activation_id") or "")
                        in invalidated_activation_ids
                    ):
                        publication["status"] = "invalidated"
                for resolution in session.get("pending_resolutions") or []:
                    if (
                        resolution.get("status") == "pending"
                        and str(resolution.get("activation_id") or "")
                        in invalidated_activation_ids
                    ):
                        resolution["status"] = "invalidated"
                for candidate in session.get("memory_candidates") or []:
                    if (
                        str(candidate.get("source_activation_id") or "")
                        in invalidated_activation_ids
                        and not candidate.get("source_event_id")
                    ):
                        candidate["status"] = "invalidated"
                authority["actor_revisions"][actor_id] = actor.revision
                authority.setdefault("actor_context_locks", {})[actor_id] = current_lock
                refreshed_actor_ids.append(str(actor_id))
        if reasons:
            session["status"] = "stale"
            session["stale_reasons"] = sorted(set(reasons))
            session["updated_at_ns"] = time.time_ns()
            npc_conversations.save(session)
            raise ValueError(
                "SESSION_STALE: authoritative state changed: " + ", ".join(session["stale_reasons"])
            )
        if refreshed_actor_ids:
            session["authority"] = authority
            session["refreshed_actor_ids"] = refreshed_actor_ids
            session["conversation_revision"] = int(session["conversation_revision"]) + 1
            session["updated_at_ns"] = time.time_ns()
            npc_conversations.save(session)

    def npc_conversation_status(session: dict[str, Any]) -> dict[str, Any]:
        result = npc_conversations.public_status(session)
        result["activations"] = npc_conversations.list_activations(session)
        result["pending_publications"] = [
            {key: deepcopy(value) for key, value in item.items() if key not in {"speaker_actor_id"}}
            for item in session.get("publications") or []
            if item.get("status") == "pending_audience"
        ]
        result["pending_resolutions"] = [
            deepcopy(item)
            for item in session.get("pending_resolutions") or []
            if item.get("status") == "pending"
        ]
        result["memory_candidates"] = npc_conversations.memory_candidates(session)
        result["refreshed_actor_ids"] = list(session.get("refreshed_actor_ids") or [])
        if session.get("status") == "stale":
            result["stale_reasons"] = list(session.get("stale_reasons") or [])
        return result

    def close_npc_conversation(
        session: dict[str, Any],
        accepted_candidate_ids: list[str],
        expected_revision: int,
        idempotency_key: str,
        principal_id: str,
    ) -> dict[str, Any]:
        session, replay = npc_conversations.begin_mutation(
            str(session["conversation_id"]),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            operation="close",
            payload={"accepted_candidate_ids": accepted_candidate_ids},
        )
        if replay is not None:
            return replay
        npc_conversation_require_fresh(session)
        if any(
            item.get("status") in {"pending", "claimed"} for item in session["activations"].values()
        ):
            raise ValueError("conversation has unfinished NPC activations")
        if any(
            item.get("status") == "pending_audience" for item in session.get("publications") or []
        ):
            raise ValueError("conversation has unpublished NPC output")
        if any(
            item.get("status") == "pending" for item in session.get("pending_resolutions") or []
        ):
            raise ValueError("conversation has unresolved mechanic requests")
        candidate_ids = list(accepted_candidate_ids)
        if (
            any(not isinstance(item, str) or not item for item in candidate_ids)
            or len(candidate_ids) != len(set(candidate_ids))
        ):
            raise ValueError("accepted_candidate_ids must be a unique non-empty string list")
        candidates = {
            str(item["candidate_id"]): item
            for item in npc_conversations.memory_candidates(session)
        }
        if unknown := sorted(set(candidate_ids) - set(candidates)):
            raise ValueError(f"accepted_candidate_ids contains unavailable candidates: {unknown}")
        facts: list[dict[str, Any]] = []
        actor_knowledge: list[dict[str, Any]] = []
        accepted_commitments: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            candidate = candidates[candidate_id]
            if candidate["status"] != "available":
                raise ValueError(f"memory candidate is not available: {candidate_id}")
            actor_id = str(candidate["actor_id"])
            value = deepcopy(candidate["value"])
            if candidate["kind"] == "fact":
                facts.append(value)
            elif candidate["kind"] == "actor_knowledge":
                actor_knowledge.append(value)
            elif candidate["kind"] == "commitment":
                commitment = value
                facts.append(
                    {
                        "action": "upsert",
                        "fact_key": (f"actor:{actor_id}:commitment:{commitment['commitment_key']}"),
                        "content": str(commitment["content"]),
                        "kind": "actor_state",
                        "subject": actor_id,
                        "subject_ref": f"actor:{actor_id}",
                        "predicate": "commitment",
                        "metadata": dict(commitment.get("metadata") or {}),
                        "importance": int(commitment.get("importance", 3)),
                        "disclosure_scope": "dm",
                    }
                )
                accepted_commitments.append(deepcopy(commitment))
            else:
                raise ValueError(f"unsupported memory candidate kind: {candidate['kind']}")
        participants = set(session["participant_ids"])
        current_facts = {
            item.fact_key: item
            for item in memories.list(
                str(session["campaign_id"]),
                branch_id=str(session["branch_id"]),
                include_inactive=True,
            )
        }
        for fact in facts:
            if str(fact.get("subject_ref") or "").removeprefix("actor:") not in participants:
                raise ValueError("accepted conversation fact belongs outside conversation")
            if str(fact.get("kind") or "") != "actor_state" or str(
                fact.get("predicate") or ""
            ) not in {"relationship_to", "goal", "commitment"}:
                raise ValueError("accepted conversation facts must be actor-state continuity")
            fact["disclosure_scope"] = "dm"
            current = current_facts.get(str(fact.get("fact_key") or ""))
            if current is not None and str(fact.get("action") or "upsert") == "upsert":
                fact.setdefault("expected_revision_id", current.revision_id)
        for item in actor_knowledge:
            if str(item.get("actor_id") or "") not in participants:
                raise ValueError("accepted ActorKnowledge belongs outside conversation")
            item["disclosure_scope"] = str(item.get("disclosure_scope") or "dm")
        transcript = [
            {
                key: deepcopy(event[key])
                for key in (
                    "event_id",
                    "sequence",
                    "type",
                    "speaker_actor_id",
                    "content",
                    "language",
                    "delivery",
                    "declared_target_actor_ids",
                    "publication_id",
                    "utterance_segments",
                    "visible_cues",
                    "visible_action",
                    "resolved_resolution_ids",
                    "audience_facts",
                    "segment_audience_facts",
                )
                if key in event
            }
            for event in session["events"]
        ]
        names = {item["actor_id"]: item["name"] for item in session["participants"]}
        retrieval_lines = []
        for item in transcript[-12:]:
            content = str(item.get("content") or "").strip()
            if content:
                retrieval_lines.append(
                    f"{names.get(str(item.get('speaker_actor_id')), 'Scene')}: {content[:300]}"
                )
        event = {
            "event_type": "npc_conversation",
            "summary": (
                f"Conversation among {', '.join(names.values())}; {len(transcript)} public events."
            ),
            "retrieval_text": "\n".join(retrieval_lines),
            "audience_scope": "dm",
            "participants": [
                {"actor_id": actor_id, "role": "witness"} for actor_id in session["participant_ids"]
            ],
            "payload": {
                "schema_version": 2,
                "conversation_id": session["conversation_id"],
                "scope_id": session["scope_id"],
                "transcript": transcript,
                "accepted_commitments": accepted_commitments,
                "authority_at_open": deepcopy(session["authority"]),
            },
        }
        request = {
            "action": "close_npc_conversation",
            "branch_id": session["branch_id"],
            "event": event,
            "facts": facts,
            "actor_knowledge": actor_knowledge,
        }
        commit = continuity_commits.commit(
            str(session["campaign_id"]),
            event=event,
            facts=facts,
            actor_knowledge=actor_knowledge,
            snapshot=None,
            branch_id=str(session["branch_id"]),
            idempotency_key=idempotency_key,
            idempotency_write=IdempotencyWrite(
                scope=(
                    f"npc-conversation-close:{session['campaign_id']}:"
                    f"{session['branch_id']}:{principal_id}"
                ),
                payload=request,
                response=lambda result: result,
            ),
        )
        session["status"] = "closed"
        for runtime in session["actor_runtimes"].values():
            runtime["status"] = "closed"
            runtime["context"] = {}
        return npc_conversations.finish_mutation(session, commit)

    @mcp.tool()
    def npc_conversation(
        action: Literal["open", "list", "get", "ingest", "publish", "close", "abort"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Run isolated, persistent per-NPC dialogue with explicit Agent audience rulings."""

        require_dm(campaign_id, principal_id)
        data = dict(data or {})
        if action == "list":
            branch_id = current_branch_id(campaign_id)
            values = npc_conversations.active_public_statuses(
                campaign_id=campaign_id,
                branch_id=branch_id,
                principal_id=principal_id,
            )
            return {"campaign_id": campaign_id, "branch_id": branch_id, "conversations": values}
        if action == "open":
            if authoritative_phase(campaign_id) != PROFILE_PLAY:
                raise ValueError("NPC conversations may open only during play")
            campaign = campaigns.get(campaign_id)
            if dict(campaign.state.get("combat") or {}).get("active") or dict(
                campaign.state.get("chase") or {}
            ).get("active"):
                raise ValueError("end active combat or chase before opening a conversation")
            participant_ids = [
                str(item).strip() for item in data.get("participant_actor_ids") or []
            ]
            if not participant_ids or len(participant_ids) > 20:
                raise ValueError("participant_actor_ids must contain 1 to 20 actors")
            if len(participant_ids) != len(set(participant_ids)) or any(
                not item for item in participant_ids
            ):
                raise ValueError("participant_actor_ids must contain unique actor ids")
            actors = [characters.get(item) for item in participant_ids]
            if any(item.campaign_id != campaign_id for item in actors):
                raise ValueError("all conversation participants must belong to campaign")
            npc_actors = [
                item for item in actors if item.character_type in {"npc", "creature", "monster"}
            ]
            if not npc_actors:
                raise ValueError("conversation requires at least one NPC or creature")
            branch_id = writable_branch_id(campaign_id, data.get("branch_id"))
            scope_id = str(data.get("scope_id") or "party")
            contexts: dict[str, dict[str, Any]] = {}
            for actor in npc_actors:
                context = npc_private_context(
                    campaign_id,
                    branch_id,
                    scope_id,
                    actor,
                    participant_ids,
                    str(data.get("query") or ""),
                )
                contexts[actor.id] = context
            return npc_conversations.open(
                campaign_id=campaign_id,
                branch_id=branch_id,
                principal_id=principal_id,
                scope_id=scope_id,
                scene_id=str(data.get("scene_id") or ""),
                authority={
                    "campaign_revision": campaign.revision,
                    "actor_revisions": {item.id: item.revision for item in actors},
                    "actor_context_locks": {
                        item.id: npc_conversation_context_lock(
                            campaign_id,
                            branch_id,
                            item.id,
                        )
                        for item in npc_actors
                    },
                    "principal_fingerprint": principal_fingerprint(principal_id),
                },
                participants=[
                    {
                        "actor_id": item.id,
                        "name": item.name,
                        "kind": item.character_type,
                        "npc_runtime": item.id in contexts,
                    }
                    for item in actors
                ],
                actor_contexts=contexts,
                idempotency_key=str(data.get("idempotency_key") or ""),
            )
        conversation_id = str(data.get("conversation_id") or "")
        session = npc_conversations.require_owner(
            conversation_id,
            campaign_id=campaign_id,
            principal_id=principal_id,
        )
        if action == "get":
            if session.get("status") == "open":
                npc_conversation_require_fresh(session)
            return npc_conversation_status(session)
        if action == "ingest":
            npc_conversation_require_fresh(session)
            event = dict(data.get("event") or {})
            allowed = {
                "type",
                "speaker_actor_id",
                "content",
                "language",
                "delivery",
                "declared_target_actor_ids",
                "resolved_resolution_ids",
            }
            if unknown := set(event) - allowed:
                raise ValueError(f"conversation event has unknown fields: {unknown}")
            event_type = str(event.get("type") or "speech")
            if event_type not in {"speech", "action", "scene_prompt", "resolution"}:
                raise ValueError("unsupported conversation event type")
            speaker = str(event.get("speaker_actor_id") or "")
            if speaker and speaker not in session["participant_ids"]:
                raise ValueError("conversation speaker must be a participant")
            if event_type in {"speech", "action"} and not speaker:
                raise ValueError("speech and action events require a speaker")
            content = str(event.get("content") or "").strip()
            if not content or len(content) > 6_000:
                raise ValueError("conversation event content must contain 1 to 6000 characters")
            targets = [str(item) for item in event.get("declared_target_actor_ids") or []]
            if len(targets) != len(set(targets)) or any(
                item not in session["participant_ids"] for item in targets
            ):
                raise ValueError("declared targets must be unique conversation participants")
            resolved = [str(item) for item in event.get("resolved_resolution_ids") or []]
            if event_type == "resolution" and not resolved:
                raise ValueError("resolution events require resolved_resolution_ids")
            if event_type != "resolution" and resolved:
                raise ValueError("only resolution events may resolve requests")
            audience = normalize_audience_facts(
                data.get("audience_facts"),
                participant_ids=set(session["participant_ids"]),
                response_actor_ids=set(session["actor_runtimes"]),
            )
            return npc_conversations.append_event(
                session,
                event={
                    "type": event_type,
                    "speaker_actor_id": speaker,
                    "content": content,
                    "language": str(event.get("language") or ""),
                    "delivery": str(event.get("delivery") or ""),
                    "declared_target_actor_ids": targets,
                    "resolved_resolution_ids": resolved,
                },
                audience_facts=audience,
                expected_revision=int(data["expected_conversation_revision"]),
                idempotency_key=str(data.get("idempotency_key") or ""),
            )
        if action == "publish":
            npc_conversation_require_fresh(session)
            audience = normalize_audience_facts(
                data.get("audience_facts"),
                participant_ids=set(session["participant_ids"]),
                response_actor_ids=set(session["actor_runtimes"]),
            )
            segment_audience = [
                normalize_audience_facts(
                    item,
                    participant_ids=set(session["participant_ids"]),
                    response_actor_ids=set(session["actor_runtimes"]),
                )
                for item in data.get("segment_audience_facts") or []
            ]
            return npc_conversations.publish(
                session,
                publication_id=str(data.get("publication_id") or ""),
                audience_facts=audience,
                segment_audience_facts=segment_audience,
                expected_revision=int(data["expected_conversation_revision"]),
                idempotency_key=str(data.get("idempotency_key") or ""),
            )
        if action == "close":
            allowed = {
                "conversation_id",
                "expected_conversation_revision",
                "accepted_candidate_ids",
                "idempotency_key",
            }
            if unknown := sorted(set(data) - allowed):
                raise ValueError(f"npc_conversation close has unknown fields: {unknown}")
            accepted_candidate_ids = data.get("accepted_candidate_ids") or []
            if not isinstance(accepted_candidate_ids, list):
                raise ValueError("accepted_candidate_ids must be a list")
            return close_npc_conversation(
                session,
                accepted_candidate_ids,
                int(data["expected_conversation_revision"]),
                str(data.get("idempotency_key") or ""),
                principal_id,
            )
        if action == "abort":
            if session.get("status") == "closed":
                raise ValueError("a committed conversation cannot be aborted")
            session, replay = npc_conversations.begin_mutation(
                conversation_id,
                expected_revision=int(data["expected_conversation_revision"]),
                idempotency_key=str(data.get("idempotency_key") or ""),
                operation="abort",
                payload={"reason": str(data.get("reason") or "")},
            )
            if replay is not None:
                return replay
            session["status"] = "aborted"
            session["abort_reason"] = str(data.get("reason") or "")
            for runtime in session["actor_runtimes"].values():
                runtime["context"] = {}
            for candidate in session.get("memory_candidates") or []:
                candidate["status"] = "invalidated"
            return npc_conversations.finish_mutation(
                session,
                {
                    "conversation_id": conversation_id,
                    "status": "aborted",
                    "recoverable": False,
                },
            )
        raise ValueError(f"unsupported npc_conversation action: {action}")

    @mcp.tool()
    def npc_conversation_transport(
        campaign_id: str,
        conversation_id: str,
        action: Literal["claim_activation", "submit_proposal", "cancel_activation"],
        payload: dict[str, Any],
        host_token: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Host-private activation transport; never appears in MCP tools/list."""

        if not config.npc_host_token or not secrets.compare_digest(
            host_token, config.npc_host_token
        ):
            raise PermissionError("NPC host transport authentication failed")
        require_dm(campaign_id, principal_id)
        session = npc_conversations.require_owner(
            conversation_id,
            campaign_id=campaign_id,
            principal_id=principal_id,
        )
        npc_conversation_require_fresh(session)
        data = dict(payload or {})
        common = {
            "activation_ref": str(data.get("activation_ref") or ""),
            "expected_revision": int(data["expected_conversation_revision"]),
            "idempotency_key": str(data.get("idempotency_key") or ""),
        }
        if action == "claim_activation":
            return npc_conversations.checkout(
                session,
                cursor=int(data.get("cursor", 0)),
                include_bootstrap=bool(data.get("include_bootstrap", True)),
                **common,
            )
        if action == "submit_proposal":
            return npc_conversations.submit(
                session,
                lease_id=str(data.get("lease_id") or ""),
                proposal=dict(data.get("proposal") or {}),
                **common,
            )
        if action == "cancel_activation":
            return npc_conversations.cancel_activation(
                session,
                lease_id=str(data.get("lease_id") or ""),
                **common,
            )
        raise ValueError(f"unsupported NPC host transport action: {action}")

    @mcp.tool()
    def actor_knowledge_query(
        action: Literal["list", "search"],
        campaign_id: str,
        actor_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        actor_access(campaign_id, actor_id, principal_id)
        data = dict(data or {})
        branch_id = readable_branch_id(campaign_id, data.get("branch_id"), principal_id)
        values = (
            knowledge.search(
                campaign_id,
                actor_id=actor_id,
                query=str(data.get("query") or " "),
                branch_id=branch_id,
                limit=int(data.get("limit", 8)),
                include_inactive=bool(data.get("include_inactive", False)),
            )
            if action == "search"
            else knowledge.list(
                campaign_id,
                actor_id=actor_id,
                branch_id=branch_id,
                include_inactive=bool(data.get("include_inactive", False)),
            )
        )
        if not is_dm(campaign_id, principal_id):
            values = [item for item in values if item.disclosure_scope in {"owner", "public"}]
        return {"knowledge": [asdict(item) for item in values]}

    @mcp.tool()
    def actor_knowledge_change(
        action: Literal["add", "revise"],
        campaign_id: str,
        actor_id: str,
        data: dict[str, Any],
        principal_id: str = "system:local",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        require_dm(campaign_id, principal_id)
        actor_access(campaign_id, actor_id, principal_id)
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required for actor knowledge writes")
        data = deepcopy(dict(data or {}))
        branch_id = writable_branch_id(campaign_id, data.get("branch_id"))
        request = {**data, "action": action, "actor_id": actor_id, "branch_id": branch_id}
        scope = f"actor-knowledge:{campaign_id}:{branch_id}:{principal_id}:{actor_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        atomic_write = IdempotencyWrite(
            scope=scope,
            payload=request,
            response=lambda result: asdict(result),
        )
        if action == "add":
            return asdict(
                knowledge.add(
                    campaign_id,
                    actor_id=actor_id,
                    knowledge_key=str(data.get("knowledge_key") or ""),
                    proposition=str(data.get("proposition") or ""),
                    subject_ref=str(data.get("subject_ref") or ""),
                    epistemic_status=str(data.get("epistemic_status") or "known"),
                    confidence=int(data.get("confidence", 3)),
                    source_event_id=data.get("source_event_id"),
                    cause=str(data.get("cause") or "witnessed"),
                    disclosure_scope=str(data.get("disclosure_scope") or "dm"),
                    branch_id=branch_id,
                    idempotency_key=key,
                    idempotency_write=atomic_write,
                )
            )
        item = knowledge.get(str(data.get("knowledge_id") or ""), branch_id=branch_id)
        if item.actor_id != actor_id:
            raise PermissionError("knowledge item belongs to another actor")
        if not data.get("expected_revision_id"):
            raise ValueError("data.expected_revision_id is required for knowledge revisions")
        return asdict(
            knowledge.revise(
                item.id,
                proposition=str(data.get("proposition") or ""),
                epistemic_status=str(data.get("epistemic_status") or "known"),
                confidence=int(data.get("confidence", 3)),
                source_event_id=data.get("source_event_id"),
                cause=str(data.get("cause") or "told_by"),
                disclosure_scope=str(data.get("disclosure_scope") or "dm"),
                branch_id=branch_id,
                expected_revision_id=str(data["expected_revision_id"]),
                idempotency_key=key,
                idempotency_write=atomic_write,
            )
        )

    @mcp.tool()
    def branch_query(
        action: Literal["current", "list", "get", "compare"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Inspect campaign timelines without changing the checked-out branch."""

        membership = access.require_campaign(campaign_id, principal_id)
        data = dict(data or {})
        if action == "current":
            return {"branch": asdict(branches.current(campaign_id))}
        if action == "list":
            values = [asdict(item) for item in branches.list(campaign_id)]
            if membership.role not in {"owner", "dm"}:
                current = current_branch_id(campaign_id)
                values = [item for item in values if item["id"] == current]
            return {"branches": values}
        if action == "get":
            branch_id = str(data.get("branch_id") or "")
            if not branch_id:
                raise ValueError("data.branch_id is required")
            if membership.role not in {"owner", "dm"} and branch_id != current_branch_id(
                campaign_id
            ):
                raise PermissionError("players may inspect only the current branch")
            return {"branch": asdict(branches.get(campaign_id, branch_id))}
        require_dm(campaign_id, principal_id)
        left_branch_id = str(data.get("left_branch_id") or "")
        right_branch_id = str(data.get("right_branch_id") or "")
        if not left_branch_id or not right_branch_id:
            raise ValueError("data.left_branch_id and data.right_branch_id are required")
        return {"comparison": branches.compare(campaign_id, left_branch_id, right_branch_id)}

    @mcp.tool()
    def branch_change(
        action: Literal["create", "checkout"],
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Create or checkout a timeline under campaign and active-branch guards."""

        require_dm(campaign_id, principal_id)
        key = str(idempotency_key or "").strip()
        branch_guard = str(expected_branch_id or "").strip()
        if not key or not branch_guard:
            raise ValueError("expected_branch_id and idempotency_key are required")
        data = dict(data or {})
        payload = {
            "action": action,
            "data": data,
            "expected_revision": int(expected_revision),
            "expected_branch_id": branch_guard,
        }
        scope = f"branch-change:{campaign_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, payload)
        if replay is not None:
            return replay
        require_campaign_revision(campaign_id, int(expected_revision))
        require_active_branch(campaign_id, branch_guard)
        if action == "checkout" or (action == "create" and bool(data.get("checkout", False))):
            require_no_active_npc_conversation(campaign_id, "branch checkout")
        if action == "create":
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("data.name is required")
            created = branches.create(
                campaign_id,
                name=name,
                from_snapshot_id=(
                    str(data["from_snapshot_id"]) if data.get("from_snapshot_id") else None
                ),
                checkout=bool(data.get("checkout", False)),
                expected_revision=int(expected_revision),
                expected_branch_id=branch_guard,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=payload,
                    response=lambda value: {
                        "branch": asdict(value["branch"]),
                        "campaign_revision": int(expected_revision) + 1,
                        "snapshot": (
                            asdict(value["snapshot"]) if value["snapshot"] is not None else None
                        ),
                    },
                ),
            )
            return {
                "branch": asdict(created),
                "campaign_revision": campaigns.get(campaign_id).revision,
                "snapshot": (
                    asdict(
                        next(
                            item
                            for item in snapshots.list(campaign_id)
                            if item.id == created.head_snapshot_id
                        )
                    )
                    if bool(data.get("checkout", False)) and created.head_snapshot_id
                    else None
                ),
            }
        branch_id = str(data.get("branch_id") or "").strip()
        if not branch_id:
            raise ValueError("data.branch_id is required")
        checked_out = branches.checkout(
            campaign_id,
            branch_id,
            expected_revision=int(expected_revision),
            expected_branch_id=branch_guard,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=payload,
                response=lambda value: {
                    "branch": asdict(value["branch"]),
                    "campaign_revision": int(expected_revision) + int(branch_id != branch_guard),
                    "snapshot": (
                        asdict(value["snapshot"]) if value["snapshot"] is not None else None
                    ),
                },
            ),
        )
        snapshot = (
            next(
                item
                for item in snapshots.list(campaign_id)
                if item.id == checked_out.head_snapshot_id
            )
            if checked_out.head_snapshot_id
            else None
        )
        return {
            "branch": asdict(checked_out),
            "campaign_revision": campaigns.get(campaign_id).revision,
            "snapshot": asdict(snapshot) if snapshot is not None else None,
        }

    @mcp.tool()
    def snapshot_query(
        action: Literal["list", "get", "verify", "lineage"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Read Keeper-only save history, payloads, integrity, and lineage."""

        require_dm(campaign_id, principal_id)
        data = dict(data or {})
        if action == "list":
            return {"snapshots": [asdict(item) for item in snapshots.list(campaign_id)]}
        if action == "lineage":
            slot = int(data["slot"]) if data.get("slot") is not None else None
            return {
                "snapshots": [asdict(item) for item in snapshots.lineage(campaign_id, slot=slot)]
            }
        if "slot" not in data:
            raise ValueError("data.slot is required")
        slot = int(data["slot"])
        if action == "verify":
            return {
                "campaign_id": campaign_id,
                "slot": slot,
                "valid": snapshots.verify(campaign_id, slot),
            }
        return {"snapshot": snapshots.get(campaign_id, slot)}

    @mcp.tool()
    def snapshot_change(
        action: Literal["create", "restore"],
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Create or restore an immutable save under revision and branch guards."""

        require_dm(campaign_id, principal_id)
        key = str(idempotency_key or "").strip()
        branch_guard = str(expected_branch_id or "").strip()
        if not key or not branch_guard:
            raise ValueError("expected_branch_id and idempotency_key are required")
        data = dict(data or {})
        payload = {
            "action": action,
            "data": data,
            "expected_revision": int(expected_revision),
            "expected_branch_id": branch_guard,
        }
        scope = f"snapshot-change:{campaign_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, payload)
        if replay is not None:
            return replay
        require_campaign_revision(campaign_id, int(expected_revision))
        require_active_branch(campaign_id, branch_guard)
        if action == "restore":
            require_no_active_npc_conversation(campaign_id, "snapshot restore")
        if action == "create":
            if "expected_head_snapshot_id" not in data:
                raise ValueError("data.expected_head_snapshot_id is required")
            expected_head = str(data.get("expected_head_snapshot_id") or "")
            actual_head = str(branches.current(campaign_id).head_snapshot_id or "")
            if actual_head != expected_head:
                raise ValueError(
                    "branch head conflict: "
                    f"expected {expected_head or '<none>'}, found {actual_head or '<none>'}"
                )
            return asdict(
                snapshots.create(
                    campaign_id,
                    label=str(data.get("label") or ""),
                    idempotency_key=key,
                    idempotency_write=IdempotencyWrite(
                        scope=scope,
                        payload=payload,
                        response=lambda value: asdict(value),
                    ),
                )
            )
        if "slot" not in data:
            raise ValueError("data.slot is required")
        return asdict(
            snapshots.restore(
                campaign_id,
                int(data["slot"]),
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=payload,
                    response=lambda value: asdict(value),
                ),
            )
        )

    @mcp.tool()
    def state_revision(
        action: Literal["history", "receipt", "undo", "redo"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Read the branch revision ledger or perform guarded undo and redo."""

        require_dm(campaign_id, principal_id)
        data = dict(data or {})
        if action == "history":
            return {
                "revisions": [
                    asdict(item)
                    for item in revisions.history(campaign_id, limit=int(data.get("limit", 100)))
                ]
            }
        if action == "receipt":
            receipt_key = str(data.get("idempotency_key") or "").strip()
            if not receipt_key:
                raise ValueError("data.idempotency_key is required")
            return {
                "receipt": asdict(
                    idempotency.receipt(
                        campaign_id,
                        receipt_key,
                        branch_id=(str(data["branch_id"]) if data.get("branch_id") else None),
                    )
                )
            }
        require_no_active_npc_conversation(campaign_id, f"state revision {action}")
        key = str(idempotency_key or "").strip()
        if "expected_history_sequence" not in data or not key:
            raise ValueError("data.expected_history_sequence and idempotency_key are required")
        expected_cursor = int(data["expected_history_sequence"])
        branch_id = current_branch_id(campaign_id)
        payload = {
            "action": action,
            "expected_history_sequence": expected_cursor,
            "branch_id": branch_id,
        }
        scope = f"state-revision:{campaign_id}:{branch_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, payload)
        if replay is not None:
            return replay
        actual_cursor = history_cursor(campaign_id)
        if actual_cursor != expected_cursor:
            raise ValueError(
                f"history cursor conflict: expected {expected_cursor}, found {actual_cursor}"
            )
        method = revisions.undo if action == "undo" else revisions.redo
        return asdict(
            method(
                campaign_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=payload,
                    response=lambda value: asdict(value),
                ),
            )
        )

    @mcp.tool()
    def coc_dice_roll(
        kind: Literal["d100", "expression"],
        campaign_id: str,
        expected_revision: int,
        idempotency_key: str,
        expression: str | None = None,
        bonus_dice: int = 0,
        penalty_dice: int = 0,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Roll from the campaign stream and atomically persist its receipt."""

        payload = {
            "kind": kind,
            "expression": expression,
            "bonus_dice": bonus_dice,
            "penalty_dice": penalty_dice,
        }

        def resolve() -> dict[str, Any]:
            if kind == "d100":
                return roll_d100(bonus_dice=bonus_dice, penalty_dice=penalty_dice)
            if not str(expression or "").strip():
                raise ValueError("expression is required for expression rolls")
            return roll_dice_expression(str(expression))

        return authoritative_random_resolution(
            campaign_id=campaign_id,
            principal_id=principal_id,
            operation="coc_dice_roll",
            payload=payload,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            resolve=resolve,
        )

    @mcp.tool()
    def development_query(
        campaign_id: str,
        actor_id: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Read the checked skills awaiting end-of-session development."""

        require_lobby(campaign_id, "development query")
        actor_access(campaign_id, actor_id, principal_id)
        campaign = campaigns.get(campaign_id)
        actor = characters.get(actor_id)
        sheet = validate_investigator_sheet(dict(actor.sheet))
        return {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision,
            "actor_id": actor_id,
            "character_revision": actor.revision,
            "pending": query_development(sheet),
        }

    @mcp.tool()
    def development_settle(
        campaign_id: str,
        actor_id: str,
        source: str,
        expected_revision: int,
        expected_character_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Atomically roll every checked skill and clear the check marks."""

        actor_access(campaign_id, actor_id, principal_id, control=True)
        source_value = " ".join(str(source or "").split()).strip()
        if not source_value or len(source_value) > 500:
            raise ValueError("source must contain 1 to 500 characters")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "source": source_value,
            "expected_revision": int(expected_revision),
            "expected_character_revision": int(expected_character_revision),
            "branch_id": branch_id,
        }
        scope = f"development-settle:{campaign_id}:{branch_id}:{actor_id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        require_lobby(campaign_id, "development settlement")
        campaign = campaigns.get(campaign_id)
        actor = characters.get(actor_id)
        if campaign.revision != int(expected_revision):
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        if actor.revision != int(expected_character_revision):
            raise ValueError(
                "character revision conflict: "
                f"expected {expected_character_revision}, found {actor.revision}"
            )
        sheet = validate_investigator_sheet(dict(actor.sheet))
        stream = CampaignRandomStream.from_campaign_state(
            campaign_id,
            campaign.state,
            operation="development_settle",
            idempotency_key=key,
        )
        with use_random_stream(stream):
            next_sheet, receipt = settle_development(
                sheet,
                source=source_value,
                actor_id=actor_id,
            )
        next_state = {
            **dict(campaign.state),
            "random_stream": stream.persisted_state(),
        }
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "actor_id": actor_id,
            "character_revision": actor.revision + 1,
            "receipt": deepcopy(receipt),
            "random_stream_receipt": stream.receipt(),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_investigator_sheet(next_sheet),
                    notes=dict(actor.notes),
                    expected_revision=actor.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="coc.development.settle",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        stream.mark_persisted()
        return response

    def group_luck_context(campaign_id: str, actor_ids: list[str]) -> dict[str, Any]:
        normalized = [str(item).strip() for item in actor_ids if str(item).strip()]
        if len(normalized) != len(actor_ids):
            raise ValueError("participant actor ids must not be empty")
        participants = []
        for actor_id in normalized:
            actor = characters.get(actor_id)
            if actor.campaign_id != campaign_id:
                raise ValueError("every group Luck participant must belong to the campaign")
            sheet = validate_investigator_sheet(dict(actor.sheet))
            participants.append(
                {"actor_id": actor_id, "name": actor.name, "luck": int(sheet["luck"])}
            )
        return {**group_luck_candidates(participants), "participants": participants}

    @mcp.tool()
    def group_luck_query(
        campaign_id: str,
        participant_actor_ids: list[str],
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Identify the lowest-Luck investigator(s) present in the scene."""

        require_dm(campaign_id, principal_id)
        campaign = require_investigation_play(campaign_id)
        context = group_luck_context(campaign_id, participant_actor_ids)
        return {"campaign_id": campaign_id, "campaign_revision": campaign.revision, **context}

    @mcp.tool()
    def group_luck_check(
        campaign_id: str,
        participant_actor_ids: list[str],
        source: str,
        goal: str,
        expected_revision: int,
        idempotency_key: str,
        selected_actor_id: str | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Roll once using the lowest current Luck among scene participants."""

        require_dm(campaign_id, principal_id)
        source_value = " ".join(str(source or "").split()).strip()
        goal_value = " ".join(str(goal or "").split()).strip()
        if not source_value or len(source_value) > 500:
            raise ValueError("source must contain 1 to 500 characters")
        if not goal_value or len(goal_value) > 500:
            raise ValueError("goal must contain 1 to 500 characters")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "campaign_id": campaign_id,
            "participant_actor_ids": [str(item) for item in participant_actor_ids],
            "source": source_value,
            "goal": goal_value,
            "expected_revision": int(expected_revision),
            "selected_actor_id": selected_actor_id,
            "branch_id": branch_id,
        }
        scope = f"group-luck-check:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_investigation_play(campaign_id)
        if campaign.revision != int(expected_revision):
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        context = group_luck_context(campaign_id, participant_actor_ids)
        candidates = list(context["candidate_actor_ids"])
        selected = str(selected_actor_id or "").strip()
        if len(candidates) > 1 and not selected:
            raise ValueError(
                "selected_actor_id is required for tied lowest Luck candidates: "
                + ", ".join(candidates)
            )
        if not selected:
            selected = candidates[0]
        if selected not in candidates:
            raise ValueError("selected_actor_id must be one of the lowest-Luck candidates")
        stream = CampaignRandomStream.from_campaign_state(
            campaign_id,
            campaign.state,
            operation="group_luck_check",
            idempotency_key=key,
        )
        with use_random_stream(stream):
            roll = roll_d100()
            outcome = resolve_skill_check(
                int(roll["total"]),
                int(context["lowest_luck"]),
                roll_kind="luck",
                skill_name="Group Luck",
            )
        history = list(campaign.state.get("group_luck_rolls") or [])
        receipt = {
            "sequence": len(history) + 1,
            "source": source_value,
            "goal": goal_value,
            "participant_actor_ids": [item["actor_id"] for item in context["participants"]],
            "candidate_actor_ids": candidates,
            "selected_actor_id": selected,
            "threshold": int(context["lowest_luck"]),
            "roll": roll,
            "outcome": outcome,
        }
        next_state = {
            **dict(campaign.state),
            "group_luck_rolls": [*history[-499:], receipt],
            "random_stream": stream.persisted_state(),
        }
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "receipt": deepcopy(receipt),
            "random_stream_receipt": stream.receipt(),
            "continuity_required": True,
            "continuity_instruction": (
                "Use memory_change(action='commit') for the Agent-decided group consequence, "
                "audience, and resulting knowledge."
            ),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            expected_campaign_revision=campaign.revision,
            operation="coc.group_luck.check",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        stream.mark_persisted()
        return response

    @mcp.tool()
    def investigation_query(
        campaign_id: str,
        actor_id: str,
        view: Literal["pending", "history"] = "pending",
        limit: int = 20,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Read one actor's pending or settled investigation checks."""

        actor_access(campaign_id, actor_id, principal_id)
        campaign = campaigns.get(campaign_id)
        ledger = investigation_ledger(dict(campaign.state))
        if view == "pending":
            pending = deepcopy(dict(ledger["pending"]).get(actor_id))
            if pending is not None:
                pending["available_actions"] = investigation_actions(campaign, pending)
            return {
                "campaign_id": campaign_id,
                "campaign_revision": campaign.revision,
                "actor_id": actor_id,
                "pending": pending,
            }
        values = [
            deepcopy(item)
            for item in list(ledger["history"])
            if str(dict(item).get("actor_id") or "") == actor_id
        ]
        return {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision,
            "actor_id": actor_id,
            "history": values[-max(1, min(int(limit), 100)) :],
        }

    @mcp.tool()
    def investigation_check(
        action: Literal["open", "spend_luck", "push", "settle", "abort"],
        campaign_id: str,
        actor_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_character_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Run a resumable source-explicit check with human Luck/Push choices."""

        actor_access(campaign_id, actor_id, principal_id, control=True)
        campaign = campaigns.get(campaign_id)
        actor = characters.get(actor_id)
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        data = deepcopy(dict(data or {}))
        branch_id = current_branch_id(campaign_id)
        request = {
            "action": action,
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "data": data,
            "expected_revision": int(expected_revision),
            "expected_character_revision": int(expected_character_revision),
            "branch_id": branch_id,
        }
        scope = f"investigation-check:{campaign_id}:{branch_id}:{actor_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_investigation_play(campaign_id)
        if campaign.revision != int(expected_revision):
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        if actor.revision != int(expected_character_revision):
            raise ValueError(
                "character revision conflict: "
                f"expected {expected_character_revision}, found {actor.revision}"
            )
        sheet = validate_investigator_sheet(dict(actor.sheet))
        ledger = investigation_ledger(dict(campaign.state))
        pending_by_actor = dict(ledger["pending"])
        pending = deepcopy(pending_by_actor.get(actor_id))

        if action == "open":
            if pending is not None:
                raise ValueError("actor already has a pending investigation check")
            source = " ".join(str(data.get("source") or "").split()).strip()
            goal = " ".join(str(data.get("goal") or "").split()).strip()
            if not source or len(source) > 500:
                raise ValueError("data.source must contain 1 to 500 characters")
            if not goal or len(goal) > 500:
                raise ValueError("data.goal must contain 1 to 500 characters")
            check_id = hashlib.sha256(
                f"{campaign_id}:{branch_id}:{actor_id}:{key}".encode()
            ).hexdigest()[:24]
            stream = CampaignRandomStream.from_campaign_state(
                campaign_id,
                campaign.state,
                operation="investigation_check.open",
                idempotency_key=key,
            )
            with use_random_stream(stream):
                resolution = resolve_investigation_check(
                    sheet,
                    data,
                    investigator_name=actor.name,
                )
            stream_receipt = stream.receipt()
            pending = {
                "id": check_id,
                "thread_id": check_id,
                "event_sequence": 1,
                "operation": "investigation_check.open",
                "status": "pending",
                "actor_id": actor_id,
                "audience": {
                    "scope": "actors",
                    "actor_refs": [actor_id],
                    "disclosure": "private",
                },
                "branch_id": branch_id,
                "campaign_revision": campaign.revision + 1,
                "actor_revision": actor.revision,
                "source": source,
                "goal": goal,
                **resolution,
                "decision": None,
                "random_stream_receipt": stream_receipt,
            }
            pending_by_actor[actor_id] = pending
            next_ledger = {**ledger, "pending": pending_by_actor}
            next_state = {
                **dict(campaign.state),
                "investigation_checks": next_ledger,
                "random_stream": stream.persisted_state(),
            }
            response = {
                "campaign_id": campaign_id,
                "campaign_revision": campaign.revision + 1,
                "character_revision": actor.revision,
                "resolution_id": check_id,
                "thread_id": check_id,
                "event_sequence": 1,
                "pending": {
                    **deepcopy(pending),
                    "available_actions": investigation_actions(campaign, pending),
                },
                "random_stream_receipt": stream_receipt,
            }
            StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=next_state,
                expected_campaign_revision=campaign.revision,
                operation="coc.investigation.check.open",
                actor=principal_id,
                branch_id=branch_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=response,
                ),
            )
            stream.mark_persisted()
            return response

        if pending is None:
            raise ValueError("actor has no pending investigation check")
        if str(data.get("check_id") or "") != str(pending["id"]):
            raise ValueError("data.check_id does not match the pending check")
        if int(pending["actor_revision"]) != actor.revision:
            raise ValueError("actor changed while the investigation check was pending")

        if action == "spend_luck":
            if not bool(campaign.settings.get("spending_luck", False)):
                raise ValueError("this campaign has not enabled optional Spending Luck")
            if "spend_luck" not in investigation_actions(campaign, pending):
                raise ValueError("the pending check cannot spend Luck")
            spent = int(data.get("luck_spent", 0))
            luck_transition = spend_luck_on_investigation(
                sheet,
                pending,
                spent,
                investigator_name=actor.name,
            )
            next_sheet = dict(luck_transition["sheet"])
            outcome = dict(luck_transition["outcome"])
            pending = {
                **pending,
                "event_sequence": int(pending.get("event_sequence") or 1) + 1,
                "operation": "investigation_check.spend_luck",
                "campaign_revision": campaign.revision + 1,
                "actor_revision": actor.revision + 1,
                "outcome": outcome,
                "decision": {"kind": "spend_luck", "spent": spent},
            }
            pending_by_actor[actor_id] = pending
            next_state = {
                **dict(campaign.state),
                "investigation_checks": {**ledger, "pending": pending_by_actor},
            }
            response = {
                "campaign_id": campaign_id,
                "campaign_revision": campaign.revision + 1,
                "character_revision": actor.revision + 1,
                "resolution_id": pending["id"],
                "thread_id": str(pending.get("thread_id") or pending["id"]),
                "event_sequence": int(pending["event_sequence"]),
                "pending": {**deepcopy(pending), "available_actions": ["settle"]},
            }
            StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=next_state,
                character_updates=[
                    CharacterStateUpdate(
                        character_id=actor_id,
                        sheet=validate_investigator_sheet(next_sheet),
                        notes=dict(actor.notes),
                        expected_revision=actor.revision,
                    )
                ],
                expected_campaign_revision=campaign.revision,
                operation="coc.investigation.check.spend_luck",
                actor=principal_id,
                branch_id=branch_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=response,
                ),
            )
            return response

        if action == "push":
            if "push" not in investigation_actions(campaign, pending):
                raise ValueError("the pending check cannot be pushed")
            justification = " ".join(str(data.get("justification") or "").split()).strip()
            consequence = " ".join(str(data.get("failure_consequence") or "").split()).strip()
            if not justification or len(justification) > 500:
                raise ValueError("data.justification must contain 1 to 500 characters")
            if not consequence or len(consequence) > 500:
                raise ValueError("data.failure_consequence must contain 1 to 500 characters")
            bonus_dice = int(data.get("bonus_dice", pending["bonus_dice"]))
            penalty_dice = int(data.get("penalty_dice", pending["penalty_dice"]))
            stream = CampaignRandomStream.from_campaign_state(
                campaign_id,
                campaign.state,
                operation="investigation_check.push",
                idempotency_key=key,
            )
            with use_random_stream(stream):
                if pending.get("check_kind") == "combined":
                    declaration = {
                        "traits": data.get("traits", pending["traits"]),
                        "requirement": data.get("requirement", pending["requirement"]),
                        "difficulty": data.get("difficulty", "regular"),
                        "bonus_dice": bonus_dice,
                        "penalty_dice": penalty_dice,
                    }
                else:
                    declaration = {
                        "trait_kind": data.get("trait_kind", pending["trait_kind"]),
                        "trait_name": data.get("trait_name", pending["trait_name"]),
                        "difficulty": data.get("difficulty", pending["difficulty"]),
                        "bonus_dice": bonus_dice,
                        "penalty_dice": penalty_dice,
                    }
                resolution = resolve_investigation_check(
                    sheet,
                    declaration,
                    investigator_name=actor.name,
                    pushed=True,
                )
            stream_receipt = stream.receipt()
            pending = {
                **pending,
                "event_sequence": int(pending.get("event_sequence") or 1) + 1,
                "operation": "investigation_check.push",
                "campaign_revision": campaign.revision + 1,
                **resolution,
                "decision": {
                    "kind": "push",
                    "justification": justification,
                    "failure_consequence": consequence,
                },
                "random_stream_receipt": stream_receipt,
            }
            pending_by_actor[actor_id] = pending
            next_state = {
                **dict(campaign.state),
                "investigation_checks": {**ledger, "pending": pending_by_actor},
                "random_stream": stream.persisted_state(),
            }
            response = {
                "campaign_id": campaign_id,
                "campaign_revision": campaign.revision + 1,
                "character_revision": actor.revision,
                "resolution_id": pending["id"],
                "thread_id": str(pending.get("thread_id") or pending["id"]),
                "event_sequence": int(pending["event_sequence"]),
                "pending": {**deepcopy(pending), "available_actions": ["settle"]},
                "random_stream_receipt": stream_receipt,
            }
            StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=next_state,
                expected_campaign_revision=campaign.revision,
                operation="coc.investigation.check.push",
                actor=principal_id,
                branch_id=branch_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=response,
                ),
            )
            stream.mark_persisted()
            return response

        if action == "abort":
            require_dm(campaign_id, principal_id)
            reason = " ".join(str(data.get("reason") or "").split()).strip()
            if not reason or len(reason) > 500:
                raise ValueError("data.reason must contain 1 to 500 characters")
            receipt = {
                **pending,
                "status": "aborted",
                "operation": "investigation_check.abort",
                "event_sequence": int(pending.get("event_sequence") or 1) + 1,
                "campaign_revision": campaign.revision + 1,
                "abort_reason": reason,
                "sequence": len(ledger["history"]) + 1,
            }
            pending_by_actor.pop(actor_id)
            history = [*list(ledger["history"])[-499:], receipt]
            next_state = {
                **dict(campaign.state),
                "investigation_checks": {
                    **ledger,
                    "pending": pending_by_actor,
                    "history": history,
                },
            }
            response = {
                "campaign_id": campaign_id,
                "campaign_revision": campaign.revision + 1,
                "character_revision": actor.revision,
                "resolution_id": receipt["id"],
                "thread_id": str(receipt.get("thread_id") or receipt["id"]),
                "event_sequence": int(receipt["event_sequence"]),
                "receipt": deepcopy(receipt),
            }
            StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=next_state,
                expected_campaign_revision=campaign.revision,
                operation="coc.investigation.check.abort",
                actor=principal_id,
                branch_id=branch_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=response,
                ),
            )
            return response

        outcome = dict(pending["outcome"])
        receipt = {
            **pending,
            "status": "settled",
            "operation": "investigation_check.settle",
            "event_sequence": int(pending.get("event_sequence") or 1) + 1,
            "campaign_revision": campaign.revision + 1,
            "sequence": len(ledger["history"]) + 1,
        }
        pending_by_actor.pop(actor_id)
        history = [*list(ledger["history"])[-499:], receipt]
        next_state = {
            **dict(campaign.state),
            "investigation_checks": {
                **ledger,
                "pending": pending_by_actor,
                "history": history,
            },
        }
        character_updates = []
        next_character_revision = actor.revision
        marked_skills = []
        if pending.get("check_kind") == "combined":
            marked_skills = [
                str(item)
                for item in list(outcome.get("development_eligible_skills") or [])
                if development_skill_eligible(str(item))
            ]
        elif (
            bool(outcome.get("development_eligible"))
            and str(pending["trait_kind"]) == "skill"
            and development_skill_eligible(str(pending["trait_name"]))
        ):
            marked_skills = [str(pending["trait_name"])]
        if marked_skills:
            next_sheet = dict(sheet)
            development = dict(next_sheet.get("development") or {})
            checked = [str(item) for item in development.get("checked_skills") or []]
            additions = [name for name in marked_skills if name not in checked]
            if additions:
                development["checked_skills"] = [*checked, *additions]
                next_sheet["development"] = development
                character_updates = [
                    CharacterStateUpdate(
                        character_id=actor_id,
                        sheet=validate_investigator_sheet(next_sheet),
                        notes=dict(actor.notes),
                        expected_revision=actor.revision,
                    )
                ]
                next_character_revision += 1
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "character_revision": next_character_revision,
            "resolution_id": receipt["id"],
            "thread_id": str(receipt.get("thread_id") or receipt["id"]),
            "event_sequence": int(receipt["event_sequence"]),
            "receipt": deepcopy(receipt),
            "continuity_required": True,
            "continuity_instruction": (
                "Use memory_change(action='commit') to record the Agent-decided clue, "
                "audience, knowledge, consequence, and source-specific facts."
            ),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            character_updates=character_updates,
            expected_campaign_revision=campaign.revision,
            operation="coc.investigation.check.settle",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        return response

    @mcp.tool()
    def coc_sanity_check(
        campaign_id: str,
        actor_id: str,
        success_loss: str,
        failure_loss: str,
        source: str,
        context: Literal["real_time", "summary"],
        expected_revision: int,
        expected_character_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Roll, settle, and audit one source-explicit SAN encounter atomically."""

        actor_access(campaign_id, actor_id, principal_id, control=True)
        source_value = " ".join(str(source or "").split()).strip()
        if not source_value or len(source_value) > 500:
            raise ValueError("source must contain 1 to 500 characters")
        if context not in {"real_time", "summary"}:
            raise ValueError("context must be real_time or summary")
        formulas = {
            "success": str(success_loss or "").strip(),
            "failure": str(failure_loss or "").strip(),
        }
        if not all(formulas.values()) or any(len(value) > 100 for value in formulas.values()):
            raise ValueError("success_loss and failure_loss must contain 1 to 100 characters")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": "coc_sanity_check",
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "success_loss": formulas["success"],
            "failure_loss": formulas["failure"],
            "source": source_value,
            "context": context,
            "expected_revision": int(expected_revision),
            "expected_character_revision": int(expected_character_revision),
            "branch_id": branch_id,
        }
        scope = f"coc-sanity:{campaign_id}:{branch_id}:{actor_id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_campaign_revision(campaign_id, int(expected_revision))
        actor = characters.get(actor_id)
        if actor.campaign_id != campaign_id:
            raise ValueError("actor must belong to the target campaign")
        if actor.revision != int(expected_character_revision):
            raise ValueError(
                "character revision conflict: "
                f"expected {expected_character_revision}, found {actor.revision}"
            )
        sheet = validate_investigator_sheet(dict(actor.sheet))
        stream = CampaignRandomStream.from_campaign_state(
            campaign_id,
            campaign.state,
            operation="coc_sanity_check",
            idempotency_key=key,
        )
        with use_random_stream(stream):
            settled = resolve_sanity_check(
                sheet,
                success_loss=formulas["success"],
                failure_loss=formulas["failure"],
                source=source_value,
                context=context,
                investigator_name=actor.name,
                event_id=key,
            )
        sheet = dict(settled["sheet"])
        event = dict(settled["event"])
        conditions = dict(settled["conditions"])
        sanity_roll = dict(event["sanity_roll"])
        succeeded = bool(event["succeeded"])
        loss_roll = dict(event["loss_roll"])
        int_roll = deepcopy(event["int_roll"])
        outcome = dict(event["outcome"])
        bout = deepcopy(event["bout"])
        receipt = stream.receipt()
        resolution_id = "resolution-" + hashlib.sha256(
            f"{campaign_id}:{branch_id}:coc_sanity_check:{key}:{actor_id}".encode("utf-8")
        ).hexdigest()[:32]
        next_state = {**dict(campaign.state), "random_stream": stream.persisted_state()}
        next_state["resolution_presentation_log"] = [
            *list(next_state.get("resolution_presentation_log") or []),
            {
                "id": resolution_id,
                "thread_id": resolution_id,
                "event_sequence": 1,
                "operation": "coc_sanity_check",
                "status": "settled",
                "audience": {
                    "scope": "actors",
                    "actor_refs": [actor_id],
                    "disclosure": "private",
                },
                "branch_id": branch_id,
                "campaign_revision": campaign.revision + 1,
                "result": {
                    "sanity_roll": deepcopy(sanity_roll),
                    "loss_roll": deepcopy(loss_roll),
                    "int_roll": deepcopy(int_roll),
                    "bout": deepcopy(bout),
                    "success": succeeded,
                    "san_loss": int(loss_roll["total"]),
                    "outcome": str(outcome.get("insanity_type") or "stable"),
                },
                "random_stream_receipt": receipt,
            },
        ][-200:]
        response = {
            "campaign_revision": campaign.revision + 1,
            "character_revision": actor.revision + 1,
            "actor_id": actor_id,
            "resolution": event,
            "san": int(outcome["new_san"]),
            "conditions": conditions,
            "random_stream_receipt": receipt,
            "resolution_id": resolution_id,
            "thread_id": resolution_id,
            "event_sequence": 1,
            "status": "settled",
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_investigator_sheet(sheet),
                    notes=dict(actor.notes),
                    expected_revision=actor.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation="coc.sanity.check",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        stream.mark_persisted()
        return response

    @mcp.tool()
    def coc_hp_change(
        action: Literal["damage", "heal"],
        campaign_id: str,
        actor_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_character_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Apply one source-explicit HP transition and any required CON roll atomically."""

        actor_access(campaign_id, actor_id, principal_id, control=True)
        data = dict(data or {})
        source = " ".join(str(data.get("source") or "").split()).strip()
        if not source or len(source) > 500:
            raise ValueError("data.source must contain 1 to 500 characters")
        amount_fields = [field for field in ("amount", "expression") if field in data]
        if len(amount_fields) != 1:
            raise ValueError("provide exactly one of data.amount or data.expression")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": f"coc_hp_change.{action}",
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "data": data,
            "expected_revision": int(expected_revision),
            "expected_character_revision": int(expected_character_revision),
            "branch_id": branch_id,
        }
        scope = f"coc-hp:{campaign_id}:{branch_id}:{actor_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_campaign_revision(campaign_id, int(expected_revision))
        actor = characters.get(actor_id)
        if actor.campaign_id != campaign_id:
            raise ValueError("actor must belong to the target campaign")
        if actor.revision != int(expected_character_revision):
            raise ValueError(
                "character revision conflict: "
                f"expected {expected_character_revision}, found {actor.revision}"
            )
        sheet = validate_investigator_sheet(dict(actor.sheet))
        stream = CampaignRandomStream.from_campaign_state(
            campaign_id,
            campaign.state,
            operation=f"coc_hp_change.{action}",
            idempotency_key=key,
        )
        with use_random_stream(stream):
            amount_roll = None
            if amount_fields[0] == "expression":
                expression = str(data.get("expression") or "").strip()
                if not expression or len(expression) > 100:
                    raise ValueError("data.expression must contain 1 to 100 characters")
                amount_roll = roll_dice_expression(expression)
                amount = int(amount_roll["total"])
            else:
                raw_amount = data.get("amount")
                if isinstance(raw_amount, bool) or not isinstance(raw_amount, int):
                    raise ValueError("data.amount must be an integer")
                amount = raw_amount
            if amount < 0:
                raise ValueError("HP change amount must not be negative")
            con_roll = None
            if action == "damage":
                preview = apply_damage(sheet, amount)
                con_success = None
                if preview["requires_con_check"]:
                    con_roll = roll_d100()
                    con_success = int(con_roll["total"]) <= int(sheet["characteristics"]["con"])
                transition = apply_damage(
                    sheet,
                    amount,
                    con_check_success=con_success,
                )
            else:
                transition = apply_healing(
                    sheet,
                    amount,
                    source=str(data.get("healing_source") or "other"),
                    extreme_success=bool(data.get("extreme_success", False)),
                )
        next_sheet = dict(transition.pop("sheet"))
        event = {
            "idempotency_key": key,
            "action": action,
            "source": source,
            "amount_roll": amount_roll,
            "con_roll": con_roll,
            "transition": transition,
        }
        next_sheet["health_events"] = [
            *list(next_sheet.get("health_events") or [])[-499:],
            event,
        ]
        has_random_draws = stream.draw_count > 0
        next_state = (
            {**dict(campaign.state), "random_stream": stream.persisted_state()}
            if has_random_draws
            else None
        )
        response = {
            "campaign_revision": campaign.revision + int(has_random_draws),
            "character_revision": actor.revision + 1,
            "actor_id": actor_id,
            "resolution": event,
            "hp": int(next_sheet["hp"]),
            "conditions": dict(next_sheet["conditions"]),
            **({"random_stream_receipt": stream.receipt()} if has_random_draws else {}),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            character_updates=[
                CharacterStateUpdate(
                    character_id=actor_id,
                    sheet=validate_investigator_sheet(next_sheet),
                    notes=dict(actor.notes),
                    expected_revision=actor.revision,
                )
            ],
            expected_campaign_revision=campaign.revision,
            operation=f"coc.hp.{action}",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        if has_random_draws:
            stream.mark_persisted()
        return response

    @mcp.tool()
    def chase_start(
        campaign_id: str,
        participants: list[dict[str, Any]],
        expected_character_revisions: dict[str, int],
        source: str,
        expected_revision: int,
        idempotency_key: str,
        route: list[dict[str, Any]] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Resolve speed checks and atomically start one source-backed chase."""

        require_dm(campaign_id, principal_id)
        source_value = " ".join(str(source or "").split()).strip()
        if not source_value or len(source_value) > 500:
            raise ValueError("source must contain 1 to 500 characters")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": "chase_start",
            "campaign_id": campaign_id,
            "participants": participants,
            "expected_character_revisions": expected_character_revisions,
            "source": source_value,
            "route": list(route or []),
            "expected_revision": int(expected_revision),
        }
        scope = f"chase-start:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_campaign_revision(campaign_id, int(expected_revision))
        if authoritative_phase(campaign_id) != PROFILE_PLAY:
            raise ValueError("chase_start is available only during play")
        require_no_active_npc_conversation(campaign_id, "starting a chase")
        if dict(campaign.state.get("combat") or {}).get("active"):
            raise ValueError("active combat must end before a chase starts")
        if dict(campaign.state.get("chase") or {}).get("active"):
            raise ValueError("campaign already has an active chase")
        if investigation_ledger(dict(campaign.state))["pending"]:
            raise ValueError("settle or abort pending investigation checks before a chase")
        revision_map = {
            str(actor_id): int(value) for actor_id, value in expected_character_revisions.items()
        }
        prepared: list[dict[str, Any]] = []
        for raw_value in participants:
            raw = dict(raw_value)
            actor_id = str(raw.get("actor_id") or "").strip()
            role = str(raw.get("role") or "").strip()
            skill_name = str(raw.get("speed_skill_name") or "").strip()
            if not actor_id or role not in {"pursuer", "fleeing"} or not skill_name:
                raise ValueError(
                    "each chase participant requires actor_id, role, and speed_skill_name"
                )
            actor = characters.get(actor_id)
            if actor.campaign_id != campaign_id:
                raise ValueError("every chase participant must belong to the campaign")
            if revision_map.get(actor_id) != actor.revision:
                raise ValueError(
                    f"character revision conflict for {actor_id}: "
                    f"expected {revision_map.get(actor_id)}, found {actor.revision}"
                )
            sheet = validate_investigator_sheet(dict(actor.sheet))
            conditions = dict(sheet.get("conditions") or {})
            if conditions.get("dead") or conditions.get("unconscious"):
                raise ValueError(f"dead or unconscious actor cannot enter a chase: {actor_id}")
            skill_values = dict(sheet.get("skills") or {})
            characteristic_values = dict(sheet.get("characteristics") or {})
            try:
                speed_skill = exact_sheet_value(
                    skill_values,
                    skill_name,
                    "speed skill",
                )
            except ValueError:
                speed_skill = exact_sheet_value(
                    characteristic_values,
                    skill_name,
                    "speed characteristic",
                )
            prepared.append(
                {
                    "actor_id": actor_id,
                    "name": actor.name,
                    "role": role,
                    "participant_kind": str(raw.get("participant_kind") or "person"),
                    "vehicle": (
                        deepcopy(dict(raw["vehicle"]))
                        if isinstance(raw.get("vehicle"), dict)
                        else None
                    ),
                    "base_mov": int(
                        dict(raw.get("vehicle") or {}).get("mov", sheet["mov"])
                        if str(raw.get("participant_kind") or "person") == "vehicle"
                        else sheet["mov"]
                    ),
                    "dex": int(sheet["characteristics"]["dex"]),
                    "position": int(raw.get("position", 0)),
                    "speed_skill_name": skill_name,
                    "speed_skill": speed_skill,
                }
            )
        if set(revision_map) != {item["actor_id"] for item in prepared}:
            raise ValueError("expected_character_revisions must exactly match participants")
        stream = CampaignRandomStream.from_campaign_state(
            campaign_id,
            campaign.state,
            operation="chase_start",
            idempotency_key=key,
        )
        with use_random_stream(stream):
            started = start_chase_with_speed_checks(
                prepared,
                source=source_value,
                route=list(route or []),
            )
        chase = dict(started["chase"])
        speed_checks = dict(started["speed_checks"])
        next_state = {
            **dict(campaign.state),
            "game_phase": PROFILE_PLAY,
            "chase": chase,
            "random_stream": stream.persisted_state(),
        }
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "phase": PROFILE_PLAY,
            "chase": deepcopy(chase),
            "speed_checks": speed_checks,
            "random_stream_receipt": stream.receipt(),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            expected_campaign_revision=campaign.revision,
            operation="coc.chase.start",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        stream.mark_persisted()
        return response

    @mcp.tool()
    def chase_query(
        campaign_id: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Return one authoritative chase and caller-specific legal actions."""

        access.require_campaign(campaign_id, principal_id)
        return chase_view(campaign_id, principal_id)

    @mcp.tool()
    def chase_action(
        action: Literal["move", "check", "speed_check", "end_turn"],
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Consume chase actions and settle explicit chase checks atomically."""

        data = dict(data or {})
        actor_id = str(data.get("actor_id") or "").strip()
        if not actor_id:
            raise ValueError("data.actor_id is required")
        actor_access(campaign_id, actor_id, principal_id, control=True)
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": f"chase_action.{action}",
            "campaign_id": campaign_id,
            "data": data,
            "expected_revision": int(expected_revision),
        }
        scope = f"chase-action:{campaign_id}:{branch_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign, chase = active_chase(campaign_id)
        if campaign.revision != int(expected_revision):
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        source_value = " ".join(str(data.get("source") or "").split()).strip()
        result: dict[str, Any] | None = None
        stream = None
        if action == "end_turn":
            transition = resolve_chase_turn_action(
                chase,
                actor_id,
                action="end_turn",
            )
        elif action == "move":
            transition = resolve_chase_turn_action(
                chase,
                actor_id,
                action="move",
                cost=int(data.get("cost", 1)),
                position_change=int(data.get("position_change", 1)),
                source=source_value,
            )
        else:
            actor = characters.get(actor_id)
            sheet = validate_investigator_sheet(dict(actor.sheet))
            skill_name = str(data.get("skill_name") or "").strip()
            if not skill_name:
                raise ValueError("data.skill_name is required for chase checks")
            try:
                skill_value = exact_sheet_value(
                    dict(sheet.get("skills") or {}),
                    skill_name,
                    "chase skill",
                )
            except ValueError:
                skill_value = exact_sheet_value(
                    dict(sheet.get("characteristics") or {}),
                    skill_name,
                    "chase characteristic",
                )
            stream = CampaignRandomStream.from_campaign_state(
                campaign_id,
                campaign.state,
                operation=f"chase_action.{action}",
                idempotency_key=key,
            )
            with use_random_stream(stream):
                transition = resolve_chase_turn_action(
                    chase,
                    actor_id,
                    action=action,
                    source=source_value,
                    cost=int(data.get("cost", 1)),
                    action_type=str(data.get("action_type") or "check"),
                    skill_name=skill_name,
                    skill_value=skill_value,
                    actor_name=actor.name,
                    difficulty=str(data.get("difficulty") or "regular"),
                    bonus_dice=int(data.get("bonus_dice", 0)),
                    penalty_dice=int(data.get("penalty_dice", 0)),
                    success_position_change=int(data.get("success_position_change", 0)),
                    failure_position_change=int(data.get("failure_position_change", 0)),
                )
        next_chase = dict(transition["chase"])
        if transition["resolution"] is not None:
            result = dict(transition["resolution"])
        next_state = {**dict(campaign.state), "chase": next_chase}
        if stream is not None:
            next_state["random_stream"] = stream.persisted_state()
            resolution_id = "resolution-" + hashlib.sha256(
                f"{campaign_id}:{branch_id}:chase_action.{action}:{key}:{actor_id}".encode(
                    "utf-8"
                )
            ).hexdigest()[:32]
            receipt = stream.receipt()
            outcome = dict((result or {}).get("outcome") or {})
            next_state["resolution_presentation_log"] = [
                *list(next_state.get("resolution_presentation_log") or []),
                {
                    "id": resolution_id,
                    "thread_id": resolution_id,
                    "event_sequence": 1,
                    "operation": f"chase_action.{action}",
                    "status": "settled",
                    "audience": {
                        "scope": "actors",
                        "actor_refs": [actor_id],
                        "disclosure": "private",
                    },
                    "branch_id": branch_id,
                    "campaign_revision": campaign.revision + 1,
                    "result": {
                        "roll": deepcopy((result or {}).get("roll")),
                        "success": bool(outcome.get("success")),
                        "success_level": outcome.get("success_level"),
                        "outcome": str(
                            outcome.get("outcome")
                            or outcome.get("result")
                            or ("success" if outcome.get("success") else "failure")
                        ),
                    },
                    "random_stream_receipt": receipt,
                },
            ][-200:]
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "phase": PROFILE_PLAY,
            "chase": deepcopy(next_chase),
            "resolution": result,
            **({"random_stream_receipt": receipt} if stream is not None else {}),
            **(
                {
                    "resolution_id": resolution_id,
                    "thread_id": resolution_id,
                    "event_sequence": 1,
                    "status": "settled",
                }
                if stream is not None
                else {}
            ),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            expected_campaign_revision=campaign.revision,
            operation=f"coc.chase.{action}",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        if stream is not None:
            stream.mark_persisted()
        return response

    @mcp.tool()
    def chase_end(
        campaign_id: str,
        outcome: Literal["escaped", "caught", "abandoned", "other"],
        source: str,
        expected_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Close one active chase with a source-explicit outcome."""

        require_dm(campaign_id, principal_id)
        source_value = " ".join(str(source or "").split()).strip()
        if not source_value or len(source_value) > 500:
            raise ValueError("source must contain 1 to 500 characters")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": "chase_end",
            "campaign_id": campaign_id,
            "outcome": outcome,
            "source": source_value,
            "expected_revision": int(expected_revision),
        }
        scope = f"chase-end:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign, chase = active_chase(campaign_id)
        if campaign.revision != int(expected_revision):
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        ended = close_chase_state(chase, outcome=outcome, source=source_value)
        next_state = {**dict(campaign.state), "game_phase": PROFILE_PLAY, "chase": ended}
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "phase": PROFILE_PLAY,
            "outcome": outcome,
            "chase": deepcopy(ended),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            expected_campaign_revision=campaign.revision,
            operation="coc.chase.end",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        return response

    @mcp.tool()
    def combat_start(
        campaign_id: str,
        participants: list[dict[str, Any]],
        expected_character_revisions: dict[str, int],
        positioning_mode: Literal["grid", "agent"],
        source: str,
        expected_revision: int,
        idempotency_key: str,
        grid_metric: Literal["chebyshev", "euclidean"] = "chebyshev",
        grid_unit_feet: float = 5.0,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Start one source-explicit authoritative CoC combat encounter."""

        require_dm(campaign_id, principal_id)
        source_value = " ".join(str(source or "").split()).strip()
        if not source_value or len(source_value) > 500:
            raise ValueError("source must contain 1 to 500 characters")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        request = {
            "operation": "combat_start",
            "campaign_id": campaign_id,
            "participants": participants,
            "expected_character_revisions": expected_character_revisions,
            "positioning_mode": positioning_mode,
            "source": source_value,
            "grid_metric": grid_metric,
            "grid_unit_feet": grid_unit_feet,
            "expected_revision": int(expected_revision),
        }
        branch_id = current_branch_id(campaign_id)
        scope = f"combat-start:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign = require_campaign_revision(campaign_id, int(expected_revision))
        if authoritative_phase(campaign_id) != PROFILE_PLAY:
            raise ValueError("combat_start is available only during play")
        require_no_active_npc_conversation(campaign_id, "starting combat")
        if dict(campaign.state.get("chase") or {}).get("active"):
            raise ValueError("active chase must end before combat starts")
        if investigation_ledger(dict(campaign.state))["pending"]:
            raise ValueError("settle or abort pending investigation checks before combat")
        normalized: list[dict[str, Any]] = []
        revision_map = {
            str(actor_id): int(value) for actor_id, value in expected_character_revisions.items()
        }
        for raw in participants:
            value = combat_participant(
                campaign_id,
                dict(raw),
                positioning_mode=positioning_mode,
            )
            actor = characters.get(value["actor_id"])
            expected_character_revision = revision_map.get(actor.id)
            if expected_character_revision is None:
                raise ValueError(f"expected_character_revisions is missing {actor.id}")
            if actor.revision != expected_character_revision:
                raise ValueError(
                    "character revision conflict: "
                    f"expected {expected_character_revision}, found {actor.revision}"
                )
            normalized.append(value)
        if set(revision_map) != {item["actor_id"] for item in normalized}:
            raise ValueError("expected_character_revisions must exactly match participants")
        combat = build_combat_state(
            normalized,
            positioning_mode=positioning_mode,
            source=source_value,
            grid_metric=grid_metric,
            grid_unit_feet=grid_unit_feet,
        )
        next_state = {**dict(campaign.state), "game_phase": PROFILE_PLAY, "combat": combat}
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "phase": PROFILE_COMBAT,
            "combat": deepcopy(combat),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            expected_campaign_revision=campaign.revision,
            operation="coc.combat.start",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        return response

    @mcp.tool()
    def combat_query(
        campaign_id: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Return the authoritative combat view and caller-specific legal tasks."""

        access.require_campaign(campaign_id, principal_id)
        return combat_view(campaign_id, principal_id)

    @mcp.tool()
    def combat_action(
        action: Literal["join", "move", "end_turn"],
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Apply one guarded non-terminal combat task."""

        data = dict(data or {})
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        actor_id = str(data.get("actor_id") or "").strip()
        if action == "join":
            require_dm(campaign_id, principal_id)
        elif action in {"move", "end_turn"}:
            if not actor_id:
                raise ValueError("data.actor_id is required")
            actor_access(campaign_id, actor_id, principal_id, control=True)
        request = {
            "operation": f"combat_action.{action}",
            "campaign_id": campaign_id,
            "data": data,
            "expected_revision": int(expected_revision),
        }
        branch_id = current_branch_id(campaign_id)
        scope = f"combat-action:{campaign_id}:{branch_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign, combat = active_combat(campaign_id)
        if campaign.revision != int(expected_revision):
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        if action == "join":
            participant = combat_participant(
                campaign_id,
                data,
                positioning_mode=str(combat["positioning_mode"]),
            )
            actor = characters.get(participant["actor_id"])
            if "expected_character_revision" not in data:
                raise ValueError("data.expected_character_revision is required")
            if actor.revision != int(data["expected_character_revision"]):
                raise ValueError(
                    "character revision conflict: "
                    f"expected {data['expected_character_revision']}, found {actor.revision}"
                )
            next_combat = join_combat_state(combat, participant)
        elif action == "move":
            if actor_id != str(combat.get("current_actor_id") or ""):
                raise ValueError("only the current actor may move")
            next_combat = move_combatant(
                combat,
                actor_id,
                destination=data.get("destination"),
                movement_budget=data.get("movement_budget"),
                agent_ruling=data.get("agent_ruling"),
            )
        else:
            if actor_id != str(combat.get("current_actor_id") or ""):
                raise ValueError("only the current actor may end the turn")
            next_combat = advance_combat_turn(combat)
        next_state = {**dict(campaign.state), "combat": next_combat}
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "phase": PROFILE_COMBAT,
            "combat": deepcopy(next_combat),
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            expected_campaign_revision=campaign.revision,
            operation=f"coc.combat.{action}",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        return response

    @mcp.tool()
    def combat_attack(
        action: Literal["open", "resolve", "abort"],
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Open, answer, or abort one authoritative attack-response choice."""

        data = dict(data or {})
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        branch_id = current_branch_id(campaign_id)
        request = {
            "operation": f"combat_attack.{action}",
            "campaign_id": campaign_id,
            "data": data,
            "expected_revision": int(expected_revision),
        }
        scope = f"combat-attack:{campaign_id}:{branch_id}:{principal_id}:{action}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign, combat = active_combat(campaign_id)
        if campaign.revision != int(expected_revision):
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )

        if action == "open":
            if combat.get("pending_choice") is not None:
                raise ValueError("combat already has a pending response choice")
            attacker_id = str(data.get("attacker_id") or "").strip()
            target_id = str(data.get("target_actor_id") or "").strip()
            if not attacker_id or not target_id or attacker_id == target_id:
                raise ValueError("distinct attacker_id and target_actor_id are required")
            actor_access(campaign_id, attacker_id, principal_id, control=True)
            if attacker_id != str(combat.get("current_actor_id") or ""):
                raise ValueError("only the current combat actor may open an attack")
            if target_id not in combat.get("participants", {}):
                raise ValueError("attack target must be a combat participant")
            source_value = " ".join(str(data.get("source") or "").split()).strip()
            if not source_value or len(source_value) > 500:
                raise ValueError("data.source must contain 1 to 500 characters")
            weapon_name = str(data.get("weapon_name") or "").strip()
            if not weapon_name:
                raise ValueError("data.weapon_name is required")
            attacker = characters.get(attacker_id)
            target = characters.get(target_id)
            expected_attacker_revision = int(data.get("expected_attacker_revision", -1))
            expected_target_revision = int(data.get("expected_target_revision", -1))
            if attacker.revision != expected_attacker_revision:
                raise ValueError(
                    "attacker revision conflict: "
                    f"expected {expected_attacker_revision}, found {attacker.revision}"
                )
            if target.revision != expected_target_revision:
                raise ValueError(
                    "target revision conflict: "
                    f"expected {expected_target_revision}, found {target.revision}"
                )
            attacker_sheet = validate_investigator_sheet(dict(attacker.sheet))
            attack_profile = combat_attack_profile(attacker_sheet, weapon_name)
            weapon = dict(attack_profile["weapon"])
            if combat["positioning_mode"] == "agent":
                spatial = dict(data.get("spatial_ruling") or {})
                if (
                    not isinstance(spatial.get("allowed"), bool)
                    or not str(spatial.get("source") or "").strip()
                ):
                    raise ValueError(
                        "agent combat requires explicit spatial_ruling.allowed and source"
                    )
                if not spatial["allowed"]:
                    raise ValueError("the explicit spatial ruling does not allow this attack")
                distance_feet = None
            else:
                if data.get("spatial_ruling") is not None:
                    raise ValueError("grid combat does not accept an Agent spatial override")
                spatial = None
                distance_feet = combat_distance_feet(combat, attacker_id, target_id)
                if not weapon["ranged"] and distance_feet > float(combat["grid_unit_feet"]):
                    raise ValueError("grid target is outside melee reach")
            pending_id = hashlib.sha256(
                f"{campaign_id}:{branch_id}:{key}:{attacker_id}:{target_id}".encode()
            ).hexdigest()[:24]
            pending = {
                "id": pending_id,
                "thread_id": pending_id,
                "event_sequence": 1,
                "status": "pending",
                "kind": "combat_attack_response",
                "attacker_id": attacker_id,
                "target_actor_id": target_id,
                "attacker_revision": attacker.revision,
                "target_revision": target.revision,
                "attacker_name": attacker.name,
                "target_name": target.name,
                **attack_profile,
                "source": source_value,
                "range_band": str(data.get("range_band") or "normal"),
                "spatial_ruling": spatial,
                "distance_feet": distance_feet,
                "branch_id": branch_id,
                "campaign_revision": campaign.revision + 1,
            }
            next_combat = {**combat, "pending_choice": pending}
            next_combat["events"] = [
                *list(combat.get("events") or []),
                {
                    "type": "attack_opened",
                    "pending_id": pending_id,
                    "actor_refs": [attacker_id, target_id],
                    "branch_id": branch_id,
                    "campaign_revision": campaign.revision + 1,
                },
            ]
            next_state = {**dict(campaign.state), "combat": next_combat}
            response = {
                "campaign_id": campaign_id,
                "campaign_revision": campaign.revision + 1,
                "phase": PROFILE_COMBAT,
                "pending_choice": deepcopy(pending),
                "resolution_id": pending_id,
                "thread_id": pending_id,
                "event_sequence": 1,
                "status": "pending",
            }
            StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=next_state,
                expected_campaign_revision=campaign.revision,
                operation="coc.combat.attack.open",
                actor=principal_id,
                branch_id=branch_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=response,
                ),
            )
            return response

        pending = dict(combat.get("pending_choice") or {})
        if not pending or pending.get("kind") != "combat_attack_response":
            raise ValueError("combat has no pending attack response")
        if str(data.get("pending_id") or "") != str(pending["id"]):
            raise ValueError("pending combat choice does not match")
        if action == "abort":
            require_dm(campaign_id, principal_id)
            reason = " ".join(str(data.get("reason") or "").split()).strip()
            if not reason:
                raise ValueError("data.reason is required to abort a combat attack")
            next_combat = {**combat, "pending_choice": None}
            next_combat["events"] = [
                *list(combat.get("events") or []),
                {
                    "type": "attack_aborted",
                    "pending_id": pending["id"],
                    "actor_refs": [pending["attacker_id"], pending["target_actor_id"]],
                    "branch_id": pending.get("branch_id") or branch_id,
                    "campaign_revision": campaign.revision + 1,
                    "reason": reason,
                },
            ]
            next_state = {**dict(campaign.state), "combat": next_combat}
            response = {
                "campaign_id": campaign_id,
                "campaign_revision": campaign.revision + 1,
                "phase": PROFILE_COMBAT,
                "aborted_pending_id": pending["id"],
                "resolution_id": pending["id"],
                "thread_id": str(pending.get("thread_id") or pending["id"]),
                "event_sequence": 2,
                "status": "aborted",
            }
            StateMutationService(storage.database).replace(
                campaign_id,
                campaign_state=next_state,
                expected_campaign_revision=campaign.revision,
                operation="coc.combat.attack.abort",
                actor=principal_id,
                branch_id=branch_id,
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=request,
                    response=response,
                ),
            )
            return response

        target_id = str(pending["target_actor_id"])
        actor_access(campaign_id, target_id, principal_id, control=True)
        defense = str(data.get("defense") or "none")
        if defense not in pending["response_options"]:
            raise ValueError("defense must be one of " + ", ".join(pending["response_options"]))
        attacker_id = str(pending["attacker_id"])
        attacker = characters.get(attacker_id)
        target = characters.get(target_id)
        if attacker.revision != int(pending["attacker_revision"]):
            raise ValueError("attacker changed while the response choice was pending")
        if target.revision != int(pending["target_revision"]):
            raise ValueError("target changed while the response choice was pending")
        attacker_sheet = validate_investigator_sheet(dict(attacker.sheet))
        target_sheet = validate_investigator_sheet(dict(target.sheet))
        weapon = dict(pending["weapon"])
        stream = CampaignRandomStream.from_campaign_state(
            campaign_id,
            campaign.state,
            operation="combat_attack.resolve",
            idempotency_key=key,
        )
        with use_random_stream(stream):
            transition = resolve_combat_attack(
                combat,
                attacker_sheet,
                target_sheet,
                pending,
                defense=defense,
                attacker_name=attacker.name,
                target_name=target.name,
                target_weapon_name=data.get("target_weapon_name"),
            )
        next_combat = dict(transition["combat"])
        resolution = dict(transition["resolution"])
        attack_roll = dict(transition["attack_roll"])
        defense_roll = transition["defense_roll"]
        dive_success = bool(transition["dive_success"])
        damaged_id = transition["damaged_actor_id"]
        health_transition = transition["health_transition"]
        con_roll = transition["con_roll"]
        event = {
            "type": "attack_resolved",
            "pending_id": pending["id"],
            "actor_refs": [attacker_id, target_id],
            "branch_id": pending.get("branch_id") or branch_id,
            "campaign_revision": campaign.revision + 1,
            "source": pending["source"],
            "attacker_id": attacker_id,
            "target_actor_id": target_id,
            "defense": defense,
            "attack_roll": attack_roll,
            "defense_roll": defense_roll,
            "dive_success": dive_success,
            "resolution": resolution,
            "damaged_actor_id": damaged_id,
            "health_transition": (
                {key: value for key, value in health_transition.items() if key != "sheet"}
                if health_transition is not None
                else None
            ),
            "con_roll": con_roll,
            "random_stream_receipt": stream.receipt(),
            "resolution_id": pending["id"],
            "thread_id": str(pending.get("thread_id") or pending["id"]),
            "event_sequence": 2,
            "status": "settled",
        }
        next_combat["events"] = [*list(next_combat.get("events") or []), event]
        next_state = {
            **dict(campaign.state),
            "combat": next_combat,
            "random_stream": stream.persisted_state(),
        }
        updates_by_id = {
            str(actor_id): dict(sheet)
            for actor_id, sheet in dict(transition["sheet_updates"]).items()
        }
        if health_transition is not None and damaged_id is not None:
            damaged_sheet = dict(updates_by_id[damaged_id])
            damaged_sheet["health_events"] = [
                *list(damaged_sheet.get("health_events") or [])[-499:],
                {
                    "idempotency_key": key,
                    "action": "combat_damage",
                    "source": pending["source"],
                    "amount_roll": resolution.get("damage") or resolution.get("counterattack"),
                    "con_roll": con_roll,
                    "transition": {
                        item_key: item_value
                        for item_key, item_value in health_transition.items()
                        if item_key != "sheet"
                    },
                },
            ]
            updates_by_id[damaged_id] = damaged_sheet
        character_updates = [
            CharacterStateUpdate(
                character_id=actor_id,
                sheet=validate_investigator_sheet(sheet),
                notes=dict(characters.get(actor_id).notes),
                expected_revision=characters.get(actor_id).revision,
            )
            for actor_id, sheet in updates_by_id.items()
        ]
        character_revisions = {
            actor_id: characters.get(actor_id).revision + 1 for actor_id in updates_by_id
        }
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "phase": PROFILE_COMBAT,
            "resolution": event,
            "combat": deepcopy(next_combat),
            "character_revisions": character_revisions,
            "random_stream_receipt": stream.receipt(),
            "resolution_id": pending["id"],
            "thread_id": str(pending.get("thread_id") or pending["id"]),
            "event_sequence": 2,
            "status": "settled",
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            character_updates=character_updates,
            expected_campaign_revision=campaign.revision,
            operation="coc.combat.attack.resolve",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        stream.mark_persisted()
        return response

    @mcp.tool()
    def combat_end(
        campaign_id: str,
        outcome: Literal["victory", "escape", "surrender", "defeat", "other"],
        source: str,
        expected_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Close the active encounter and return the campaign to Play."""

        require_dm(campaign_id, principal_id)
        source_value = " ".join(str(source or "").split()).strip()
        if not source_value or len(source_value) > 500:
            raise ValueError("source must contain 1 to 500 characters")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        request = {
            "operation": "combat_end",
            "campaign_id": campaign_id,
            "outcome": outcome,
            "source": source_value,
            "expected_revision": int(expected_revision),
        }
        branch_id = current_branch_id(campaign_id)
        scope = f"combat-end:{campaign_id}:{branch_id}:{principal_id}"
        replay = replay_response(scope, key, request)
        if replay is not None:
            return replay
        campaign, combat = active_combat(campaign_id)
        if campaign.revision != int(expected_revision):
            raise ValueError(
                "campaign revision conflict: "
                f"expected {expected_revision}, found {campaign.revision}"
            )
        if combat.get("pending_choice") is not None:
            raise ValueError("resolve or abort the pending combat choice before combat_end")
        ended = {
            **combat,
            "active": False,
            "outcome": outcome,
            "ended_source": source_value,
        }
        recovery_actor_ids = [
            actor_id
            for actor_id in combat.get("participants", {})
            if dict(
                validate_investigator_sheet(dict(characters.get(actor_id).sheet)).get("conditions")
                or {}
            ).get("dying")
        ]
        next_state = {**dict(campaign.state), "game_phase": PROFILE_PLAY, "combat": ended}
        response = {
            "campaign_id": campaign_id,
            "campaign_revision": campaign.revision + 1,
            "phase": PROFILE_PLAY,
            "outcome": outcome,
            "combat": deepcopy(ended),
            "recovery_required_actor_ids": recovery_actor_ids,
        }
        StateMutationService(storage.database).replace(
            campaign_id,
            campaign_state=next_state,
            expected_campaign_revision=campaign.revision,
            operation="coc.combat.end",
            actor=principal_id,
            branch_id=branch_id,
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=request,
                response=response,
            ),
        )
        return response

    @mcp.tool()
    def coc_resolve(
        kind: Literal[
            "skill",
            "opposed",
            "sanity",
            "melee",
            "ranged",
            "chase_speed",
            "chase_action",
        ],
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        def resolve() -> dict[str, Any]:
            if kind == "skill":
                return resolve_skill_check(**data)
            if kind == "opposed":
                return resolve_opposed_check(**data)
            if kind == "sanity":
                return resolve_sanity_loss(**data)
            if kind == "melee":
                return resolve_melee_attack(**data)
            if kind == "ranged":
                return resolve_ranged_attack(**data)
            if kind == "chase_speed":
                return resolve_chase_speed_check(**data)
            return resolve_chase_action(**data)

        return authoritative_random_resolution(
            campaign_id=campaign_id,
            principal_id=principal_id,
            operation=f"coc_resolve.{kind}",
            payload={"kind": kind, "data": data},
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            resolve=resolve,
        )

    @mcp.tool()
    def skill_query(
        action: Literal["list", "read"],
        campaign_id: str,
        skill_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        require_dm(campaign_id, principal_id)
        if action == "list":
            return {
                "skills": [
                    {"id": item.id, "title": item.title, "source": item.source}
                    for item in skills.list()
                ]
            }
        if skill_id is None:
            raise ValueError("skill_id is required")
        return {"skill_id": skill_id, "content": skills.read(skill_id)}

    @mcp.tool()
    async def exposure(
        action: Literal["open", "get", "search", "set"],
        campaign_id: str | None = None,
        query: str = "",
        add_tool_ids: list[str] | None = None,
        remove_tool_ids: list[str] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Open, inspect, search, or mutate this session's native tool list."""

        campaign_id = str(campaign_id or "").strip() or None
        if config.bound_principal_id is not None:
            principal_id = config.bound_principal_id
        request = mcp._request_session()
        session_key = request[0] if request is not None else f"direct:{principal_id}"
        if action == "open":
            current = exposures.active(session_key)
            if (
                current is not None
                and current.principal_id == principal_id
                and current.campaign_id == campaign_id
            ):
                raise ExposureError(
                    "This MCP session is already bound to that campaign. "
                    "Use exposure(action='get', 'search', or 'set')."
                )
            phase = PROFILE_LOBBY
            if campaign_id:
                access.require_campaign(campaign_id, principal_id)
                phase = authoritative_phase(campaign_id)
            opened = exposures.open(
                session_key=session_key,
                principal_id=principal_id,
                campaign_id=campaign_id,
                phase=phase,
                authorization_fingerprint=(
                    access.authorization_fingerprint(campaign_id, principal_id)
                    if campaign_id
                    else ""
                ),
            )
            return {
                **exposures.status(opened),
                "native_dynamic_tools": request is not None,
                "next": "Use exposure(action='search'), then exposure(action='set').",
            }

        current = exposures.active(session_key)
        if current is None:
            raise ExposureError("No active exposure for this MCP session. Use action='open'.")
        if current.principal_id != principal_id:
            raise ExposureError("The active exposure belongs to another principal.")
        if campaign_id is not None and campaign_id != current.campaign_id:
            raise ExposureError("Reopen the exposure to bind a different campaign.")
        if current.campaign_id:
            exposures.refresh_phase(current, authoritative_phase(current.campaign_id))
        if action == "get":
            return exposures.status(current)

        if action == "search":
            terms = {term.casefold() for term in query.split() if term.strip()}
            matches: list[dict[str, Any]] = []
            for tool in mcp._tool_manager.list_tools():
                policy = policy_for_tool(tool.name)
                if policy is None or current.phase not in policy.phases:
                    continue
                if policy.requires_campaign and current.campaign_id is None:
                    continue
                roles = policy.roles(current.phase)
                if roles:
                    if current.campaign_id is None:
                        continue
                    try:
                        access.require_campaign(
                            current.campaign_id,
                            current.principal_id,
                            roles=set(roles),
                        )
                    except PermissionError:
                        continue
                haystack = f"{tool.name} {tool.description or ''}".casefold()
                if terms and not all(term in haystack for term in terms):
                    continue
                matches.append(
                    {
                        "tool_id": tool.name,
                        "description": tool.description or "",
                        "loaded": tool.name in current.loaded_tools,
                        "roles": sorted(roles),
                    }
                )
            return {
                **exposures.status(current),
                "query_semantics": "all_terms_match_one_tool",
                "matches": matches,
            }

        additions = list(add_tool_ids or [])
        removals = list(remove_tool_ids or [])
        if not additions and not removals:
            raise ValueError("exposure(set) requires add_tool_ids or remove_tool_ids")
        for tool_id in additions:
            policy = policy_for_tool(tool_id)
            if policy is not None and policy.roles(current.phase):
                if current.campaign_id is None:
                    raise ExposureError(f"Tool {tool_id!r} requires a campaign.")
                access.require_campaign(
                    current.campaign_id,
                    current.principal_id,
                    roles=set(policy.roles(current.phase)),
                )
        async with mcp._exposure_lock(current.id):
            changed = exposures.set_tools(current, add=additions, remove=removals)
        return {**exposures.status(current), "changed": changed}

    registered_tools = mcp._tool_manager.list_tools()
    validate_profile_coverage(tool.name for tool in registered_tools)
    for registered_tool in registered_tools:
        registered_tool.meta = {
            **dict(registered_tool.meta or {}),
            "sagasmith_domain_context": "sagasmith-coc",
        }
        if registered_tool.name == "campaign_query":
            registered_tool.meta["sagasmith_context_sync"] = True

    return mcp


def main() -> None:
    transport = os.environ.get("SAGASMITH_COC_MCP_TRANSPORT", "stdio").strip().casefold()
    if transport not in {"stdio", "streamable-http"}:
        raise ValueError("SAGASMITH_COC_MCP_TRANSPORT must be 'stdio' or 'streamable-http'")
    create_server(McpConfig.from_environment()).run(transport=transport)


if __name__ == "__main__":
    main()
