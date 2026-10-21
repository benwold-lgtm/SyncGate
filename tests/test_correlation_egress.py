# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""ADR-0026: the correlation id must reach the device, not just the gateway's own logs.

ADR-0026 accepts service-identity-per-device permanently: the appliance sees one account,
never the human. The whole weight of "who really did this" then rests on being able to
join the gateway's audit record to the device's own log — and that join needs one value
present on both sides. So these tests treat the outbound `X-Request-Id` as a **required
property of a device call**, not as a diagnostic nicety.

They deliberately look at the far end of the wire wherever they can. The obvious way to
test this — patch `httpx.AsyncClient.request` and inspect `kwargs["headers"]`, as the
existing header-injection tests do — cannot see the id at all: the stamp is a request
event hook, which httpx runs inside `send()`, after `request()` has built the Request. A
test written that way would pass on a build that stamps nothing.
"""

import json
import time

import fakeredis.aioredis
import httpx
import pytest

from device_mcp_gateway.core.correlation import (
    CORRELATION_HEADER,
    current_request_id,
    stamp_correlation,
    use_request_id,
    with_correlation_hook,
)
from device_mcp_gateway.core.translator import McpManifest, McpTool
from device_mcp_gateway.pods.device_pod import DevicePod
from device_mcp_gateway.shared.registry_backend import RedisRegistryBackend
from device_mcp_gateway.worker.runner import DeviceWorker

from tests.conftest import device_request_ids

HOST = "dev1"


# --- the pod's own egress ----------------------------------------------------


def _pod(param_locations: dict | None = None) -> DevicePod:
    manifest = McpManifest(
        server_name="m",
        server_version="1",
        hostname=HOST,
        tools=[
            McpTool(
                name="t",
                description="d",
                schema={"type": "object", "properties": {}},
                method="GET",
                path="/x",
                param_locations=param_locations or {},
            )
        ],
    )
    return DevicePod(hostname=HOST, manifest=manifest, base_url="http://dev.local")


def _intercept(pod: DevicePod) -> list[httpx.Headers]:
    """Swap the pod's transport for a recorder, keeping the real client and its hooks."""
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, json={}, headers={"content-type": "application/json"})

    pod._client()._transport = httpx.MockTransport(handler)
    return seen


@pytest.mark.asyncio
async def test_a_device_call_carries_the_id_the_gateway_logged():
    pod = _pod()
    seen = _intercept(pod)

    with use_request_id("rid-abc"):
        await pod._tool_dispatch["t"]()

    assert seen[0][CORRELATION_HEADER] == "rid-abc"
    await pod.aclose()


@pytest.mark.asyncio
async def test_a_tool_argument_cannot_choose_the_correlation_id():
    """The id is evidence. An `in: header` parameter is attacker-reachable input.

    Both defences are asserted at once: the sanitiser drops the smuggled value, and the
    egress hook assigns the real one over whatever survived.
    """
    pod = _pod(param_locations={"X-Request-Id": "header"})
    seen = _intercept(pod)

    with use_request_id("rid-real"):
        await pod._tool_dispatch["t"](**{"X-Request-Id": "rid-forged"})

    assert seen[0][CORRELATION_HEADER] == "rid-real"
    await pod.aclose()


@pytest.mark.asyncio
async def test_no_id_is_invented_when_no_request_is_in_scope():
    """An id minted at egress would look like a correlation id and join to nothing."""
    pod = _pod()
    seen = _intercept(pod)

    await pod._tool_dispatch["t"]()

    assert CORRELATION_HEADER not in seen[0]
    await pod.aclose()


@pytest.mark.asyncio
async def test_a_dash_is_not_a_correlation_id():
    """`rid` defaults to "-" on the worker's stream read; stamping that is worse than nothing."""
    pod = _pod()
    seen = _intercept(pod)

    with use_request_id("-"):
        await pod._tool_dispatch["t"]()

    assert CORRELATION_HEADER not in seen[0]
    await pod.aclose()


@pytest.mark.asyncio
async def test_a_resource_read_is_a_device_call_too():
    """`resources/read` reaches the same appliance under the same service identity."""
    pod = _pod()
    seen = _intercept(pod)

    with use_request_id("rid-res"):
        await pod._read_resource(f"device://{HOST}/status", 1)

    assert seen[0][CORRELATION_HEADER] == "rid-res"
    await pod.aclose()


# --- the seam itself ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_hook_overwrites_rather_than_defers_to_what_is_there():
    request = httpx.Request("GET", "http://d.local/x", headers={CORRELATION_HEADER: "forged"})
    with use_request_id("rid-real"):
        await stamp_correlation(request)
    assert request.headers[CORRELATION_HEADER] == "rid-real"


def test_installing_the_hook_keeps_a_callers_own_hooks():
    async def other(_request):  # pragma: no cover - identity is all that is asserted
        return None

    hooks = with_correlation_hook({"request": [other], "response": []})
    assert hooks["request"][0] is other
    assert hooks["request"][-1] is stamp_correlation
    assert "response" in hooks


