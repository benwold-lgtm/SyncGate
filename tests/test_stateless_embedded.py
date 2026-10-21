# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""The stateless era (2026-07-28) on a device endpoint, embedded mode — Workstream B PR 4.

Against a real gateway process and the mock device, with raw HTTP so every header and status
is the test's own choice. `test_interop_eras.py` shows the official SDK client works; this
pins the parts a conforming client never exercises — the refusals — and checks every
successful result against the SDK's 2026-07-28 wire models, which a client that tolerates
extra or missing fields would not.
"""

from __future__ import annotations

import time

import pytest

from device_mcp_gateway.core import protocol

MODERN = protocol.MODERN_PROTOCOL_VERSIONS[0]
HOST = "stateless-sensor.local"
URL = f"/v1/devices/{HOST}/mcp"


@pytest.fixture(scope="module")
def device(client, mock_target_url):
    resp = client.post("/v1/devices", json={"hostname": HOST, "base_url": mock_target_url, "auth_type": "none"})
    assert resp.status_code == 200, resp.text
    deadline = time.time() + 10
    while not client.get(f"/v1/devices/{HOST}").json().get("pod_active"):
        assert time.time() < deadline, "pod never became active"
        time.sleep(0.2)
    yield
    client.delete(f"/v1/devices/{HOST}")


def _meta(version=MODERN):
    return {protocol.PROTOCOL_VERSION_META_KEY: version, protocol.CLIENT_CAPABILITIES_META_KEY: {}}


def _post(client, method, params=None, *, msg_id=1, headers=None, version=MODERN, name=None):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": {"_meta": _meta(version), **(params or {})}}
    if msg_id is None:
        del body["id"]
    h = {"MCP-Protocol-Version": version, "Mcp-Method": method, "Accept": "application/json, text/event-stream"}
    if name is not None:
        h["Mcp-Name"] = name
    h.update(headers or {})
    return client.post(URL, json=body, headers={k: v for k, v in h.items() if v is not None})


# --- what a modern request gets ---------------------------------------------------------------


def test_discover_names_the_modern_versions_and_the_devices_capabilities(client, device):
    resp = _post(client, "server/discover")
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["supportedVersions"] == [MODERN]
    assert set(result["capabilities"]) == {"tools", "resources", "prompts"}
    assert result["resultType"] == "complete" and result["cacheScope"] == "private" and result["ttlMs"] > 0


def test_no_session_is_ever_minted(client, device):
    for method in ("server/discover", "tools/list"):
        resp = _post(client, method)
        assert resp.status_code == 200
        assert "mcp-session-id" not in {k.lower() for k in resp.headers}


def test_a_tool_call_needs_no_handshake_and_returns_the_devices_answer(client, device):
    resp = _post(client, "tools/call", {"name": "get_device_status", "arguments": {}}, name="get_device_status")
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert '"online"' in result["content"][0]["text"] and result["resultType"] == "complete"


def test_the_clients_id_is_echoed_and_used_for_nothing_else(client, device):
    """ADR-0027 §2. Two requests with the same id are two requests."""
    a = _post(client, "tools/list", msg_id=1).json()
    b = _post(client, "tools/call", {"name": "get_device_status", "arguments": {}}, msg_id=1, name="get_device_status")
    assert a["id"] == b.json()["id"] == 1
    assert "tools" in a["result"] and "content" in b.json()["result"]


mcp_types = pytest.importorskip("mcp_types", reason="the SDK's wire types come with the dev extra")


@pytest.mark.parametrize(
    "method,params,name",
    [
        ("server/discover", None, None),
        ("tools/list", None, None),
        ("tools/call", {"name": "get_device_status", "arguments": {}}, "get_device_status"),
        ("resources/list", None, None),
        ("resources/read", {"uri": f"device://{HOST}/status"}, f"device://{HOST}/status"),
        ("prompts/list", None, None),
    ],
)
def test_every_result_is_valid_and_complete_at_2026_07_28(client, device, method, params, name):
    """Against the SDK's wire model for this revision, and nothing dropped by it (trap 1, trap 4)."""
    from mcp_types import methods as sdk_methods

    resp = _post(client, method, params, name=name)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["resultType"] == "complete"  # trap 4: required on every result
    assert result["_meta"][protocol.SERVER_INFO_META_KEY]["name"]
    assert sdk_methods.serialize_server_result(method, MODERN, result) == result


# --- refusals: the ladder, in the SDK's order -------------------------------------------------


def _error(resp):
    return resp.status_code, resp.json()["error"]["code"]


def test_a_missing_envelope_is_invalid_params(client, device):
    resp = client.post(
        URL,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"},
    )
    assert _error(resp) == (400, protocol.INVALID_PARAMS)
    assert protocol.CLIENT_CAPABILITIES_META_KEY in resp.json()["error"]["message"]


def test_an_envelope_without_its_header_is_a_header_mismatch(client, device):
    """Routed modern by the body alone, then told which header is missing."""
    resp = _post(client, "tools/list", headers={"MCP-Protocol-Version": None})
    assert _error(resp) == (400, protocol.HEADER_MISMATCH)


def test_a_client_that_disagrees_with_itself_is_told_so(client, device):
    assert _error(_post(client, "tools/list", headers={"Mcp-Method": "tools/call"})) == (400, protocol.HEADER_MISMATCH)
    resp = _post(client, "tools/call", {"name": "get_device_status", "arguments": {}}, name="control_fan")
    assert _error(resp) == (400, protocol.HEADER_MISMATCH)


def test_the_name_header_may_be_base64_wrapped(client, device):
    import base64

    wrapped = "=?base64?" + base64.b64encode(b"get_device_status").decode() + "?="
    resp = _post(client, "tools/call", {"name": "get_device_status", "arguments": {}}, name=wrapped)
    assert resp.status_code == 200, resp.text


def test_an_unknown_modern_version_names_what_is_served(client, device):
    resp = _post(client, "tools/list", version="2099-01-01")
    assert _error(resp) == (400, protocol.UNSUPPORTED_PROTOCOL_VERSION)
    assert resp.json()["error"]["data"] == {"supported": [MODERN], "requested": "2099-01-01"}


@pytest.mark.parametrize("method", ["initialize", "ping", "logging/setLevel", "resources/subscribe", "no/such"])
def test_a_method_this_era_does_not_have_is_not_found(client, device, method):
    assert _error(_post(client, method)) == (404, protocol.METHOD_NOT_FOUND)


def test_an_unknown_tool_is_invalid_params_not_a_404(client, device):
    """-32601 is a 404 in this era, which a client may read as "no tools/call here"."""
    resp = _post(client, "tools/call", {"name": "nope", "arguments": {}}, name="nope")
    assert _error(resp) == (400, protocol.INVALID_PARAMS)


def test_a_notification_is_acknowledged_and_dropped(client, device):
    resp = _post(client, "notifications/cancelled", {"requestId": 1}, msg_id=None)
    assert resp.status_code == 202 and resp.content == b""


def test_a_notification_at_an_unserved_version_is_refused(client, device):
    resp = _post(client, "notifications/cancelled", {"requestId": 1}, msg_id=None, version="2099-01-01")
    assert _error(resp) == (400, protocol.UNSUPPORTED_PROTOCOL_VERSION)


# --- the handshake era is untouched -----------------------------------------------------------


def test_a_handshake_client_on_the_same_url_still_gets_a_session(client, device):
    resp = client.post(
        URL,
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    )
    assert resp.status_code == 200 and resp.headers.get("Mcp-Session-Id")
    assert "resultType" not in resp.json()["result"]  # a handshake-era result is not restamped
