# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""The protocol facts in ``core/protocol.py``, checked against an implementation nobody here wrote.

Advertising a revision is a conformance claim: ``initialize`` echoes a supported version back,
so a client asking for ``2025-11-25`` is told the gateway speaks it. So every result shape a pod
produces is validated against the official SDK's wire models for each handshake revision, and
must come back **unchanged** — the models ignore unknown fields, so validating alone would let a
field the revision does not define pass silently (roadmap trap 1).
"""

from __future__ import annotations

import httpx
import pytest

from device_mcp_gateway.core import protocol
from device_mcp_gateway.core.translator import SpecTranslator
from device_mcp_gateway.fleet_service import fleet_initialize_response
from device_mcp_gateway.pods.device_pod import DevicePod

mcp_types = pytest.importorskip("mcp_types", reason="the SDK's wire types come with the dev extra")
from mcp_types import methods as sdk_methods  # noqa: E402
from mcp_types import version as sdk_version  # noqa: E402

SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Sensor API", "version": "1.0.0", "description": "Reads probes."},
    "paths": {
        "/items/{item_id}": {
            "get": {
                "operationId": "get_item",
                "parameters": [{"name": "item_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/status": {"get": {"operationId": "status", "responses": {"200": {"description": "ok"}}}},
    },
}


def _pod() -> DevicePod:
    manifest = SpecTranslator().translate(SPEC, "sensor")
    pod = DevicePod(hostname="sensor", manifest=manifest, base_url="http://sensor.local")
    pod._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
    return pod


def _requests(pod: DevicePod) -> list[dict]:
    return [
        {"method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"method": "ping"},
        {"method": "tools/list"},
        {"method": "tools/call", "params": {"name": "status", "arguments": {}}},
        {"method": "resources/list"},
        {"method": "resources/read", "params": {"uri": "device://sensor/status"}},
        {"method": "prompts/list"},
        {"method": "prompts/get", "params": {"name": pod.manifest.prompts[0].name}},
    ]


# --- the version lists ----------------------------------------------------------------------


def test_every_advertised_revision_is_one_the_sdk_knows_in_the_same_era():
    assert set(protocol.HANDSHAKE_PROTOCOL_VERSIONS) <= set(sdk_version.HANDSHAKE_PROTOCOL_VERSIONS)
    assert set(protocol.MODERN_PROTOCOL_VERSIONS) <= set(sdk_version.MODERN_PROTOCOL_VERSIONS)


def test_the_preferred_handshake_version_is_what_an_sdk_default_client_offers():
    """So the common client negotiates without a fallback round trip (roadmap decision 3)."""
    assert protocol.PREFERRED_HANDSHAKE_VERSION == sdk_version.LATEST_HANDSHAKE_VERSION


@pytest.mark.parametrize("dropped", ["2025-03-26", "2024-11-05"])
def test_untested_revisions_are_no_longer_echoed(dropped):
    assert protocol.negotiate_protocol_version(dropped) == protocol.PREFERRED_HANDSHAKE_VERSION


def test_a_modern_revision_is_never_negotiated_on_the_handshake():
    """2026-07-28 has no initialize; asking for it there is asking for something that does not exist."""
    assert protocol.negotiate_protocol_version("2026-07-28") == protocol.PREFERRED_HANDSHAKE_VERSION


# --- conformance ----------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("version", protocol.HANDSHAKE_PROTOCOL_VERSIONS)
async def test_every_pod_result_is_valid_and_complete_at_each_handshake_revision(version):
    pod = _pod()
    for i, req in enumerate(_requests(pod)):
        resp = await pod._handle_mcp_message({"jsonrpc": "2.0", "id": i, **req})
        assert "result" in resp, (req["method"], resp)
        sieved = sdk_methods.serialize_server_result(req["method"], version, resp["result"])
        assert sieved == resp["result"], f"{req['method']} at {version}: the SDK model dropped or changed a field"


@pytest.mark.parametrize("version", protocol.HANDSHAKE_PROTOCOL_VERSIONS)
def test_the_fleet_initialize_is_valid_at_each_handshake_revision(version):
    result = fleet_initialize_response(1, {"protocolVersion": version})["result"]
    assert result["protocolVersion"] == version
    assert sdk_methods.serialize_server_result("initialize", version, result) == result


# --- one builder ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_and_fleet_initialize_differ_only_in_identity_and_capabilities():
    device = (await _pod()._handle_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"}))["result"]
    fleet = fleet_initialize_response(1, None)["result"]
    assert device["protocolVersion"] == fleet["protocolVersion"] == protocol.PREFERRED_HANDSHAKE_VERSION
    assert set(device) == set(fleet) == {"protocolVersion", "capabilities", "serverInfo"}
    assert fleet["capabilities"] == {"tools": {"listChanged": False}}  # a fleet serves tools only (ADR-0008)
    assert set(device["capabilities"]) == {"tools", "resources", "prompts"}


def test_a_caller_editing_its_result_cannot_change_the_next_one():
    first = fleet_initialize_response(1, None)
    first["result"]["capabilities"]["tools"]["listChanged"] = True
    assert fleet_initialize_response(2, None)["result"]["capabilities"]["tools"] == {"listChanged": False}
