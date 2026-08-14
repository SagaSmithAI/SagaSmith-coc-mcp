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
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    return result.get("result", result) if isinstance(result, dict) else result


def config(tmp_path: Path) -> McpConfig:
    return McpConfig(
        home=tmp_path / "home",
        database_url=None,
        coc_skills_dir=tmp_path / "missing-coc-skills",
        modulegen_skills_dir=tmp_path / "missing-modulegen-skills",
        npc_host_token="test-host-token",
        module_import_roots=(tmp_path,),
    )


def test_rulebook_pack_import_activation_and_rule_query(tmp_path: Path) -> None:
    source = tmp_path / "quick-start.md"
    source.write_text(
        "# Checks\nRoll percentile dice against the applicable skill value.\n",
        encoding="utf-8",
    )
    server = create_server(config(tmp_path))

    async def scenario() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {"action": "create", "data": {"name": "Rules", "idempotency_key": "campaign"}},
        )
        campaign_id = campaign["id"]
        started = await call(
            server,
            "rulebook_draft",
            {
                "action": "start",
                "campaign_id": campaign_id,
                "data": {
                    "source_path": str(source),
                    "source_key": "quick-start.private",
                    "title": "Quick-Start Rules",
                },
                "idempotency_key": "rules-start",
            },
        )
        evidence = await call(
            server,
            "rulebook_draft",
            {
                "action": "evidence",
                "campaign_id": campaign_id,
                "data": {"job_id": started["job"]["id"], "query": "percentile skill"},
            },
        )
        assert evidence["hits"]
        finalized = await call(
            server,
            "rulebook_draft",
            {
                "action": "finalize",
                "campaign_id": campaign_id,
                "data": {
                    "job_id": started["job"]["id"],
                    "package_id": "coc7e.rules.quick-start.private",
                    "version": "1.0.0",
                    "title": "Quick-Start Rules",
                    "confirmation": {"confirmed": True, "note": "Reviewed test source."},
                },
                "expected_revision": started["job"]["revision"],
                "idempotency_key": "rules-finalize",
            },
        )
        imported = await call(
            server,
            "content_pack",
            {
                "action": "import",
                "campaign_id": campaign_id,
                "data": {"artifact": finalized["artifact"]},
                "expected_revision": 1,
                "idempotency_key": "rules-import",
            },
        )
        assert imported["status"] == "installed"
        assert imported["activated"] is False
        activated = await call(
            server,
            "content_pack",
            {
                "action": "activate",
                "campaign_id": campaign_id,
                "data": {"pack_id": imported["pack_id"], "version": imported["version"]},
                "expected_revision": 1,
                "idempotency_key": "rules-activate",
            },
        )
        assert activated["activation"]["enabled"] is True
        result = await call(
            server,
            "rule_query",
            {
                "action": "search",
                "campaign_id": campaign_id,
                "data": {"query": "percentile skill"},
            },
        )
        assert result["hits"]
        effective = await call(
            server,
            "rule_query",
            {"action": "effective", "campaign_id": campaign_id},
        )
        assert effective["lock"][0]["pack_id"] == imported["pack_id"]

    asyncio.run(scenario())


