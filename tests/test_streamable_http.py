# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Streamable HTTP inbound, embedded mode (Phase 6, Workstream A1).

End-to-end against a real server and the mock target API, not a mocked pod. The transport's
whole point is that the JSON-RPC response comes back on the POST that carried the request,
and a mocked pod would let the test agree with the implementation about that rather than
check it — the failure mode this project keeps hitting.

Nothing here pre-seeds a session: every test that needs one gets it by performing a real
`initialize`, so the cold path (a first request from a client with no session) is the path
under test rather than the one skipped.
"""

import time

import pytest

MCP = "/v1/devices/{h}/mcp"
SESSION_HEADER = "Mcp-Session-Id"


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
def device_url(client, mock_target_url):
    """One device for the whole module, unregistered afterwards.

    The devices SQLite store is shared across the session, and every persisted device is
    re-spawned when the *next* module starts its server. A module that registers a device
    per test therefore taxes every module after it — enough, at seven devices, to push
    startup past the fixture's fixed wait and fail unrelated tests with a connection
    refusal. One device, cleaned up, keeps that cost at zero.
    """
    hostname = "sh-device.local"
    _register(client, hostname, mock_target_url)
    _wait_pod_active(client, hostname)
    yield MCP.format(h=hostname)
    client.delete(f"/v1/devices/{hostname}")


def _rpc(mid, method, params=None):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _initialize(client, url):
    """Perform a real handshake and return (session_id, body)."""
    resp = client.post(url, json=_rpc(1, "initialize", {"protocolVersion": "2025-06-18"}))
    assert resp.status_code == 200, resp.text
    return resp.headers.get(SESSION_HEADER), resp.json()


# --- the inversion this transport exists for ---------------------------------


def test_the_response_comes_back_on_the_post_that_asked(client, device_url):
    """The SSE transport acknowledges a POST and delivers the result on a stream held
    elsewhere. Here the result must be the body of this response — that inversion is the
    entire feature, so it is asserted directly rather than inferred from a 200."""
    url = device_url
    url = device_url

    session_id, init = _initialize(client, url)
    assert session_id, "initialize must mint a session id"
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["id"] == 1

    listed = client.post(url, json=_rpc(2, "tools/list"), headers={SESSION_HEADER: session_id})
    assert listed.status_code == 200
    body = listed.json()
    assert body["id"] == 2
    names = [t["name"] for t in body["result"]["tools"]]
    assert "get_device_status" in names, names

    called = client.post(
        url,
        json=_rpc(3, "tools/call", {"name": "get_device_status", "arguments": {}}),
        headers={SESSION_HEADER: session_id},
    )
    assert called.status_code == 200
    result = called.json()
    assert result["id"] == 3
    assert "result" in result, result
    # The mock target answers with real content; the point is that it arrived *here*.
    assert result["result"]["content"], result


def test_a_notification_gets_202_and_no_body(client, device_url):
    """A message with no id has no response. Returning `null` would be a malformed
    JSON-RPC response, so the transport's 202-with-empty-body is what must happen."""
    url = device_url
    session_id, _ = _initialize(client, url)

    resp = client.post(
        url,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={SESSION_HEADER: session_id},
    )
    assert resp.status_code == 202
    assert resp.content in (b"", b"null") or resp.content == b""


# --- session handling, cold ---------------------------------------------------


def test_an_unknown_session_is_refused_so_the_client_reinitializes(client, device_url):
    """404 rather than silently minting one: a client holding a stale id must be told to
    start again, not handed a fresh session it does not know it has."""
    url = device_url
    resp = client.post(url, json=_rpc(9, "tools/list"), headers={SESSION_HEADER: "not-a-real-session"})
    assert resp.status_code == 404


