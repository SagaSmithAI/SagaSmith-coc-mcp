from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sagasmith_coc.random_stream import initial_random_stream
from sagasmith_core import CharacterService, Database, ModuleService
from sagasmith_core.database import sqlite_database_url
from sagasmith_core.models import CampaignSnapshot

from sagasmith_coc_mcp import server as server_module
from sagasmith_coc_mcp.config import McpConfig
from sagasmith_coc_mcp.exposure import ExposureError, ExposureRegistry
from sagasmith_coc_mcp.server import create_server
from sagasmith_coc_mcp.tool_profiles import CORE_TOOLS


async def call(server, name: str, arguments: dict):
    if name == "character_change" and arguments.get("action") in {"create", "instantiate"}:
        data = arguments["data"]
        data.setdefault(
            "idempotency_key",
            f"test-{arguments['action']}-{data.get('name') or data.get('template_id')}",
        )
        if "expected_campaign_revision" not in data:
            _, campaign = await server.call_tool(
                "campaign_query",
                {"action": "get", "campaign_id": arguments["campaign_id"]},
            )
            data["expected_campaign_revision"] = campaign["revision"]
    _, result = await server.call_tool(name, arguments)
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    return result.get("result", result) if isinstance(result, dict) else result


def write_text_pdf(path: Path, lines: list[str]) -> None:
    """Write a small extractable PDF without adding a test-only runtime dependency."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    commands = ["BT /F1 12 Tf 20 260 Td"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            commands.append("0 -20 Td")
        commands.append(f"({escaped}) Tj")
    commands.append("ET")
    content = DecodedStreamObject()
    content.set_data(" ".join(commands).encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as stream:
        writer.write(stream)


def synthetic_pack_decisions(receipt: dict, *, title: str = "Synthetic Case") -> dict:
    return {
        "manifest": {
            "title": title,
            "classification": "scenario",
            "compatibility": {
                "editions": ["7e"],
                "required_capabilities": ["module_pack_v2"],
            },
            "play_profile": {
                "investigator_count": {
                    "minimum": 1,
                    "maximum": 4,
                    "source_refs": [receipt],
                },
                "ruleset": {
                    "supported": ["classic"],
                    "recommended": "classic",
                    "source_refs": [receipt],
                },
                "era": {"value": "1920s", "source_refs": [receipt]},
                "estimated_sessions": {
                    "minimum": 1,
                    "maximum": 1,
                    "source_refs": [receipt],
                },
                "pregenerated_characters": {
                    "available": False,
                    "applicability": "None",
                    "source_refs": [receipt],
                },
                "solo_play": {"supported": False, "source_refs": [receipt]},
            },
            "continuity": {
                "series_id": None,
                "order": None,
                "continues_from": None,
                "state_policy": {},
            },
            "activation": {"mode": "campaign_attach", "default_active": False},
        },
        "catalogs": {
            "clues": [{"id": "clue:synthetic", "source_refs": [receipt]}],
            "handouts": [],
            "encounters": [],
            "hazards": [],
            "tomes": [],
            "spells": [],
            "mechanics": [],
        },
        "narrative": {
            "dossiers": [],
            "endings": [{"id": "ending:resolved", "trigger": "resolve the case"}],
        },
        "metadata": {"license": "private", "attribution": "Synthetic test"},
        "version": "1.0.0",
    }


def test_create_server_preloads_optional_pdf_runtime(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        server_module,
        "_preload_optional_pdf_runtime",
        lambda: calls.append("pdf"),
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "missing-coc-skills",
        modulegen_skills_dir=tmp_path / "missing-modulegen-skills",
    )

    server_module.create_server(config)

    assert calls == ["pdf"]


def test_coc_mcp_persists_campaign_modules_and_actor_knowledge(tmp_path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "missing-coc-skills",
        modulegen_skills_dir=tmp_path / "missing-modulegen-skills",
    )
    server = create_server(config)

    async def scenario() -> tuple[str, str, str]:
        capabilities = await call(server, "server_capabilities", {})
        assert capabilities["progressive_exposure"] is True
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "The Haunting", "idempotency_key": "campaign-1"},
            },
        )
        campaign_id = campaign["id"]
        alice = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign_id,
                "data": {"name": "Alice", "sheet": {"pow": 60}},
            },
        )
        bob = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign_id,
                "data": {"name": "Bob", "sheet": {"pow": 55}},
            },
        )
        for principal, actor_id in (("player:alice", alice["id"]), ("player:bob", bob["id"])):
            await call(
                server,
                "campaign_change",
                {
                    "action": "grant_campaign",
                    "campaign_id": campaign_id,
                    "data": {"target_principal_id": principal, "role": "player"},
                },
            )
            await call(
                server,
                "campaign_change",
                {
                    "action": "grant_actor",
                    "campaign_id": campaign_id,
                    "data": {"target_principal_id": principal, "actor_id": actor_id},
                },
            )
        await call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "campaign_id": campaign_id,
                "actor_id": alice["id"],
                "data": {
                    "knowledge_key": "attic-whisper",
                    "proposition": "The whisper came from the attic.",
                    "disclosure_scope": "owner",
                },
                "idempotency_key": "alice-attic-whisper",
            },
        )
        alice_knowledge = await call(
            server,
            "actor_knowledge_query",
            {
                "action": "list",
                "campaign_id": campaign_id,
                "actor_id": alice["id"],
                "principal_id": "player:alice",
            },
        )
        assert alice_knowledge["knowledge"][0]["knowledge_key"] == "attic-whisper"
        with pytest.raises(Exception, match="cannot access actor"):
            await call(
                server,
                "actor_knowledge_query",
                {
                    "action": "list",
                    "campaign_id": campaign_id,
                    "actor_id": alice["id"],
                    "principal_id": "player:bob",
                },
            )
        draft = await call(
            server,
            "module_draft",
            {
                "action": "start",
                "campaign_id": campaign_id,
                "data": {
                    "name": "haunting.md",
                    "source_key": "haunting.md",
                    "title": "The Haunting",
                    "content": (
                        "# Boston\n## Corbitt House\nTwo investigators arrive in the 1920s. "
                        "A hidden clue waits upstairs.\n## Handout: Public Notice\n"
                        "The public notice warns visitors.\n## Ending\nThe case is solved."
                    ),
                },
                "idempotency_key": "haunting-draft",
            },
        )
        assert draft["status"] == "editing"
        draft_replay = await call(
            server,
            "module_draft",
            {
                "action": "start",
                "campaign_id": campaign_id,
                "data": {
                    "name": "haunting.md",
                    "source_key": "haunting.md",
                    "title": "The Haunting",
                    "content": (
                        "# Boston\n## Corbitt House\nTwo investigators arrive in the 1920s. "
                        "A hidden clue waits upstairs.\n## Handout: Public Notice\n"
                        "The public notice warns visitors.\n## Ending\nThe case is solved."
                    ),
                },
                "idempotency_key": "haunting-draft",
            },
        )
        assert draft_replay == draft
        evidence = await call(
            server,
            "module_draft",
            {
                "action": "evidence",
                "campaign_id": campaign_id,
                "data": {"job_id": draft["job_id"]},
            },
        )
        receipt = evidence["evidence"][0]["source_ref"]
        decisions = {
            "manifest": {
                "title": "The Haunting",
                "classification": "scenario",
                "compatibility": {
                    "editions": ["7e"],
                    "required_capabilities": ["module_pack_v2"],
                },
                "play_profile": {
                    "investigator_count": {
                        "minimum": 2,
                        "maximum": 2,
                        "source_refs": [receipt],
                    },
                    "ruleset": {
                        "supported": ["classic"],
                        "recommended": "classic",
                        "source_refs": [receipt],
                    },
                    "era": {"value": "1920s", "source_refs": [receipt]},
                    "estimated_sessions": {
                        "minimum": 1,
                        "maximum": 1,
                        "source_refs": [receipt],
                    },
                    "pregenerated_characters": {
                        "available": False,
                        "applicability": "None",
                        "source_refs": [receipt],
                    },
                    "solo_play": {"supported": False, "source_refs": [receipt]},
                },
                "continuity": {
                    "series_id": None,
                    "order": None,
                    "continues_from": None,
                    "state_policy": {},
                },
                "activation": {"mode": "campaign_attach", "default_active": False},
            },
            "catalogs": {
                "clues": [{"id": "clue:hidden", "source_refs": [receipt]}],
                "handouts": [],
                "encounters": [],
                "hazards": [],
                "tomes": [],
                "spells": [],
                "mechanics": [],
            },
            "narrative": {
                "dossiers": [],
                "endings": [{"id": "ending:solved", "trigger": "solve the case"}],
            },
            "metadata": {"license": "private", "attribution": "Synthetic test"},
            "version": "1.0.0",
        }
        edited = await call(
            server,
            "module_draft",
            {
                "action": "edit",
                "campaign_id": campaign_id,
                "data": {
                    "job_id": draft["job_id"],
                    "operation": "package",
                    **decisions,
                },
                "expected_revision": draft["job"]["revision"],
                "idempotency_key": "haunting-decisions",
            },
        )
        finalized = await call(
            server,
            "module_draft",
            {
                "action": "finalize",
                "campaign_id": campaign_id,
                "data": {
                    "job_id": draft["job_id"],
                    "package_id": "coc7e.module.haunting.synthetic",
                    "confirmation": {"confirmed": True, "note": "Reviewed all source facts."},
                },
                "expected_revision": edited["job"]["revision"],
                "idempotency_key": "haunting-finalize",
            },
        )
        campaign_revision = (
            await call(
                server,
                "campaign_query",
                {"action": "get", "campaign_id": campaign_id},
            )
        )["revision"]
        imported = await call(
            server,
            "content_pack",
            {
                "action": "import",
                "campaign_id": campaign_id,
                "data": {"artifact": finalized["artifact"]},
                "expected_revision": campaign_revision,
                "idempotency_key": "haunting-pack-import",
            },
        )
        activated = await call(
            server,
            "content_pack",
            {
                "action": "activate",
                "campaign_id": campaign_id,
                "data": {"module_id": imported["module_id"]},
                "expected_revision": campaign_revision,
                "idempotency_key": "haunting-pack-activate",
            },
        )
        assert activated["activation"]["active"] is True
        scenes = await call(
            server,
            "module_query",
            {"action": "index", "campaign_id": campaign_id},
        )
        assert any(scene["title"] == "Corbitt House" for scene in scenes["scenes"])
        player_scenes = await call(
            server,
            "module_query",
            {
                "action": "index",
                "campaign_id": campaign_id,
                "principal_id": "player:alice",
            },
        )
        assert [scene["title"] for scene in player_scenes["scenes"]] == ["Handout: Public Notice"]
        check = await call(
            server,
            "coc_resolve",
            {
                "kind": "skill",
                "campaign_id": campaign_id,
                "data": {"d100_total": 31, "threshold": 60, "difficulty": "regular"},
                "expected_revision": (
                    await call(
                        server,
                        "campaign_query",
                        {"action": "get", "campaign_id": campaign_id},
                    )
                )["revision"],
                "idempotency_key": "haunting-check-1",
            },
        )
        assert check["resolution"]["success"] is True
        campaign = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        branch = await call(
            server,
            "branch_query",
            {"action": "current", "campaign_id": campaign_id},
        )
        await call(
            server,
            "snapshot_change",
            {
                "action": "create",
                "campaign_id": campaign_id,
                "data": {"label": "Start", "expected_head_snapshot_id": ""},
                "expected_revision": campaign["revision"],
                "expected_branch_id": branch["branch"]["id"],
                "idempotency_key": "haunting-snapshot-start",
            },
        )
        return campaign_id, alice["id"], bob["id"]

    campaign_id, alice_id, _ = asyncio.run(scenario())
    restarted = create_server(config)

    async def verify_restart() -> None:
        campaign = await call(
            restarted,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        assert campaign["name"] == "The Haunting"
        values = await call(
            restarted,
            "actor_knowledge_query",
            {"action": "list", "campaign_id": campaign_id, "actor_id": alice_id},
        )
        assert values["knowledge"][0]["proposition"].endswith("attic.")

    asyncio.run(verify_restart())


def test_module_draft_pack_round_trip_is_finalized_replayable_and_cross_campaign(
    tmp_path,
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)

    async def author() -> tuple[str, str, str, dict]:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Authoring", "idempotency_key": "authoring-campaign"},
            },
        )
        draft_args = {
            "action": "start",
            "campaign_id": campaign["id"],
            "data": {
                "name": "lantern.md",
                "title": "The Lantern Case",
                "source_key": "lantern.md",
                "content": (
                    "# The Lantern Case\n## Arrival\nOne to four investigators arrive in "
                    "Arkham in the 1920s.\n## Ending\nThe investigators resolve the case."
                ),
            },
            "idempotency_key": "lantern-start",
        }
        draft = await call(server, "module_draft", draft_args)
        assert await call(server, "module_draft", draft_args) == draft
        evidence = await call(
            server,
            "module_draft",
            {
                "action": "evidence",
                "campaign_id": campaign["id"],
                "data": {"job_id": draft["job_id"], "query": "investigators"},
            },
        )
        receipt = evidence["evidence"][0]["source_ref"]
        edit_args = {
            "action": "edit",
            "campaign_id": campaign["id"],
            "data": {
                "job_id": draft["job_id"],
                "operation": "package",
                **synthetic_pack_decisions(receipt, title="The Lantern Case"),
            },
            "expected_revision": draft["job"]["revision"],
            "idempotency_key": "lantern-edit",
        }
        edited = await call(server, "module_draft", edit_args)
        assert await call(server, "module_draft", edit_args) == edited
        finalize_args = {
            "action": "finalize",
            "campaign_id": campaign["id"],
            "data": {
                "job_id": draft["job_id"],
                "package_id": "coc7e.module.lantern-case",
                "include_package": True,
                "confirmation": {
                    "confirmed": True,
                    "note": "Reviewed scenes, profile, clue, ending, and source evidence.",
                },
            },
            "expected_revision": edited["job"]["revision"],
            "idempotency_key": "lantern-finalize",
        }
        finalized = await call(server, "module_draft", finalize_args)
        assert await call(server, "module_draft", finalize_args) == finalized
        assert finalized["package"]["schema_version"] == 2
        with pytest.raises(Exception, match="mechanically imported draft"):
            await call(
                server,
                "module_draft",
                {
                    **edit_args,
                    "expected_revision": finalized["job"]["revision"],
                    "idempotency_key": "edit-finalized",
                },
            )
        return (
            campaign["id"],
            draft["job_id"],
            finalized["artifact"],
            finalized["package"],
        )

    authoring_id, job_id, artifact, package = asyncio.run(author())
    restarted = create_server(config)

    async def import_elsewhere() -> None:
        persisted = await call(
            restarted,
            "module_draft",
            {
                "action": "get",
                "campaign_id": authoring_id,
                "data": {"job_id": job_id},
            },
        )
        assert persisted["job"]["state"] == "compiled"
        inspected = await call(
            restarted,
            "content_pack",
            {
                "action": "get",
                "campaign_id": authoring_id,
                "data": {"artifact": artifact},
            },
        )
        assert inspected["package"]["checksum"] == package["checksum"]
        campaign = await call(
            restarted,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Playback", "idempotency_key": "playback-campaign"},
            },
        )
        import_args = {
            "action": "import",
            "campaign_id": campaign["id"],
            "data": {"artifact": artifact},
            "expected_revision": campaign["revision"],
            "idempotency_key": "lantern-import",
        }
        with pytest.raises(Exception, match="campaign revision conflict"):
            await call(
                restarted,
                "content_pack",
                {**import_args, "expected_revision": campaign["revision"] + 1},
            )
        imported = await call(restarted, "content_pack", import_args)
        assert await call(restarted, "content_pack", import_args) == imported
        assert imported["activated"] is False
        activate_args = {
            "action": "activate",
            "campaign_id": campaign["id"],
            "data": {"module_id": imported["module_id"]},
            "expected_revision": campaign["revision"],
            "idempotency_key": "lantern-activate",
        }
        activated = await call(restarted, "content_pack", activate_args)
        assert await call(restarted, "content_pack", activate_args) == activated
        assert activated["activation"]["active"] is True
        listed = await call(
            restarted,
            "content_pack",
            {"action": "list", "campaign_id": campaign["id"]},
        )
        assert [item["id"] for item in listed["packs"]] == [imported["module_id"]]
        with pytest.raises(Exception, match="deactivate or replace"):
            await call(
                restarted,
                "content_pack",
                {
                    "action": "remove",
                    "campaign_id": campaign["id"],
                    "data": {"module_id": imported["module_id"]},
                    "expected_revision": campaign["revision"],
                    "idempotency_key": "remove-active",
                },
            )

    asyncio.run(import_elsewhere())


def test_module_draft_advance_resumes_an_interrupted_mechanical_first_pass(
    tmp_path, monkeypatch
) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)
    original_preview = ModuleService.preview_path

    async def exercise() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Interrupted import", "idempotency_key": "advance-campaign"},
            },
        )

        def interrupt_preview(*args, **kwargs):
            raise RuntimeError("simulated interruption after durable staging")

        monkeypatch.setattr(ModuleService, "preview_path", interrupt_preview)
        with pytest.raises(Exception, match="simulated interruption"):
            await call(
                server,
                "module_draft",
                {
                    "action": "start",
                    "campaign_id": campaign["id"],
                    "data": {
                        "name": "interrupted.md",
                        "title": "Interrupted Case",
                        "content": "# Case\n## Scene\nA source-backed clue.\n",
                    },
                    "idempotency_key": "interrupted-start",
                },
            )
        monkeypatch.setattr(ModuleService, "preview_path", original_preview)
        jobs = await call(
            server,
            "module_draft",
            {"action": "get", "campaign_id": campaign["id"]},
        )
        assert len(jobs["jobs"]) == 1
        assert jobs["jobs"][0]["state"] == "staged"
        arguments = {
            "action": "edit",
            "campaign_id": campaign["id"],
            "data": {"job_id": jobs["jobs"][0]["job_id"], "operation": "advance"},
            "idempotency_key": "resume-first-pass",
        }
        advanced = await call(server, "module_draft", arguments)
        assert advanced["job"]["state"] == "imported"
        assert advanced["status"] == "editing"
        assert await call(server, "module_draft", arguments) == advanced

    asyncio.run(exercise())


def test_module_source_path_must_be_inside_configured_import_roots(tmp_path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    allowed_source = allowed / "case.md"
    allowed_source.write_text("# Case\n## Scene\nEvidence.\n", encoding="utf-8")
    outside_source = tmp_path / "outside.md"
    outside_source.write_text("# Outside\n## Scene\nNo.\n", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(allowed,),
    )
    server = create_server(config)

    async def exercise() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Sources", "idempotency_key": "sources-campaign"},
            },
        )
        staged = await call(
            server,
            "module_draft",
            {
                "action": "start",
                "campaign_id": campaign["id"],
                "data": {"source_path": str(allowed_source)},
                "idempotency_key": "allowed-source",
            },
        )
        assert staged["job"]["artifact_checksum"]
        with pytest.raises(Exception, match="outside configured import roots"):
            await call(
                server,
                "module_draft",
                {
                    "action": "start",
                    "campaign_id": campaign["id"],
                    "data": {"source_path": str(outside_source)},
                    "idempotency_key": "outside-source",
                },
            )

    asyncio.run(exercise())


def test_module_draft_content_asset_and_actor_edits_enter_final_pack(tmp_path, monkeypatch) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    handout = allowed / "handout.txt"
    handout.write_text("The lantern bears the Marsh family mark.", encoding="utf-8")
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(allowed,),
    )
    server = create_server(config)

    async def exercise() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Draft edits", "idempotency_key": "edits-campaign"},
            },
        )
        npc = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Mr Marsh",
                    "character_type": "npc",
                    "sheet": {"pow": 55},
                },
            },
        )
        pregen = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Harriet Vane",
                    "sheet": {
                        "characteristics": {"pow": 60, "dex": 70},
                        "skills": {"Spot Hidden": 65},
                    },
                },
            },
        )
        draft = await call(
            server,
            "module_draft",
            {
                "action": "start",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "marsh-case.md",
                    "title": "The Marsh Case",
                    "content": (
                        "# The Marsh Case\n## Study\nOne to four investigators search the "
                        "1920s study. The lantern bears a family mark.\n"
                        "## Ending\nThe investigators resolve the case."
                    ),
                },
                "idempotency_key": "edits-start",
            },
        )
        evidence = await call(
            server,
            "module_draft",
            {
                "action": "evidence",
                "campaign_id": campaign["id"],
                "data": {"job_id": draft["job_id"], "query": "lantern"},
            },
        )
        chunk = evidence["evidence"][0]
        content_args = {
            "action": "edit",
            "campaign_id": campaign["id"],
            "data": {
                "job_id": draft["job_id"],
                "operation": "content",
                "scene_id": chunk["scene_id"],
                "content_key": "clue:lantern-mark",
                "content_kind": "clue",
                "normalized_content": "The lantern bears the Marsh family mark.",
                "source_chunk_ids": [chunk["id"]],
                "observation": "Transcribed the clue from the source chunk.",
            },
            "expected_revision": draft["job"]["revision"],
            "idempotency_key": "edits-content",
        }
        content_edit = await call(server, "module_draft", content_args)
        assert await call(server, "module_draft", content_args) == content_edit
        statblock_args = {
            "action": "edit",
            "campaign_id": campaign["id"],
            "data": {
                "job_id": draft["job_id"],
                "operation": "statblock",
                "scene_id": chunk["scene_id"],
                "content_key": "statblock:mr-marsh",
                "statblock": {
                    "actor_type": "npc",
                    "name": "Mr Marsh",
                    "characteristics": {"dex": 55},
                    "skills": {"Persuade": 50},
                    "notes": ["The source prints only social statistics."],
                },
                "source_chunk_ids": [chunk["id"]],
                "observation": "Reviewed the partial NPC card without filling absent combat data.",
            },
            "expected_revision": content_edit["job"]["revision"],
            "idempotency_key": "edits-statblock",
        }
        statblock_edit = await call(server, "module_draft", statblock_args)
        assert await call(server, "module_draft", statblock_args) == statblock_edit
        assert statblock_edit["runtime_readiness"]["combat_ready"] is False
        assert "hit_points" in statblock_edit["runtime_readiness"]["missing_for_combat"]
        with pytest.raises(Exception, match="use operation=statblock"):
            await call(
                server,
                "module_draft",
                {
                    "action": "edit",
                    "campaign_id": campaign["id"],
                    "data": {
                        "job_id": draft["job_id"],
                        "operation": "content",
                        "scene_id": chunk["scene_id"],
                        "content_key": "statblock:bypass",
                        "content_kind": "coc7e_statblock",
                        "normalized_content": "{}",
                        "source_chunk_ids": [chunk["id"]],
                        "observation": "Attempted bypass.",
                    },
                    "expected_revision": statblock_edit["job"]["revision"],
                    "idempotency_key": "edits-statblock-bypass",
                },
            )
        asset_args = {
            "action": "edit",
            "campaign_id": campaign["id"],
            "data": {
                "job_id": draft["job_id"],
                "operation": "asset",
                "source_path": str(handout),
                "asset_kind": "handout",
                "scene_id": chunk["scene_id"],
                "title": "Lantern Mark",
            },
            "expected_revision": statblock_edit["job"]["revision"],
            "idempotency_key": "edits-asset",
        }
        asset_edit = await call(server, "module_draft", asset_args)
        assert await call(server, "module_draft", asset_args) == asset_edit
        actor_args = {
            "action": "edit",
            "campaign_id": campaign["id"],
            "data": {
                "job_id": draft["job_id"],
                "operation": "actor",
                "character_id": npc["id"],
                "actor_card_id": "coc7e.actor.mr-marsh",
                "binding_kind": "cast",
                "role": "witness",
                "scene_id": chunk["scene_id"],
            },
            "expected_revision": asset_edit["job"]["revision"],
            "idempotency_key": "edits-actor",
        }
        actor_edit = await call(server, "module_draft", actor_args)
        assert await call(server, "module_draft", actor_args) == actor_edit
        pregen_edit = await call(
            server,
            "module_draft",
            {
                "action": "edit",
                "campaign_id": campaign["id"],
                "data": {
                    "job_id": draft["job_id"],
                    "operation": "actor",
                    "character_id": pregen["id"],
                    "actor_card_id": "coc7e.actor.harriet-vane",
                    "binding_kind": "preset_pc",
                    "role": "investigator",
                },
                "expected_revision": actor_edit["job"]["revision"],
                "idempotency_key": "edits-pregen",
            },
        )
        package_edit = await call(
            server,
            "module_draft",
            {
                "action": "edit",
                "campaign_id": campaign["id"],
                "data": {
                    "job_id": draft["job_id"],
                    "operation": "package",
                    **synthetic_pack_decisions(chunk["source_ref"], title="The Marsh Case"),
                },
                "expected_revision": pregen_edit["job"]["revision"],
                "idempotency_key": "edits-package",
            },
        )
        finalized = await call(
            server,
            "module_draft",
            {
                "action": "finalize",
                "campaign_id": campaign["id"],
                "data": {
                    "job_id": draft["job_id"],
                    "package_id": "coc7e.module.marsh-case",
                    "include_package": True,
                    "confirmation": {
                        "confirmed": True,
                        "note": "Reviewed content, handout, cast, profile, and ending.",
                    },
                },
                "expected_revision": package_edit["job"]["revision"],
                "idempotency_key": "edits-finalize",
            },
        )
        package = finalized["package"]
        assert [actor["id"] for actor in package["actors"]] == [
            "coc7e.actor.harriet-vane",
            "coc7e.actor.mr-marsh",
        ]
        assert [item["kind"] for item in package["content_reviews"]] == [
            "clue",
            "coc7e_statblock",
        ]
        stored_statblock = json.loads(package["content_reviews"][1]["normalized_content"])
        assert stored_statblock["characteristics"] == {"dex": 55}
        assert "hit_points" not in stored_statblock
        assert any(asset["kind"] == "handout" for asset in package["assets"])
        stored = await call(
            server,
            "module_draft",
            {
                "action": "evidence",
                "campaign_id": campaign["id"],
                "data": {"job_id": draft["job_id"], "kind": "reviews"},
            },
        )
        assert stored["reviews"][0]["content_key"] == "clue:lantern-mark"

        target = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Recovered import", "idempotency_key": "recovered-campaign"},
            },
        )
        import_args = {
            "action": "import",
            "campaign_id": target["id"],
            "data": {"artifact": finalized["artifact"]},
            "expected_revision": target["revision"],
            "idempotency_key": "recovered-pack-import",
        }
        original_bind = ModuleService.bind_actor
        original_import_actor = CharacterService.import_content_actor
        interrupted = False
        imported_asset_maps: list[dict] = []

        def capture_package_assets(self, actor, **kwargs):
            imported_asset_maps.append(dict(kwargs.get("assets_by_key") or {}))
            return original_import_actor(self, actor, **kwargs)

        def interrupt_after_binding(self, *args, **kwargs):
            nonlocal interrupted
            result = original_bind(self, *args, **kwargs)
            if not interrupted:
                interrupted = True
                raise RuntimeError("simulated interruption after actor binding")
            return result

        monkeypatch.setattr(CharacterService, "import_content_actor", capture_package_assets)
        monkeypatch.setattr(ModuleService, "bind_actor", interrupt_after_binding)
        with pytest.raises(Exception, match="simulated interruption"):
            await call(server, "content_pack", import_args)
        assert await call(
            server,
            "content_pack",
            {"action": "list", "campaign_id": target["id"]},
        ) == {"packs": [], "finalized_drafts": [], "rule_packs": []}
        assert await call(
            server,
            "character_query",
            {"action": "list", "campaign_id": target["id"]},
        ) == {"characters": []}
        monkeypatch.setattr(ModuleService, "bind_actor", original_bind)
        imported = await call(server, "content_pack", import_args)
        assert imported_asset_maps
        assert all(
            any(asset["kind"] == "handout" for asset in assets.values())
            for assets in imported_asset_maps
        )
        assert await call(server, "content_pack", import_args) == imported
        target_modules = await call(
            server,
            "content_pack",
            {"action": "list", "campaign_id": target["id"]},
        )
        assert [item["id"] for item in target_modules["packs"]] == [imported["module_id"]]
        target_characters = await call(
            server,
            "character_query",
            {"action": "list", "campaign_id": target["id"]},
        )
        assert [item["name"] for item in target_characters["characters"]] == ["Mr Marsh"]
        instantiated = await call(
            server,
            "character_change",
            {
                "action": "instantiate",
                "campaign_id": target["id"],
                "data": {
                    "template_id": imported["actor_map"]["coc7e.actor.harriet-vane"],
                    "player_name": "Player One",
                },
            },
        )
        assert instantiated["template_id"] == imported["actor_map"]["coc7e.actor.harriet-vane"]
        assert instantiated["campaign_id"] == target["id"]
        assert instantiated["sheet"]["skills"]["Spot Hidden"] == 65
        target_characters = await call(
            server,
            "character_query",
            {"action": "list", "campaign_id": target["id"]},
        )
        assert [item["name"] for item in target_characters["characters"]] == [
            "Harriet Vane",
            "Mr Marsh",
        ]

        roll_one = await call(
            server,
            "coc_dice_roll",
            {
                "kind": "d100",
                "campaign_id": target["id"],
                "expected_revision": target["revision"],
                "idempotency_key": "recovered-roll-1",
            },
        )
        assert await call(server, "content_pack", import_args) == imported
        activate_args = {
            "action": "activate",
            "campaign_id": target["id"],
            "data": {"module_id": imported["module_id"]},
            "expected_revision": roll_one["campaign_revision"],
            "idempotency_key": "recovered-activate",
        }
        activated = await call(server, "content_pack", activate_args)
        roll_two = await call(
            server,
            "coc_dice_roll",
            {
                "kind": "d100",
                "campaign_id": target["id"],
                "expected_revision": roll_one["campaign_revision"],
                "idempotency_key": "recovered-roll-2",
            },
        )
        assert await call(server, "content_pack", activate_args) == activated
        deactivate_args = {
            "action": "deactivate",
            "campaign_id": target["id"],
            "data": {"module_id": imported["module_id"]},
            "expected_revision": roll_two["campaign_revision"],
            "idempotency_key": "recovered-deactivate",
        }
        deactivated = await call(server, "content_pack", deactivate_args)
        assert deactivated["deactivation"]["active"] is False
        roll_three = await call(
            server,
            "coc_dice_roll",
            {
                "kind": "d100",
                "campaign_id": target["id"],
                "expected_revision": roll_two["campaign_revision"],
                "idempotency_key": "recovered-roll-3",
            },
        )
        assert await call(server, "content_pack", deactivate_args) == deactivated
        remove_args = {
            "action": "remove",
            "campaign_id": target["id"],
            "data": {"module_id": imported["module_id"]},
            "expected_revision": roll_three["campaign_revision"],
            "idempotency_key": "recovered-remove",
        }
        removed = await call(server, "content_pack", remove_args)
        assert removed == {"module_id": imported["module_id"], "removed": True}
        assert await call(server, "content_pack", remove_args) == removed

    asyncio.run(exercise())


def test_pdf_page_evidence_and_source_text_revision_are_checksum_bound(tmp_path) -> None:
    imports = tmp_path / "imports"
    imports.mkdir()
    source = imports / "review.pdf"
    write_text_pdf(
        source,
        [
            "# Lantern Case",
            "## Sceen",
            "One investigator finds a clue in the 1920s study.",
            "## Ending",
            "The case is resolved.",
        ],
    )
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
        module_import_roots=(imports,),
    )
    server = create_server(config)

    async def exercise() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "PDF review", "idempotency_key": "pdf-campaign"},
            },
        )
        draft = await call(
            server,
            "module_draft",
            {
                "action": "start",
                "campaign_id": campaign["id"],
                "data": {"source_path": str(source)},
                "idempotency_key": "pdf-start",
            },
        )
        page = await call(
            server,
            "module_draft",
            {
                "action": "evidence",
                "campaign_id": campaign["id"],
                "data": {
                    "job_id": draft["job_id"],
                    "kind": "page",
                    "page_number": 1,
                    "scale": 1.0,
                },
            },
        )
        assert page["source_checksum"] == draft["job"]["artifact_checksum"]
        assert Path(page["image"]["managed_path"]).read_bytes().startswith(b"\x89PNG")
        assert "Sceen" in page["normalized"]["text"]
        review_args = {
            "action": "edit",
            "campaign_id": campaign["id"],
            "data": {
                "job_id": draft["job_id"],
                "operation": "source_text",
                "page_number": 1,
                "base_text_sha256": page["normalized"]["text_sha256"],
                "replacements": [{"old": "Sceen", "new": "Scene"}],
                "rationale": "Correct a bounded heading transcription typo.",
                "evidence_basis": "agent_context",
                "review_method": "agent",
            },
            "expected_revision": draft["job"]["revision"],
            "idempotency_key": "pdf-source-review",
        }
        reviewed = await call(server, "module_draft", review_args)
        assert await call(server, "module_draft", review_args) == reviewed
        assert reviewed["review"]["source_checksum"] == page["source_checksum"]
        assert reviewed["module_id"] != draft["module_id"]
        corrected = await call(
            server,
            "module_draft",
            {
                "action": "evidence",
                "campaign_id": campaign["id"],
                "data": {"job_id": draft["job_id"], "query": "Scene"},
            },
        )
        assert corrected["evidence"]
        assert any("Scene" in item["content"] for item in corrected["evidence"])
        with pytest.raises(Exception, match="base_text_sha256"):
            await call(
                server,
                "module_draft",
                {
                    **review_args,
                    "data": {
                        **review_args["data"],
                        "base_text_sha256": "0" * 64,
                    },
                    "expected_revision": reviewed["job"]["revision"],
                    "idempotency_key": "pdf-forged-base",
                },
            )

    asyncio.run(exercise())


def test_random_roll_is_atomic_idempotent_and_persists_across_restart(tmp_path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)

    async def exercise() -> tuple[str, dict]:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Random stream", "idempotency_key": "random-campaign"},
            },
        )
        arguments = {
            "kind": "d100",
            "campaign_id": campaign["id"],
            "expected_revision": campaign["revision"],
            "idempotency_key": "roll-1",
            "bonus_dice": 1,
        }
        first = await call(server, "coc_dice_roll", arguments)
        replay = await call(server, "coc_dice_roll", arguments)
        assert replay == first
        current = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign["id"]},
        )
        assert current["state"]["random_stream"]["position"] == 3
        assert first["random_stream_receipt"]["draw_count"] == 3
        with pytest.raises(Exception, match="revision.*conflict"):
            await call(
                server,
                "coc_dice_roll",
                {**arguments, "idempotency_key": "roll-stale"},
            )
        return campaign["id"], first

    campaign_id, first = asyncio.run(exercise())
    restarted = create_server(config)

    async def verify() -> None:
        current = await call(
            restarted,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        assert current["state"]["random_stream"]["last_receipt"] == first["random_stream_receipt"]

    asyncio.run(verify())


def test_sanity_check_commits_rolls_actor_state_and_bout_atomically(tmp_path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)

    async def exercise() -> tuple[str, str, dict]:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Sanity case", "idempotency_key": "sanity-campaign"},
            },
        )
        investigator = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Dr Armitage",
                    "sheet": {"characteristics": {"pow": 80, "int": 100}},
                },
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_campaign",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": "player:armitage", "role": "player"},
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_actor",
                "campaign_id": campaign["id"],
                "data": {
                    "target_principal_id": "player:armitage",
                    "actor_id": investigator["id"],
                },
            },
        )
        play = await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign["id"],
                "data": {"phase": "play", "expected_revision": campaign["revision"]},
            },
        )
        arguments = {
            "campaign_id": campaign["id"],
            "actor_id": investigator["id"],
            "success_loss": "5",
            "failure_loss": "5",
            "source": "Witnessed a source-backed Mythos manifestation.",
            "context": "real_time",
            "expected_revision": play["revision"],
            "expected_character_revision": investigator["revision"],
            "idempotency_key": "sanity-manifestation",
            "principal_id": "player:armitage",
        }
        settled = await call(server, "coc_sanity_check", arguments)
        assert await call(server, "coc_sanity_check", arguments) == settled
        assert settled["san"] == 75
        assert settled["conditions"]["temporary_insanity"] is True
        assert settled["conditions"]["indefinite_insanity"] is False
        assert settled["resolution"]["bout"]["duration_unit"] == "rounds"
        assert settled["random_stream_receipt"]["draw_count"] == 6
        presentation = await call(
            server,
            "resolution_presentation",
            {
                "campaign_id": campaign["id"],
                "resolution_id": settled["resolution_id"],
                "principal_id": "player:armitage",
            },
        )
        assert presentation["operation"] == "coc_sanity_check"
        assert presentation["audience"]["actor_refs"] == [investigator["id"]]
        assert len(presentation["rolls"]) >= 3
        assert presentation["outcome"]["san_loss"] == 5
        actor = await call(
            server,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": investigator["id"],
                "principal_id": "player:armitage",
            },
        )
        assert actor["sheet"]["san"] == 75
        assert actor["sheet"]["san_daily_loss"] == 5
        assert actor["sheet"]["sanity_loss_events"][0]["source"].startswith("Witnessed")
        history = await call(
            server,
            "state_revision",
            {"action": "history", "campaign_id": campaign["id"]},
        )
        assert {item["entity_type"] for item in history["revisions"][:2]} == {
            "campaign",
            "character",
        }
        with pytest.raises(Exception, match="different request"):
            await call(
                server,
                "coc_sanity_check",
                {**arguments, "source": "A different manifestation."},
            )
        return campaign["id"], investigator["id"], settled

    campaign_id, actor_id, settled = asyncio.run(exercise())
    restarted = create_server(config)

    async def verify_restart() -> None:
        campaign = await call(
            restarted,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        actor = await call(
            restarted,
            "character_query",
            {"action": "get", "campaign_id": campaign_id, "character_id": actor_id},
        )
        assert (
            campaign["state"]["random_stream"]["last_receipt"] == settled["random_stream_receipt"]
        )
        assert actor["sheet"]["conditions"]["temporary_insanity"] is True

    asyncio.run(verify_restart())


def test_hp_changes_commit_major_wound_dying_and_first_aid_transitions(tmp_path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)

    async def exercise() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {"action": "create", "data": {"name": "Wounds", "idempotency_key": "hp-campaign"}},
        )
        actor = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Hardy Investigator",
                    "sheet": {
                        "characteristics": {"con": 100, "siz": 20},
                        "max_hp": 12,
                        "hp": 12,
                    },
                },
            },
        )
        play = await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign["id"],
                "data": {"phase": "play", "expected_revision": campaign["revision"]},
            },
        )
        first_args = {
            "action": "damage",
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "data": {"amount": 6, "source": "A single source-backed blow."},
            "expected_revision": play["revision"],
            "expected_character_revision": actor["revision"],
            "idempotency_key": "hp-major-wound",
        }
        first = await call(server, "coc_hp_change", first_args)
        assert await call(server, "coc_hp_change", first_args) == first
        assert first["hp"] == 6
        assert first["conditions"]["major_wound"] is True
        assert first["conditions"]["unconscious"] is False
        assert first["random_stream_receipt"]["draw_count"] == 2
        second_args = {
            "action": "damage",
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "data": {"amount": 6, "source": "A second source-backed blow."},
            "expected_revision": first["campaign_revision"],
            "expected_character_revision": first["character_revision"],
            "idempotency_key": "hp-dying",
        }
        second = await call(server, "coc_hp_change", second_args)
        assert second["hp"] == 0
        assert second["conditions"]["dying"] is True
        assert second["conditions"]["unconscious"] is True
        aid_args = {
            "action": "heal",
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "data": {
                "amount": 1,
                "source": "Successful First Aid.",
                "healing_source": "first_aid",
            },
            "expected_revision": second["campaign_revision"],
            "expected_character_revision": second["character_revision"],
            "idempotency_key": "hp-first-aid",
        }
        aided = await call(server, "coc_hp_change", aid_args)
        assert await call(server, "coc_hp_change", aid_args) == aided
        assert aided["campaign_revision"] == second["campaign_revision"]
        assert aided["hp"] == 1
        assert aided["conditions"]["dying"] is False
        assert aided["conditions"]["major_wound"] is True
        medicine = await call(
            server,
            "coc_hp_change",
            {
                "action": "heal",
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "data": {
                    "amount": 5,
                    "source": "A week of successful Medicine treatment.",
                    "healing_source": "medicine",
                },
                "expected_revision": aided["campaign_revision"],
                "expected_character_revision": aided["character_revision"],
                "idempotency_key": "hp-medicine",
            },
        )
        assert medicine["hp"] == 6
        assert medicine["conditions"]["major_wound"] is False
        current = await call(
            server,
            "character_query",
            {"action": "get", "campaign_id": campaign["id"], "character_id": actor["id"]},
        )
        assert len(current["sheet"]["health_events"]) == 4

    asyncio.run(exercise())


def test_combat_start_order_move_join_end_and_restart_are_authoritative(tmp_path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)

    async def exercise() -> tuple[str, dict]:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {
                    "name": "Combat case",
                    "idempotency_key": "combat-campaign",
                    "state": {"random_stream": initial_random_stream("combat-case-seed")},
                },
            },
        )
        investigator = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Investigator",
                    "sheet": {
                        "characteristics": {"dex": 60},
                        "skills": {"Fighting (Brawl)": 100},
                        "weapons": [
                            {
                                "name": "Knife",
                                "skill": {"name": "Fighting (Brawl)"},
                                "damage": "1",
                                "ammo": 0,
                                "properties": {"rngd": False, "impl": True},
                            }
                        ],
                    },
                },
            },
        )
        cultist = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Cultist",
                    "character_type": "npc",
                    "sheet": {
                        "characteristics": {"dex": 40},
                        "skills": {"Firearms (Handgun)": 100},
                        "weapons": [
                            {
                                "name": "Pistol",
                                "skill": {"name": "Firearms (Handgun)"},
                                "damage": "1",
                                "ammo": 3,
                                "malfunction": 100,
                                "properties": {"rngd": True, "impl": True},
                            }
                        ],
                    },
                },
            },
        )
        ally = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Late Ally",
                    "character_type": "npc",
                    "sheet": {"characteristics": {"dex": 50}},
                },
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_campaign",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": "player:investigator", "role": "player"},
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_actor",
                "campaign_id": campaign["id"],
                "data": {
                    "target_principal_id": "player:investigator",
                    "actor_id": investigator["id"],
                },
            },
        )
        play = await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign["id"],
                "data": {"phase": "play", "expected_revision": campaign["revision"]},
            },
        )
        start_arguments = {
            "campaign_id": campaign["id"],
            "participants": [
                {
                    "actor_id": investigator["id"],
                    "side": "investigators",
                    "position": [0, 0],
                },
                {
                    "actor_id": cultist["id"],
                    "side": "opposition",
                    "position": [3, 0],
                    "ready_firearm": True,
                },
            ],
            "expected_character_revisions": {
                investigator["id"]: investigator["revision"],
                cultist["id"]: cultist["revision"],
            },
            "positioning_mode": "grid",
            "source": "A source-backed confrontation begins.",
            "expected_revision": play["revision"],
            "idempotency_key": "combat-start",
        }
        started = await call(server, "combat_start", start_arguments)
        assert await call(server, "combat_start", start_arguments) == started
        assert started["phase"] == "combat"
        assert started["combat"]["order"] == [cultist["id"], investigator["id"]]
        assert (await call(server, "game_phase", {"campaign_id": campaign["id"]}))[
            "phase"
        ] == "combat"
        player_view = await call(
            server,
            "combat_query",
            {
                "campaign_id": campaign["id"],
                "principal_id": "player:investigator",
            },
        )
        assert "move" not in player_view["available_actions"]
        incoming = await call(
            server,
            "combat_attack",
            {
                "action": "open",
                "campaign_id": campaign["id"],
                "data": {
                    "attacker_id": cultist["id"],
                    "target_actor_id": investigator["id"],
                    "weapon_name": "Pistol",
                    "source": "The cultist fires the readied pistol.",
                    "expected_attacker_revision": cultist["revision"],
                    "expected_target_revision": investigator["revision"],
                },
                "expected_revision": started["campaign_revision"],
                "idempotency_key": "incoming-open",
            },
        )
        assert incoming["pending_choice"]["response_options"] == [
            "none",
            "dive_for_cover",
        ]
        pending_presentation = await call(
            server,
            "resolution_presentation",
            {
                "campaign_id": campaign["id"],
                "resolution_id": incoming["resolution_id"],
                "principal_id": "player:investigator",
            },
        )
        assert pending_presentation["status"] == "pending"
        assert pending_presentation["event_sequence"] == 1
        assert pending_presentation["pending_choice"]["available_actions"] == [
            "none",
            "dive_for_cover",
        ]
        incoming_arguments = {
            "action": "resolve",
            "campaign_id": campaign["id"],
            "data": {
                "pending_id": incoming["pending_choice"]["id"],
                "defense": "dive_for_cover",
            },
            "expected_revision": incoming["campaign_revision"],
            "idempotency_key": "incoming-resolve",
            "principal_id": "player:investigator",
        }
        incoming_resolved = await call(server, "combat_attack", incoming_arguments)
        assert await call(server, "combat_attack", incoming_arguments) == incoming_resolved
        assert incoming_resolved["resolution"]["defense"] == "dive_for_cover"
        resolved_presentation = await call(
            server,
            "resolution_presentation",
            {
                "campaign_id": campaign["id"],
                "resolution_id": incoming_resolved["resolution_id"],
                "principal_id": "player:investigator",
            },
        )
        assert resolved_presentation["thread_id"] == pending_presentation["thread_id"]
        assert resolved_presentation["event_sequence"] == 2
        assert resolved_presentation["status"] == "settled"
        assert resolved_presentation["rolls"]
        assert (
            incoming_resolved["combat"]["participants"][investigator["id"]]["forfeit_next_action"]
            is True
        )
        assert (
            await call(
                server,
                "character_query",
                {
                    "action": "get",
                    "campaign_id": campaign["id"],
                    "character_id": cultist["id"],
                },
            )
        )["sheet"]["weapons"][0]["ammo"] == 2
        enemy_done = await call(
            server,
            "combat_action",
            {
                "action": "end_turn",
                "campaign_id": campaign["id"],
                "data": {"actor_id": cultist["id"]},
                "expected_revision": incoming_resolved["campaign_revision"],
                "idempotency_key": "cultist-turn",
            },
        )
        assert enemy_done["combat"]["round"] == 2
        assert enemy_done["combat"]["last_skipped_actor_ids"] == [investigator["id"]]
        assert enemy_done["combat"]["current_actor_id"] == cultist["id"]
        enemy_round_two_done = await call(
            server,
            "combat_action",
            {
                "action": "end_turn",
                "campaign_id": campaign["id"],
                "data": {"actor_id": cultist["id"]},
                "expected_revision": enemy_done["campaign_revision"],
                "idempotency_key": "cultist-round-two-turn",
            },
        )
        assert enemy_round_two_done["combat"]["current_actor_id"] == investigator["id"]
        moved = await call(
            server,
            "combat_action",
            {
                "action": "move",
                "campaign_id": campaign["id"],
                "data": {
                    "actor_id": investigator["id"],
                    "destination": [2, 1],
                    "movement_budget": 2,
                },
                "expected_revision": enemy_round_two_done["campaign_revision"],
                "idempotency_key": "investigator-move",
                "principal_id": "player:investigator",
            },
        )
        assert moved["combat"]["participants"][investigator["id"]]["position"] == [
            2.0,
            1.0,
        ]
        current_investigator = await call(
            server,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": investigator["id"],
            },
        )
        current_cultist = await call(
            server,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": cultist["id"],
            },
        )
        opened = await call(
            server,
            "combat_attack",
            {
                "action": "open",
                "campaign_id": campaign["id"],
                "data": {
                    "attacker_id": investigator["id"],
                    "target_actor_id": cultist["id"],
                    "weapon_name": "Knife",
                    "source": "The investigator attacks the adjacent cultist.",
                    "expected_attacker_revision": current_investigator["revision"],
                    "expected_target_revision": current_cultist["revision"],
                },
                "expected_revision": moved["campaign_revision"],
                "idempotency_key": "attack-open",
                "principal_id": "player:investigator",
            },
        )
        assert opened["pending_choice"]["response_options"] == [
            "none",
            "dodge",
            "fight-back",
        ]
        attack_arguments = {
            "action": "resolve",
            "campaign_id": campaign["id"],
            "data": {
                "pending_id": opened["pending_choice"]["id"],
                "defense": "none",
            },
            "expected_revision": opened["campaign_revision"],
            "idempotency_key": "attack-resolve",
        }
        attacked = await call(server, "combat_attack", attack_arguments)
        assert await call(server, "combat_attack", attack_arguments) == attacked
        assert attacked["combat"]["pending_choice"] is None
        assert (
            attacked["combat"]["participants"][investigator["id"]]["attacks_taken_this_turn"] == 1
        )
        assert attacked["resolution"]["resolution"]["winner"] == "attacker"
        assert attacked["resolution"]["damaged_actor_id"] == cultist["id"]
        cultist_after = await call(
            server,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": cultist["id"],
            },
        )
        assert cultist_after["sheet"]["hp"] == cultist["sheet"]["hp"] - 1
        join_arguments = {
            "action": "join",
            "campaign_id": campaign["id"],
            "data": {
                "actor_id": ally["id"],
                "side": "investigators",
                "position": [1, 0],
                "expected_character_revision": ally["revision"],
            },
            "expected_revision": attacked["campaign_revision"],
            "idempotency_key": "ally-joins",
        }
        joined = await call(server, "combat_action", join_arguments)
        assert await call(server, "combat_action", join_arguments) == joined
        assert joined["combat"]["participants"][ally["id"]]["available_from_round"] == 3
        ended = await call(
            server,
            "combat_end",
            {
                "campaign_id": campaign["id"],
                "outcome": "escape",
                "source": "The surviving investigators explicitly withdrew.",
                "expected_revision": joined["campaign_revision"],
                "idempotency_key": "combat-end",
            },
        )
        assert ended["phase"] == "play"
        assert ended["combat"]["active"] is False
        assert (await call(server, "game_phase", {"campaign_id": campaign["id"]}))[
            "phase"
        ] == "play"
        return campaign["id"], ended

    campaign_id, ended = asyncio.run(exercise())
    restarted = create_server(config)

    async def verify_restart() -> None:
        campaign = await call(
            restarted,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        assert campaign["revision"] == ended["campaign_revision"]
        assert campaign["state"]["combat"]["outcome"] == "escape"
        assert (await call(restarted, "game_phase", {"campaign_id": campaign_id}))[
            "phase"
        ] == "play"

    asyncio.run(verify_restart())


def test_chase_is_atomic_actor_scoped_combat_exclusive_and_restartable(tmp_path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)

    async def exercise() -> tuple[str, dict]:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {
                    "name": "Chase case",
                    "idempotency_key": "chase-campaign",
                    "state": {"random_stream": initial_random_stream("chase-case-seed")},
                },
            },
        )
        fleeing = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Fleeing Investigator",
                    "sheet": {
                        "characteristics": {"con": 100, "dex": 60},
                        "mov": 9,
                        "skills": {"Climb": 100},
                    },
                },
            },
        )
        pursuer = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Pursuing Cultist",
                    "character_type": "npc",
                    "sheet": {"characteristics": {"con": 100, "dex": 40}, "mov": 7},
                },
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_campaign",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": "player:fleeing", "role": "player"},
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_actor",
                "campaign_id": campaign["id"],
                "data": {
                    "target_principal_id": "player:fleeing",
                    "actor_id": fleeing["id"],
                },
            },
        )
        play = await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign["id"],
                "data": {"phase": "play", "expected_revision": campaign["revision"]},
            },
        )
        start_arguments = {
            "campaign_id": campaign["id"],
            "participants": [
                {
                    "actor_id": fleeing["id"],
                    "role": "fleeing",
                    "position": 2,
                    "speed_skill_name": "con",
                },
                {
                    "actor_id": pursuer["id"],
                    "role": "pursuer",
                    "position": 0,
                    "speed_skill_name": "con",
                },
            ],
            "expected_character_revisions": {
                fleeing["id"]: fleeing["revision"],
                pursuer["id"]: pursuer["revision"],
            },
            "source": "A source-backed pursuit begins.",
            "route": [
                {
                    "id": "road",
                    "index": 0,
                    "title": "Coastal Road",
                    "source": "scene:coastal-road",
                },
                {
                    "id": "fence",
                    "index": 3,
                    "title": "Fence",
                    "kind": "barrier",
                    "source": "scene:fence",
                },
            ],
            "expected_revision": play["revision"],
            "idempotency_key": "chase-start",
        }
        started = await call(server, "chase_start", start_arguments)
        assert await call(server, "chase_start", start_arguments) == started
        assert started["chase"]["current_actor_id"] == fleeing["id"]
        assert started["random_stream_receipt"]["draw_count"] == 4
        assert (
            started["chase"]["participants"][fleeing["id"]]["action_points"]
            >= started["chase"]["participants"][pursuer["id"]]["action_points"]
        )
        player_view = await call(
            server,
            "chase_query",
            {"campaign_id": campaign["id"], "principal_id": "player:fleeing"},
        )
        assert {"move", "check", "speed_check", "end_turn"} <= set(player_view["available_actions"])
        with pytest.raises(Exception, match="active chase"):
            await call(
                server,
                "combat_start",
                {
                    "campaign_id": campaign["id"],
                    "participants": [
                        {
                            "actor_id": fleeing["id"],
                            "side": "investigators",
                            "position": [0, 0],
                        },
                        {
                            "actor_id": pursuer["id"],
                            "side": "opposition",
                            "position": [1, 0],
                        },
                    ],
                    "expected_character_revisions": {
                        fleeing["id"]: fleeing["revision"],
                        pursuer["id"]: pursuer["revision"],
                    },
                    "positioning_mode": "grid",
                    "source": "Invalid overlapping combat.",
                    "expected_revision": started["campaign_revision"],
                    "idempotency_key": "combat-during-chase",
                },
            )
        check_arguments = {
            "action": "check",
            "campaign_id": campaign["id"],
            "data": {
                "actor_id": fleeing["id"],
                "skill_name": "Climb",
                "action_type": "barrier",
                "difficulty": "regular",
                "cost": 1,
                "success_position_change": 1,
                "failure_position_change": 0,
                "source": "The route requires crossing the sourced fence barrier.",
            },
            "expected_revision": started["campaign_revision"],
            "idempotency_key": "fence-check",
            "principal_id": "player:fleeing",
        }
        checked = await call(server, "chase_action", check_arguments)
        assert await call(server, "chase_action", check_arguments) == checked
        assert checked["resolution"]["skill_name"] == "Climb"
        assert checked["random_stream_receipt"]["draw_count"] == 2
        assert checked["chase"]["participants"][fleeing["id"]]["position"] == 3
        presentation = await call(
            server,
            "resolution_presentation",
            {
                "campaign_id": campaign["id"],
                "resolution_id": checked["resolution_id"],
                "principal_id": "player:fleeing",
            },
        )
        assert presentation["operation"] == "chase_action.check"
        assert presentation["rolls"]
        assert presentation["outcome"]["outcome"] in {"success", "failure"}
        ended_turn = await call(
            server,
            "chase_action",
            {
                "action": "end_turn",
                "campaign_id": campaign["id"],
                "data": {"actor_id": fleeing["id"]},
                "expected_revision": checked["campaign_revision"],
                "idempotency_key": "fleeing-end-turn",
                "principal_id": "player:fleeing",
            },
        )
        assert ended_turn["chase"]["current_actor_id"] == pursuer["id"]
        ended = await call(
            server,
            "chase_end",
            {
                "campaign_id": campaign["id"],
                "outcome": "escaped",
                "source": "The investigator reached the sourced safe location.",
                "expected_revision": ended_turn["campaign_revision"],
                "idempotency_key": "chase-end",
            },
        )
        assert ended["chase"]["active"] is False
        assert ended["outcome"] == "escaped"
        return campaign["id"], ended

    campaign_id, ended = asyncio.run(exercise())
    restarted = create_server(config)

    async def verify_restart() -> None:
        campaign = await call(
            restarted,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        assert campaign["revision"] == ended["campaign_revision"]
        assert campaign["state"]["chase"]["outcome"] == "escaped"
        assert campaign["state"]["chase"]["active"] is False

    asyncio.run(verify_restart())


def test_branch_snapshot_and_revision_recovery_are_guarded_and_replayable(tmp_path) -> None:
    config = McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "coc",
        modulegen_skills_dir=tmp_path / "modulegen",
    )
    server = create_server(config)

    async def exercise() -> tuple[str, str, str, dict]:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Forked case", "idempotency_key": "forked-campaign"},
            },
        )
        campaign_id = campaign["id"]
        original = (
            await call(
                server,
                "branch_query",
                {"action": "current", "campaign_id": campaign_id},
            )
        )["branch"]
        baseline_args = {
            "action": "create",
            "campaign_id": campaign_id,
            "data": {"label": "Lobby baseline", "expected_head_snapshot_id": ""},
            "expected_revision": campaign["revision"],
            "expected_branch_id": original["id"],
            "idempotency_key": "snapshot-baseline",
        }
        baseline = await call(server, "snapshot_change", baseline_args)
        assert await call(server, "snapshot_change", baseline_args) == baseline
        verified = await call(
            server,
            "snapshot_query",
            {"action": "verify", "campaign_id": campaign_id, "data": {"slot": 1}},
        )
        assert verified["valid"] is True
        document = (
            await call(
                server,
                "snapshot_query",
                {"action": "get", "campaign_id": campaign_id, "data": {"slot": 1}},
            )
        )["snapshot"]
        assert "storage_mode" not in document
        assert document["valid"] is True
        assert document["payload"]["campaign"]["name"] == "Forked case"
        stored_database = Database(sqlite_database_url(config.database_path))
        try:
            with stored_database.transaction() as session:
                stored = session.get(CampaignSnapshot, baseline["id"])
                assert stored is not None
                assert stored.schema_version == 9
                assert stored.payload_codec == "zlib-1"
                assert stored.uncompressed_size > 0
                assert stored.compressed_payload
        finally:
            stored_database.dispose()

        play = await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign_id,
                "data": {"phase": "play", "expected_revision": campaign["revision"]},
            },
        )
        play_save = await call(
            server,
            "snapshot_change",
            {
                "action": "create",
                "campaign_id": campaign_id,
                "data": {
                    "label": "Play head",
                    "expected_head_snapshot_id": baseline["id"],
                },
                "expected_revision": play["revision"],
                "expected_branch_id": original["id"],
                "idempotency_key": "snapshot-play",
            },
        )
        fork_args = {
            "action": "create",
            "campaign_id": campaign_id,
            "data": {
                "name": "alternate-lobby",
                "from_snapshot_id": baseline["id"],
            },
            "expected_revision": play["revision"],
            "expected_branch_id": original["id"],
            "idempotency_key": "branch-alternate",
        }
        forked = await call(server, "branch_change", fork_args)
        assert await call(server, "branch_change", fork_args) == forked
        assert forked["campaign_revision"] == play["revision"] + 1

        after_fork = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        assert after_fork["revision"] == forked["campaign_revision"]

        checkout_fork_args = {
            "action": "checkout",
            "campaign_id": campaign_id,
            "data": {"branch_id": forked["branch"]["id"]},
            "expected_revision": after_fork["revision"],
            "expected_branch_id": original["id"],
            "idempotency_key": "checkout-alternate",
        }
        checked_out = await call(server, "branch_change", checkout_fork_args)
        assert await call(server, "branch_change", checkout_fork_args) == checked_out
        assert checked_out["campaign_revision"] == after_fork["revision"] + 1
        assert (await call(server, "game_phase", {"campaign_id": campaign_id}))["phase"] == "lobby"

        current = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        checkout_original = await call(
            server,
            "branch_change",
            {
                "action": "checkout",
                "campaign_id": campaign_id,
                "data": {"branch_id": original["id"]},
                "expected_revision": current["revision"],
                "expected_branch_id": forked["branch"]["id"],
                "idempotency_key": "checkout-original",
            },
        )
        assert checkout_original["campaign_revision"] == current["revision"] + 1
        assert checkout_original["snapshot"]["id"] == play_save["id"]
        assert (await call(server, "game_phase", {"campaign_id": campaign_id}))["phase"] == "play"

        current = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        restore_args = {
            "action": "restore",
            "campaign_id": campaign_id,
            "data": {"slot": baseline["slot"]},
            "expected_revision": current["revision"],
            "expected_branch_id": original["id"],
            "idempotency_key": "restore-lobby",
        }
        restored = await call(server, "snapshot_change", restore_args)
        assert await call(server, "snapshot_change", restore_args) == restored
        assert (await call(server, "game_phase", {"campaign_id": campaign_id}))["phase"] == "lobby"
        restore_branch = (
            await call(
                server,
                "branch_query",
                {"action": "current", "campaign_id": campaign_id},
            )
        )["branch"]
        assert restore_branch["id"] not in {original["id"], forked["branch"]["id"]}
        comparison = await call(
            server,
            "branch_query",
            {
                "action": "compare",
                "campaign_id": campaign_id,
                "data": {
                    "left_branch_id": original["id"],
                    "right_branch_id": restore_branch["id"],
                },
            },
        )
        assert comparison["comparison"]["merge_policy"].startswith("explicit-per-fact")

        current = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        roll = await call(
            server,
            "coc_dice_roll",
            {
                "kind": "d100",
                "campaign_id": campaign_id,
                "expected_revision": current["revision"],
                "idempotency_key": "branch-roll",
            },
        )
        history = await call(
            server,
            "state_revision",
            {"action": "history", "campaign_id": campaign_id},
        )
        cursor = history["revisions"][0]["sequence"]
        undo_args = {
            "action": "undo",
            "campaign_id": campaign_id,
            "data": {"expected_history_sequence": cursor},
            "idempotency_key": "undo-branch-roll",
        }
        undone = await call(server, "state_revision", undo_args)
        assert await call(server, "state_revision", undo_args) == undone
        after_undo = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        assert after_undo["state"]["random_stream"]["position"] == 0
        redo_args = {
            "action": "redo",
            "campaign_id": campaign_id,
            "data": {"expected_history_sequence": 0},
            "idempotency_key": "redo-branch-roll",
        }
        redone = await call(server, "state_revision", redo_args)
        assert await call(server, "state_revision", redo_args) == redone
        after_redo = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign_id},
        )
        assert after_redo["state"]["random_stream"]["last_receipt"] == roll["random_stream_receipt"]
        return campaign_id, restore_branch["id"], original["id"], restored

    campaign_id, restore_branch_id, original_branch_id, restored = asyncio.run(exercise())
    restarted = create_server(config)

    async def verify_restart() -> None:
        current = await call(
            restarted,
            "branch_query",
            {"action": "current", "campaign_id": campaign_id},
        )
        assert current["branch"]["id"] == restore_branch_id
        branches = await call(
            restarted,
            "branch_query",
            {"action": "list", "campaign_id": campaign_id},
        )
        assert {item["id"] for item in branches["branches"]} >= {
            restore_branch_id,
            original_branch_id,
        }
        lineage = await call(
            restarted,
            "snapshot_query",
            {
                "action": "lineage",
                "campaign_id": campaign_id,
                "data": {"slot": restored["slot"]},
            },
        )
        assert lineage["snapshots"][-1]["id"] == restored["id"]

    asyncio.run(verify_restart())


def test_exposure_registry_is_session_and_phase_scoped() -> None:
    registry = ExposureRegistry()
    alice = registry.open(
        session_key="session:alice",
        principal_id="player:alice",
        campaign_id="campaign:one",
        phase="play",
    )
    bob = registry.open(
        session_key="session:bob",
        principal_id="player:bob",
        campaign_id="campaign:one",
        phase="play",
    )
    registry.set_tools(alice, add=["coc_resolve"])
    assert "coc_resolve" in registry.visible_tools(alice)
    assert "coc_resolve" not in registry.visible_tools(bob)
    registry.require_tool(alice, "coc_resolve")
    assert "coc_resolve" in registry.visible_tools(alice)
    registry.refresh_phase(bob, "combat")
    with pytest.raises(ExposureError):
        registry.set_tools(bob, add=["module_change"])


def test_native_tool_list_is_independent_per_session(tmp_path) -> None:
    config = McpConfig(tmp_path / "home", None, tmp_path / "coc", tmp_path / "modulegen")

    async def exercise() -> None:
        server = create_server(config)
        server._request_session = lambda: ("mcp:alice", object())  # type: ignore[method-assign]
        assert {tool.name for tool in await server.list_tools()} == set(CORE_TOOLS)
        exposure = server.exposure_registry.open(
            session_key="mcp:alice",
            principal_id="system:local",
            campaign_id=None,
            phase="lobby",
        )
        server.exposure_registry.set_tools(exposure, add=["campaign_change"])
        assert "campaign_change" in {tool.name for tool in await server.list_tools()}
        server._request_session = lambda: ("mcp:bob", object())  # type: ignore[method-assign]
        assert {tool.name for tool in await server.list_tools()} == set(CORE_TOOLS)

    asyncio.run(exercise())


def test_stdio_client_can_discover_load_and_call(tmp_path) -> None:
    imports = tmp_path / "imports"
    imports.mkdir()
    source = imports / "stdio-module.pdf"
    write_text_pdf(
        source,
        [
            "# Stdio Module",
            "## Arrival",
            "The investigator arrives in Arkham.",
            "## Ending",
            "The investigation is resolved.",
        ],
    )

    async def exercise() -> None:
        env = dict(os.environ)
        env["SAGASMITH_COC_MCP_HOME"] = str(tmp_path / "home")
        env["SAGASMITH_COC_MCP_MODULE_IMPORT_ROOTS"] = str(imports)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sagasmith_coc_mcp.server"],
            cwd=Path(__file__).parents[1],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert {item.name for item in (await session.list_tools()).tools} == set(CORE_TOOLS)
                opened = await session.call_tool("exposure", {"action": "open"})
                assert json.loads(opened.content[0].text)["native_dynamic_tools"] is True
                loaded = await session.call_tool(
                    "exposure",
                    {"action": "set", "add_tool_ids": ["campaign_change"]},
                )
                assert not loaded.isError
                assert "campaign_change" in {
                    item.name for item in (await session.list_tools()).tools
                }
                created = await session.call_tool(
                    "campaign_change",
                    {
                        "action": "create",
                        "data": {"name": "Stdio", "idempotency_key": "stdio-create"},
                    },
                )
                assert not created.isError
                created_campaign = json.loads(created.content[0].text)
                campaign_id = created_campaign["id"]
                rebound = await session.call_tool(
                    "exposure",
                    {"action": "open", "campaign_id": campaign_id},
                )
                assert not rebound.isError
                loaded_module_draft = await session.call_tool(
                    "exposure",
                    {"action": "set", "add_tool_ids": ["module_draft"]},
                )
                assert not loaded_module_draft.isError
                imported = await session.call_tool(
                    "module_draft",
                    {
                        "action": "start",
                        "campaign_id": campaign_id,
                        "data": {
                            "source_path": str(source),
                            "source_key": "test.stdio.pdf",
                        },
                        "idempotency_key": "stdio-pdf-import",
                    },
                )
                assert not imported.isError
                assert json.loads(imported.content[0].text)["job"]["state"] == "imported"
                listed_skills = await session.call_tool(
                    "skill_query",
                    {"action": "list", "campaign_id": campaign_id},
                )
                assert not listed_skills.isError
                skill_ids = {
                    item["id"] for item in json.loads(listed_skills.content[0].text)["skills"]
                }
                assert {
                    "coc.full",
                    "coc.full.skills.coc7-keeper",
                    "coc.full.skills.coc7-campaign-manager",
                    "modulegen.root",
                } <= skill_ids
                loaded_skill = await session.call_tool(
                    "skill_query",
                    {
                        "action": "read",
                        "campaign_id": campaign_id,
                        "skill_id": "coc.full",
                    },
                )
                assert not loaded_skill.isError
                skill_content = json.loads(loaded_skill.content[0].text)["content"]
                assert "sagasmith_coc MCP" in skill_content
                assert "sagasmith-coc --json" not in skill_content
                found = await session.call_tool(
                    "exposure",
                    {"action": "search", "query": "module draft"},
                )
                matches = json.loads(found.content[0].text)["matches"]
                assert [item["tool_id"] for item in matches] == ["module_draft"]
                loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": [
                            "module_draft",
                            "content_pack",
                            "campaign_change",
                            "character_change",
                            "branch_query",
                            "snapshot_change",
                        ],
                    },
                )
                assert not loaded.isError
                visible = {item.name for item in (await session.list_tools()).tools}
                assert {
                    "module_draft",
                    "content_pack",
                    "campaign_change",
                    "character_change",
                    "branch_query",
                    "snapshot_change",
                } <= visible
                staged = await session.call_tool(
                    "module_draft",
                    {
                        "action": "start",
                        "campaign_id": campaign_id,
                        "data": {
                            "name": "stdio-case.md",
                            "content": "# Case\n## Scene\nEvidence.\n",
                        },
                        "idempotency_key": "stdio-draft",
                    },
                )
                assert not staged.isError
                investigator_result = await session.call_tool(
                    "character_change",
                    {
                        "action": "create",
                        "campaign_id": campaign_id,
                        "data": {
                            "name": "Stdio Investigator",
                            "expected_campaign_revision": created_campaign["revision"],
                            "idempotency_key": "stdio-investigator",
                            "sheet": {
                                "characteristics": {"dex": 60},
                                "skills": {"Spot Hidden": 0},
                                "development": {"checked_skills": ["Spot Hidden"]},
                            },
                        },
                    },
                )
                assert not investigator_result.isError
                investigator = json.loads(investigator_result.content[0].text)
                development_loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["development_query", "development_settle"],
                    },
                )
                assert not development_loaded.isError
                pending_development = await session.call_tool(
                    "development_query",
                    {"campaign_id": campaign_id, "actor_id": investigator["id"]},
                )
                assert not pending_development.isError
                pending_development_value = json.loads(pending_development.content[0].text)
                settled_development = await session.call_tool(
                    "development_settle",
                    {
                        "campaign_id": campaign_id,
                        "actor_id": investigator["id"],
                        "source": "Stdio end-of-session development fixture.",
                        "expected_revision": pending_development_value["campaign_revision"],
                        "expected_character_revision": pending_development_value[
                            "character_revision"
                        ],
                        "idempotency_key": "stdio-development",
                    },
                )
                assert not settled_development.isError
                settled_development_value = json.loads(settled_development.content[0].text)
                investigator["revision"] = settled_development_value["character_revision"]
                cultist_result = await session.call_tool(
                    "character_change",
                    {
                        "action": "create",
                        "campaign_id": campaign_id,
                        "data": {
                            "name": "Stdio Cultist",
                            "character_type": "npc",
                            "sheet": {"characteristics": {"dex": 40}},
                            "expected_campaign_revision": settled_development_value[
                                "campaign_revision"
                            ],
                            "idempotency_key": "stdio-cultist",
                        },
                    },
                )
                assert not cultist_result.isError
                cultist = json.loads(cultist_result.content[0].text)
                campaign = await session.call_tool(
                    "campaign_query",
                    {"action": "get", "campaign_id": campaign_id},
                )
                campaign_value = json.loads(campaign.content[0].text)
                branch = await session.call_tool(
                    "branch_query",
                    {"action": "current", "campaign_id": campaign_id},
                )
                branch_id = json.loads(branch.content[0].text)["branch"]["id"]
                saved = await session.call_tool(
                    "snapshot_change",
                    {
                        "action": "create",
                        "campaign_id": campaign_id,
                        "data": {
                            "label": "stdio lobby",
                            "expected_head_snapshot_id": "",
                        },
                        "expected_revision": campaign_value["revision"],
                        "expected_branch_id": branch_id,
                        "idempotency_key": "stdio-snapshot",
                    },
                )
                assert not saved.isError
                played = await session.call_tool(
                    "campaign_change",
                    {
                        "action": "set_phase",
                        "campaign_id": campaign_id,
                        "data": {
                            "phase": "play",
                            "expected_revision": campaign_value["revision"],
                        },
                    },
                )
                assert not played.isError
                play_value = json.loads(played.content[0].text)
                play_tools = {item.name for item in (await session.list_tools()).tools}
                assert "module_draft" not in play_tools
                assert "content_pack" not in play_tools
                assert "snapshot_change" in play_tools
                assert "development_query" not in play_tools
                assert "development_settle" not in play_tools
                continuity_loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": [
                            "campaign_event",
                            "continuity_context",
                            "investigation_check",
                            "investigation_query",
                            "group_luck_check",
                            "group_luck_query",
                        ],
                    },
                )
                assert not continuity_loaded.isError
                group_luck = await session.call_tool(
                    "group_luck_query",
                    {
                        "campaign_id": campaign_id,
                        "participant_actor_ids": [investigator["id"], cultist["id"]],
                    },
                )
                assert not group_luck.isError
                group_luck_value = json.loads(group_luck.content[0].text)
                group_luck_result = await session.call_tool(
                    "group_luck_check",
                    {
                        "campaign_id": campaign_id,
                        "participant_actor_ids": [investigator["id"], cultist["id"]],
                        "selected_actor_id": investigator["id"],
                        "source": "Stdio group circumstance fixture.",
                        "goal": "Determine whether transport arrives before the storm.",
                        "expected_revision": group_luck_value["campaign_revision"],
                        "idempotency_key": "stdio-group-luck",
                    },
                )
                assert not group_luck_result.isError
                play_value["revision"] = json.loads(group_luck_result.content[0].text)[
                    "campaign_revision"
                ]
                opened_check_result = await session.call_tool(
                    "investigation_check",
                    {
                        "action": "open",
                        "campaign_id": campaign_id,
                        "actor_id": investigator["id"],
                        "data": {
                            "trait_kind": "characteristic",
                            "trait_name": "dex",
                            "goal": "Cross the rain-slick porch without falling.",
                            "source": "Stdio synthetic source-backed investigation check.",
                        },
                        "expected_revision": play_value["revision"],
                        "expected_character_revision": investigator["revision"],
                        "idempotency_key": "stdio-investigation-open",
                    },
                )
                assert not opened_check_result.isError
                opened_check = json.loads(opened_check_result.content[0].text)
                queried_check = await session.call_tool(
                    "investigation_query",
                    {"campaign_id": campaign_id, "actor_id": investigator["id"]},
                )
                assert not queried_check.isError
                assert (
                    json.loads(queried_check.content[0].text)["pending"]["id"]
                    == (opened_check["pending"]["id"])
                )
                settled_check_result = await session.call_tool(
                    "investigation_check",
                    {
                        "action": "settle",
                        "campaign_id": campaign_id,
                        "actor_id": investigator["id"],
                        "data": {"check_id": opened_check["pending"]["id"]},
                        "expected_revision": opened_check["campaign_revision"],
                        "expected_character_revision": opened_check["character_revision"],
                        "idempotency_key": "stdio-investigation-settle",
                    },
                )
                assert not settled_check_result.isError
                settled_check = json.loads(settled_check_result.content[0].text)
                play_value["revision"] = settled_check["campaign_revision"]
                investigator["revision"] = settled_check["character_revision"]
                recorded = await session.call_tool(
                    "campaign_event",
                    {
                        "action": "add",
                        "campaign_id": campaign_id,
                        "data": {
                            "summary": "The investigators enter the rain-soaked house.",
                            "audience_scope": "party",
                        },
                        "idempotency_key": "stdio-arrival-event",
                    },
                )
                assert not recorded.isError
                context = await session.call_tool(
                    "continuity_context",
                    {"campaign_id": campaign_id, "query": "rain-soaked house"},
                )
                assert not context.isError
                assert len(json.loads(context.content[0].text)["events"]) == 1
                combat_loaded = await session.call_tool(
                    "exposure",
                    {"action": "set", "add_tool_ids": ["combat_start"]},
                )
                assert not combat_loaded.isError
                started_result = await session.call_tool(
                    "combat_start",
                    {
                        "campaign_id": campaign_id,
                        "participants": [
                            {
                                "actor_id": investigator["id"],
                                "side": "investigators",
                                "position": [0, 0],
                            },
                            {
                                "actor_id": cultist["id"],
                                "side": "opposition",
                                "position": [2, 0],
                            },
                        ],
                        "expected_character_revisions": {
                            investigator["id"]: investigator["revision"],
                            cultist["id"]: cultist["revision"],
                        },
                        "positioning_mode": "grid",
                        "source": "Stdio source-backed confrontation.",
                        "expected_revision": play_value["revision"],
                        "idempotency_key": "stdio-combat-start",
                    },
                )
                assert not started_result.isError
                started = json.loads(started_result.content[0].text)
                combat_tools = {item.name for item in (await session.list_tools()).tools}
                assert "combat_start" not in combat_tools
                assert "campaign_event" not in combat_tools
                assert "continuity_context" in combat_tools
                assert "investigation_check" not in combat_tools
                assert "investigation_query" not in combat_tools
                assert "group_luck_check" not in combat_tools
                assert "group_luck_query" not in combat_tools
                combat_loaded = await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["combat_query", "combat_action", "combat_end"],
                    },
                )
                assert not combat_loaded.isError
                status = await session.call_tool(
                    "combat_query",
                    {"campaign_id": campaign_id},
                )
                assert not status.isError
                assert json.loads(status.content[0].text)["phase"] == "combat"
                ended_result = await session.call_tool(
                    "combat_end",
                    {
                        "campaign_id": campaign_id,
                        "outcome": "other",
                        "source": "The stdio encounter was explicitly closed.",
                        "expected_revision": started["campaign_revision"],
                        "idempotency_key": "stdio-combat-end",
                    },
                )
                assert not ended_result.isError
                ended = json.loads(ended_result.content[0].text)
                assert ended["phase"] == "play"
                assert "combat_end" not in {
                    item.name for item in (await session.list_tools()).tools
                }
                restored = await session.call_tool(
                    "snapshot_change",
                    {
                        "action": "restore",
                        "campaign_id": campaign_id,
                        "data": {"slot": 1},
                        "expected_revision": ended["campaign_revision"],
                        "expected_branch_id": branch_id,
                        "idempotency_key": "stdio-restore",
                    },
                )
                assert not restored.isError
                phase = await session.call_tool("game_phase", {"campaign_id": campaign_id})
                phase_payload = json.loads(phase.content[0].text)
                assert phase_payload["phase"] == "lobby"
                binding = phase_payload["host_context_binding"]
                assert binding["domain"] == "sagasmith-coc"
                assert len(binding["authorization_fingerprint"]) == 64
                assert len(binding["context_epoch"]) == 64
                reloaded = await session.call_tool(
                    "exposure",
                    {"action": "set", "add_tool_ids": ["module_draft"]},
                )
                assert not reloaded.isError
                resumed = await session.call_tool(
                    "module_draft",
                    {"action": "get", "campaign_id": campaign_id},
                )
                assert not resumed.isError
                assert json.loads(resumed.content[0].text)["jobs"]

    asyncio.run(exercise())


def test_only_one_exposure_facade_is_registered(tmp_path) -> None:
    config = McpConfig(tmp_path / "home", None, tmp_path / "coc", tmp_path / "modulegen")
    names = {tool.name for tool in create_server(config)._tool_manager.list_tools()}

    assert "exposure" in names
    assert not any(name.startswith("exposure_") for name in names)


def test_tools_advertise_agent_domain_context_contract(tmp_path) -> None:
    config = McpConfig(tmp_path / "home", None, tmp_path / "coc", tmp_path / "modulegen")
    tools = {
        tool.name: tool for tool in create_server(config)._tool_manager.list_tools()
    }

    assert all(
        tool.meta.get("sagasmith_domain_context") == "sagasmith-coc"
        for tool in tools.values()
    )
    assert tools["campaign_query"].meta.get("sagasmith_context_sync") is True