def test_inventory_wallet_and_source_study_are_atomic_and_idempotent(tmp_path: Path) -> None:
    server = create_server(config(tmp_path))

    async def scenario() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {
                "action": "create",
                "data": {
                    "name": "Character state",
                    "settings": {"luck_recovery": True},
                    "idempotency_key": "campaign",
                },
            },
        )
        campaign_id = campaign["id"]
        actor = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign_id,
                "data": {
                    "name": "Morgan",
                    "sheet": {"monetary": {}, "inventory": [], "luck": 40},
                    "expected_campaign_revision": campaign["revision"],
                    "idempotency_key": "create-morgan-state",
                },
            },
        )
        added_request = {
            "action": "add",
            "campaign_id": campaign_id,
            "actor_id": actor["id"],
            "data": {"item": {"id": "item.lantern", "name": "Lantern", "quantity": 2}},
            "expected_revision": 1,
            "expected_character_revision": 1,
            "idempotency_key": "inventory-add",
        }
        added = await call(server, "inventory_change", added_request)
        assert await call(server, "inventory_change", added_request) == added
        wallet = await call(
            server,
            "wallet_change",
            {
                "action": "set",
                "campaign_id": campaign_id,
                "actor_id": actor["id"],
                "data": {"field": "cash_cents", "value": 500},
                "expected_revision": 2,
                "expected_character_revision": 2,
                "idempotency_key": "wallet-set",
            },
        )
        studied = await call(
            server,
            "long_term_change",
            {
                "action": "source_study",
                "campaign_id": campaign_id,
                "actor_id": actor["id"],
                "data": {
                    "source": "Reviewed private tome card.",
                    "kind": "tome",
                    "source_id": "tome.private-fragment",
                    "title": "Private Fragment",
                    "sanity_loss": 2,
                    "mythos_gain": 4,
                },
                "expected_revision": wallet["campaign_revision"],
                "expected_character_revision": wallet["character_revision"],
                "idempotency_key": "study-tome",
            },
        )
        assert studied["receipt"]["cthulhu_mythos"]["after"] == 4
        recovered = await call(
            server,
            "long_term_change",
            {
                "action": "luck_recovery",
                "campaign_id": campaign_id,
                "actor_id": actor["id"],
                "data": {"source": "Campaign optional Luck recovery."},
                "expected_revision": studied["campaign_revision"],
                "expected_character_revision": studied["character_revision"],
                "idempotency_key": "luck-recovery",
            },
        )
        assert recovered["random_stream_receipt"]["draw_count"] >= 1
        therapy = await call(
            server,
            "long_term_change",
            {
                "action": "therapy",
                "campaign_id": campaign_id,
                "actor_id": actor["id"],
                "data": {"source": "Reviewed downtime interval.", "amount": 2},
                "expected_revision": recovered["campaign_revision"],
                "expected_character_revision": recovered["character_revision"],
                "idempotency_key": "therapy",
            },
        )
        assert therapy["receipt"]["san"]["gain"] == 2
        aged = await call(
            server,
            "long_term_change",
            {
                "action": "aging",
                "campaign_id": campaign_id,
                "actor_id": actor["id"],
                "data": {
                    "source": "Reviewed age-band adjustment.",
                    "characteristic_changes": {"edu": 1, "dex": -1},
                },
                "expected_revision": therapy["campaign_revision"],
                "expected_character_revision": therapy["character_revision"],
                "idempotency_key": "aging",
            },
        )
        assert aged["receipt"]["after"]["edu"] == aged["receipt"]["before"]["edu"] + 1
        current = await call(
            server,
            "character_query",
            {"action": "get", "campaign_id": campaign_id, "character_id": actor["id"]},
        )
        assert current["sheet"]["inventory"][0]["id"] == "item.lantern"
        assert current["sheet"]["monetary"]["cash_cents"] == 500
        assert current["sheet"]["books"][0]["id"] == "tome.private-fragment"

    asyncio.run(scenario())


