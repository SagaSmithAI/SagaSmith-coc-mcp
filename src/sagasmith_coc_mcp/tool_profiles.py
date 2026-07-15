"""Server-owned Lobby/Play/Combat capability catalogue."""

from __future__ import annotations

from dataclasses import dataclass

PROFILE_LOBBY = "lobby"
PROFILE_PLAY = "play"
PROFILE_COMBAT = "combat"
PROFILES = (PROFILE_LOBBY, PROFILE_PLAY, PROFILE_COMBAT)


@dataclass(frozen=True)
class ToolGroup:
    id: str
    phase: str
    title: str
    description: str
    risk: str
    tools: frozenset[str]
    requires_campaign: bool = True
    local_only: bool = False


CORE_TOOLS = frozenset(
    {
        "exposure_open",
        "exposure_status",
        "exposure_search",
        "exposure_inspect",
        "exposure_load",
        "exposure_unload",
        "exposure_call",
        "server_capabilities",
        "storage_status",
        "campaign_query",
        "game_phase",
    }
)


def _group(
    id: str,
    phase: str,
    title: str,
    description: str,
    risk: str,
    *tools: str,
    requires_campaign: bool = True,
    local_only: bool = False,
) -> ToolGroup:
    return ToolGroup(
        id, phase, title, description, risk, frozenset(tools), requires_campaign, local_only
    )


TOOL_GROUPS = (
    _group(
        "lobby.bootstrap",
        PROFILE_LOBBY,
        "Campaign bootstrap",
        "Create a CoC campaign and grant explicit campaign or actor access.",
        "write",
        "campaign_change",
        requires_campaign=False,
    ),
    _group(
        "lobby.characters",
        PROFILE_LOBBY,
        "Investigator creation",
        "Create and update validated investigator, NPC and creature sheets.",
        "write",
        "character_query",
        "character_change",
    ),
    _group(
        "lobby.modules",
        PROFILE_LOBBY,
        "Scenario import",
        "Import structured scenario Markdown and inspect the scene index.",
        "write",
        "module_change",
        "module_query",
    ),
    _group(
        "lobby.continuity",
        PROFILE_LOBBY,
        "Continuity and actor knowledge",
        "Maintain branch-scoped campaign facts and separate actor beliefs.",
        "write",
        "memory_query",
        "memory_change",
        "actor_knowledge_query",
        "actor_knowledge_change",
        "snapshot_query",
        "snapshot_change",
        "skill_query",
    ),
    _group(
        "play.investigation",
        PROFILE_PLAY,
        "Investigation play",
        "Read and advance scenes, resolve checks, and update continuity.",
        "write",
        "module_query",
        "module_change",
        "character_query",
        "character_change",
        "memory_query",
        "memory_change",
        "actor_knowledge_query",
        "actor_knowledge_change",
        "coc_resolve",
        "snapshot_query",
        "snapshot_change",
    ),
    _group(
        "combat.resolve",
        PROFILE_COMBAT,
        "Combat resolution",
        "Resolve CoC attacks and checks while state writes remain explicit.",
        "write",
        "character_query",
        "character_change",
        "coc_resolve",
        "memory_query",
        "actor_knowledge_query",
        "snapshot_change",
    ),
)

GROUP_BY_ID = {group.id: group for group in TOOL_GROUPS}
