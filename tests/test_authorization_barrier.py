from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
