# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from device_mcp_gateway.core.translator import McpManifest, McpResource, McpTool
from device_mcp_gateway.pods.device_pod import DevicePod
from device_mcp_gateway.pods.sse_server import SseTransport


@pytest.fixture
def simple_manifest() -> McpManifest:
    return McpManifest(
        server_name="mcp-test",
        server_version="1.0.0",
        hostname="test-device",
        tools=[
            McpTool(
                name="ping",
                description="Ping test",
                schema={"type": "object", "properties": {}},
                method="GET",
                path="/ping",
            )
        ],
    )


@pytest.mark.anyio
async def test_device_pod_start_and_stop(monkeypatch, simple_manifest):
    started = {"count": 0}

    async def fake_run(self, mount_path=None):
        started["count"] += 1
        try:
            while True:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(SseTransport, "start", fake_run)

    pod = DevicePod(
        hostname="test-device",
        manifest=simple_manifest,
        transport="sse",
        base_url="http://localhost:1234",
    )

    await pod.start()
    assert pod._running is True
    assert pod._task is not None
    await asyncio.sleep(0)
    assert started["count"] == 1

    pod.stop()
    await asyncio.sleep(0)
    assert pod._running is False
    assert pod._task.done() is True
    assert pod._task.cancelled() is True


@pytest.mark.anyio
async def test_device_pod_unsupported_transport(simple_manifest):
    pod = DevicePod(
        hostname="test-device",
        manifest=simple_manifest,
        transport="unsupported",
        base_url="http://localhost:1234",
    )

    with pytest.raises(ValueError, match="Unsupported transport"):
        await pod.start()


@pytest.mark.anyio
async def test_path_param_substituted_in_url():
    """Path parameters must be interpolated into the URL, not sent as query params."""
    manifest = McpManifest(
        server_name="mcp-test",
        server_version="1.0.0",
        hostname="test-device",
        tools=[
            McpTool(
                name="get_item",
                description="Get item by ID",
                schema={"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]},
                method="GET",
                path="/items/{item_id}",
                param_locations={"item_id": "path"},
            )
        ],
    )

    captured: dict = {}

    async def fake_request(self, method, url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": 42}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("httpx.AsyncClient.request", fake_request):
        pod = DevicePod(
            hostname="test-device",
            manifest=manifest,
            transport="sse",
            base_url="http://device.local",
        )
        # Directly invoke the tool's dispatch closure
        tool_fn = pod._tool_dispatch["get_item"]
        await tool_fn(item_id=42)

    assert captured["url"] == "http://device.local/items/42", f"Unexpected URL: {captured['url']}"
    assert not captured.get("params"), "Path param should not appear as query param"


@pytest.mark.anyio
async def test_query_params_not_in_path():
    """Query parameters must be sent as ?key=val, not interpolated into the URL."""
    manifest = McpManifest(
        server_name="mcp-test",
        server_version="1.0.0",
        hostname="test-device",
        tools=[
            McpTool(
                name="list_items",
                description="List items",
                schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
                method="GET",
                path="/items",
                param_locations={"limit": "query"},
            )
        ],
    )

    captured: dict = {}

    async def fake_request(self, method, url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = []
        resp.raise_for_status = MagicMock()
        return resp

    with patch("httpx.AsyncClient.request", fake_request):
        pod = DevicePod(
            hostname="test-device",
            manifest=manifest,
            transport="sse",
            base_url="http://device.local",
        )
        tool_fn = pod._tool_dispatch["list_items"]
        await tool_fn(limit=10)

    assert captured["url"] == "http://device.local/items"
    assert captured["params"] == {"limit": 10}


def _resource_manifest() -> McpManifest:
    return McpManifest(
        server_name="mcp-test",
        server_version="1.0.0",
        hostname="test-device",
        tools=[],
        resources=[McpResource(uri="device://test-device/status", name="status", description="Device status")],
    )


@pytest.mark.anyio
async def test_resources_list_returns_manifest_resources():
    pod = DevicePod(hostname="test-device", manifest=_resource_manifest(), transport="sse", base_url="http://d.local")
    response = await pod._handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}})
    assert response["result"]["resources"] == [
        {
            "uri": "device://test-device/status",
            "name": "status",
            "description": "Device status",
            "mimeType": "application/json",
        }
    ]


@pytest.mark.anyio
async def test_resources_read_fetches_device_endpoint():
    pod = DevicePod(hostname="test-device", manifest=_resource_manifest(), transport="sse", base_url="http://d.local")
    captured: dict = {}

    async def fake_request(self, method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/json"}
        resp.json.return_value = {"status": "online"}
        resp.raise_for_status = MagicMock()
        return resp

    with patch("httpx.AsyncClient.request", fake_request):
        response = await pod._handle_mcp_message(
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "device://test-device/status"}}
        )

    assert captured["method"] == "GET"
    assert captured["url"] == "http://d.local/status"
    contents = response["result"]["contents"]
    assert contents[0]["uri"] == "device://test-device/status"
    assert "online" in contents[0]["text"]


@pytest.mark.anyio
async def test_resources_read_unknown_uri_returns_error():
    pod = DevicePod(hostname="test-device", manifest=_resource_manifest(), transport="sse", base_url="http://d.local")
    response = await pod._handle_mcp_message(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "device://other-host/foo"}}
    )
    assert "error" in response
    assert response["error"]["code"] == -32602


@pytest.mark.anyio
async def test_initialize_advertises_resources_capability():
    pod = DevicePod(hostname="test-device", manifest=_resource_manifest(), transport="sse", base_url="http://d.local")
    response = await pod._handle_mcp_message(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
    )
    caps = response["result"]["capabilities"]
    assert "resources" in caps
    assert caps["resources"]["subscribe"] is False


@pytest.mark.anyio
async def test_sse_stop_pushes_sentinel_even_when_queue_is_full():
    """stop() must unblock event_stream even when the client queue is at maxsize."""
    from device_mcp_gateway.pods.sse_server import SseTransport

    async def noop_handler(msg):
        return None

    transport = SseTransport("test-host", noop_handler)
    q = transport.register_client("sess1", "/messages?session_id=sess1")

    # Fill the queue to its maxsize=1000 capacity
    for i in range(1000):
        try:
            q.put_nowait({"event": "message", "data": str(i)})
        except asyncio.QueueFull:
            break

    # stop() must drain the backlog and push the sentinel without raising
    await transport.stop()

    # After stop(), exactly the sentinel remains in the queue
    assert q.qsize() == 1


@pytest.mark.anyio
async def test_oauth2_lock_prevents_duplicate_token_fetches():
    """Concurrent ensure_token calls must trigger exactly one _fetch_token, not N."""
    from device_mcp_gateway.auth.oauth2 import OAuth2Auth

    auth = OAuth2Auth(
        token_endpoint="http://auth.local/token",
        client_id="cid",
        client_secret="csec",
    )
    call_count = 0

    async def fake_fetch():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # simulate network round-trip
        auth._access_token = "test-token"
        auth._token_expiry = time.time() + 3600

    with patch.object(auth, "_fetch_token", fake_fetch):
        await asyncio.gather(*[auth.ensure_token() for _ in range(5)])

    assert call_count == 1, f"Expected 1 _fetch_token call, got {call_count}"
    assert auth._access_token == "test-token"
