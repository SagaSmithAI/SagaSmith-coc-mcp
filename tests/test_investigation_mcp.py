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


async def campaign_and_investigator(
    server,
    *,
    seed: str,
    spending_luck: bool,
) -> tuple[dict, dict]:
    campaign = await call(
        server,
        "campaign_change",
        {
            "action": "create",
            "data": {
                "name": "Investigation check",
                "settings": {"spending_luck": spending_luck},
                "state": {"random_stream": initial_random_stream(seed)},
                "idempotency_key": f"campaign-{seed}",
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
                "idempotency_key": f"create-alice-{seed}",
                "sheet": {
                    "luck": 20,
                    "skills": {"Spot Hidden": 40},
                    "characteristics": {"pow": 60},
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
            "data": {"target_principal_id": "player:alice", "actor_id": actor["id"]},
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
    return played, actor


def test_luck_choice_is_persisted_spent_and_followed_by_explicit_continuity(
    tmp_path: Path,
) -> None:
    config = config_for(tmp_path)

    async def exercise() -> tuple[str, str, str]:
        server = create_server(config)
        campaign, actor = await campaign_and_investigator(
            server,
            seed="seed-0",
            spending_luck=True,
        )
        open_arguments = {
            "action": "open",
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "data": {
                "trait_kind": "skill",
                "trait_name": "Spot Hidden",
                "difficulty": "regular",
                "goal": "Notice the salt water beneath the locked door.",
                "source": "Synthetic private-Pack clue source.",
            },
            "expected_revision": campaign["revision"],
            "expected_character_revision": actor["revision"],
            "idempotency_key": "open-salt-water-check",
            "principal_id": "player:alice",
        }
        opened = await call(server, "investigation_check", open_arguments)
        assert await call(server, "investigation_check", open_arguments) == opened
        assert opened["pending"]["roll"]["total"] == 45
        assert opened["pending"]["outcome"]["success"] is False
        assert set(opened["pending"]["available_actions"]) == {
            "settle",
            "spend_luck",
            "push",
        }
        assert opened["resolution_id"] == opened["pending"]["id"]
        assert opened["thread_id"] == opened["resolution_id"]
        assert opened["event_sequence"] == 1

        restarted = create_server(config)
        pending = await call(
            restarted,
            "investigation_query",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "principal_id": "player:alice",
            },
        )
        assert pending["pending"]["id"] == opened["pending"]["id"]
        presentation = await call(
            restarted,
            "resolution_presentation",
            {
                "campaign_id": campaign["id"],
                "resolution_id": opened["resolution_id"],
                "principal_id": "player:alice",
            },
        )
        assert presentation["schema"] == "sagasmith.resolution-presentation/v1"
        assert presentation["system_id"] == "coc7e"
        assert presentation["status"] == "pending"
        assert presentation["event_sequence"] == 1
        assert presentation["rolls"][0]["total"] == 45
        assert set(presentation["pending_choice"]["available_actions"]) == {
            "settle",
            "spend_luck",
            "push",
        }
        assert "source" not in repr(presentation)
        with pytest.raises(Exception, match="pending investigation checks"):
            await call(
                restarted,
                "combat_start",
                {
                    "campaign_id": campaign["id"],
                    "participants": [],
                    "expected_character_revisions": {},
                    "positioning_mode": "agent",
                    "source": "A confrontation cannot interrupt an unresolved choice.",
                    "expected_revision": opened["campaign_revision"],
                    "idempotency_key": "blocked-combat",
                },
            )

        luck_arguments = {
            "action": "spend_luck",
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "data": {"check_id": opened["pending"]["id"], "luck_spent": 5},
            "expected_revision": opened["campaign_revision"],
            "expected_character_revision": opened["character_revision"],
            "idempotency_key": "spend-five-luck",
            "principal_id": "player:alice",
        }
        adjusted = await call(restarted, "investigation_check", luck_arguments)
        assert await call(restarted, "investigation_check", luck_arguments) == adjusted
        assert adjusted["pending"]["outcome"]["success"] is True
        assert adjusted["pending"]["outcome"]["modified_total"] == 40
        assert adjusted["pending"]["available_actions"] == ["settle"]
        assert adjusted["resolution_id"] == opened["resolution_id"]
        assert adjusted["event_sequence"] == 2
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
        assert character["sheet"]["luck"] == 15

        settle_arguments = {
            "action": "settle",
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "data": {"check_id": opened["pending"]["id"]},
            "expected_revision": adjusted["campaign_revision"],
            "expected_character_revision": adjusted["character_revision"],
            "idempotency_key": "settle-salt-water-check",
            "principal_id": "player:alice",
        }
        settled = await call(restarted, "investigation_check", settle_arguments)
        assert await call(restarted, "investigation_check", settle_arguments) == settled
        assert settled["continuity_required"] is True
        assert settled["resolution_id"] == opened["resolution_id"]
        assert settled["event_sequence"] == 3
        settled_presentation = await call(
            restarted,
            "resolution_presentation",
            {
                "campaign_id": campaign["id"],
                "resolution_id": settled["resolution_id"],
                "principal_id": "player:alice",
            },
        )
        assert settled_presentation["status"] == "settled"
        assert settled_presentation["event_sequence"] == 3
        assert settled_presentation["pending_choice"] is None
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
        assert character["sheet"]["development"]["checked_skills"] == ["Spot Hidden"]

        committed = await call(
            restarted,
            "memory_change",
            {
                "action": "commit",
                "campaign_id": campaign["id"],
                "data": {
                    "event": {
                        "event_type": "clue_discovery",
                        "summary": "Alice notices salt water beneath the locked door.",
                        "audience_scope": "actor",
                        "participants": [{"actor_id": actor["id"], "role": "witness"}],
                        "payload": {"mechanical_check_id": settled["receipt"]["id"]},
                    },
                    "actor_knowledge": [
                        {
                            "actor_id": actor["id"],
                            "knowledge_key": "salt-water-under-locked-door",
                            "proposition": "Salt water is seeping from beneath the locked door.",
                            "disclosure_scope": "owner",
                        }
                    ],
                },
                "expected_revision": settled["campaign_revision"],
                "idempotency_key": "commit-salt-water-clue",
            },
        )
        assert committed["actor_knowledge"][0]["source_event_id"] == committed["event"]["id"]
        context = await call(
            restarted,
            "continuity_context",
            {
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "query": "salt water",
                "principal_id": "player:alice",
            },
        )
        assert context["actor_knowledge"][0]["knowledge_key"] == (
            "salt-water-under-locked-door"
        )
        return campaign["id"], actor["id"], settled["receipt"]["id"]

    campaign_id, actor_id, receipt_id = asyncio.run(exercise())

    async def verify_restart() -> None:
        restarted = create_server(config)
        history = await call(
            restarted,
            "investigation_query",
            {
                "campaign_id": campaign_id,
                "actor_id": actor_id,
                "view": "history",
                "principal_id": "player:alice",
            },
        )
        assert history["history"][-1]["id"] == receipt_id
        assert history["history"][-1]["status"] == "settled"

    asyncio.run(verify_restart())


def test_failed_push_persists_justification_and_keeper_consequence(tmp_path: Path) -> None:
    config = config_for(tmp_path)

    async def exercise() -> None:
        server = create_server(config)
        campaign, actor = await campaign_and_investigator(
            server,
            seed="seed-6",
            spending_luck=True,
        )
        opened = await call(
            server,
            "investigation_check",
            {
                "action": "open",
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "data": {
                    "trait_kind": "skill",
                    "trait_name": "Spot Hidden",
                    "goal": "Find a hidden mechanism in the radio.",
                    "source": "Synthetic private-Pack radio source.",
                },
                "expected_revision": campaign["revision"],
                "expected_character_revision": actor["revision"],
                "idempotency_key": "open-radio-check",
                "principal_id": "player:alice",
            },
        )
        assert opened["pending"]["roll"]["total"] == 58
        pushed = await call(
            server,
            "investigation_check",
            {
                "action": "push",
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "data": {
                    "check_id": opened["pending"]["id"],
                    "justification": "Alice dismantles the radio and inspects every contact.",
                    "failure_consequence": "The fragile radio is rendered permanently inoperable.",
                },
                "expected_revision": opened["campaign_revision"],
                "expected_character_revision": opened["character_revision"],
                "idempotency_key": "push-radio-check",
                "principal_id": "player:alice",
            },
        )
        assert pushed["pending"]["roll"]["total"] == 89
        assert pushed["pending"]["outcome"]["failed_pushed_roll"] is True
        assert pushed["pending"]["outcome"]["luck_options"] == {}
        assert pushed["pending"]["decision"]["failure_consequence"].startswith(
            "The fragile radio"
        )
        with pytest.raises(Exception, match="cannot spend Luck"):
            await call(
                server,
                "investigation_check",
                {
                    "action": "spend_luck",
                    "campaign_id": campaign["id"],
                    "actor_id": actor["id"],
                    "data": {"check_id": opened["pending"]["id"], "luck_spent": 1},
                    "expected_revision": pushed["campaign_revision"],
                    "expected_character_revision": pushed["character_revision"],
                    "idempotency_key": "illegal-luck-after-push",
                    "principal_id": "player:alice",
                },
            )
        settled = await call(
            server,
            "investigation_check",
            {
                "action": "settle",
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "data": {"check_id": opened["pending"]["id"]},
                "expected_revision": pushed["campaign_revision"],
                "expected_character_revision": pushed["character_revision"],
                "idempotency_key": "settle-radio-failure",
                "principal_id": "player:alice",
            },
        )
        assert settled["receipt"]["outcome"]["failed_pushed_roll"] is True
        assert settled["character_revision"] == actor["revision"]

        luck_roll = await call(
            server,
            "investigation_check",
            {
                "action": "open",
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "data": {
                    "trait_kind": "luck",
                    "trait_name": "Luck",
                    "goal": "Determine whether a spare radio valve is nearby.",
                    "source": "Synthetic external-circumstance source.",
                },
                "expected_revision": settled["campaign_revision"],
                "expected_character_revision": settled["character_revision"],
                "idempotency_key": "open-luck-roll",
                "principal_id": "player:alice",
            },
        )
        assert luck_roll["pending"]["outcome"]["roll_kind"] == "luck"
        assert luck_roll["pending"]["available_actions"] == ["settle"]
        with pytest.raises(Exception, match="pending investigation checks"):
            await call(
                server,
                "campaign_change",
                {
                    "action": "set_phase",
                    "campaign_id": campaign["id"],
                    "data": {
                        "phase": "lobby",
                        "expected_revision": luck_roll["campaign_revision"],
                    },
                },
            )
        abort_arguments = {
            "action": "abort",
            "campaign_id": campaign["id"],
            "actor_id": actor["id"],
            "data": {
                "check_id": luck_roll["pending"]["id"],
                "reason": "The Keeper closes the test before administrative recovery.",
            },
            "expected_revision": luck_roll["campaign_revision"],
            "expected_character_revision": luck_roll["character_revision"],
            "idempotency_key": "abort-luck-roll",
        }
        aborted = await call(server, "investigation_check", abort_arguments)
        assert await call(server, "investigation_check", abort_arguments) == aborted
        returned = await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign["id"],
                "data": {
                    "phase": "lobby",
                    "expected_revision": aborted["campaign_revision"],
                },
            },
        )
        assert returned["state"]["game_phase"] == "lobby"

    asyncio.run(exercise())


def test_combined_check_uses_one_roll_and_marks_each_successful_skill(tmp_path: Path) -> None:
    config = config_for(tmp_path)

    async def exercise() -> None:
        server = create_server(config)
        campaign, actor = await campaign_and_investigator(
            server,
            seed="seed-0",
            spending_luck=True,
        )
        opened = await call(
            server,
            "investigation_check",
            {
                "action": "open",
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "data": {
                    "traits": [
                        {"trait_kind": "characteristic", "trait_name": "pow"},
                        {"trait_kind": "skill", "trait_name": "Spot Hidden"},
                    ],
                    "requirement": "all",
                    "goal": "Notice and comprehend the impossible reflection.",
                    "source": "Synthetic combined-check source.",
                },
                "expected_revision": campaign["revision"],
                "expected_character_revision": actor["revision"],
                "idempotency_key": "open-combined-check",
                "principal_id": "player:alice",
            },
        )
        assert opened["random_stream_receipt"]["draw_count"] == 2
        assert opened["pending"]["check_kind"] == "combined"
        assert opened["pending"]["outcome"]["success"] is False
        assert opened["pending"]["outcome"]["luck_options"] == {
            "meet_requirement": 5
        }
        adjusted = await call(
            server,
            "investigation_check",
            {
                "action": "spend_luck",
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "data": {"check_id": opened["pending"]["id"], "luck_spent": 5},
                "expected_revision": opened["campaign_revision"],
                "expected_character_revision": opened["character_revision"],
                "idempotency_key": "buy-combined-check",
                "principal_id": "player:alice",
            },
        )
        assert adjusted["pending"]["outcome"]["success"] is True
        assert adjusted["pending"]["outcome"]["development_eligible_skills"] == [
            "Spot Hidden"
        ]
        settled = await call(
            server,
            "investigation_check",
            {
                "action": "settle",
                "campaign_id": campaign["id"],
                "actor_id": actor["id"],
                "data": {"check_id": opened["pending"]["id"]},
                "expected_revision": adjusted["campaign_revision"],
                "expected_character_revision": adjusted["character_revision"],
                "idempotency_key": "settle-combined-check",
                "principal_id": "player:alice",
            },
        )
        character = await call(
            server,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": actor["id"],
                "principal_id": "player:alice",
            },
        )
        assert character["sheet"]["development"]["checked_skills"] == ["Spot Hidden"]
        assert settled["receipt"]["outcome"]["requirement"] == "all"

    asyncio.run(exercise())


