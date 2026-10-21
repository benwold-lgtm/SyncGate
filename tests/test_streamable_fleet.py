# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Fleet over Streamable HTTP, embedded mode (Phase 6, Workstream A3).

End-to-end against a real server and the mock target API. Two devices share the same mock
API, so the same tool exists on both and must come back correctly namespaced — the fleet's
whole job.

The headline assertion is that **``tools/call`` answers on the POST**. On the SSE fleet
surface the result arrives on a stream instead, and the two modes disagree about which
methods do that; here there is no stream, so a test that finds the result in the response
body is checking the thing this workstream exists to deliver.

Nothing is pre-seeded: every session is opened by a real ``initialize``.
"""

import time

import pytest

FLEET = "/v1/fleet/mcp"
SESSION_HEADER = "Mcp-Session-Id"
HOST_A = "sf-fleet-a.local"
HOST_B = "sf-fleet-b.local"


def _register(client, hostname, mock_target_url):
    resp = client.post(
        "/v1/devices",
        json={"hostname": hostname, "base_url": mock_target_url, "auth_type": "none"},
    )
    assert resp.status_code == 200, f"Registration failed for {hostname}: {resp.json()}"


def _wait_pod_active(client, hostname, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        devices = client.get("/v1/devices").json().get("devices", [])
        dev = next((d for d in devices if d["hostname"] == hostname), None)
        if dev and dev.get("pod_active"):
            return
        time.sleep(0.2)
    raise AssertionError(f"Pod for {hostname} did not become active")


@pytest.fixture(scope="module")
def fleet(client, mock_target_url):
    """Two devices for the whole module, deregistered afterwards.

    Devices persist in the session-shared SQLite store and are re-spawned by the next
    module's server, so a module that leaves them behind taxes every module after it.
    """
    for h in (HOST_A, HOST_B):
        _register(client, h, mock_target_url)
        _wait_pod_active(client, h)
    yield f"{FLEET}?devices={HOST_A},{HOST_B}"
    for h in (HOST_A, HOST_B):
        client.delete(f"/v1/devices/{h}")


def _rpc(mid, method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        msg["id"] = mid
    if params is not None:
        msg["params"] = params
    return msg


def _initialize(client, url):
    resp = client.post(url, json=_rpc(1, "initialize", {"protocolVersion": "2025-06-18"}))
    assert resp.status_code == 200, resp.text
    return resp.headers.get(SESSION_HEADER), resp.json()


# --- the asymmetry this workstream removes ------------------------------------


def test_a_fleet_tool_call_answers_on_the_post(client, fleet):
    """The result must be in *this* response body.

    On the SSE fleet surface a distributed `tools/call` returns `{"status": "accepted"}` and
    the real answer arrives on the stream, while embedded delivers everything on the stream.
    Both are legal MCP and the split is a recorded finding. Here the body is the answer.
    """
    session_id, init = _initialize(client, fleet)
    assert session_id, "initialize must mint a session id"
    assert init["result"]["serverInfo"]["name"] == "mcp-fleet"

    listed = client.post(fleet, json=_rpc(2, "tools/list"), headers={SESSION_HEADER: session_id})
    assert listed.status_code == 200
    names = [t["name"] for t in listed.json()["result"]["tools"]]
    # Same tool on both devices, so it must be namespaced per host rather than collapsed.
    a_tools = [n for n in names if n.startswith("sf_fleet_a")]
    b_tools = [n for n in names if n.startswith("sf_fleet_b")]
    assert a_tools and b_tools, names
    assert len(set(names)) == len(names), f"display names must be unique: {names}"

    target = next(n for n in a_tools if "get_device_status" in n)
    called = client.post(
        fleet,
        json=_rpc(3, "tools/call", {"name": target, "arguments": {}}),
        headers={SESSION_HEADER: session_id},
    )
    assert called.status_code == 200
    body = called.json()
    assert body["id"] == 3
    assert "result" in body, body
    assert body["result"]["content"], body
    assert "status" not in body, "an 'accepted' ack means the result went somewhere else"


def test_ping_and_notifications_behave_as_the_transport_requires(client, fleet):
    session_id, _ = _initialize(client, fleet)

    pinged = client.post(fleet, json=_rpc(4, "ping"), headers={SESSION_HEADER: session_id})
    assert pinged.status_code == 200 and pinged.json()["result"] == {}

    noted = client.post(
        fleet,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={SESSION_HEADER: session_id},
    )
    assert noted.status_code == 202 and not noted.content


# --- the fleet is fixed at open ------------------------------------------------


def test_the_device_list_cannot_be_widened_after_initialize(client, mock_target_url):
    """`devices` is read on initialize and ignored afterwards.

    Honouring it later would let a caller quietly extend the session's reach by appending a
    hostname to a subsequent request, which is an authorization decision made in the wrong
    place.

    The widening target is a **live, registered** device deliberately left out of the session.
    Naming an unregistered host instead would make this pass whether the parameter was
    honoured or not, since an unreachable device is skipped either way — a test that cannot
    fail for the reason it claims to check.
    """
    outsider = "sf-fleet-outsider.local"
    _register(client, outsider, mock_target_url)
    _wait_pod_active(client, outsider)
    try:
        # Prove the outsider *would* show up if it were in the fleet, so the negative below
        # is about the session's boundary rather than about the device being unavailable.
        proof_session, _ = _initialize(client, f"{FLEET}?devices={HOST_A},{outsider}")
        proof = client.post(FLEET, json=_rpc(4, "tools/list"), headers={SESSION_HEADER: proof_session})
        assert [n for n in [t["name"] for t in proof.json()["result"]["tools"]] if n.startswith("sf_fleet_outsider")]

        session_id, _ = _initialize(client, f"{FLEET}?devices={HOST_A}")
        widened = client.post(
            f"{FLEET}?devices={HOST_A},{outsider}",
            json=_rpc(5, "tools/list"),
            headers={SESSION_HEADER: session_id},
        )
        assert widened.status_code == 200
        names = [t["name"] for t in widened.json()["result"]["tools"]]
        assert not [n for n in names if n.startswith("sf_fleet_outsider")], names
    finally:
        client.delete(f"/v1/devices/{outsider}")


def test_initialize_requires_at_least_one_device(client):
    resp = client.post(FLEET, json=_rpc(1, "initialize"))
    assert resp.status_code == 400
    assert "devices" in resp.text


def test_too_many_devices_is_refused(client):
    many = ",".join(f"host-{i}.local" for i in range(50))
    resp = client.post(f"{FLEET}?devices={many}", json=_rpc(1, "initialize"))
    assert resp.status_code == 400
    assert "max" in resp.text.lower()


def test_a_fleet_of_only_unreachable_devices_is_404(client):
    resp = client.post(f"{FLEET}?devices=nope-1.local,nope-2.local", json=_rpc(1, "initialize"))
    assert resp.status_code == 404


# --- tools/list is rebuilt, not frozen ----------------------------------------


def test_a_device_that_was_down_at_open_joins_once_it_is_up(client, mock_target_url, fleet):
    """A manifest frozen at session open would leave a recovered device missing for the
    session's whole life. The list is rebuilt against the originally requested hostnames."""
    latecomer = "sf-fleet-late.local"
    url = f"{FLEET}?devices={HOST_A},{latecomer}"
    session_id, _ = _initialize(client, url)

    before = client.post(FLEET, json=_rpc(2, "tools/list"), headers={SESSION_HEADER: session_id})
    assert not [n for n in [t["name"] for t in before.json()["result"]["tools"]] if n.startswith("sf_fleet_late")]

    try:
        _register(client, latecomer, mock_target_url)
        _wait_pod_active(client, latecomer)
        after = client.post(FLEET, json=_rpc(3, "tools/list"), headers={SESSION_HEADER: session_id})
        names = [t["name"] for t in after.json()["result"]["tools"]]
        assert [n for n in names if n.startswith("sf_fleet_late")], names
    finally:
        client.delete(f"/v1/devices/{latecomer}")


