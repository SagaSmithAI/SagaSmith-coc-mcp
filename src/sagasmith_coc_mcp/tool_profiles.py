"""One authoritative phase and role policy per public CoC MCP tool."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sagasmith_core.access import CAMPAIGN_DM_ROLES

PROFILE_LOBBY = "lobby"
PROFILE_PLAY = "play"
PROFILE_COMBAT = "combat"
PROFILES = (PROFILE_LOBBY, PROFILE_PLAY, PROFILE_COMBAT)


def campaign_phase(state: Mapping[str, Any] | None) -> str:
    """Resolve the authoritative phase from persisted campaign state."""

    value = dict(state or {})
    combat = value.get("combat")
    if isinstance(combat, Mapping) and bool(combat.get("active", False)):
        return PROFILE_COMBAT
    phase = str(value.get("game_phase") or PROFILE_LOBBY)
    if phase not in {PROFILE_LOBBY, PROFILE_PLAY}:
        raise ValueError(f"unsupported persisted campaign phase: {phase}")
    return phase


CORE_TOOLS = frozenset(
    {
        "exposure",
        "server_capabilities",
        "storage_status",
        "campaign_query",
        "game_phase",
        "skill_query",
    }
)


def _names(value: str) -> frozenset[str]:
    return frozenset(value.split())


PHASE_TOOLS = {
    PROFILE_LOBBY: _names(
        """
        actor_knowledge_change actor_knowledge_query campaign_change character_change
        character_query coc_check coc_dice_roll coc_resolve memory_change memory_query
        content_pack module_change module_draft module_query
        snapshot_change snapshot_query
        """
    ),
    PROFILE_PLAY: _names(
        """
        actor_knowledge_change actor_knowledge_query campaign_change character_change
        character_query coc_check coc_dice_roll coc_resolve memory_change memory_query
        module_change module_query
        snapshot_change snapshot_query
        """
    ),
    PROFILE_COMBAT: _names(
        """
        actor_knowledge_query character_change character_query coc_check coc_dice_roll
        coc_resolve memory_query module_query snapshot_change snapshot_query
        """
    ),
}

PHASE_DM_TOOLS = {
    PROFILE_LOBBY: _names(
        """
        actor_knowledge_change character_change content_pack memory_change module_change
        module_draft snapshot_change
        """
    ),
    PROFILE_PLAY: _names(
        """
        actor_knowledge_change campaign_change memory_change module_change snapshot_change
        """
    ),
    PROFILE_COMBAT: _names("snapshot_change"),
}

NO_CAMPAIGN_TOOLS = frozenset({"campaign_change"})
LOCAL_ONLY_TOOLS = frozenset()


@dataclass(frozen=True)
class ToolPolicy:
    id: str
    phases: frozenset[str]
    roles_by_phase: Mapping[str, frozenset[str]]
    requires_campaign: bool
    local_only: bool

    def roles(self, phase: str) -> frozenset[str]:
        return self.roles_by_phase.get(phase, frozenset())


def _build_policies() -> dict[str, ToolPolicy]:
    tool_ids = frozenset().union(*PHASE_TOOLS.values())
    return {
        tool_id: ToolPolicy(
            id=tool_id,
            phases=frozenset(phase for phase in PROFILES if tool_id in PHASE_TOOLS[phase]),
            roles_by_phase={
                phase: frozenset(CAMPAIGN_DM_ROLES)
                for phase in PROFILES
                if tool_id in PHASE_DM_TOOLS[phase]
            },
            requires_campaign=tool_id not in NO_CAMPAIGN_TOOLS,
            local_only=tool_id in LOCAL_ONLY_TOOLS,
        )
        for tool_id in tool_ids
    }


TOOL_POLICIES = _build_policies()


def policy_for_tool(name: str) -> ToolPolicy | None:
    return TOOL_POLICIES.get(name)


def tools_for_phase(phase: str) -> frozenset[str]:
    if phase not in PHASE_TOOLS:
        raise ValueError(f"unsupported tool phase: {phase}")
    return PHASE_TOOLS[phase] | CORE_TOOLS


def validate_profile_coverage(tool_names: Iterable[str]) -> None:
    missing = sorted(
        name for name in tool_names if name not in CORE_TOOLS and name not in TOOL_POLICIES
    )
    if missing:
        raise RuntimeError(f"MCP tools missing a tool policy: {', '.join(missing)}")


def profile_catalog() -> dict[str, list[str]]:
    return {profile: sorted(tools_for_phase(profile)) for profile in PROFILES}


def tool_catalog() -> list[dict[str, object]]:
    return [
        {
            "id": policy.id,
            "phases": sorted(policy.phases),
            "roles_by_phase": {
                phase: sorted(roles) for phase, roles in policy.roles_by_phase.items()
            },
            "requires_campaign": policy.requires_campaign,
            "local_only": policy.local_only,
        }
        for policy in sorted(TOOL_POLICIES.values(), key=lambda item: item.id)
    ]
