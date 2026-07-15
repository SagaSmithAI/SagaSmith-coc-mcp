from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from sagasmith_coc_mcp.config import McpConfig
from sagasmith_coc_mcp.exposure import ExposureError, ExposureRegistry
from sagasmith_coc_mcp.server import create_server
from sagasmith_coc_mcp.tool_profiles import CORE_TOOLS


async def call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    return result.get("result", result) if isinstance(result, dict) else result


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
        imported = await call(
            server,
            "module_change",
            {
                "action": "import",
                "campaign_id": campaign_id,
                "data": {
                    "source_key": "haunting.md",
                    "title": "The Haunting",
                    "content": "# Boston\n## Corbitt House\nA hidden clue waits upstairs.",
                },
            },
        )
        assert imported["scenes"] >= 1
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
            },
        )
        assert check["success"] is True
        await call(
            server,
            "snapshot_change",
            {"action": "create", "campaign_id": campaign_id, "data": {"label": "Start"}},
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
    registry.load(alice, "play.investigation", ttl_calls=1)
    assert "coc_resolve" in registry.visible_tools(alice)
    assert "coc_resolve" not in registry.visible_tools(bob)
    registry.consume_tool(alice, "coc_resolve")
    assert "coc_resolve" not in registry.visible_tools(alice)
    with pytest.raises(ExposureError):
        registry.load(bob, "lobby.characters")


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
        server.exposure_registry.load(exposure, "lobby.bootstrap")
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
                assert {item.name for item in (await session.list_tools()).tools} == set(
                    CORE_TOOLS
                )
                opened = await session.call_tool("exposure_open", {})
                exposure_id = json.loads(opened.content[0].text)["exposure_id"]
                loaded = await session.call_tool(
                    "exposure_load",
                    {"exposure_id": exposure_id, "group_id": "lobby.bootstrap"},
                )
                assert not loaded.isError
                assert "campaign_change" in {
                    item.name for item in (await session.list_tools()).tools
                }
                created = await session.call_tool(
                    "exposure_call",
                    {
                        "exposure_id": exposure_id,
                        "tool_id": "campaign_change",
                        "arguments": {
                            "action": "create",
                            "data": {"name": "Stdio", "idempotency_key": "stdio-create"},
                        },
                    },
                )
                assert not created.isError

    asyncio.run(exercise())