def test_the_server_mints_the_session_id_not_the_client(client, device_url):
    """A client-supplied id on initialize must not be honoured — accepting one would let a
    caller graft themselves onto another principal's session (F-37)."""
    url = device_url
    resp = client.post(
        url,
        json=_rpc(1, "initialize", {"protocolVersion": "2025-06-18"}),
        headers={SESSION_HEADER: "attacker-chosen"},
    )
    assert resp.status_code == 200
    assert resp.headers.get(SESSION_HEADER) not in (None, "attacker-chosen")


def test_a_session_can_be_terminated_and_then_no_longer_works(client, device_url):
    url = device_url
    session_id, _ = _initialize(client, url)

    gone = client.request("DELETE", url, headers={SESSION_HEADER: session_id})
    assert gone.status_code == 204

    after = client.post(url, json=_rpc(4, "tools/list"), headers={SESSION_HEADER: session_id})
    assert after.status_code == 404, "a terminated session must not keep working"


# --- transport conformance ----------------------------------------------------


def test_get_is_refused_because_no_server_stream_is_offered(client, device_url):
    """The transport requires 405 when the server offers no server-initiated stream here,
    and we genuinely offer none — listChanged is false and there is no sampling or roots.
    405 is what tells a conforming client to stop asking."""
    url = device_url
    resp = client.get(url)
    assert resp.status_code == 405


def test_a_batched_request_is_refused_with_a_reason(client, device_url):
    """Batching was removed in 2025-06-18. A list body means a client on an older revision,
    and saying so beats a generic 422 from the JSON parser."""
    url = device_url
    resp = client.post(url, json=[_rpc(1, "tools/list")])
    assert resp.status_code == 400
    assert "batch" in resp.text.lower()


def test_an_unknown_device_is_404_before_any_session_work(client):
    resp = client.post(MCP.format(h="sh-nonexistent.local"), json=_rpc(1, "initialize"))
    assert resp.status_code == 404


# --- the same operation, so the same limits -----------------------------------


def _route_scopes(router, path, method):
    """The scope strings a route's dependencies actually close over.

    Asserting that *a* dependency is attached passes just as happily when the wrong one is
    attached, so this reads the scope out of the `require_scope` closure instead.

    Takes the **router module's** object rather than a built app, because ``app.routes`` is
    not a reliable place to look for an included route. FastAPI 0.137 stopped flattening
    ``include_router`` into the parent's route list and now keeps an ``_IncludedRouter``
    there instead: on 0.137.2 this app reports six routes — the four docs routes and two
    nameless ``_IncludedRouter`` objects — and none of the ``/v1`` paths. Two earlier
    versions of this test walked an app and failed for exactly that reason.

    (The failure looked CI-specific and was originally mis-diagnosed as import-time global
    state; it was a version skew. ``requirements.txt`` pins 0.137.2 and CI resolves the same
    from the pyproject range, so CI and the shipped image agreed — only a stale local venv
    on 0.136.3 still flattened, which is what made it look environmental.)

    Routing itself is unaffected; only introspection moved. A router's own ``.routes`` holds
    the declaration being asserted and is stable across both versions.
    """
    for route in router.routes:
        if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
            found = []
            for dep in route.dependant.dependencies:
                for cell in dep.call.__closure__ or ():
                    value = cell.cell_contents
                    if isinstance(value, str) and ":" in value:
                        found.append(value)
            return found
    known = [(sorted(r.methods), r.path) for r in router.routes]
    raise AssertionError(f"no route for {method} {path}; router has {known}")


def test_every_mcp_route_requires_tools_call():
    """No method on the device MCP endpoint is reachable without the tool-call scope. (Until
    0.4.0 this compared against the HTTP+SSE message route; that route is gone, so the scope
    is asserted directly.)"""
    from device_mcp_gateway.api import streamable as streamable_routes

    for method in ("POST", "GET", "DELETE"):
        scopes = _route_scopes(streamable_routes.router, "/devices/{hostname}/mcp", method)
        assert "tools:call" in scopes, f"{method} /mcp requires {scopes}"
