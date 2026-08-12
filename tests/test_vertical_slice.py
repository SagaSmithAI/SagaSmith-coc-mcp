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

from sagasmith_coc_mcp.config import McpConfig
from sagasmith_coc_mcp.exposure import ExposureError, ExposureRegistry
from sagasmith_coc_mcp.server import create_server
from sagasmith_coc_mcp.tool_profiles import CORE_TOOLS


async def call(server, name: str, arguments: dict):
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
                },
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
                        "A hidden clue waits upstairs.\n## Ending\nThe case is solved."
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
                        "A hidden clue waits upstairs.\n## Ending\nThe case is solved."
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


def test_module_draft_content_asset_and_actor_edits_enter_final_pack(tmp_path) -> None:
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
            "expected_revision": content_edit["job"]["revision"],
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
                "expected_revision": actor_edit["job"]["revision"],
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
        assert [actor["id"] for actor in package["actors"]] == ["coc7e.actor.mr-marsh"]
        assert package["content_reviews"][0]["kind"] == "clue"
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
        with pytest.raises(Exception, match="revision conflict"):
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

        checkout_fork_args = {
            "action": "checkout",
            "campaign_id": campaign_id,
            "data": {"branch_id": forked["branch"]["id"]},
            "expected_revision": play["revision"],
            "expected_branch_id": original["id"],
            "idempotency_key": "checkout-alternate",
        }
        checked_out = await call(server, "branch_change", checkout_fork_args)
        assert await call(server, "branch_change", checkout_fork_args) == checked_out
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
    async def exercise() -> None:
        env = dict(os.environ)
        env["SAGASMITH_COC_MCP_HOME"] = str(tmp_path / "home")
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
                campaign_id = json.loads(created.content[0].text)["id"]
                rebound = await session.call_tool(
                    "exposure",
                    {"action": "open", "campaign_id": campaign_id},
                )
                assert not rebound.isError
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
                restored = await session.call_tool(
                    "snapshot_change",
                    {
                        "action": "restore",
                        "campaign_id": campaign_id,
                        "data": {"slot": 1},
                        "expected_revision": play_value["revision"],
                        "expected_branch_id": branch_id,
                        "idempotency_key": "stdio-restore",
                    },
                )
                assert not restored.isError
                phase = await session.call_tool("game_phase", {"campaign_id": campaign_id})
                assert json.loads(phase.content[0].text)["phase"] == "lobby"
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
