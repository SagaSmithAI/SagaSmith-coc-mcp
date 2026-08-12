"""MCP surface for SagaSmith Call of Cthulhu 7e."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

from mcp.server.fastmcp import FastMCP
from sagasmith_coc.content_packages import (
    build_module_content_package,
    validate_coc_content_package,
)
from sagasmith_coc.engine.checks.chase import (
    resolve_chase_action,
    resolve_chase_speed_check,
)
from sagasmith_coc.engine.checks.combat import resolve_melee_attack, resolve_ranged_attack
from sagasmith_coc.engine.checks.sanity import resolve_sanity_loss
from sagasmith_coc.engine.checks.skill import resolve_opposed_check, resolve_skill_check
from sagasmith_coc.engine.dice.rolls import roll_d100, roll_dice_expression
from sagasmith_coc.module_profile import CocModuleProfile
from sagasmith_coc.random_stream import (
    CampaignRandomStream,
    initial_random_stream,
    use_random_stream,
)
from sagasmith_coc.statblocks import coc7e_statblock_readiness, validate_coc7e_statblock
from sagasmith_coc.system import validate_investigator_sheet
from sagasmith_core import (
    AccessService,
    ActorKnowledgeService,
    BranchService,
    CampaignService,
    CharacterService,
    IdempotencyService,
    IdempotencyWrite,
    ImportJobService,
    MemoryService,
    ModuleService,
    RevisionService,
    SnapshotService,
    StateMutationService,
    apply_document_page_revisions,
    default_local_principal,
    extract_pdf_page_text,
    normalize_document,
    normalized_document_page_text,
    render_pdf_page,
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
    branches = BranchService(storage.database)
    characters = CharacterService(storage.database)
    access = AccessService(storage.database)
    memories = MemoryService(storage.database)
    knowledge = ActorKnowledgeService(storage.database)
    modules = ModuleService(storage.database)
    import_jobs = ImportJobService(storage.database)
    snapshots = SnapshotService(storage.database)
    revisions = RevisionService(storage.database)
    idempotency = IdempotencyService(storage.database)
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
        next_state = {**dict(campaign.state), "random_stream": stream.persisted_state()}
        response = {
            "resolution": result,
            "campaign_revision": campaign.revision + 1,
            "random_stream_receipt": stream.receipt(),
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
            "content_pack": {
                "format": "sagasmith.content-package",
                "schema_version": 2,
                "kinds": ["module"],
                "draft_stages": [
                    "module_draft(start)",
                    "module_draft(edit:advance)",
                    "module_draft(evidence)",
                    "module_draft(edit:source_text|content|statblock|asset|actor)",
                    "module_draft(edit:package)",
                    "module_draft(finalize)",
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
            character_type = str(data.get("character_type") or "investigator")
            if character_type not in {"investigator", "npc", "creature"}:
                raise ValueError("character_type must be investigator, npc, or creature")
            if character_type != "investigator" and membership.role not in {"owner", "dm"}:
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

    @mcp.tool()
    def content_pack(
        action: Literal["list", "get", "import", "export", "activate", "deactivate", "remove"],
        campaign_id: str,
        data: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        """Inspect and manage finalized CoC Module Pack archives."""

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
                    for item in import_jobs.list(campaign_id, kind="module")
                    if dict(item.result or {}).get("finalized_package")
                ],
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
            if package["kind"] != "module" or package["system_id"] != "coc7e":
                raise ValueError("content package must be a coc7e module")
            managed = storage.write_content_archive(package, blobs)
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
        action: Literal["set_progress"],
        campaign_id: str,
        data: dict[str, Any],
        principal_id: str = "system:local",
    ) -> dict[str, Any]:
        require_dm(campaign_id, principal_id)
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
                idempotency_key=key,
                idempotency_write=IdempotencyWrite(
                    scope=scope,
                    payload=payload,
                    response=lambda value: {
                        "branch": asdict(value["branch"]),
                        "snapshot": (
                            asdict(value["snapshot"]) if value["snapshot"] is not None else None
                        ),
                    },
                ),
            )
            return {
                "branch": asdict(created),
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
            idempotency_key=key,
            idempotency_write=IdempotencyWrite(
                scope=scope,
                payload=payload,
                response=lambda value: {
                    "branch": asdict(value["branch"]),
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
    def coc_check(
        campaign_id: str,
        skill_name: str,
        difficulty: Literal["regular", "hard", "extreme"],
        expected_revision: int,
        idempotency_key: str,
        actor_id: str | None = None,
        threshold: int | None = None,
        bonus_dice: int = 0,
        penalty_dice: int = 0,
        pushed: bool = False,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        """Roll and resolve one source-explicit CoC check from campaign randomness."""

        resolved_threshold = threshold
        actor_name = ""
        if actor_id is not None:
            actor_access(campaign_id, actor_id, principal_id, control=True)
            actor = characters.get(actor_id)
            actor_name = actor.name
            sheet = validate_investigator_sheet(dict(actor.sheet))
            folded_name = skill_name.casefold()
            skill_matches = [
                int(value)
                for name, value in dict(sheet.get("skills") or {}).items()
                if str(name).casefold() == folded_name
            ]
            characteristic_matches = [
                int(value)
                for name, value in dict(sheet.get("characteristics") or {}).items()
                if str(name).casefold() == folded_name
            ]
            matches = skill_matches or characteristic_matches
            if not matches:
                raise ValueError(f"actor sheet has no skill or characteristic {skill_name!r}")
            resolved_threshold = matches[0]
        if resolved_threshold is None:
            raise ValueError("threshold or actor_id is required")
        payload = {
            "actor_id": actor_id,
            "skill_name": skill_name,
            "threshold": int(resolved_threshold),
            "difficulty": difficulty,
            "bonus_dice": bonus_dice,
            "penalty_dice": penalty_dice,
            "pushed": pushed,
        }

        def resolve() -> dict[str, Any]:
            rolled = roll_d100(bonus_dice=bonus_dice, penalty_dice=penalty_dice)
            outcome = resolve_skill_check(
                d100_total=int(rolled["total"]),
                threshold=int(resolved_threshold),
                difficulty=difficulty,
                bonus_dice=bonus_dice,
                penalty_dice=penalty_dice,
                skill_name=skill_name,
                investigator_name=actor_name,
            )
            return {"roll": rolled, "outcome": outcome, "pushed": pushed}

        return authoritative_random_resolution(
            campaign_id=campaign_id,
            principal_id=principal_id,
            operation="coc_check",
            payload=payload,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            resolve=resolve,
        )

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
