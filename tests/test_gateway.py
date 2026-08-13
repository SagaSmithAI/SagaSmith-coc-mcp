from __future__ import annotations

from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from mcp.types import CallToolResult, TextContent

from sagasmith_coc_mcp.gateway import COOKIE_NAME, GATEWAY_KEY, GatewayConfig, create_app


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool: str, arguments: dict) -> CallToolResult:
        self.calls.append((tool, arguments))
        return CallToolResult(
            content=[TextContent(type="text", text='{"campaigns": []}')],
            structuredContent={"campaigns": []},
        )


async def test_gateway_binds_principal_server_side_and_sets_sticky_cookie() -> None:
    app = create_app(GatewayConfig())
    fake = FakeClient()

    async def session(_: str | None, campaign_id: str | None):
        assert campaign_id is None
        return "opaque", fake, True

    app[GATEWAY_KEY].session = session  # type: ignore[method-assign]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/coc/mcp/tool",
            json={"tool": "campaign_query", "arguments": {"action": "list"}},
        )
        assert response.status == 200
        assert (await response.json())["result"]["structuredContent"] == {"campaigns": []}
        assert COOKIE_NAME in response.cookies
        assert response.cookies[COOKIE_NAME]["httponly"] is True
        assert fake.calls == [("campaign_query", {"action": "list"})]
    finally:
        await client.close()


async def test_gateway_rejects_browser_principal_and_cross_origin() -> None:
    app = create_app(GatewayConfig(allowed_origins=("https://allowed.test",)))
    app[GATEWAY_KEY].session = SimpleNamespace()  # request is rejected before session lookup
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        forged = await client.post(
            "/api/coc/mcp/tool",
            json={
                "tool": "campaign_query",
                "arguments": {"action": "list", "principal_id": "owner:forged"},
            },
        )
        assert forged.status == 400
        origin = await client.get("/api/health", headers={"Origin": "https://evil.test"})
        assert origin.status == 403
    finally:
        await client.close()


async def test_gateway_health_does_not_claim_authoritative_state() -> None:
    client = TestClient(TestServer(create_app(GatewayConfig())))
    await client.start_server()
    try:
        response = await client.get("/api/health")
        assert response.status == 200
        assert await response.json() == {
            "ok": True,
            "mcp_url": "http://127.0.0.1:8769/mcp",
        }
    finally:
        await client.close()
