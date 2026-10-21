# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Fleet over Streamable HTTP, distributed mode (Phase 6, Workstream A3).

Covers what only distributed mode has: fleet session state living in Redis so any replica can
serve a request, and the guard that keeps a *per-device* session from being driven through the
fleet surface. That guard is the reason this file exists — mutating it out leaves the whole
embedded suite green, because the check it removes is on the distributed branch.

Follows ``test_fleet_distributed.py``'s harness: a stubbed Registry/backend over fakeredis with
the distributed lifespan replaced, so no real Redis or worker is needed for routing logic.

**What is stubbed, and why that is honest here.** ``tools/call`` publishes to the device's
stream and then waits on ``session:{id}:results`` for a worker's answer. The waiting half is
the shared ``ResultExchange`` and is already tested end-to-end against real Redis in
``test_streamable_distributed.py`` — including the cursor-before-publish ordering. Only
``await_result`` is replaced here, so what these tests actually exercise is the fleet-specific
part: resolving a namespaced display name to the right host, rewriting it to the device's own
tool name, and publishing that. The full round trip is verified on the live cluster.
"""

import asyncio
import copy
from contextlib import asynccontextmanager

import fakeredis.aioredis
import yaml
from fastapi.testclient import TestClient

from device_mcp_gateway.main import create_app
from device_mcp_gateway.rbac import Authenticator
from device_mcp_gateway.shared.registry_backend import DeviceConfig
from device_mcp_gateway.shared.session_router import SessionRouter

FLEET = "/v1/fleet/mcp"
SESSION_HEADER = "Mcp-Session-Id"


def _tool(name, description="d", schema=None):
    return {"name": name, "description": description, "schema": schema or {}}


class _StubBackend:
    def __init__(self, backlog_by_host=None):
        self._backlog_by_host = backlog_by_host or {}
        self.published: list[dict] = []

    async def call_backlog(self, hostname: str) -> int:
        return self._backlog_by_host.get(hostname, 0)

    async def publish_tool_call(self, **kwargs):
        self.published.append(kwargs)


class _StubRegistry:
    def __init__(self, devices, manifests, backend):
        self._devices = devices
        self._manifests = manifests
        self._backend = backend

    async def get_device(self, hostname):
        active = self._devices.get(hostname)
        if active is None:
            return None
        return DeviceConfig(hostname=hostname, base_url=f"http://{hostname}", pod_active=active)

    async def get_manifest(self, hostname):
        return self._manifests.get(hostname)


def _app(devices, manifests, monkeypatch, backlog_by_host=None, result=None):
    cfg = copy.deepcopy(yaml.safe_load(open("config.yaml")))
    cfg.setdefault("registry", {})
    cfg["registry"]["mode"] = "distributed"
    cfg["registry"]["call_backlog_limit"] = 1000
    cfg.setdefault("gateway", {})["allow_plaintext_credentials"] = True
    cfg["gateway"]["allow_anonymous"] = True
    cfg.setdefault("redis", {})["allow_insecure"] = True
    app = create_app(override_config=cfg)

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    app.router.lifespan_context = _noop_lifespan
    backend = _StubBackend(backlog_by_host)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.redis = redis
    router = SessionRouter(redis)

    async def _canned(session_id, msg_id, *, cursor, timeout):
        return result if result is not None else {"jsonrpc": "2.0", "id": msg_id, "result": {"content": [], "ok": True}}

    router.await_result = _canned  # see module docstring: the tested half stays real
    app.state.session_router = router
    app.state.registry = _StubRegistry(devices, manifests, backend)
    monkeypatch.setattr(app.state, "authenticator", Authenticator({}, enabled=False))
    return app, backend


def _rpc(mid, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        msg["id"] = mid
    if params is not None:
        msg["params"] = params
    return msg


def _init(client, devices):
    resp = client.post(f"{FLEET}?devices={devices}", json=_rpc(1, "initialize", {"protocolVersion": "2025-06-18"}))
    assert resp.status_code == 200, resp.text
    return resp.headers.get(SESSION_HEADER)


# --- the guard the embedded suite cannot see ----------------------------------


def test_a_per_device_session_is_refused_on_the_fleet_surface(monkeypatch):
    """Both session kinds live in one Redis hash namespace, so presenting a device session
    here has to be rejected explicitly rather than by luck.

    Without the fleet-hosts check this returns a tools/list built from an empty device list —
    a 200 that quietly means nothing — instead of 404.
    """
    app, _ = _app({"a": True}, {"a": {"tools": [_tool("get_status")]}}, monkeypatch)
    other = SessionRouter(app.state.redis)

    async def _seed_device_session():
        # Exactly what the per-device transport writes: no fleet-hosts field.
        await other.register("dev-session", "a", "gw-b", owner="key:legacy")

    asyncio.run(_seed_device_session())

    with TestClient(app) as client:
        resp = client.post(FLEET, json=_rpc(2, "tools/list"), headers={SESSION_HEADER: "dev-session"})
    assert resp.status_code == 404, f"a device session must not be usable as a fleet session: {resp.text}"


def test_a_fleet_session_written_by_another_replica_is_usable_here(monkeypatch):
    """The initialize may have been served by a different replica than this request."""
    app, _ = _app(
        {"a": True, "b": True}, {"a": {"tools": [_tool("get_status")]}, "b": {"tools": [_tool("reboot")]}}, monkeypatch
    )
    other = SessionRouter(app.state.redis)

    async def _seed():
        # Seeded as the subject this request will actually carry, so the assertion is about
        # cross-replica visibility rather than accidentally about the F-37 owner check.
        await other.register("s-remote", "", "gw-b", owner="anonymous", extra={"fleet_hosts": "a,b"})

    asyncio.run(_seed())

    with TestClient(app) as client:
        resp = client.post(FLEET, json=_rpc(2, "tools/list"), headers={SESSION_HEADER: "s-remote"})
    assert resp.status_code == 200, resp.text
    names = sorted(t["name"] for t in resp.json()["result"]["tools"])
    assert names == ["a_get_status", "b_reboot"], names


def test_a_fleet_session_is_bound_to_the_principal_that_opened_it(monkeypatch):
    """F-37 across replicas — the binding lives on the Redis hash, not in a local dict."""
    app, _ = _app({"a": True}, {"a": {"tools": [_tool("get_status")]}}, monkeypatch)
    other = SessionRouter(app.state.redis)

    async def _seed():
        await other.register("s-owned", "", "gw-b", owner="key:someone-else", extra={"fleet_hosts": "a"})

    asyncio.run(_seed())

    with TestClient(app) as client:
        # allow_anonymous makes this request's subject "anonymous", not "key:someone-else".
        resp = client.post(FLEET, json=_rpc(2, "tools/list"), headers={SESSION_HEADER: "s-owned"})
        deleted = client.request("DELETE", FLEET, headers={SESSION_HEADER: "s-owned"})
    assert resp.status_code == 403, resp.text
    assert deleted.status_code == 403, "another principal must not terminate someone else's session"


# --- dispatch resolution -------------------------------------------------------


def test_a_namespaced_call_is_published_to_the_right_device_under_its_real_name(monkeypatch):
    """The fleet's actual job: `b_reboot` must reach device `b` as `reboot`.

    Asserted on what was published rather than on the response, because a wrong host with a
    right-looking answer is exactly the failure that matters.
    """
    devices = {"a": True, "b": True}
    manifests = {"a": {"tools": [_tool("reboot")]}, "b": {"tools": [_tool("reboot")]}}
    app, backend = _app(devices, manifests, monkeypatch)

    with TestClient(app) as client:
        session_id = _init(client, "a,b")
        resp = client.post(
            FLEET,
            json=_rpc(7, "tools/call", {"name": "b_reboot", "arguments": {"force": True}}),
            headers={SESSION_HEADER: session_id},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == 7
    assert len(backend.published) == 1, backend.published
    pub = backend.published[0]
    assert pub["hostname"] == "b", "resolved to the wrong device"
    assert pub["message"]["params"]["name"] == "reboot", "display name must be rewritten to the device's own name"
    assert pub["message"]["params"]["arguments"] == {"force": True}
    assert pub["message"]["id"] == 7, "the client's id must survive the rewrite"
    assert pub["session_id"] == session_id


def test_an_unknown_tool_never_reaches_a_device(monkeypatch):
    """A routing guess would be worse than an error — nothing may be published."""
    app, backend = _app({"a": True}, {"a": {"tools": [_tool("get_status")]}}, monkeypatch)

    with TestClient(app) as client:
        session_id = _init(client, "a")
        resp = client.post(
            FLEET,
            json=_rpc(8, "tools/call", {"name": "b_reboot", "arguments": {}}),
            headers={SESSION_HEADER: session_id},
        )

    assert resp.status_code == 200
    assert "error" in resp.json()
    assert backend.published == [], "an unresolvable tool must not be dispatched anywhere"


def test_an_overloaded_device_sheds_without_publishing(monkeypatch):
    """Admission control (F-06) is inherited from the shared exchange; confirm the fleet path
    actually goes through it rather than around it."""
    app, backend = _app(
        {"a": True},
        {"a": {"tools": [_tool("get_status")]}},
        monkeypatch,
        backlog_by_host={"a": 5000},
    )

    with TestClient(app) as client:
        session_id = _init(client, "a")
        resp = client.post(
            FLEET,
            json=_rpc(9, "tools/call", {"name": "a_get_status", "arguments": {}}),
            headers={SESSION_HEADER: session_id},
        )

    assert resp.status_code == 429, resp.text
    assert backend.published == [], "a shed call must not be published"


def test_initialize_stores_the_fleet_so_a_later_request_needs_no_devices_param(monkeypatch):
    app, _ = _app({"a": True, "b": True}, {"a": {"tools": [_tool("x")]}, "b": {"tools": [_tool("y")]}}, monkeypatch)

    with TestClient(app) as client:
        session_id = _init(client, "a,b")
        resp = client.post(FLEET, json=_rpc(3, "tools/list"), headers={SESSION_HEADER: session_id})

    assert resp.status_code == 200
    assert sorted(t["name"] for t in resp.json()["result"]["tools"]) == ["a_x", "b_y"]


def test_a_terminated_fleet_session_is_gone_for_every_replica(monkeypatch):
    app, _ = _app({"a": True}, {"a": {"tools": [_tool("x")]}}, monkeypatch)

    with TestClient(app) as client:
        session_id = _init(client, "a")
        assert client.request("DELETE", FLEET, headers={SESSION_HEADER: session_id}).status_code == 204
        after = client.post(FLEET, json=_rpc(4, "tools/list"), headers={SESSION_HEADER: session_id})
    assert after.status_code == 404

    other = SessionRouter(app.state.redis)
    assert asyncio.run(other.get(session_id)) is None, "the session must be gone from Redis, not just locally"
