from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sagasmith_coc.random_stream import initial_random_stream

from sagasmith_coc_mcp.config import McpConfig
from sagasmith_coc_mcp.server import create_server


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
    return result


def config_for(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "missing-coc-skills",
        modulegen_skills_dir=tmp_path / "missing-modulegen-skills",
    )


def test_checked_skills_settle_atomically_and_replay_after_restart(tmp_path: Path) -> None:
    config = config_for(tmp_path)

    async def exercise() -> tuple[str, str, dict]:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {
                    "name": "Development",
                    "state": {"random_stream": initial_random_stream("development")},
                    "idempotency_key": "create-development",
                },
            },
        )
        actor = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Alice",
                    "expected_campaign_revision": campaign["revision"],
                    "idempotency_key": "create-alice-development",
                    "sheet": {
                        "san": 45,
                        "skills": {
                            "Spot Hidden": 0,
                            "Library Use": 100,
                            "Cthulhu Mythos": 12,
                        },
                        "development": {
                            "checked_skills": [
                                "Spot Hidden",
                                "Library Use",
                                "Cthulhu Mythos",
                            ]
                        },
                    },
                },
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_campaign",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": "player:alice", "role": "player"},
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_actor",
                "campaign_id": campaign["id"],
                "data": {
                    "target_principal_id": "player:alice",
                    "actor_id": actor["id"],
                },
            },
        )
        pending = await call(
            server,
            "development_query",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "principal_id": "player:alice",
            },
        )
        assert [item["skill_name"] for item in pending["pending"]] == [
            "Spot Hidden",
            "Library Use",
            "Cthulhu Mythos",
        ]
        assert pending["pending"][-1]["eligible"] is False
        arguments = {
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "source": "End of the first synthetic session.",
            "expected_revision": pending["campaign_revision"],
            "expected_character_revision": pending["character_revision"],
            "idempotency_key": "settle-first-development",
            "principal_id": "player:alice",
        }
        settled = await call(server, "development_settle", arguments)
        assert settled["random_stream_receipt"]["draw_count"] == 3
        assert settled["receipt"]["results"][0]["improved"] is True
        assert settled["receipt"]["results"][1]["improved"] is False
        assert settled["receipt"]["results"][2]["eligible"] is False
        restarted = create_server(config)
        assert await call(restarted, "development_settle", arguments) == settled
        character = await call(
            restarted,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": actor["id"],
                "principal_id": "player:alice",
            },
        )
        assert character["sheet"]["skills"]["Spot Hidden"] > 0
        assert character["sheet"]["skills"]["Library Use"] == 100
        assert character["sheet"]["skills"]["Cthulhu Mythos"] == 12
        assert character["sheet"]["development"]["checked_skills"] == []
        assert character["sheet"]["development"]["history"][-1] == settled["receipt"]
        return campaign["id"], actor["id"], settled

    campaign_id, actor_id, settled = asyncio.run(exercise())

    async def verify_consumed() -> None:
        restarted = create_server(config)
        empty = await call(
            restarted,
            "development_query",
            {
                "campaign_id": campaign_id,
                "actor_id": actor_id,
                "principal_id": "player:alice",
            },
        )
        assert empty["pending"] == []
        with pytest.raises(Exception, match="no checked skills"):
            await call(
                restarted,
                "development_settle",
                {
                    "campaign_id": campaign_id,
                    "actor_id": actor_id,
                    "source": "No second settlement is available.",
                    "expected_revision": settled["campaign_revision"],
                    "expected_character_revision": settled["character_revision"],
                    "idempotency_key": "illegal-second-development",
                    "principal_id": "player:alice",
                },
            )

    asyncio.run(verify_consumed())


def test_development_is_lobby_only(tmp_path: Path) -> None:
    config = config_for(tmp_path)

    async def exercise() -> None:
        server = create_server(config)
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {"name": "Phase", "idempotency_key": "create-phase"},
            },
        )
        actor = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Alice",
                    "sheet": {"skills": {"Listen": 40}},
                    "expected_campaign_revision": campaign["revision"],
                    "idempotency_key": "create-alice-phase",
                },
            },
        )
        played = await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign["id"],
                "data": {"phase": "play", "expected_revision": campaign["revision"]},
            },
        )
        with pytest.raises(Exception, match="only during lobby"):
            await call(
                server,
                "development_query",
                {"campaign_id": campaign["id"], "actor_id": actor["id"]},
            )
        assert played["state"]["game_phase"] == "play"

    asyncio.run(exercise())