# --- errors stay inside the session -------------------------------------------


def test_an_unknown_tool_is_a_json_rpc_error_not_an_http_error(client, fleet):
    """The session must survive a bad tool name — a 4xx would end the exchange instead."""
    session_id, _ = _initialize(client, fleet)
    resp = client.post(
        fleet,
        json=_rpc(6, "tools/call", {"name": "no_such_fleet_tool", "arguments": {}}),
        headers={SESSION_HEADER: session_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 6 and "error" in body, body

    still_working = client.post(fleet, json=_rpc(7, "ping"), headers={SESSION_HEADER: session_id})
    assert still_working.status_code == 200


def test_an_unknown_method_is_a_json_rpc_error(client, fleet):
    session_id, _ = _initialize(client, fleet)
    resp = client.post(fleet, json=_rpc(8, "resources/list"), headers={SESSION_HEADER: session_id})
    assert resp.status_code == 200
    assert "error" in resp.json()


# --- sessions ------------------------------------------------------------------


def test_a_request_without_a_session_is_refused(client, fleet):
    resp = client.post(FLEET, json=_rpc(9, "tools/list"))
    assert resp.status_code == 400
    assert SESSION_HEADER.lower() in resp.text.lower()


def test_an_unknown_session_is_refused(client, fleet):
    resp = client.post(FLEET, json=_rpc(9, "tools/list"), headers={SESSION_HEADER: "not-a-real-session"})
    assert resp.status_code == 404


def test_the_server_mints_the_session_id_not_the_client(client, fleet):
    resp = client.post(
        fleet,
        json=_rpc(1, "initialize", {"protocolVersion": "2025-06-18"}),
        headers={SESSION_HEADER: "attacker-chosen"},
    )
    assert resp.status_code == 200
    assert resp.headers.get(SESSION_HEADER) not in (None, "attacker-chosen")


def test_a_per_device_session_cannot_be_driven_through_the_fleet_surface(client, mock_target_url, fleet):
    """The two surfaces have different tool namespaces and different dispatch.

    A per-device session id presented here must be refused rather than half-work, which is
    what sharing one session store would otherwise allow.
    """
    device_url = f"/v1/devices/{HOST_A}/mcp"
    resp = client.post(device_url, json=_rpc(1, "initialize", {"protocolVersion": "2025-06-18"}))
    assert resp.status_code == 200
    device_session = resp.headers.get(SESSION_HEADER)
    assert device_session

    leaked = client.post(FLEET, json=_rpc(2, "tools/list"), headers={SESSION_HEADER: device_session})
    assert leaked.status_code == 404, "a device session must not be usable as a fleet session"


def test_a_session_can_be_terminated_and_then_no_longer_works(client, fleet):
    session_id, _ = _initialize(client, fleet)
    gone = client.request("DELETE", FLEET, headers={SESSION_HEADER: session_id})
    assert gone.status_code == 204
    after = client.post(FLEET, json=_rpc(10, "tools/list"), headers={SESSION_HEADER: session_id})
    assert after.status_code == 404


def test_delete_without_a_session_header_is_refused(client, fleet):
    resp = client.request("DELETE", FLEET)
    assert resp.status_code == 400


# --- transport conformance -----------------------------------------------------


def test_get_is_refused_because_no_server_stream_is_offered(client, fleet):
    assert client.get(FLEET).status_code == 405


def test_a_batched_request_is_refused_with_a_reason(client, fleet):
    resp = client.post(fleet, json=[_rpc(1, "tools/list")])
    assert resp.status_code == 400
    assert "batch" in resp.text.lower()


def test_every_fleet_mcp_route_requires_tools_call():
    """Asserted directly since the HTTP+SSE fleet route it used to be compared with is gone."""
    from device_mcp_gateway.api import fleets as handle_routes
    from device_mcp_gateway.api import streamable_fleet as sf_routes

    from .test_streamable_http import _route_scopes

    for method in ("POST", "GET", "DELETE"):
        assert "tools:call" in _route_scopes(sf_routes.router, "/fleet/mcp", method)
    for path, method in (("/fleet/{handle}/mcp", "POST"), ("/fleets", "POST"), ("/fleets", "GET")):
        assert "tools:call" in _route_scopes(handle_routes.router, path, method), (path, method)