def test_vehicle_chase_preserves_source_bound_cards_through_public_facade(
    tmp_path: Path,
) -> None:
    server = create_server(config(tmp_path))

    async def scenario() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {"action": "create", "data": {"name": "Vehicles", "idempotency_key": "c"}},
        )
        campaign_id = campaign["id"]

        async def create_driver(name: str) -> dict:
            return await call(
                server,
                "character_change",
                {
                    "action": "create",
                    "campaign_id": campaign_id,
                    "data": {
                        "name": name,
                        "expected_campaign_revision": campaign["revision"],
                        "idempotency_key": f"create-{name.lower().replace(' ', '-')}",
                        "sheet": {
                            "characteristics": {"con": 60, "dex": 60},
                            "skills": {"Drive Auto": 60},
                            "mov": 8,
                        },
                    },
                },
            )

        lead = await create_driver("Lead Driver")
        pursuit = await create_driver("Pursuit Driver")
        phased = await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign_id,
                "data": {"phase": "play", "expected_revision": campaign["revision"]},
            },
        )
        started = await call(
            server,
            "chase_start",
            {
                "campaign_id": campaign_id,
                "participants": [
                    {
                        "actor_id": lead["id"],
                        "role": "fleeing",
                        "speed_skill_name": "Drive Auto",
                        "participant_kind": "vehicle",
                        "vehicle": {
                            "source_id": "vehicle.sedan",
                            "name": "Sedan",
                            "mov": 12,
                            "build": 5,
                        },
                    },
                    {
                        "actor_id": pursuit["id"],
                        "role": "pursuer",
                        "speed_skill_name": "Drive Auto",
                        "participant_kind": "vehicle",
                        "vehicle": {
                            "source_id": "vehicle.truck",
                            "name": "Truck",
                            "mov": 11,
                            "build": 6,
                        },
                    },
                ],
                "expected_character_revisions": {
                    lead["id"]: lead["revision"],
                    pursuit["id"]: pursuit["revision"],
                },
                "source": "Reviewed source vehicle chase setup.",
                "expected_revision": phased["revision"],
                "idempotency_key": "vehicle-chase",
            },
        )
        assert started["chase"]["participants"][lead["id"]]["vehicle"] == {
            "source_id": "vehicle.sedan",
            "name": "Sedan",
            "mov": 12,
            "build": 5,
        }

    asyncio.run(scenario())