def test_group_luck_requires_a_tie_choice_and_uses_the_lowest_current_value(
    tmp_path: Path,
) -> None:
    config = config_for(tmp_path)

    async def exercise() -> None:
        server = create_server(config)
        campaign, actor = await campaign_and_investigator(
            server,
            seed="group-luck",
            spending_luck=True,
        )
        second = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Bob",
                    "sheet": {"luck": 20},
                    "expected_campaign_revision": campaign["revision"],
                    "idempotency_key": "create-bob-luck",
                },
            },
        )
        queried = await call(
            server,
            "group_luck_query",
            {
                "campaign_id": campaign["id"],
                "participant_actor_ids": [actor["id"], second["id"]],
            },
        )
        assert queried["lowest_luck"] == 20
        assert queried["candidate_actor_ids"] == [actor["id"], second["id"]]
        base = {
            "campaign_id": campaign["id"],
            "participant_actor_ids": [actor["id"], second["id"]],
            "source": "Synthetic group circumstance source.",
            "goal": "Determine whether a cab passes the group in time.",
            "expected_revision": queried["campaign_revision"],
            "idempotency_key": "group-luck-roll",
        }
        with pytest.raises(Exception, match="tied lowest Luck"):
            await call(server, "group_luck_check", base)
        arguments = {**base, "selected_actor_id": second["id"]}
        resolved = await call(server, "group_luck_check", arguments)
        assert await call(server, "group_luck_check", arguments) == resolved
        assert resolved["receipt"]["selected_actor_id"] == second["id"]
        assert resolved["receipt"]["threshold"] == 20
        assert resolved["receipt"]["outcome"]["roll_kind"] == "luck"
        assert resolved["receipt"]["outcome"]["luck_options"] == {}
        assert resolved["continuity_required"] is True

    asyncio.run(exercise())
