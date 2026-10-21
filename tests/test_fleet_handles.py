# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Fleet handles — ADR-0027 §5, Workstream B PR 6.

A fleet becomes a server-minted handle, fixed at mint and bound to the principal who minted
it, addressed in the URL: ``POST /v1/fleet/{handle}/mcp`` serves both eras. Two principals
here, so "a non-owner gets 404" is tested against a real second caller rather than inferred.
The devices are real pods over the mock device, so a tool call's answer is the device's own.
"""

from __future__ import annotations

import time

import threading

import httpx
import pytest
import uvicorn

from device_mcp_gateway.core import protocol
from device_mcp_gateway.fleet_handles import MemoryFleetHandles, SqliteFleetHandles, looks_like_handle, mint_handle
from device_mcp_gateway.rbac import ROLE_SCOPES, Authenticator, Principal

MODERN = protocol.MODERN_PROTOCOL_VERSIONS[0]
A = {"Authorization": "Bearer tok-alice"}
B = {"Authorization": "Bearer tok-bob"}
ADMIN = {"Authorization": "Bearer tok-admin"}
HOSTS = ["fh-one.local", "fh-two.local"]


def _principal(name):
    return Principal(subject=f"key:{name}", scopes=ROLE_SCOPES["admin"], auth_method="api_key")


@pytest.fixture(scope="module")
def gw(tmp_path_factory):
    """A real gateway process of its own, so its app state is this module's to set.

    Real pods need one long-lived event loop, which an in-process TestClient does not give;
    and the suite's shared gateway does not expose its app. Two principals and an admin.
    """
    from device_mcp_gateway.cfg import load_config
    from device_mcp_gateway.main import create_app

    # Its own SQLite file: the default one is shared with the suite's gateway, and starting
    # over it re-spawns every device other modules left there, which can outlast any wait.
    cfg = load_config()
    cfg.setdefault("storage", {})["db_path"] = str(tmp_path_factory.mktemp("fleet-handles") / "devices.db")
    app = create_app(override_config=cfg)
    app.state.authenticator = Authenticator(
        {"tok-alice": _principal("alice"), "tok-bob": _principal("bob"), "tok-admin": _principal("admin")},
        enabled=True,
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started:
        assert time.time() < deadline, "gateway did not start"
        time.sleep(0.05)
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        yield app, c
    server.should_exit = True
    thread.join(timeout=5)


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def client(gw):
    app, c = gw
    app.state.fleet_handles = MemoryFleetHandles()  # each test starts with no handles
    return c


def _register(c, hostname, base_url):
    r = c.post("/v1/devices", json={"hostname": hostname, "base_url": base_url, "auth_type": "none"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    deadline = time.time() + 10
    while not c.get(f"/v1/devices/{hostname}", headers=ADMIN).json().get("pod_active"):
        assert time.time() < deadline, f"pod for {hostname} never became active"
        time.sleep(0.2)


@pytest.fixture(scope="module")
def devices(gw, mock_target_url):
    _app, c = gw
    for h in HOSTS:
        _register(c, h, mock_target_url)
    yield HOSTS
    for h in HOSTS:
        c.delete(f"/v1/devices/{h}", headers=ADMIN)


def _mint(client, headers=A, devices=None):
    return client.post("/v1/fleets", json={"devices": devices or HOSTS}, headers=headers)


def _modern(client, url, method, params=None, headers=A, name=None):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "_meta": {protocol.PROTOCOL_VERSION_META_KEY: MODERN, protocol.CLIENT_CAPABILITIES_META_KEY: {}},
            **(params or {}),
        },
    }
    h = {**headers, "MCP-Protocol-Version": MODERN, "Mcp-Method": method}
    if name:
        h["Mcp-Name"] = name
    return client.post(url, json=body, headers=h)


# --- minting -------------------------------------------------------------------------------


def test_minting_returns_an_opaque_handle_and_its_url(client, devices):
    resp = _mint(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert looks_like_handle(body["handle"]) and body["url"] == f"/v1/fleet/{body['handle']}/mcp"
    assert body["devices"] == sorted(HOSTS)


def test_handles_are_unguessable():
    handles = {mint_handle() for _ in range(1000)}
    assert len(handles) == 1000 and all(len(h) == len("flt_") + 32 for h in handles)


def test_an_unregistered_device_is_refused_at_mint(client, devices):
    resp = _mint(client, devices=[HOSTS[0], "no-such.local"])
    assert resp.status_code == 400 and "no-such.local" in resp.json()["detail"]


def test_the_device_cap_applies_at_mint(gw, client, devices, monkeypatch):
    monkeypatch.setitem(gw[0].state.config.setdefault("registry", {}), "fleet_max_devices", 1)
    assert _mint(client).status_code == 400


def test_a_principal_cannot_mint_without_bound(gw, client, devices, monkeypatch):
    monkeypatch.setitem(gw[0].state.config.setdefault("registry", {}), "fleet_handles_per_principal", 2)
    assert _mint(client).status_code == 201 and _mint(client).status_code == 201
    assert _mint(client).status_code == 409
    assert _mint(client, headers=B).status_code == 201  # per principal, not global


# --- ownership -----------------------------------------------------------------------------


def test_listing_shows_only_the_callers_own(client, devices):
    mine = _mint(client).json()["handle"]
    theirs = _mint(client, headers=B).json()["handle"]
    assert [f["handle"] for f in client.get("/v1/fleets", headers=A).json()["fleets"]] == [mine]
    assert [f["handle"] for f in client.get("/v1/fleets", headers=B).json()["fleets"]] == [theirs]


def test_a_non_owner_gets_404_not_403_on_every_route(client, devices):
    """404 either way, so the endpoint is not an oracle for which handles exist."""
    handle = _mint(client).json()["handle"]
    url = f"/v1/fleet/{handle}/mcp"
    assert _modern(client, url, "tools/list", headers=B).status_code == 404
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
    assert client.post(url, json=init, headers=B).status_code == 404
    assert client.delete(f"/v1/fleets/{handle}", headers=B).status_code == 404
    missing = f"/v1/fleet/{mint_handle()}/mcp"
    assert _modern(client, missing, "tools/list").status_code == 404  # indistinguishable from someone else's
    assert _modern(client, "/v1/fleet/not-a-handle/mcp", "tools/list").status_code == 404


def test_only_the_owner_deletes_and_then_it_is_gone(client, devices):
    handle = _mint(client).json()["handle"]
    assert client.delete(f"/v1/fleets/{handle}", headers=A).status_code == 204
    assert _modern(client, f"/v1/fleet/{handle}/mcp", "tools/list").status_code == 404


# --- serving, stateless era ----------------------------------------------------------------


def test_a_stateless_client_lists_and_calls_through_the_handle(client, devices):
    url = _mint(client).json()["url"]
    listed = _modern(client, url, "tools/list")
    assert listed.status_code == 200, listed.text
    result = listed.json()["result"]
    names = [t["name"] for t in result["tools"]]
    assert len(names) == 4 and all(n.startswith(("fh_one", "fh_two")) for n in names)  # namespaced per device
    assert result["resultType"] == "complete" and result["ttlMs"] > 0  # a modern client re-lists

    status_tool = next(n for n in names if n.startswith("fh_one") and n.endswith("get_device_status"))
    called = _modern(client, url, "tools/call", {"name": status_tool, "arguments": {}}, name=status_tool)
    assert called.status_code == 200, called.text
    assert '"online"' in called.json()["result"]["content"][0]["text"]  # the device's own answer


def test_discover_names_a_tools_only_server(client, devices):
    result = _modern(client, _mint(client).json()["url"], "server/discover").json()["result"]
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["supportedVersions"] == [MODERN]


def test_a_request_cannot_reach_a_device_outside_the_handle(client, devices):
    url = _mint(client, devices=[HOSTS[0]]).json()["url"]
    outside = "fh_two_local__get_device_status"
    resp = _modern(client, url, "tools/call", {"name": outside, "arguments": {}}, name=outside)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == protocol.INVALID_PARAMS


def test_a_deregistered_member_drops_out_of_the_list(client, devices, mock_target_url):
    _register(client, "fh-temp.local", mock_target_url)
    url = _mint(client, devices=[HOSTS[0], "fh-temp.local"]).json()["url"]
    assert any(t["name"].startswith("fh_temp") for t in _modern(client, url, "tools/list").json()["result"]["tools"])

    client.delete("/v1/devices/fh-temp.local", headers=ADMIN)
    tools = _modern(client, url, "tools/list").json()["result"]["tools"]
    assert tools and not any(t["name"].startswith("fh_temp") for t in tools)


# --- serving, handshake era, on the same URL -----------------------------------------------


def test_a_handshake_client_opens_a_session_over_the_handles_devices(client, devices):
    url = _mint(client).json()["url"]
    init = client.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        headers=A,
    )
    assert init.status_code == 200, init.text
    session = init.headers["Mcp-Session-Id"]
    listed = client.post(
        url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers={**A, "Mcp-Session-Id": session}
    )
    assert len(listed.json()["result"]["tools"]) == 4
    assert "resultType" not in listed.json()["result"]


# --- the deprecated ?devices= surface --------------------------------------------------------


def test_a_stateless_request_to_devices_query_is_pointed_at_handles(client, devices):
    resp = _modern(client, f"/v1/fleet/mcp?devices={','.join(HOSTS)}", "tools/list")
    assert resp.status_code == 400 and "/v1/fleets" in resp.json()["error"]["message"]
    assert resp.headers.get("Deprecation") == "true"


def test_the_devices_query_still_serves_the_handshake_and_says_it_is_deprecated(client, devices):
    resp = client.post(
        f"/v1/fleet/mcp?devices={','.join(HOSTS)}",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        headers=A,
    )
    assert resp.status_code == 200 and resp.headers.get("Mcp-Session-Id")
    assert resp.headers.get("Deprecation") == "true"


# --- persistence ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_embedded_handle_survives_a_restart(tmp_path):
    from device_mcp_gateway.fleet_handles import FleetHandle

    db = str(tmp_path / "devices.db")
    first = SqliteFleetHandles(db)
    fleet = FleetHandle(handle=mint_handle(), owner="key:alice", devices=["a", "b"])
    await first.create(fleet)

    again = SqliteFleetHandles(db)  # a new process over the same file
    got = await again.get(fleet.handle)
    assert got is not None and got.owner == "key:alice" and got.devices == ["a", "b"]
    assert [f.handle for f in await again.list_for("key:alice")] == [fleet.handle]
    await again.delete(fleet.handle)
    assert await again.get(fleet.handle) is None


@pytest.mark.asyncio
async def test_a_distributed_handle_is_in_redis(real_redis):
    from device_mcp_gateway.fleet_handles import FleetHandle, RedisFleetHandles

    store = RedisFleetHandles(real_redis)
    fleet = FleetHandle(handle=mint_handle(), owner="key:alice", devices=["a"])
    await store.create(fleet)
    other = RedisFleetHandles(real_redis)  # another replica
    assert (await other.get(fleet.handle)).devices == ["a"]
    assert [f.handle for f in await other.list_for("key:alice")] == [fleet.handle]
    assert await other.list_for("key:bob") == []
    await other.delete(fleet.handle)
    assert await store.get(fleet.handle) is None and await store.list_for("key:alice") == []
