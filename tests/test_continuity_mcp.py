from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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


async def create_campaign_with_two_players(server) -> tuple[dict, dict, dict]:
    campaign = await call(
        server,
        "campaign_change",
        {
            "action": "create",
            "data": {"name": "Continuity case", "idempotency_key": "campaign"},
        },
    )
    actors = []
    for name, principal in (("Alice", "player:alice"), ("Bob", "player:bob")):
        actor = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": name,
                    "sheet": {"pow": 60},
                    "expected_campaign_revision": campaign["revision"],
                    "idempotency_key": f"create-{name.lower()}",
                },
            },
        )
        actors.append(actor)
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_campaign",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": principal, "role": "player"},
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_actor",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": principal, "actor_id": actor["id"]},
            },
        )
    return campaign, actors[0], actors[1]


def test_actor_audiences_are_explicit_redacted_and_restartable(tmp_path: Path) -> None:
    config = config_for(tmp_path)

    async def exercise() -> tuple[str, str, str, dict]:
        server = create_server(config)
        campaign, alice, bob = await create_campaign_with_two_players(server)
        arguments = {
            "action": "add",
            "campaign_id": campaign["id"],
            "data": {
                "summary": "Alice alone notices salt water beneath the locked door.",
                "event_type": "clue_discovery",
                "audience_scope": "actor",
                "participants": [{"actor_id": alice["id"], "role": "witness"}],
                "known_by_actor_ids": [alice["id"]],
                "knowledge_key": "salt-water-under-door",
                "knowledge_proposition": "Salt water is seeping from beneath the locked door.",
                "knowledge_disclosure_scope": "owner",
            },
            "idempotency_key": "alice-secret-clue",
        }
        event = await call(server, "campaign_event", arguments)
        assert await call(server, "campaign_event", arguments) == event

        alice_context = await call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "query": "salt water",
                "actor_id": alice["id"],
                "audience": "player",
                "principal_id": "player:alice",
            },
        )
        bob_context = await call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "query": "salt water",
                "actor_id": bob["id"],
                "audience": "dm",
                "principal_id": "player:bob",
            },
        )
        assert [item["id"] for item in alice_context["events"]] == [event["id"]]
        assert [item["knowledge_key"] for item in alice_context["actor_knowledge"]] == [
            "salt-water-under-door"
        ]
        assert bob_context["events"] == []
        assert bob_context["actor_knowledge"] == []
        with pytest.raises(Exception, match="cannot access campaign"):
            await call(
                server,
                "memory_query",
                {
                    "action": "list",
                    "campaign_id": campaign["id"],
                    "principal_id": "player:alice",
                },
            )
        return campaign["id"], alice["id"], bob["id"], event

    campaign_id, alice_id, bob_id, event = asyncio.run(exercise())

    async def verify_restart() -> None:
        restarted = create_server(config)
        alice_context = await call(
            restarted,
            "continuity_context",
            {
                "campaign_id": campaign_id,
                "query": "salt water",
                "actor_id": alice_id,
                "principal_id": "player:alice",
            },
        )
        bob_context = await call(
            restarted,
            "continuity_context",
            {
                "campaign_id": campaign_id,
                "query": "salt water",
                "actor_id": bob_id,
                "principal_id": "player:bob",
            },
        )
        assert [item["id"] for item in alice_context["events"]] == [event["id"]]
        assert bob_context["events"] == []

    asyncio.run(verify_restart())