def test_the_guarded_client_is_where_the_hook_is_installed():
    """Every server-side fetch of an operator-supplied URL goes through this builder, so
    a new outbound path inherits correlation the way it inherits the SSRF guard."""
    from device_mcp_gateway.security.url_policy import build_guarded_client

    client = build_guarded_client(allow_private=True)
    assert stamp_correlation in client._event_hooks["request"]


def test_the_id_does_not_outlive_the_request():
    with use_request_id("rid-1"):
        assert current_request_id() == "rid-1"
    assert current_request_id() == ""


# --- the worker binds it, because there is no HTTP request there -------------


class _CapturingPod:
    """Records the id in scope at the moment the pod is called."""

    def __init__(self):
        self.seen: list[str] = []
        self.manifest = type("M", (), {"tools": []})()

    async def call_tool(self, message):
        self.seen.append(current_request_id())
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"ok": True}}


def _msg(msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": "t", "arguments": {}}}


async def _deliver(redis, fields):
    stream, group = f"device:{HOST}:calls", f"workers-{HOST}"
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception:
        pass
    mid = await redis.xadd(stream, fields)
    await redis.xreadgroup(group, "w1", {stream: ">"}, count=10)
    return stream, group, mid


def _worker(redis, pod):
    w = DeviceWorker(
        worker_id="w1", config={"registry": {"health_check_interval": 30, "tool_call_timeout": 30}}, redis_client=redis
    )
    w._backend = RedisRegistryBackend(redis)
    w._pods[HOST] = pod
    w._assigned.add(HOST)
    return w


@pytest.mark.asyncio
async def test_the_worker_binds_the_gateway_id_for_the_device_call():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pod = _CapturingPod()
    w = _worker(r, pod)

    fields = {"session_id": "s1", "request_id": "r1", "rid": "rid-from-gateway", "message": json.dumps(_msg())}
    s, g, mid = await _deliver(r, fields)
    await w._dispatch_call(HOST, s, g, mid, fields)

    assert pod.seen == ["rid-from-gateway"]


@pytest.mark.asyncio
async def test_one_calls_id_never_bleeds_onto_the_next():
    """The worker dispatches many calls in one task. A set without a reset would attach a
    previous caller's id to an unrelated later call — a correlation id that is a lie is
    worse than one that is missing."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pod = _CapturingPod()
    w = _worker(r, pod)

    first = {"session_id": "s1", "request_id": "r1", "rid": "rid-first", "message": json.dumps(_msg(1))}
    s, g, mid = await _deliver(r, first)
    await w._dispatch_call(HOST, s, g, mid, first)

    second = {"session_id": "s2", "request_id": "r2", "message": json.dumps(_msg(2))}  # no rid
    s, g, mid = await _deliver(r, second)
    await w._dispatch_call(HOST, s, g, mid, second)

    assert pod.seen == ["rid-first", ""]
    assert current_request_id() == ""


# --- end to end, against a real device -------------------------------------


def test_the_device_sees_the_id_the_caller_was_given(client, mock_target_url):
    """The cold path: a real gateway, a real HTTP hop, a real device recording what arrived.

    This is the test that makes the requirement load-bearing — everything above pins a
    mechanism, and only this one shows the mechanism survives the whole embedded-mode
    dispatch (middleware → pod → wire) with the id the caller can also see in the
    response header and the audit log.
    """
    hostname = "mock-iot-rid.local"
    reg = client.post(
        "/v1/devices",
        json={"hostname": hostname, "base_url": mock_target_url, "auth_type": "none", "transport": "sse"},
    )
    assert reg.status_code == 200, reg.text

    deadline = time.time() + 10
    while time.time() < deadline:
        devices = client.get("/v1/devices").json().get("devices", [])
        if any(d["hostname"] == hostname and d.get("pod_active") for d in devices):
            break
        time.sleep(0.2)
    else:  # pragma: no cover - a pod that never spawns fails the assertions below anyway
        raise AssertionError("device pod did not become active")

    with client.stream("GET", f"/v1/devices/{hostname}/sse") as event_resp:
        assert event_resp.status_code == 200
        session_id = None
        event_name = None
        data_payload = ""
        posted = False
        deadline = time.time() + 15
        for line in event_resp.iter_lines():
            if time.time() > deadline:
                break
            if line is None:
                continue
            line = line.strip()
            if line == "":
                if event_name == "endpoint" and "session_id=" in data_payload:
                    session_id = data_payload.split("session_id=")[-1]
                    device_request_ids.clear()
                    send = client.post(
                        f"/v1/devices/{hostname}/messages?session_id={session_id}",
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "get_device_status", "arguments": {}},
                        },
                        headers={"X-Request-Id": "rid-end-to-end"},
                    )
                    assert send.status_code == 200
                    # The gateway echoes the id it will log — the caller's half of the join.
                    assert send.headers["X-Request-Id"] == "rid-end-to-end"
                    posted = True
                elif event_name == "message" and data_payload and posted:
                    break
                event_name = None
                data_payload = ""
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_payload += line[len("data:") :].strip()

    assert posted, "tool call was never sent"
    # …and the device's half. Same value, no gateway-side translation.
    assert "rid-end-to-end" in device_request_ids

    client.delete(f"/v1/devices/{hostname}")
