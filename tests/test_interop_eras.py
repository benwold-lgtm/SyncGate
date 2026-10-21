# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""The era interop matrix: the official MCP SDK against the gateway, in both directions.

The roadmap's verification section asks for the SDK's server **and** client at both eras, in
all four client/server combinations, because the only bug this codebase shipped in MCP
passthrough survived a full suite in which every stub agreed with the implementation
(trap 1). So nothing on the far side of the wire here is ours.

**Inbound** — the gateway as a server. A real gateway process (``gateway_url``) with an
OpenAPI device behind it, driven by ``mcp.Client`` in each of its three modes:

- ``legacy`` — the ``initialize`` handshake, which is what the gateway has always served;
- ``auto`` — probes ``server/discover`` at ``2026-07-28`` and falls back to the handshake on
  anything that is not positive modern evidence;
- ``2026-07-28`` — pinned modern, with no fallback at all.

**Outbound** — the gateway as a client. ``StreamableHttpClient`` against three upstreams: the
SDK server (which serves both eras), and that same server restricted to one era, which is what
a server that has not upgraded, and one that has dropped the handshake, look like from here.

A cell the gateway cannot pass yet is a **strict** ``xfail`` naming the PR that closes it, so
the PR that makes it pass fails the suite until the marker comes off — the matrix cannot
quietly claim less than the code does, or more.
"""

from __future__ import annotations

import json

import httpx
import pytest

pytest.importorskip("mcp", reason="the reference SDK comes with the dev extra")

import httpx2  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

from device_mcp_gateway.core.protocol import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS  # noqa: E402
from device_mcp_gateway.upstream.mcp_client import McpUpstreamError, StreamableHttpClient  # noqa: E402

MODERN = MODERN_PROTOCOL_VERSIONS[0]
HOST = "interop-sensor.local"

NOT_YET_OUTBOUND = pytest.mark.xfail(
    strict=True, reason="the gateway detects an upstream's era from 0.4.0 Workstream B PR 7"
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- inbound: the SDK client against a real gateway ------------------------------------------


@pytest.fixture(scope="module")
def device(client, mock_target_url):
    """One OpenAPI device with an active pod, for the whole module (see test_streamable_http)."""
    import time

    resp = client.post("/v1/devices", json={"hostname": HOST, "base_url": mock_target_url, "auth_type": "none"})
    assert resp.status_code == 200, resp.text
    deadline = time.time() + 10
    while time.time() < deadline:
        if client.get(f"/v1/devices/{HOST}").json().get("pod_active"):
            break
        time.sleep(0.2)
    else:
        raise AssertionError(f"pod for {HOST} never became active")
    yield f"/v1/devices/{HOST}/mcp"
    client.delete(f"/v1/devices/{HOST}")


def _sdk_client(gateway_url: str, path: str, mode: str) -> tuple[httpx2.AsyncClient, Client]:
    http = httpx2.AsyncClient(timeout=15)
    return http, Client(streamable_http_client(f"{gateway_url}{path}", http_client=http), mode=mode)


async def _exercise(c: Client) -> dict:
    """Everything a device offers, through the SDK's own typed calls.

    The mock device's spec has no description, so the gateway generates no prompt for it:
    ``prompts/list`` is exercised for its answer (an empty list), not skipped.
    """
    tools = (await c.list_tools()).tools
    called = await c.call_tool("get_device_status", {})
    resources = (await c.list_resources()).resources
    prompts = (await c.list_prompts()).prompts
    return {
        "tools": sorted(t.name for t in tools),
        "call_text": called.content[0].text,
        "resources": sorted(str(r.uri) for r in resources),
        "prompts": [p.name for p in prompts],
    }


@pytest.mark.anyio
async def test_a_legacy_client_is_served_on_the_handshake(gateway_url, device):
    http, client = _sdk_client(gateway_url, device, "legacy")
    async with http, client as c:
        assert c.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
        seen = await _exercise(c)
    assert seen["tools"] == ["control_fan", "get_device_status"]
    assert json.loads(seen["call_text"])["body"]["status"] == "online"  # the mock device's own answer
    assert seen["resources"] and seen["prompts"] == []


@pytest.mark.anyio
async def test_an_auto_client_reaches_every_feature_whichever_era_it_lands_in(gateway_url, device):
    """The property that must hold before, during and after the era work: an SDK-default
    client connects and works. Before PR 4 it gets there by falling back to the handshake."""
    http, client = _sdk_client(gateway_url, device, "auto")
    async with http, client as c:
        seen = await _exercise(c)
    assert json.loads(seen["call_text"])["body"]["status"] == "online" and seen["resources"]


@pytest.mark.anyio
async def test_an_auto_client_lands_in_the_stateless_era(gateway_url, device):
    http, client = _sdk_client(gateway_url, device, "auto")
    async with http, client as c:
        assert c.protocol_version == MODERN


@pytest.mark.anyio
async def test_a_pinned_modern_client_is_served_statelessly(gateway_url, device):
    http, client = _sdk_client(gateway_url, device, MODERN)
    async with http, client as c:
        seen = await _exercise(c)
    assert json.loads(seen["call_text"])["body"]["status"] == "online"


# --- outbound: the gateway's client against the SDK server -----------------------------------


def _sdk_server() -> MCPServer:
    srv = MCPServer("reference-upstream-sdk2", version="2.3.0")

    @srv.tool(name="add")
    def add(a: int, b: int) -> int:
        return a + b

    return srv


class _OneEra:
    """The SDK server as a server that speaks only one era would answer.

    The SDK serves both, so a single-era peer is the SDK server with the other era refused the
    way such a peer actually refuses it:

    - **handshake only** (a server that has not upgraded): a modern request carries no session
      and is not ``initialize``, which is a ``400`` — what an SDK 1.x server answers.
    - **modern only** (a server that has dropped the handshake): ``initialize`` is answered
      ``-32022`` naming the modern versions it serves, the SDK's own modern-locked answer.
    """

    def __init__(self, app, era: str):
        self.app, self.era = app, era

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            headers = dict(scope["headers"])
            version = headers.get(b"mcp-protocol-version", b"").decode()
            body = b""
            more = True
            while more:
                msg = await receive()
                body += msg.get("body", b"")
                more = msg.get("more_body", False)
            payload = json.loads(body or b"{}")
            refusal = self._refusal(version, payload)
            if refusal is not None:
                status, out = refusal
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": json.dumps(out).encode()})
                return
            replayed = False

            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)
        return await self.app(scope, receive, send)

    def _refusal(self, version: str, payload: dict):
        is_modern = bool(version) and version not in HANDSHAKE_PROTOCOL_VERSIONS
        if self.era == "handshake" and is_modern:
            return 400, {
                "jsonrpc": "2.0",
                "id": "server-error",
                "error": {"code": -32600, "message": "Bad Request: Missing session ID"},
            }
        if self.era == "modern" and payload.get("method") == "initialize":
            return 400, {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {
                    "code": -32022,
                    "message": "Unsupported protocol version",
                    "data": {
                        "supported": list(MODERN_PROTOCOL_VERSIONS),
                        "requested": (payload.get("params") or {}).get("protocolVersion"),
                    },
                },
            }
        return None


class _Upstream:
    def __init__(self, era: str | None):
        srv = _sdk_server()
        self.inner = srv.streamable_http_app(
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
        )
        self.app = _OneEra(self.inner, era) if era else self.inner

    async def __aenter__(self):
        self._life = self.inner.router.lifespan_context(self.inner)
        await self._life.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://up")
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()
        await self._life.__aexit__(*exc)

    def client(self) -> StreamableHttpClient:
        return StreamableHttpClient(url="http://up/mcp", get_client=lambda: self.http)


async def _list_and_call(up: _Upstream) -> tuple[list[str], dict]:
    c = up.client()
    tools = await c.list_tools()
    result = await c.call_tool("add", {"a": 40, "b": 2})
    return [t["name"] for t in tools], result.message or {}


@pytest.mark.anyio
async def test_the_gateway_reaches_a_dual_era_upstream():
    async with _Upstream(None) as up:
        names, result = await _list_and_call(up)
    assert names == ["add"] and "42" in json.dumps(result)


@pytest.mark.anyio
async def test_the_gateway_reaches_a_handshake_only_upstream():
    async with _Upstream("handshake") as up:
        names, result = await _list_and_call(up)
    assert names == ["add"] and "42" in json.dumps(result)


@pytest.mark.anyio
@NOT_YET_OUTBOUND
async def test_the_gateway_reaches_a_modern_only_upstream():
    async with _Upstream("modern") as up:
        names, result = await _list_and_call(up)
    assert names == ["add"] and "42" in json.dumps(result)


@pytest.mark.anyio
async def test_a_modern_only_upstream_fails_cleanly_until_then():
    """Fails with a protocol error naming the cause, not a hang or a generic 500."""
    async with _Upstream("modern") as up:
        try:
            await up.client().list_tools()
        except McpUpstreamError as exc:
            # The upstream's own reason, not just "HTTP 400": an operator has to be able to act.
            assert "-32022: Unsupported protocol version" in str(exc), str(exc)
            return
    pytest.xfail("reached: PR 7 has landed, delete this test")


# --- the error report a refusal produces ------------------------------------------------------


def _resp(status: int, content: bytes) -> httpx.Response:
    return httpx.Response(status, content=content, request=httpx.Request("POST", "http://up/mcp"))


def test_a_refusal_carries_the_upstreams_code_and_reason():
    from device_mcp_gateway.upstream.mcp_client import _error_detail

    body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32022, "message": "Unsupported protocol version"}})
    assert _error_detail(_resp(400, body.encode())) == "-32022: Unsupported protocol version"


@pytest.mark.parametrize("junk", [b"", b"not json", b"[1, 2]", b'{"error": "flat"}', b'{"result": {}}'])
def test_a_refusal_with_no_json_rpc_error_says_nothing_rather_than_guessing(junk):
    from device_mcp_gateway.upstream.mcp_client import _error_detail

    assert _error_detail(_resp(400, junk)) is None


def test_the_upstreams_text_cannot_forge_a_log_line_or_flood_one():
    """Untrusted text into an operator-facing message: one line, bounded."""
    from device_mcp_gateway.upstream.mcp_client import _ERROR_DETAIL_MAX, _error_detail

    evil = "ok\nFAKE LOG LINE: admin approved\r\n" + "x" * 5000
    detail = _error_detail(_resp(400, json.dumps({"error": {"code": 1, "message": evil}}).encode()))
    assert "\n" not in detail and "\r" not in detail
    assert len(detail) <= _ERROR_DETAIL_MAX