def test_investigation_commit_is_atomic_idempotent_and_source_linked(tmp_path: Path) -> None:
    config = config_for(tmp_path)

    async def exercise() -> None:
        server = create_server(config)
        campaign, alice, _bob = await create_campaign_with_two_players(server)
        arguments = {
            "action": "commit",
            "campaign_id": campaign["id"],
            "data": {
                "event": {
                    "event_type": "clue_discovery",
                    "summary": "Alice deciphers the lighthouse log.",
                    "audience_scope": "actor",
                    "participants": [{"actor_id": alice["id"], "role": "witness"}],
                    "payload": {"source_ref": "pack:lightless-beacon#log"},
                },
                "facts": [
                    {
                        "action": "add",
                        "fact_key": "lighthouse-log-last-entry",
                        "kind": "clue",
                        "subject": "lighthouse log",
                        "subject_ref": "object:lighthouse-log",
                        "predicate": "last_entry",
                        "content": "The final entry reports something striking the beacon lens.",
                        "disclosure_scope": "dm",
                    }
                ],
                "actor_knowledge": [
                    {
                        "action": "add",
                        "actor_id": alice["id"],
                        "knowledge_key": "lighthouse-log-last-entry",
                        "subject_ref": "object:lighthouse-log",
                        "proposition": "The final log entry reports an impact at the beacon lens.",
                        "cause": "read",
                        "disclosure_scope": "owner",
                    }
                ],
                "snapshot": {"label": "Decoded lighthouse log"},
            },
            "expected_revision": campaign["revision"],
            "idempotency_key": "decode-log",
        }
        committed = await call(server, "memory_change", arguments)
        assert await call(server, "memory_change", arguments) == committed
        assert committed["facts"][0]["source_event_ids"] == [committed["event"]["id"]]
        assert committed["actor_knowledge"][0]["source_event_id"] == committed["event"]["id"]
        assert committed["snapshot"]["label"] == "Decoded lighthouse log"

        upsert_arguments = {
            "action": "upsert",
            "campaign_id": campaign["id"],
            "data": {
                "fact_key": "storm-intensifies",
                "kind": "world_state",
                "content": "The storm around the lighthouse is intensifying.",
                "disclosure_scope": "public",
            },
            "idempotency_key": "storm-intensifies",
        }
        upserted = await call(server, "memory_change", upsert_arguments)
        assert await call(server, "memory_change", upsert_arguments) == upserted

        before_events = await call(
            server,
            "campaign_event",
            {"action": "list", "campaign_id": campaign["id"]},
        )
        invalid = {
            "action": "commit",
            "campaign_id": campaign["id"],
            "data": {
                "event": {
                    "summary": "This settlement must roll back.",
                    "audience_scope": "party",
                },
                "facts": [
                    {
                        "action": "add",
                        "fact_key": "must-not-survive",
                        "content": "This fact must not survive rollback.",
                    }
                ],
                "actor_knowledge": [
                    {
                        "action": "add",
                        "actor_id": alice["id"],
                        "knowledge_key": "lighthouse-log-last-entry",
                        "proposition": "A conflicting duplicate.",
                    }
                ],
            },
            "idempotency_key": "rollback-duplicate",
        }
        with pytest.raises(Exception, match="knowledge key already exists"):
            await call(server, "memory_change", invalid)
        after_events = await call(
            server,
            "campaign_event",
            {"action": "list", "campaign_id": campaign["id"]},
        )
        assert after_events == before_events
        memories = await call(
            server,
            "memory_query",
            {"action": "list", "campaign_id": campaign["id"]},
        )
        assert {item["fact_key"] for item in memories["memories"]} == {
            "lighthouse-log-last-entry",
            "storm-intensifies",
        }

        original_branch = await call(
            server,
            "branch_query",
            {"action": "current", "campaign_id": campaign["id"]},
        )
        current_campaign = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign["id"]},
        )
        clean_head = await call(
            server,
            "snapshot_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "label": "Continuity branch head",
                    "expected_head_snapshot_id": committed["snapshot"]["id"],
                },
                "expected_revision": current_campaign["revision"],
                "expected_branch_id": original_branch["branch"]["id"],
                "idempotency_key": "continuity-clean-head",
            },
        )
        forked = await call(
            server,
            "branch_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Alternative lighthouse reading",
                    "from_snapshot_id": clean_head["id"],
                    "checkout": True,
                },
                "expected_revision": current_campaign["revision"],
                "expected_branch_id": original_branch["branch"]["id"],
                "idempotency_key": "fork-lighthouse-reading",
            },
        )
        branch_event = await call(
            server,
            "campaign_event",
            {
                "action": "add",
                "campaign_id": campaign["id"],
                "data": {
                    "summary": "On this timeline Alice interprets the impact as sabotage.",
                    "audience_scope": "party",
                },
                "idempotency_key": "alternative-sabotage-reading",
            },
        )
        current_log = await call(
            server,
            "campaign_event",
            {"action": "list", "campaign_id": campaign["id"]},
        )
        original_log = await call(
            server,
            "campaign_event",
            {
                "action": "list",
                "campaign_id": campaign["id"],
                "data": {"branch_id": original_branch["branch"]["id"]},
            },
        )
        assert {item["id"] for item in current_log["events"]} == {
            committed["event"]["id"],
            branch_event["id"],
        }
        assert [item["id"] for item in original_log["events"]] == [committed["event"]["id"]]

        restarted = create_server(config)
        restarted_current = await call(
            restarted,
            "campaign_event",
            {"action": "list", "campaign_id": campaign["id"]},
        )
        assert {item["id"] for item in restarted_current["events"]} == {
            committed["event"]["id"],
            branch_event["id"],
        }
        assert (
            await call(
                restarted,
                "branch_query",
                {"action": "current", "campaign_id": campaign["id"]},
            )
        )["branch"]["id"] == forked["branch"]["id"]

    asyncio.run(exercise())
