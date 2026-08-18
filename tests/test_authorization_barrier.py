from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from sagasmith_coc_mcp.config import McpConfig
from sagasmith_coc_mcp.server import create_server


async def call(server, name: str, arguments: dict):
    _, result = await server.call_tool(name, arguments)
    return result.get("result", result) if isinstance(result, dict) else result


def config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "skills",
        modulegen_skills_dir=tmp_path / "modulegen",
    )


def test_actor_permission_and_campaign_revoke_force_session_barriers(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = create_server(config(tmp_path))
        campaign = await call(
            server,
            "campaign_change",
            {"action": "create", "data": {"name": "Barrier", "idempotency_key": "c"}},
        )
        actor = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign["id"],
                "data": {
                    "name": "Private investigator",
                    "character_type": "investigator",
                    "sheet": {"skills": {"Library Use": 50}},
                    "expected_campaign_revision": campaign["revision"],
                    "idempotency_key": "actor",
                },
            },
        )
        target = "discord:reader"
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_campaign",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": target, "role": "player"},
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_actor",
                "campaign_id": campaign["id"],
                "data": {
                    "target_principal_id": target,
                    "actor_id": actor["id"],
                    "can_control": True,
                    "can_view_private": True,
                },
            },
        )
        exposure = server.exposure_registry.open(
            session_key="reader-session",
            principal_id=target,
            campaign_id=campaign["id"],
            phase="lobby",
        )

        class Session:
            notifications = 0

            async def send_tool_list_changed(self) -> None:
                self.notifications += 1

        session = Session()
        server._sessions["reader-session"] = session
        await server._refresh("reader-session", campaign["id"])
        before = exposure.authorization_fingerprint
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_actor",
                "campaign_id": campaign["id"],
                "data": {
                    "target_principal_id": target,
                    "actor_id": actor["id"],
                    "can_control": False,
                    "can_view_private": False,
                },
            },
        )
        assert await server._refresh("reader-session", campaign["id"]) is True
        assert exposure.authorization_fingerprint != before
        assert session.notifications == 1
        redacted = await call(
            server,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": actor["id"],
                "principal_id": target,
            },
        )
        assert "sheet" not in redacted

        await call(
            server,
            "campaign_change",
            {
                "action": "revoke_campaign",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": target},
            },
        )
        assert await server._refresh("reader-session", campaign["id"]) is True
        assert session.notifications == 2

    asyncio.run(scenario())


