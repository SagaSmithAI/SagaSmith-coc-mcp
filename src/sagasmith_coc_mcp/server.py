"""MCP surface for SagaSmith Call of Cthulhu 7e."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from mcp.server.fastmcp import FastMCP
from sagasmith_coc.engine.checks.chase import (
    resolve_chase_action,
    resolve_chase_speed_check,
)
from sagasmith_coc.engine.checks.combat import resolve_melee_attack, resolve_ranged_attack
from sagasmith_coc.engine.checks.sanity import resolve_sanity_loss
from sagasmith_coc.engine.checks.skill import resolve_opposed_check, resolve_skill_check
from sagasmith_coc.module_profile import CocModuleProfile
from sagasmith_coc.system import validate_investigator_sheet
from sagasmith_core import (
    AccessService,
    ActorKnowledgeService,
    CampaignService,
    CharacterService,
    MemoryService,
    ModuleService,
    SnapshotService,
    default_local_principal,
)
from sagasmith_core.access import LOCAL_SYSTEM_PRINCIPAL_ID
from sagasmith_core.modules import MarkdownModuleParser

from .config import McpConfig
from .exposure import Exposure, ExposureError, ExposureRegistry
from .skills import SkillCatalog
from .storage import SagaSmithStorage
from .tool_profiles import (
    CORE_TOOLS,
    PROFILE_COMBAT,
    PROFILE_LOBBY,
    PROFILE_PLAY,
    PROFILES,
    policy_for_tool,
    tool_catalog,
    validate_profile_coverage,
)


class SessionExposureFastMCP(FastMCP):
    """Filter native tools/list by one server-owned MCP-session exposure."""

    def __init__(
        self,
        *args: Any,
        exposure_registry: ExposureRegistry,
        phase_lookup: Any,
        scope_validator: Any,
        bound_principal_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.exposure_registry = exposure_registry
        self._phase_lookup = phase_lookup
        self._scope_validator = scope_validator
        self._bound_principal_id = bound_principal_id.strip() if bound_principal_id else None
        self._sessions: WeakValueDictionary[str, Any] = WeakValueDictionary()
        self._exposure_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        super().__init__(*args, **kwargs)

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

    async def _refresh(self, session_key: str) -> bool:
        exposure = self.exposure_registry.active(session_key)
        if exposure is None or exposure.campaign_id is None:
            return False
        changed = self.exposure_registry.refresh_phase(
            exposure, self._phase_lookup(exposure.campaign_id)
        )
        if changed:
            session = self._sessions.get(session_key)
            if session is not None:
                await session.send_tool_list_changed()
        return changed

    async def list_tools(self):  # type: ignore[override]
        request = self._request_session()
        if request is None:
            return await super().list_tools()
        session_key, _ = request
        await self._refresh(session_key)
        visible = self.exposure_registry.visible_tools(self.exposure_registry.active(session_key))
        return [tool for tool in await super().list_tools() if tool.name in visible]

    async def call_tool(self, name: str, arguments: dict[str, Any]):  # type: ignore[override]
        request = self._request_session()
        if request is None:
            return await super().call_tool(name, arguments)
        session_key, _ = request
        await self._refresh(session_key)
        exposure = self.exposure_registry.active(session_key)
        if name not in CORE_TOOLS and exposure is None:
            raise ExposureError("Open and load a session exposure before calling domain tools.")
        bound = self._bind_configured_principal(name, arguments)
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
            await self._refresh(session_key)
        return result


def create_server(config: McpConfig | None = None) -> FastMCP:
    config = config or McpConfig.from_environment()
    storage = SagaSmithStorage(config)
    storage.migrate()
    campaigns = CampaignService(storage.database)
    characters = CharacterService(storage.database)
    access = AccessService(storage.database)
    memories = MemoryService(storage.database)
    knowledge = ActorKnowledgeService(storage.database)
    modules = ModuleService(storage.database)
    snapshots = SnapshotService(storage.database)
    default_local_principal(storage.database)
    parser = MarkdownModuleParser(profile=CocModuleProfile())
    skills = SkillCatalog(
        coc_root=config.coc_skills_dir,
        modulegen_root=config.modulegen_skills_dir,
    )
    exposures = ExposureRegistry()

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

    mcp = SessionExposureFastMCP(
        "SagaSmith CoC",
        instructions=(
            "Call of Cthulhu 7e campaign runtime. Engine tools return deterministic "
            "resolution data; character state changes remain explicit writes."
        ),
        exposure_registry=exposures,
        phase_lookup=authoritative_phase,
        scope_validator=validate_scope,
        bound_principal_id=config.bound_principal_id,
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
            "resolution_boundary": "pure result first; explicit state mutation second",
            "tool_catalog": tool_catalog(),
        }

    @mcp.tool()
    def storage_status() -> dict[str, Any]:
        return storage.status()

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
                    asdict(item) for item in campaigns.list(system_id="coc7e") if item.id in allowed
                ]
            }
        if campaign_id is None:
            raise ValueError("campaign_id is required")
        access.require_campaign(campaign_id, principal_id)
        return asdict(campaigns.get(campaign_id))

    @mcp.tool()
    def game_phase(campaign_id: str, principal_id: str = "system:local") -> dict[str, str]:
        access.require_campaign(campaign_id, principal_id)
        return {"campaign_id": campaign_id, "phase": authoritative_phase(campaign_id)}

    @mcp.tool()
    def campaign_change(
        action: Literal["create", "set_phase", "grant_campaign", "grant_actor"],
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
                state={"game_phase": PROFILE_LOBBY, **dict(data.get("state") or {})},
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
        action: Literal["create", "update"],
        campaign_id: str,
        data: dict[str, Any],
        character_id: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        membership = access.require_campaign(campaign_id, principal_id)
        if action == "create":
            character_type = str(data.get("character_type") or "pc")
            if character_type != "pc" and membership.role not in {"owner", "dm"}:
                raise PermissionError("only the Keeper may create NPCs or creatures")
            name = str(data.get("name") or "").strip()
            if not name:
                raise ValueError("data.name is required")
            created = characters.create(
                system_id="coc7e",
                campaign_id=campaign_id,
                name=name,
                character_type=character_type,
                player_name=data.get("player_name"),
                summary=str(data.get("summary") or ""),
                sheet=validate_investigator_sheet(dict(data.get("sheet") or {})),
                notes=dict(data.get("notes") or {}),
            )
            access.grant_actor(
                campaign_id,
                principal_id,
                created.id,
                can_control=True,
                can_view_private=True,
            )
            return asdict(created)
        if character_id is None:
            raise ValueError("character_id is required")
        actor_access(campaign_id, character_id, principal_id, control=True)
        if authoritative_phase(campaign_id) == PROFILE_COMBAT and membership.role not in {
            "owner",
            "dm",
        }:
            raise PermissionError("combat character mutations require Keeper authority")
        current = characters.get(character_id)
        sheet = validate_investigator_sheet(dict(data["sheet"])) if "sheet" in data else None
        return asdict(
            characters.update(
                character_id,
                name=data.get("name"),
                player_name=data.get("player_name"),
                summary=data.get("summary"),
                sheet=sheet,
                notes=dict(data["notes"]) if "notes" in data else None,
                expected_revision=int(data.get("expected_revision", current.revision)),
            )
        )

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
                else [item for item in values if item["visibility"] == "player"]
            }
        if action == "current":
            value = modules.current_scene(
                campaign_id, scope_id=str(data.get("scope_id") or "party")
            )
            if value and not keeper and value.get("visibility") != "player":
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
        hits = modules.search(campaign_id=campaign_id, query=str(data["query"]))
        values = [asdict(item) for item in hits]
        return {
            "hits": values
            if keeper
            else [item for item in values if item.get("metadata", {}).get("visibility") == "player"]
        }

    @mcp.tool()
    def module_change(
        action: Literal["import", "set_progress"],
        campaign_id: str,
        data: dict[str, Any],
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        require_dm(campaign_id, principal_id)
        if action == "import":
            content = str(data.get("content") or "")
            title = str(data.get("title") or "").strip()
            source_key = str(data.get("source_key") or "").strip()
            if not all((content, title, source_key)):
                raise ValueError("data.content, data.title and data.source_key are required")
            artifact = storage.write_module(source_key, content)
            result = modules.ingest(
                campaign_id=campaign_id,
                source_key=source_key,
                title=title,
                content=content,
                parser=parser,
                source_path=str(artifact),
            )
            return asdict(result)
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
        access.require_campaign(campaign_id, principal_id)
        data = dict(data or {})
        values = (
            memories.search(campaign_id, str(data.get("query") or ""))
            if action == "search"
            else memories.list(campaign_id, kind=data.get("kind"))
        )
        return {"memories": [asdict(item) for item in values]}

    @mcp.tool()
    def memory_change(
        action: Literal["add", "revise"],
        campaign_id: str,
        data: dict[str, Any],
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        require_dm(campaign_id, principal_id)
        if action == "add":
            return asdict(
                memories.add(
                    campaign_id,
                    content=str(data.get("content") or ""),
                    kind=str(data.get("kind") or "fact"),
                    subject=str(data.get("subject") or ""),
                    metadata=dict(data.get("metadata") or {}),
                )
            )
        return asdict(
            memories.revise(
                str(data.get("memory_id") or ""),
                content=str(data.get("content") or ""),
                metadata=dict(data.get("metadata") or {}),
            )
        )

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
        values = (
            knowledge.search(campaign_id, actor_id=actor_id, query=str(data.get("query") or ""))
            if action == "search"
            else knowledge.list(campaign_id, actor_id=actor_id)
        )
        return {"knowledge": [asdict(item) for item in values]}

    @mcp.tool()
    def actor_knowledge_change(
        action: Literal["add", "revise"],
        campaign_id: str,
        actor_id: str,
        data: dict[str, Any],
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        actor_access(campaign_id, actor_id, principal_id, control=True)
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
                    cause=str(data.get("cause") or "witnessed"),
                    disclosure_scope=str(data.get("disclosure_scope") or "dm"),
                )
            )
        item = knowledge.get(str(data.get("knowledge_id") or ""))
        if item.actor_id != actor_id:
            raise PermissionError("knowledge item belongs to another actor")
        return asdict(
            knowledge.revise(
                item.id,
                proposition=str(data.get("proposition") or ""),
                epistemic_status=str(data.get("epistemic_status") or "known"),
                confidence=int(data.get("confidence", 3)),
                cause=str(data.get("cause") or "told_by"),
                disclosure_scope=str(data.get("disclosure_scope") or "dm"),
            )
        )

    @mcp.tool()
    def snapshot_query(
        action: Literal["list", "lineage"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        access.require_campaign(campaign_id, principal_id)
        data = dict(data or {})
        values = (
            snapshots.lineage(campaign_id, slot=data.get("slot"))
            if action == "lineage"
            else snapshots.list(campaign_id)
        )
        return {"snapshots": [asdict(item) for item in values]}

    @mcp.tool()
    def snapshot_change(
        action: Literal["create", "restore"],
        campaign_id: str,
        data: dict[str, Any],
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        require_dm(campaign_id, principal_id)
        if action == "create":
            return asdict(snapshots.create(campaign_id, label=str(data.get("label") or "")))
        return asdict(snapshots.restore(campaign_id, int(data["slot"])))

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
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        access.require_campaign(campaign_id, principal_id)
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

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
