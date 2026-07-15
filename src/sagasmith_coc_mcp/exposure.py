"""Session-scoped progressive capability exposure owned by the MCP server."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .tool_profiles import CORE_TOOLS, GROUP_BY_ID, TOOL_GROUPS


class ExposureError(ValueError):
    pass


@dataclass
class Exposure:
    id: str
    session_key: str
    principal_id: str
    campaign_id: str | None
    phase: str
    loaded_groups: set[str] = field(default_factory=set)
    remaining_calls: dict[str, int | None] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(hours=12))


class ExposureRegistry:
    def __init__(self, *, ttl: timedelta = timedelta(hours=12)) -> None:
        self._by_id: dict[str, Exposure] = {}
        self._active_by_session: dict[str, str] = {}
        self._ttl = ttl

    def _prune(self) -> None:
        now = datetime.now(UTC)
        for exposure_id in [key for key, value in self._by_id.items() if value.expires_at <= now]:
            exposure = self._by_id.pop(exposure_id)
            if self._active_by_session.get(exposure.session_key) == exposure_id:
                self._active_by_session.pop(exposure.session_key, None)

    def touch(self, exposure: Exposure) -> Exposure:
        now = datetime.now(UTC)
        exposure.updated_at = now
        exposure.expires_at = now + self._ttl
        return exposure

    def open(
        self, *, session_key: str, principal_id: str, campaign_id: str | None, phase: str
    ) -> Exposure:
        self._prune()
        prior = self._active_by_session.get(session_key)
        if prior:
            self._by_id.pop(prior, None)
        exposure = Exposure(
            id=f"exp_{uuid4().hex}",
            session_key=session_key,
            principal_id=principal_id,
            campaign_id=campaign_id,
            phase=phase,
            expires_at=datetime.now(UTC) + self._ttl,
        )
        self._by_id[exposure.id] = exposure
        self._active_by_session[session_key] = exposure.id
        return exposure

    def get(self, exposure_id: str, session_key: str | None = None) -> Exposure:
        self._prune()
        exposure = self._by_id.get(exposure_id)
        if exposure is None:
            raise ExposureError("Unknown or expired exposure_id.")
        if session_key is not None and exposure.session_key != session_key:
            raise ExposureError("exposure_id belongs to another MCP session.")
        return self.touch(exposure)

    def active(self, session_key: str) -> Exposure | None:
        self._prune()
        exposure_id = self._active_by_session.get(session_key)
        exposure = self._by_id.get(exposure_id) if exposure_id else None
        return self.touch(exposure) if exposure else None

    def refresh_phase(self, exposure: Exposure, phase: str) -> bool:
        if exposure.phase == phase:
            return False
        exposure.phase = phase
        exposure.loaded_groups = {
            group_id for group_id in exposure.loaded_groups if GROUP_BY_ID[group_id].phase == phase
        }
        exposure.remaining_calls = {
            group_id: calls
            for group_id, calls in exposure.remaining_calls.items()
            if group_id in exposure.loaded_groups
        }
        self.touch(exposure)
        return True

    def load(self, exposure: Exposure, group_id: str, ttl_calls: int | None = None) -> Exposure:
        group = GROUP_BY_ID.get(group_id)
        if group is None:
            raise ExposureError(f"Unknown tool group: {group_id}")
        if group.phase != exposure.phase:
            raise ExposureError(f"Tool group {group_id!r} requires phase {group.phase!r}.")
        if group.requires_campaign and exposure.campaign_id is None:
            raise ExposureError(f"Tool group {group_id!r} requires a campaign-bound exposure.")
        if group.local_only and exposure.principal_id != "system:local":
            raise ExposureError(f"Tool group {group_id!r} is local-only.")
        if ttl_calls is not None and ttl_calls < 1:
            raise ExposureError("ttl_calls must be at least 1")
        exposure.loaded_groups.add(group_id)
        exposure.remaining_calls[group_id] = ttl_calls
        return self.touch(exposure)

    def unload(self, exposure: Exposure, group_id: str) -> Exposure:
        exposure.loaded_groups.discard(group_id)
        exposure.remaining_calls.pop(group_id, None)
        return self.touch(exposure)

    def visible_tools(self, exposure: Exposure | None) -> set[str]:
        values = set(CORE_TOOLS)
        if exposure:
            for group_id in exposure.loaded_groups:
                values.update(GROUP_BY_ID[group_id].tools)
        return values

    def require_tool(self, exposure: Exposure, tool_id: str) -> None:
        if tool_id in CORE_TOOLS:
            return
        matching = [
            group_id
            for group_id in exposure.loaded_groups
            if tool_id in GROUP_BY_ID[group_id].tools
        ]
        if not matching:
            raise ExposureError(f"Tool {tool_id!r} is not exposed for this session.")
        if all(exposure.remaining_calls.get(group_id) == 0 for group_id in matching):
            raise ExposureError(f"Tool {tool_id!r} has no remaining exposure calls.")

    def consume_tool(self, exposure: Exposure, tool_id: str) -> bool:
        matching = sorted(
            group_id
            for group_id in exposure.loaded_groups
            if tool_id in GROUP_BY_ID[group_id].tools
        )
        if any(exposure.remaining_calls.get(group_id) is None for group_id in matching):
            self.touch(exposure)
            return False
        for group_id in matching:
            remaining = exposure.remaining_calls.get(group_id)
            if remaining is not None:
                if remaining <= 1:
                    self.unload(exposure, group_id)
                else:
                    exposure.remaining_calls[group_id] = remaining - 1
                    self.touch(exposure)
                return True
        return False

    def status(self, exposure: Exposure) -> dict[str, Any]:
        return {
            "exposure_id": exposure.id,
            "principal_id": exposure.principal_id,
            "campaign_id": exposure.campaign_id,
            "phase": exposure.phase,
            "loaded_groups": sorted(exposure.loaded_groups),
            "visible_tools": sorted(self.visible_tools(exposure)),
            "created_at": exposure.created_at.isoformat(),
            "updated_at": exposure.updated_at.isoformat(),
            "expires_at": exposure.expires_at.isoformat(),
        }

    def search(self, query: str, phase: str | None = None) -> list[dict[str, Any]]:
        terms = {term.casefold() for term in query.split() if term.strip()}
        scored = []
        for group in TOOL_GROUPS:
            if phase and group.phase != phase:
                continue
            haystack = " ".join((group.id, group.title, group.description, *group.tools)).casefold()
            score = sum(term in haystack for term in terms)
            if score or not terms:
                scored.append((score, group))
        return [
            {
                "id": group.id,
                "phase": group.phase,
                "title": group.title,
                "description": group.description,
                "risk": group.risk,
                "requires_campaign": group.requires_campaign,
            }
            for _, group in sorted(scored, key=lambda item: (-item[0], item[1].id))
        ]

    def inspect(self, group_id: str) -> dict[str, Any]:
        group = GROUP_BY_ID.get(group_id)
        if group is None:
            raise ExposureError(f"Unknown tool group: {group_id}")
        return {
            "id": group.id,
            "phase": group.phase,
            "title": group.title,
            "description": group.description,
            "risk": group.risk,
            "requires_campaign": group.requires_campaign,
            "tools": sorted(group.tools),
        }
