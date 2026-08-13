"""Authenticated sticky-session browser gateway for the authoritative CoC MCP."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from sagasmith_core.access import LOCAL_SYSTEM_PRINCIPAL_ID

LOGGER = logging.getLogger(__name__)
JsonHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]
COOKIE_NAME = "sagasmith_coc_session"


class McpToolRejectedError(ValueError):
    """The authoritative MCP rejected a structurally valid request."""


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8768
    mcp_url: str = "http://127.0.0.1:8769/mcp"
    bearer_token: str | None = None
    principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID
    ui_dist: Path | None = None
    session_ttl_seconds: int = 12 * 60 * 60
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:4322",
        "http://localhost:4322",
    )

    @classmethod
    def from_environment(cls) -> "GatewayConfig":
        origins = tuple(
            item.strip()
            for item in os.environ.get(
                "SAGASMITH_COC_GATEWAY_ORIGINS",
                "http://127.0.0.1:4322,http://localhost:4322",
            ).split(",")
            if item.strip()
        )
        ui_value = os.environ.get("SAGASMITH_COC_UI_DIST", "").strip()
        return cls(
            host=os.environ.get("SAGASMITH_COC_GATEWAY_HOST", "127.0.0.1"),
            port=int(os.environ.get("SAGASMITH_COC_GATEWAY_PORT", "8768")),
            mcp_url=os.environ.get("SAGASMITH_COC_MCP_URL", "http://127.0.0.1:8769/mcp"),
            bearer_token=os.environ.get("SAGASMITH_COC_GATEWAY_TOKEN") or None,
            principal_id=os.environ.get(
                "SAGASMITH_COC_GATEWAY_PRINCIPAL_ID", LOCAL_SYSTEM_PRINCIPAL_ID
            ),
            ui_dist=Path(ui_value).expanduser().resolve() if ui_value else None,
            session_ttl_seconds=int(
                os.environ.get("SAGASMITH_COC_GATEWAY_SESSION_TTL", str(12 * 60 * 60))
            ),
            allowed_origins=origins,
        )


@dataclass
class _Request:
    tool: str
    arguments: dict[str, Any]
    future: asyncio.Future[CallToolResult]
    attempts: int = 0


@dataclass
class CocMcpClient:
    """Own one MCP ClientSession for exactly one browser gateway session."""

    url: str
    principal_id: str
    _queue: asyncio.Queue[_Request | None] = field(default_factory=asyncio.Queue, init=False)
    _ready: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _startup_error: BaseException | None = field(default=None, init=False)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="sagasmith-coc-gateway-mcp")
        await asyncio.wait_for(self._ready.wait(), timeout=15)
        if self._startup_error is not None:
            error = self._startup_error
            await self.stop()
            raise RuntimeError(f"CoC MCP connection failed at {self.url}") from error

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> CallToolResult:
        if self._task is None:
            raise RuntimeError("CoC MCP client is not started")
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(_Request(tool, dict(arguments), future))
        return await future

    async def _run(self) -> None:
        pending: _Request | None = None
        first_attempt = True
        while True:
            try:
                async with streamable_http_client(self.url) as streams:
                    read_stream, write_stream, _ = streams
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        if first_attempt:
                            self._ready.set()
                            first_attempt = False
                        while True:
                            request = pending or await self._queue.get()
                            pending = None
                            if request is None:
                                return
                            try:
                                result = await self._call_in_session(
                                    session, request.tool, request.arguments
                                )
                            except McpToolRejectedError as exc:
                                if not request.future.done():
                                    request.future.set_exception(exc)
                            except Exception as exc:
                                if request.attempts == 0:
                                    request.attempts += 1
                                    pending = request
                                    break
                                if not request.future.done():
                                    request.future.set_exception(exc)
                            else:
                                if not request.future.done():
                                    request.future.set_result(result)
            except asyncio.CancelledError:
                if pending is not None and not pending.future.done():
                    pending.future.cancel()
                raise
            except Exception as exc:
                if first_attempt:
                    self._startup_error = exc
                    self._ready.set()
                    return
                if pending is not None and pending.attempts >= 1:
                    if not pending.future.done():
                        pending.future.set_exception(exc)
                    pending = None
                await asyncio.sleep(0.25)

    async def _call_in_session(
        self,
        session: ClientSession,
        tool: str,
        arguments: dict[str, Any],
    ) -> CallToolResult:
        tool_defs = {item.name: item for item in (await session.list_tools()).tools}
        listed = set(tool_defs)
        if tool not in listed:
            campaign_id = str(arguments.get("campaign_id") or "").strip() or None
            opened = await session.call_tool(
                "exposure",
                {
                    "action": "open",
                    "campaign_id": campaign_id,
                    "principal_id": self.principal_id,
                },
            )
            if opened.isError and "already bound" not in self._error_text(opened):
                self._raise_tool_error(opened)
            loaded = await session.call_tool(
                "exposure",
                {
                    "action": "set",
                    "campaign_id": campaign_id,
                    "add_tool_ids": [tool],
                    "principal_id": self.principal_id,
                },
            )
            self._raise_tool_error(loaded)
            tool_defs = {
                item.name: item for item in (await self._list_tools(session)).tools
            }
            listed = set(tool_defs)
            if tool not in listed:
                raise RuntimeError(
                    f"CoC MCP did not expose {tool!r} after tools/list_changed"
                )
        principal_key = self._principal_key(tool_defs[tool])
        if principal_key:
            arguments = {**arguments, principal_key: self.principal_id}
        result = await session.call_tool(tool, arguments)
        self._raise_tool_error(result)
        return result

    @staticmethod
    async def _list_tools(session: ClientSession) -> Any:
        """Refresh only after the preceding tools/call response handoff unwinds."""

        async def refresh() -> Any:
            await asyncio.sleep(0)
            return await session.list_tools()

        return await asyncio.shield(
            asyncio.create_task(refresh(), name="sagasmith-coc-gateway-tools-refresh")
        )

    @staticmethod
    def _principal_key(tool_def: Any) -> str | None:
        schema = getattr(tool_def, "inputSchema", None)
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for name in ("auth_principal_id", "by_principal_id", "principal_id"):
            if name in properties:
                return name
        return None

    @staticmethod
    def _error_text(result: CallToolResult) -> str:
        return next(
            (
                str(getattr(item, "text", "")).strip()
                for item in result.content
                if str(getattr(item, "text", "")).strip()
            ),
            "CoC MCP rejected the request",
        )

    @classmethod
    def _raise_tool_error(cls, result: CallToolResult) -> None:
        if result.isError:
            raise McpToolRejectedError(cls._error_text(result)[:2000])


@dataclass
class _BrowserSession:
    client: CocMcpClient
    touched_at: float
    campaign_id: str | None = None


class CocGateway:
    def __init__(self, config: GatewayConfig):
        self.config = config
        self.sessions: dict[str, _BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def session(
        self,
        token: str | None,
        campaign_id: str | None,
    ) -> tuple[str, CocMcpClient, bool]:
        async with self._lock:
            now = time.monotonic()
            expired = [
                key
                for key, value in self.sessions.items()
                if now - value.touched_at > self.config.session_ttl_seconds
            ]
            for key in expired:
                stale = self.sessions.pop(key)
                await stale.client.stop()
            if token and token in self.sessions:
                current = self.sessions[token]
                if (
                    current.campaign_id is None
                    or campaign_id is None
                    or current.campaign_id == campaign_id
                ):
                    current.touched_at = now
                    current.campaign_id = current.campaign_id or campaign_id
                    return token, current.client, False
                await current.client.stop()
                client = CocMcpClient(self.config.mcp_url, self.config.principal_id)
                await client.start()
                self.sessions[token] = _BrowserSession(client, now, campaign_id)
                return token, client, False
            token = secrets.token_urlsafe(32)
            client = CocMcpClient(self.config.mcp_url, self.config.principal_id)
            await client.start()
            self.sessions[token] = _BrowserSession(client, now, campaign_id)
            return token, client, True

    async def close(self) -> None:
        for current in list(self.sessions.values()):
            await current.client.stop()
        self.sessions.clear()


GATEWAY_KEY = web.AppKey("gateway", CocGateway)


def _json_result(result: CallToolResult) -> dict[str, Any]:
    structured = result.structuredContent
    if structured is not None:
        return {"ok": True, "result": {"structuredContent": structured}}
    texts = [str(getattr(item, "text", "")) for item in result.content]
    if len(texts) == 1:
        try:
            return {"ok": True, "result": json.loads(texts[0])}
        except json.JSONDecodeError:
            pass
    return {"ok": True, "result": {"content": texts}}


def create_app(config: GatewayConfig | None = None) -> web.Application:
    gateway_config = config or GatewayConfig.from_environment()
    gateway = CocGateway(gateway_config)

    @web.middleware
    async def boundary(request: web.Request, handler: JsonHandler) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if origin and origin not in gateway_config.allowed_origins:
            raise web.HTTPForbidden(text="origin is not allowed")
        if gateway_config.bearer_token:
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(supplied, gateway_config.bearer_token):
                raise web.HTTPUnauthorized(text="invalid gateway token")
        elif request.remote not in {"127.0.0.1", "::1", None}:
            raise web.HTTPForbidden(text="a bearer token is required for non-loopback access")
        if request.method == "OPTIONS":
            response: web.StreamResponse = web.Response(status=204)
        else:
            try:
                response = await handler(request)
            except web.HTTPException:
                raise
            except PermissionError as exc:
                response = web.json_response({"error": str(exc)}, status=403)
            except McpToolRejectedError as exc:
                response = web.json_response({"error": str(exc)}, status=400)
            except (KeyError, TypeError, ValueError) as exc:
                response = web.json_response({"error": str(exc)}, status=400)
            except Exception:
                LOGGER.exception("unhandled CoC gateway request failure")
                response = web.json_response({"error": "internal gateway error"}, status=500)
        if origin and origin in gateway_config.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Vary"] = "Origin"
        return response

    app = web.Application(middlewares=[boundary], client_max_size=64 * 1024 * 1024)
    app[GATEWAY_KEY] = gateway

    async def cleanup(_: web.Application):
        yield
        await gateway.close()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "mcp_url": gateway_config.mcp_url})

    async def options(_: web.Request) -> web.Response:
        return web.Response(status=204)

    async def tool(request: web.Request) -> web.Response:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        tool_id = str(body.get("tool") or "").strip()
        arguments = body.get("arguments") or {}
        if not tool_id or not isinstance(arguments, dict):
            raise ValueError("tool and object arguments are required")
        forbidden = {"principal_id", "by_principal_id", "auth_principal_id"} & arguments.keys()
        if forbidden:
            raise ValueError("browser requests cannot choose an authoritative principal")
        campaign_id = str(arguments.get("campaign_id") or "").strip() or None
        token, client, created = await gateway.session(
            request.cookies.get(COOKIE_NAME), campaign_id
        )
        result = await client.call_tool(tool_id, arguments)
        response = web.json_response(_json_result(result))
        if created:
            response.set_cookie(
                COOKIE_NAME,
                token,
                httponly=True,
                samesite="Strict",
                secure=False,
                max_age=gateway_config.session_ttl_seconds,
            )
        return response

    app.cleanup_ctx.append(cleanup)
    app.router.add_route("OPTIONS", "/{tail:.*}", options)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/coc/mcp/tool", tool)

    if gateway_config.ui_dist is not None:
        ui_root = gateway_config.ui_dist.resolve()
        if not (ui_root / "index.html").is_file():
            raise ValueError(f"CoC UI dist is missing index.html: {ui_root}")

        async def ui_file(request: web.Request) -> web.FileResponse:
            relative = request.match_info.get("path", "").strip("/")
            candidates = (
                [ui_root / relative, ui_root / relative / "index.html"]
                if relative
                else [ui_root / "index.html"]
            )
            target = next(
                (
                    candidate.resolve()
                    for candidate in candidates
                    if candidate.is_file() and candidate.resolve().is_relative_to(ui_root)
                ),
                None,
            )
            if target is None:
                raise web.HTTPNotFound(text="CoC UI route not found")
            return web.FileResponse(target)

        app.router.add_get("/{path:.*}", ui_file)
    return app


def main() -> None:
    config = GatewayConfig.from_environment()
    web.run_app(create_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