def test_bounded_render_and_isolated_npc_conversation_settle_public_state(
    tmp_path: Path,
) -> None:
    server = create_server(config(tmp_path))

    async def scenario() -> None:
        campaign = await call(
            server,
            "campaign_change",
            {"action": "create", "data": {"name": "Dialogue", "idempotency_key": "c"}},
        )
        campaign_id = campaign["id"]
        investigator = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign_id,
                    "data": {
                        "name": "Morgan",
                        "character_type": "investigator",
                        "expected_campaign_revision": campaign["revision"],
                        "idempotency_key": "create-morgan",
                    },
            },
        )
        npc = await call(
            server,
            "character_change",
            {
                "action": "create",
                "campaign_id": campaign_id,
                    "data": {
                        "name": "Harbormaster",
                        "character_type": "npc",
                        "summary": "Knows when the lighthouse boat departed.",
                        "expected_campaign_revision": campaign["revision"],
                        "idempotency_key": "create-harbormaster",
                    },
            },
        )
        await call(
            server,
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": campaign_id,
                "data": {"phase": "play", "expected_revision": campaign["revision"]},
            },
        )
        bounded = await call(
            server,
            "continuity_context",
            {
                "campaign_id": campaign_id,
                "actor_id": investigator["id"],
                "audience": "player",
                "purpose": "audience_render",
            },
        )
        validated = await call(
            server,
            "bounded_evaluation",
            {
                "action": "validate",
                "campaign_id": campaign_id,
                "bundle_receipt": bounded["bundle_receipt"],
                "proposal": {
                    "schema_version": 1,
                    "bundle_id": bounded["bundle_id"],
                    "purpose": "audience_render",
                    "text": "The harbormaster watches the fog.",
                    "cited_basis_refs": [],
                    "omitted_sensitive_refs": [],
                    "decision_summary": "No private Keeper facts disclosed.",
                },
            },
        )
        assert validated["publication"]["text"].startswith("The harbormaster")
        assert validated["authoritative_state_changed"] is False

        opened = await call(
            server,
            "npc_conversation",
            {
                "action": "open",
                "campaign_id": campaign_id,
                "data": {
                    "participant_actor_ids": [investigator["id"], npc["id"]],
                    "query": "lighthouse boat departure",
                    "idempotency_key": "open",
                },
            },
        )
        restarted = create_server(config(tmp_path))
        recovered = await call(
            restarted,
            "npc_conversation",
            {
                "action": "get",
                "campaign_id": campaign_id,
                "data": {"conversation_id": opened["conversation_id"]},
            },
        )
        assert recovered["status"] == "open"
        await call(
            server,
            "actor_knowledge_change",
            {
                "action": "add",
                "campaign_id": campaign_id,
                "actor_id": npc["id"],
                "data": {
                    "knowledge_key": "new-tide-report",
                    "proposition": "The tide report arrived during the conversation.",
                },
                "idempotency_key": "new-tide-report",
            },
        )
        refreshed = await call(
            server,
            "npc_conversation",
            {
                "action": "get",
                "campaign_id": campaign_id,
                "data": {"conversation_id": opened["conversation_id"]},
            },
        )
        assert refreshed["refreshed_actor_ids"] == [npc["id"]]
        with pytest.raises(Exception, match="active NPC conversation"):
            await call(
                server,
                "campaign_change",
                {
                    "action": "set_phase",
                    "campaign_id": campaign_id,
                    "data": {"phase": "lobby", "expected_revision": 2},
                },
            )
        with pytest.raises(Exception, match="active NPC conversation"):
            await call(
                server,
                "combat_start",
                {
                    "campaign_id": campaign_id,
                    "participants": [],
                    "expected_character_revisions": {},
                    "positioning_mode": "agent",
                    "source": "Conversation interruption regression.",
                    "expected_revision": 2,
                    "idempotency_key": "blocked-combat",
                },
            )
        branch = await call(
            server,
            "branch_query",
            {"action": "current", "campaign_id": campaign_id, "data": {}},
        )
        branch_id = branch["branch"]["id"]
        with pytest.raises(Exception, match="active NPC conversation"):
            await call(
                server,
                "branch_change",
                {
                    "action": "create",
                    "campaign_id": campaign_id,
                    "data": {"name": "blocked", "checkout": True},
                    "expected_revision": 2,
                    "expected_branch_id": branch_id,
                    "idempotency_key": "blocked-branch",
                },
            )
        with pytest.raises(Exception, match="active NPC conversation"):
            await call(
                server,
                "snapshot_change",
                {
                    "action": "restore",
                    "campaign_id": campaign_id,
                    "data": {"slot": 1},
                    "expected_revision": 2,
                    "expected_branch_id": branch_id,
                    "idempotency_key": "blocked-restore",
                },
            )
        with pytest.raises(Exception, match="active NPC conversation"):
            await call(
                server,
                "state_revision",
                {
                    "action": "undo",
                    "campaign_id": campaign_id,
                    "data": {},
                },
            )
        with pytest.raises(Exception, match="active NPC conversation"):
            await call(
                server,
                "module_change",
                {
                    "action": "set_progress",
                    "campaign_id": campaign_id,
                    "data": {"scene_id": "blocked-scene"},
                },
            )
        ingested = await call(
            server,
            "npc_conversation",
            {
                "action": "ingest",
                "campaign_id": campaign_id,
                "data": {
                    "conversation_id": opened["conversation_id"],
                    "event": {
                        "type": "speech",
                        "speaker_actor_id": investigator["id"],
                        "content": "When did the lighthouse boat leave?",
                        "declared_target_actor_ids": [npc["id"]],
                    },
                    "audience_facts": {
                        "decision_id": "audience-1",
                        "resolver": "agent",
                        "perceived_actor_ids": [investigator["id"], npc["id"]],
                        "understood_actor_ids": [investigator["id"], npc["id"]],
                        "response_actor_ids": [npc["id"]],
                        "partial_renditions": {},
                        "basis_refs": [],
                        "reason": "Both participants are face to face and share English.",
                    },
                    "expected_conversation_revision": refreshed["conversation_revision"],
                    "idempotency_key": "ingest",
                },
            },
        )
        activation = ingested["activations"][0]
        claimed = await call(
            server,
            "npc_conversation_transport",
            {
                "action": "claim_activation",
                "campaign_id": campaign_id,
                "conversation_id": opened["conversation_id"],
                "payload": {
                    "activation_ref": activation["activation_ref"],
                    "expected_conversation_revision": ingested["conversation_revision"],
                    "idempotency_key": "claim",
                },
                "host_token": "test-host-token",
            },
        )
        submitted = await call(
            server,
            "npc_conversation_transport",
            {
                "action": "submit_proposal",
                "campaign_id": campaign_id,
                "conversation_id": opened["conversation_id"],
                "payload": {
                    "activation_ref": activation["activation_ref"],
                    "lease_id": claimed["lease_id"],
                    "expected_conversation_revision": claimed["conversation_revision"],
                    "idempotency_key": "submit",
                    "proposal": {
                        "schema_version": 4,
                        "conversation_id": opened["conversation_id"],
                        "activation_id": claimed["activation_id"],
                        "actor_runtime_id": claimed["actor_runtime_id"],
                        "response_bid": {
                            "should_respond": True,
                            "urgency": 30,
                            "reason": "A direct factual question was asked.",
                        },
                        "private_intent": "Avoid mentioning the damaged lens.",
                        "utterance_segments": [
                            {
                                "text": "Just before dusk.",
                                "speech_act": "answer",
                                "truth_posture": "supported",
                                "basis_refs": [
                                    claimed["inbox"][0]["event_id"]
                                ],
                                "targets": [investigator["id"]],
                                "language": "English",
                                "delivery": "quietly",
                            }
                        ],
                        "proposed_action": {
                            "summary": "",
                            "target_refs": [],
                            "settlement": "narrative",
                            "mechanic_hint": "",
                        },
                        "resolution_requests": [],
                        "working_deltas": {
                            "facts": [],
                            "actor_knowledge": [],
                            "commitments": [],
                        },
                        "visible_cues": ["glances toward the harbor"],
                        "decision_summary": "Answers the investigator.",
                    },
                },
                "host_token": "test-host-token",
            },
        )
        assert "private_intent" not in repr(submitted)
        published = await call(
            server,
            "npc_conversation",
            {
                "action": "publish",
                "campaign_id": campaign_id,
                "data": {
                    "conversation_id": opened["conversation_id"],
                    "publication_id": submitted["publication"]["publication_id"],
                    "audience_facts": {
                        "decision_id": "audience-2",
                        "resolver": "agent",
                        "perceived_actor_ids": [investigator["id"], npc["id"]],
                        "understood_actor_ids": [investigator["id"], npc["id"]],
                        "response_actor_ids": [],
                        "partial_renditions": {},
                        "basis_refs": [],
                        "reason": "The reply is spoken clearly in shared English.",
                    },
                    "expected_conversation_revision": submitted[
                        "conversation_revision"
                    ],
                    "idempotency_key": "publish",
                },
            },
        )
        status = await call(
            server,
            "npc_conversation",
            {
                "action": "get",
                "campaign_id": campaign_id,
                "data": {"conversation_id": opened["conversation_id"]},
            },
        )
        accepted_candidate_ids = [
            item["candidate_id"] for item in status["memory_candidates"]
        ]
        assert len(accepted_candidate_ids) == 2
        closed = await call(
            server,
            "npc_conversation",
            {
                "action": "close",
                "campaign_id": campaign_id,
                "data": {
                    "conversation_id": opened["conversation_id"],
                    "accepted_candidate_ids": accepted_candidate_ids,
                    "expected_conversation_revision": published[
                        "conversation_revision"
                    ],
                    "idempotency_key": "close",
                },
            },
        )
        assert closed["conversation_revision"] == published["conversation_revision"] + 1
        events = await call(
            server,
            "campaign_event",
            {"action": "list", "campaign_id": campaign_id, "data": {}},
        )
        conversation_event = next(
            item for item in events["events"] if item["event_type"] == "npc_conversation"
        )
        assert conversation_event["payload"]["transcript"][-1]["content"] == (
            "Just before dusk."
        )
        assert conversation_event["payload"]["conversation_id"] == opened[
            "conversation_id"
        ]
        assert conversation_event["retrieval_text"].endswith("Just before dusk.")
        investigator_knowledge = await call(
            server,
            "actor_knowledge_query",
            {
                "action": "list",
                "campaign_id": campaign_id,
                "actor_id": investigator["id"],
                "data": {},
            },
        )
        assert any(
            item["proposition"] == f"{npc['id']} said: Just before dusk."
            for item in investigator_knowledge["knowledge"]
        )

    asyncio.run(scenario())