def test_player_campaign_combat_and_chase_views_redact_keeper_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = create_server(config(tmp_path))
        player = "discord:reader"
        combat_secret = "Keeper-only ambush source"
        combat = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {
                    "name": "Combat projection",
                    "idempotency_key": "combat-campaign",
                    "state": {
                        "game_phase": "play",
                        "random_stream": {"seed": "never disclose"},
                        "investigation_checks": {
                            "pending": {"private_evidence": "hidden clue"}
                        },
                        "combat": {
                            "schema": "sagasmith.coc7e-combat.v1",
                            "active": True,
                            "source": combat_secret,
                            "positioning_mode": "grid",
                            "grid_metric": "chebyshev",
                            "grid_unit_feet": 5.0,
                            "round": 1,
                            "turn_index": 0,
                            "current_actor_id": "npc:hidden",
                            "order": ["npc:hidden", "pc:reader"],
                            "participants": {
                                "npc:hidden": {
                                    "actor_id": "npc:hidden",
                                    "name": "A visible assailant",
                                    "side": "foe",
                                    "dex": 90,
                                    "initiative_score": 140,
                                    "position": [1.0, 1.0],
                                    "available_from_round": 1,
                                },
                                "pc:reader": {
                                    "actor_id": "pc:reader",
                                    "name": "Reader",
                                    "side": "investigators",
                                    "dex": 40,
                                    "initiative_score": 40,
                                    "position": [0.0, 0.0],
                                    "available_from_round": 1,
                                },
                            },
                            "pending_choice": {
                                "id": "private-pending",
                                "kind": "combat_attack_response",
                                "target_actor_id": "pc:reader",
                                "weapon": {"name": "Knife", "damage": "1d4+db"},
                                "attacker_threshold": 95,
                                "source": combat_secret,
                                "response_options": ["none", "dodge"],
                            },
                            "events": [{"source": combat_secret, "attack_roll": 1}],
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
                "campaign_id": combat["id"],
                "data": {"target_principal_id": player, "role": "player"},
            },
        )

        player_get = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": combat["id"], "principal_id": player},
        )
        player_list = await call(
            server,
            "campaign_query",
            {"action": "list", "principal_id": player},
        )
        assert player_get["state_redacted"] is True
        assert player_list["campaigns"][0]["state"] == player_get["state"]
        assert set(player_get["state"]) == {"game_phase", "combat"}
        player_combat = await call(
            server,
            "combat_query",
            {"campaign_id": combat["id"], "principal_id": player},
        )
        projection = player_combat["combat"]
        assert projection["audience_redacted"] is True
        assert "source" not in projection
        assert "events" not in projection
        assert "pending_choice" not in projection
        assert "dex" not in projection["participants"]["npc:hidden"]
        assert "initiative_score" not in projection["participants"]["npc:hidden"]
        assert combat_secret not in repr(player_get)
        assert combat_secret not in repr(player_combat)

        keeper_get = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": combat["id"]},
        )
        assert keeper_get["state"]["combat"]["source"] == combat_secret
        assert keeper_get["state"]["random_stream"]["seed"] == "never disclose"

        chase_secret = "Keeper-only route source"
        chase = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {
                    "name": "Chase projection",
                    "idempotency_key": "chase-campaign",
                    "state": {
                        "game_phase": "play",
                        "chase": {
                            "schema": "sagasmith.coc7e-chase.v1",
                            "active": True,
                            "source": chase_secret,
                            "round": 1,
                            "turn_index": 0,
                            "current_actor_id": "npc:fleeing",
                            "order": ["npc:fleeing", "pc:reader"],
                            "participants": {
                                "npc:fleeing": {
                                    "actor_id": "npc:fleeing",
                                    "name": "Fleeing suspect",
                                    "role": "fleeing",
                                    "participant_kind": "person",
                                    "effective_mov": 9,
                                    "dex": 80,
                                    "position": 2,
                                    "action_points": 2,
                                    "action_points_remaining": 2,
                                    "status": "active",
                                }
                            },
                            "route": [
                                {
                                    "id": "alley",
                                    "title": "Alley",
                                    "index": 1,
                                    "kind": "hazard",
                                    "source": chase_secret,
                                    "private_hazard": "locked gate",
                                }
                            ],
                            "events": [{"source": chase_secret}],
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
                "campaign_id": chase["id"],
                "data": {"target_principal_id": player, "role": "player"},
            },
        )
        player_chase = await call(
            server,
            "chase_query",
            {"campaign_id": chase["id"], "principal_id": player},
        )
        chase_projection = player_chase["chase"]
        assert chase_projection["audience_redacted"] is True
        assert "source" not in chase_projection
        assert "events" not in chase_projection
        assert "effective_mov" not in chase_projection["participants"]["npc:fleeing"]
        assert "dex" not in chase_projection["participants"]["npc:fleeing"]
        assert chase_projection["route"] == [
            {"id": "alley", "title": "Alley", "index": 1, "kind": "hazard"}
        ]
        assert chase_secret not in repr(player_chase)

    asyncio.run(scenario())


def test_keeper_hidden_roll_has_no_player_presentation(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = create_server(config(tmp_path))
        player = "discord:hidden-roll-player"
        campaign = await call(
            server,
            "campaign_change",
            {"action": "create", "data": {"name": "Hidden roll", "idempotency_key": "c"}},
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "grant_campaign",
                "campaign_id": campaign["id"],
                "data": {"target_principal_id": player, "role": "player"},
            },
        )
        current = await call(
            server,
            "campaign_query",
            {"action": "get", "campaign_id": campaign["id"]},
        )
        rolled = await call(
            server,
            "coc_dice_roll",
            {
                "kind": "d100",
                "campaign_id": campaign["id"],
                "expected_revision": current["revision"],
                "idempotency_key": "keeper-secret-roll",
            },
        )
        keeper_view = await call(
            server,
            "resolution_presentation",
            {"campaign_id": campaign["id"], "resolution_id": rolled["resolution_id"]},
        )
        assert keeper_view["audience"] == {
            "scope": "dm",
            "actor_refs": [],
            "disclosure": "hidden",
        }
        assert keeper_view["rolls"]
        with pytest.raises(ToolError, match="resolution presentation not found"):
            await call(
                server,
                "resolution_presentation",
                {
                    "campaign_id": campaign["id"],
                    "resolution_id": rolled["resolution_id"],
                    "principal_id": player,
                },
            )

    asyncio.run(scenario())


def test_character_lifecycle_retry_update_undo_and_redo_use_public_facade(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        server = create_server(config(tmp_path))
        campaign = await call(
            server,
            "campaign_change",
            {"action": "create", "data": {"name": "Lifecycle", "idempotency_key": "c"}},
        )
        arguments = {
            "action": "create",
            "campaign_id": campaign["id"],
            "data": {
                "name": "Investigator",
                "character_type": "investigator",
                "sheet": {"skills": {"Library Use": 40}},
                "expected_campaign_revision": campaign["revision"],
                "idempotency_key": "create-actor",
            },
        }
        created = await call(server, "character_change", arguments)
        replay = await call(server, "character_change", arguments)
        assert replay == created

        update_arguments = {
            "action": "update",
            "campaign_id": campaign["id"],
            "character_id": created["id"],
            "data": {
                "summary": "Updated",
                "sheet": {**created["sheet"], "skills": {"Library Use": 55}},
                "expected_revision": created["revision"],
                "idempotency_key": "update-actor",
            },
        }
        updated = await call(server, "character_change", update_arguments)
        assert await call(server, "character_change", update_arguments) == updated
        assert updated["revision"] == created["revision"] + 1

        history = await call(
            server,
            "state_revision",
            {"action": "history", "campaign_id": campaign["id"], "data": {}},
        )
        cursor = history["revisions"][0]["sequence"]
        await call(
            server,
            "state_revision",
            {
                "action": "undo",
                "campaign_id": campaign["id"],
                "data": {"expected_history_sequence": cursor},
                "idempotency_key": "undo-update",
            },
        )
        restored = await call(
            server,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": created["id"],
            },
        )
        assert restored["summary"] == created["summary"]
        await call(
            server,
            "state_revision",
            {
                "action": "undo",
                "campaign_id": campaign["id"],
                "data": {"expected_history_sequence": cursor - 1},
                "idempotency_key": "undo-create",
            },
        )
        with pytest.raises(Exception):
            await call(
                server,
                "character_query",
                {
                    "action": "get",
                    "campaign_id": campaign["id"],
                    "character_id": created["id"],
                },
            )
        await call(
            server,
            "state_revision",
            {
                "action": "redo",
                "campaign_id": campaign["id"],
                "data": {"expected_history_sequence": 0},
                "idempotency_key": "redo-create",
            },
        )
        redone = await call(
            server,
            "character_query",
            {
                "action": "get",
                "campaign_id": campaign["id"],
                "character_id": created["id"],
            },
        )
        assert redone["id"] == created["id"]

    asyncio.run(scenario())
