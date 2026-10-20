# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""ADR-0028: a proxied MCP server's resources and prompts, discovered and forwarded.

The end-to-end tests run against the **official MCP SDK 2.3 server**, in process — the
reference implementation the `dev` extra carries for exactly this (roadmap, trap 1: test
against an implementation nobody here wrote). Discovery, the manifest, the pod and the wire
are all the real ones; only the network is an ASGI transport.
"""

import httpx
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from device_mcp_gateway.core.translator import McpManifest, manifest_to_dict
from device_mcp_gateway.pods.mcp_proxy_pod import McpProxyPod
from device_mcp_gateway.security.fingerprint import QUARANTINED_METHODS
from device_mcp_gateway.upstream.mcp_client import StreamableHttpClient
from device_mcp_gateway.upstream.mcp_discovery import (
    build_proxy_manifest,
    build_proxy_manifest_from,
    canonical_snapshot_hash,
    canonical_tools_hash,
    snapshot_upstream,
)
from device_mcp_gateway.worker.runner import _dict_to_manifest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"  # the SDK server's session manager is anyio/asyncio-based


def _sdk_server(*, with_resources=True, with_prompts=True) -> MCPServer:
    srv = MCPServer("reference-upstream-sdk2", version="2.3.0")

    @srv.tool(name="add")
    def add(a: int, b: int) -> int:
        return a + b

    if with_resources:

        @srv.resource("ref://status", name="status", description="Device status", mime_type="text/plain")
        def status() -> str:
            return "all good"

    if with_prompts:

        @srv.prompt(name="Summarise Reading", description="Summarise a reading")
        def summarise(unit: str) -> str:
            return f"Summarise the reading in {unit}."

    return srv


class _Upstream:
    """An in-process SDK server and an httpx client wired to it."""

    def __init__(self, srv: MCPServer):
        self.app = srv.streamable_http_app(
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
        )

    async def __aenter__(self):
        self._life = self.app.router.lifespan_context(self.app)
        await self._life.__aenter__()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://up")
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()
        await self._life.__aexit__(*exc)

    def client(self) -> StreamableHttpClient:
        return StreamableHttpClient(url="http://up/mcp", get_client=lambda: self.http)

    async def pod(self) -> McpProxyPod:
        c = self.client()
        snapshot = await snapshot_upstream(c)
        await c.close_session()
        pod = McpProxyPod(hostname="ref", manifest=build_proxy_manifest_from("ref", snapshot), base_url="http://up/mcp")
        pod._http = self.http  # the pod's own upstream client now reaches the SDK server
        return pod


def _rpc(i, method, params=None):
    return {"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params is not None else {})}


# --- discovery against the SDK server ---------------------------------------------------


async def test_discovery_collects_advertised_resources_and_prompts():
    async with _Upstream(_sdk_server()) as up:
        snap = await snapshot_upstream(up.client())
    assert snap["discovered"] == {"resources": True, "prompts": True}
    assert [r["uri"] for r in snap["resources"]] == ["ref://status"]
    assert [p["name"] for p in snap["prompts"]] == ["Summarise Reading"]


async def test_a_capability_the_server_does_not_advertise_is_discovered_as_none():
    async with _Upstream(_sdk_server(with_resources=False, with_prompts=False)) as up:
        snap = await snapshot_upstream(up.client())
    # The SDK advertises a capability only when something is registered for it; not
    # advertised means the server has none, which is a discovered fact, not a failure.
    assert snap["resources"] == [] and snap["prompts"] == []
    assert snap["discovered"] == {"resources": True, "prompts": True}


async def test_a_failing_list_costs_only_that_kind():
    class Flaky(StreamableHttpClient):
        async def list_prompts(self):
            raise RuntimeError("upstream broke prompts/list")

    async with _Upstream(_sdk_server()) as up:
        snap = await snapshot_upstream(Flaky(url="http://up/mcp", get_client=lambda: up.http))
    assert len(snap["tools"]) == 1 and len(snap["resources"]) == 1
    assert snap["prompts"] == [] and snap["discovered"] == {"resources": True, "prompts": False}


# --- the proxy pod, end to end ----------------------------------------------------------


async def test_lists_are_served_from_the_manifest_with_upstream_prompt_arguments():
    async with _Upstream(_sdk_server()) as up:
        pod = await up.pod()
        resources = (await pod.call_tool(_rpc(1, "resources/list")))["result"]["resources"]
        prompts = (await pod.call_tool(_rpc(2, "prompts/list")))["result"]["prompts"]
    assert resources == [
        {"uri": "ref://status", "name": "status", "description": "Device status", "mimeType": "text/plain"}
    ]
    # The listed name is sanitised (lowercased, like a tool name); the argument's `required` is the upstream's own.
    assert prompts == [
        {
            "name": "summarise_reading",
            "description": "Summarise a reading",
            "arguments": [{"name": "unit", "required": True}],
        }
    ]


async def test_resource_read_is_forwarded_and_returned_untouched():
    async with _Upstream(_sdk_server()) as up:
        pod = await up.pod()
        resp = await pod.call_tool(_rpc(3, "resources/read", {"uri": "ref://status"}))
    assert resp["result"] == {"contents": [{"uri": "ref://status", "mimeType": "text/plain", "text": "all good"}]}


async def test_prompt_get_goes_upstream_under_the_upstreams_own_name():
    async with _Upstream(_sdk_server()) as up:
        pod = await up.pod()
        resp = await pod.call_tool(_rpc(4, "prompts/get", {"name": "summarise_reading", "arguments": {"unit": "°C"}}))
    assert resp["result"]["messages"][0]["content"]["text"] == "Summarise the reading in °C."


async def test_only_what_discovery_listed_can_be_fetched():
    async with _Upstream(_sdk_server()) as up:
        pod = await up.pod()
        unlisted = await pod.call_tool(_rpc(5, "resources/read", {"uri": "file:///etc/passwd"}))
        unknown = await pod.call_tool(_rpc(6, "prompts/get", {"name": "Summarise Reading"}))  # the wire name
    assert unlisted["error"]["code"] == -32602
    assert unknown["error"]["code"] == -32602


async def test_a_missing_required_argument_is_refused_before_the_round_trip():
    async with _Upstream(_sdk_server()) as up:
        pod = await up.pod()
        resp = await pod.call_tool(_rpc(7, "prompts/get", {"name": "summarise_reading", "arguments": {}}))
    assert resp["error"]["code"] == -32602 and "unit" in resp["error"]["message"]


# --- manifest building: the protections tools already had --------------------------------


def test_resource_uris_are_verbatim_or_dropped():
    m = build_proxy_manifest(
        "h",
        [],
        resources=[
            {"uri": "ref://ok", "name": "ok"},
            {"uri": "ref://bad\x07bell", "name": "bell"},  # sanitising would change it: dropped
            {"uri": "ref://ok", "name": "dup"},  # duplicate URI: dropped
            {"name": "no-uri"},
        ],
        discovered={"resources": True, "prompts": True},
    )
    assert [r.uri for r in m.resources] == ["ref://ok"]


def test_llm_facing_text_is_sanitised_like_a_tool_description():
    m = build_proxy_manifest(
        "h",
        [],
        prompts=[{"name": "p", "description": "Hello\u202eDLROW"}],
        discovered={"resources": True, "prompts": True},
    )
    assert "\u202e" not in m.prompts[0].description


def test_over_the_cap_is_not_discovered_and_does_not_cost_the_tools():
    tools = [{"name": "t", "inputSchema": {"type": "object"}}]
    m = build_proxy_manifest(
        "h",
        tools,
        resources=[{"uri": f"ref://{i}"} for i in range(5)],
        discovered={"resources": True, "prompts": True},
        max_tools=3,
    )
    assert len(m.tools) == 1 and m.resources == []
    assert m.metadata["discovered"] == {"resources": False, "prompts": True}


def test_a_snapshot_from_before_adr_0028_reports_nothing_looked_for():
    m = build_proxy_manifest_from("h", {"upstream_kind": "mcp", "tools": []})
    assert m.metadata["discovered"] == {"resources": False, "prompts": False}


def test_prompt_fields_survive_the_workers_redis_round_trip():
    """The cache-hit spawn path rebuilds the manifest from Redis; losing upstream_name there
    would make every proxied prompts/get forward the sanitised name and reach nothing."""
    m = build_proxy_manifest(
        "h",
        [],
        prompts=[{"name": "Summarise Reading", "arguments": [{"name": "unit", "required": True}]}],
        discovered={"resources": True, "prompts": True},
    )
    [p] = _dict_to_manifest(manifest_to_dict(m)).prompts
    assert p.upstream_name == "Summarise Reading" and p.required_arguments == ["unit"]


# --- change detection: upgrade-stable --------------------------------------------------


def test_snapshot_hash_equals_the_tools_hash_when_there_is_nothing_else():
    """Upgrading must not make every MCP device in a fleet look changed on its first poll."""
    tools = [{"name": "add", "inputSchema": {"type": "object"}}]
    assert canonical_snapshot_hash({"tools": tools, "resources": [], "prompts": []}) == canonical_tools_hash(tools)
    assert canonical_snapshot_hash({"tools": tools}) == canonical_tools_hash(tools)


def test_snapshot_hash_moves_with_a_resource_and_ignores_ordering():
    tools = [{"name": "add"}]
    a = {"tools": tools, "resources": [{"uri": "r://1"}, {"uri": "r://2"}], "prompts": []}
    b = {"tools": tools, "resources": [{"uri": "r://2"}, {"uri": "r://1"}], "prompts": []}
    assert canonical_snapshot_hash(a) == canonical_snapshot_hash(b)
    assert canonical_snapshot_hash(a) != canonical_tools_hash(tools)


def test_prompts_get_is_quarantined_with_the_other_use_paths():
    assert {"tools/call", "resources/read", "prompts/get"} <= QUARANTINED_METHODS


def test_manifest_round_trip_keeps_discovered_metadata():
    m = McpManifest(server_name="s", server_version="1", hostname="h", metadata={"discovered": {"resources": True}})
    assert _dict_to_manifest(manifest_to_dict(m)).metadata["discovered"] == {"resources": True}
