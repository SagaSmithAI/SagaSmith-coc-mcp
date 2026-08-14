"""Ephemeral, session-scoped native MCP tool exposure."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable
from uuid import uuid4

from sagasmith_core.access import LOCAL_SYSTEM_PRINCIPAL_ID
from sagasmith_core.clock import operational_utcnow

from .tool_profiles import CORE_TOOLS, policy_for_tool, tools_for_phase


class ExposureError(ValueError):
    """Raised when a session attempts to expose or call an unavailable tool."""


@dataclass
class Exposure:
    id: str
    session_key: str
    principal_id: str
    campaign_id: str | None
    phase: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    revision: int = 0
    loaded_tools: set[str] = field(default_factory=set)
    authorization_fingerprint: str = ""


class ExposureRegistry:
    """Own mutable native tool lists without becoming campaign authority."""

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(hours=12),
        clock: Callable[[], datetime] = operational_utcnow,
    ) -> None:
        self._by_id: dict[str, Exposure] = {}
        self._active_by_session: dict[str, str] = {}
        self._ttl = ttl
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("exposure clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _prune(self) -> None:
        now = self._now()
        for exposure_id in [key for key, item in self._by_id.items() if item.expires_at <= now]:
            exposure = self._by_id.pop(exposure_id)
            if self._active_by_session.get(exposure.session_key) == exposure_id:
                self._active_by_session.pop(exposure.session_key, None)

    def touch(self, exposure: Exposure) -> Exposure:
        now = self._now()
        exposure.updated_at = now
        exposure.expires_at = now + self._ttl
        return exposure

    def open(
        self,
        *,
        session_key: str,
        principal_id: str,
        campaign_id: str | None,
        phase: str,
        authorization_fingerprint: str = "",
    ) -> Exposure:
        self._prune()
        prior_id = self._active_by_session.get(session_key)
        if prior_id:
            self._by_id.pop(prior_id, None)
        now = self._now()
        exposure = Exposure(
            id=f"exp_{uuid4().hex}",
            session_key=session_key,
            principal_id=principal_id,
            campaign_id=campaign_id,
            phase=phase,
            created_at=now,
            updated_at=now,
            expires_at=now + self._ttl,
            authorization_fingerprint=authorization_fingerprint,
        )
        self._by_id[exposure.id] = exposure
        self._active_by_session[session_key] = exposure.id
        return exposure

    def active(self, session_key: str) -> Exposure | None:
        self._prune()
        exposure_id = self._active_by_session.get(session_key)
        exposure = self._by_id.get(exposure_id) if exposure_id else None
        return self.touch(exposure) if exposure else None

    def active_items(self, campaign_id: str | None = None) -> tuple[tuple[str, Exposure], ...]:
        self._prune()
        return tuple(
            (session_key, exposure)
            for session_key, exposure_id in self._active_by_session.items()
            if (exposure := self._by_id.get(exposure_id)) is not None
            and (campaign_id is None or exposure.campaign_id == campaign_id)
        )

    def refresh_phase(
        self,
        exposure: Exposure,
        phase: str,
        *,
        allowed_tools: Iterable[str] | None = None,
    ) -> bool:
        allowed = (
            tools_for_phase(phase)
            if allowed_tools is None
            else set(allowed_tools) | set(CORE_TOOLS)
        )
        retained = exposure.loaded_tools & allowed
        changed = exposure.phase != phase or retained != exposure.loaded_tools
        if changed:
            exposure.phase = phase
            exposure.loaded_tools = retained
            exposure.revision += 1
            self.touch(exposure)
        return changed

    def refresh_authorization(self, exposure: Exposure, fingerprint: str) -> bool:
        if not exposure.authorization_fingerprint:
            exposure.authorization_fingerprint = fingerprint
            self.touch(exposure)
            return False
        if exposure.authorization_fingerprint == fingerprint:
            return False
        exposure.authorization_fingerprint = fingerprint
        exposure.revision += 1
        self.touch(exposure)
        return True

    def set_tools(
        self,
        exposure: Exposure,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
    ) -> bool:
        add_set = {str(item).strip() for item in add if str(item).strip()}
        remove_set = {str(item).strip() for item in remove if str(item).strip()}
        if add_set & remove_set:
            raise ExposureError("the same tool cannot be added and removed in one request")
        for tool_id in sorted(add_set):
            policy = policy_for_tool(tool_id)
            if policy is None:
                raise ExposureError(f"Unknown loadable tool: {tool_id}")
            if exposure.phase not in policy.phases:
                raise ExposureError(f"Tool {tool_id!r} is unavailable during {exposure.phase!r}.")
            if policy.requires_campaign and exposure.campaign_id is None:
                raise ExposureError(f"Tool {tool_id!r} requires a campaign-bound exposure.")
            if policy.local_only and exposure.principal_id != LOCAL_SYSTEM_PRINCIPAL_ID:
                raise ExposureError(
                    f"Tool {tool_id!r} is restricted to {LOCAL_SYSTEM_PRINCIPAL_ID}."
                )
        updated = (exposure.loaded_tools | add_set) - remove_set
        changed = updated != exposure.loaded_tools
        if changed:
            exposure.loaded_tools = updated
            exposure.revision += 1
            self.touch(exposure)
        return changed

    def visible_tools(self, exposure: Exposure | None) -> set[str]:
        return set(CORE_TOOLS) if exposure is None else set(CORE_TOOLS) | exposure.loaded_tools

    def require_tool(self, exposure: Exposure, tool_id: str) -> None:
        if tool_id not in CORE_TOOLS and tool_id not in exposure.loaded_tools:
            raise ExposureError(
                f"Tool {tool_id!r} is not exposed for this session. "
                "Use exposure(action='search') and exposure(action='set') first."
            )
        self.touch(exposure)

    def status(self, exposure: Exposure) -> dict[str, Any]:
        return {
            "exposure_id": exposure.id,
            "revision": exposure.revision,
            "campaign_id": exposure.campaign_id,
            "principal_id": exposure.principal_id,
            "phase": exposure.phase,
            "loaded_tools": sorted(exposure.loaded_tools),
            "visible_tools": sorted(self.visible_tools(exposure)),
            "authorization_fingerprint": exposure.authorization_fingerprint,
            "created_at": exposure.created_at.isoformat(),
            "updated_at": exposure.updated_at.isoformat(),
            "expires_at": exposure.expires_at.isoformat(),
        }
