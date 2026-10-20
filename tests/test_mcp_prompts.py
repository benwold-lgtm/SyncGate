# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""prompts/list and prompts/get over MCP.

Prompts were generated from every spec, recorded in the manifest and listed by
GET /v1/devices/{h}/features, but the MCP router had no handler for them and initialize did
not advertise them — so an MCP client asking for a device's prompts got "method not found".
These tests pin the router half, including one built from a real translated spec so the
prompts the gateway actually generates are the ones exercised.
"""

import pytest

from device_mcp_gateway.core.translator import McpManifest, McpPrompt, SpecTranslator
from device_mcp_gateway.pods.device_pod import DevicePod


def _pod(prompts):
    manifest = McpManifest(server_name="mcp-dev", server_version="1", hostname="dev", prompts=prompts)
    return DevicePod(hostname="dev", manifest=manifest, transport="sse", base_url="http://dev.local")


def _rpc(i, method, params=None):
    return {"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params is not None else {})}


@pytest.mark.anyio
async def test_initialize_advertises_prompts():
    caps = (await _pod([])._handle_mcp_message(_rpc(1, "initialize", {})))["result"]["capabilities"]
    assert caps["prompts"] == {"listChanged": False}


@pytest.mark.anyio
async def test_prompts_list_describes_each_prompt_and_its_arguments():
    pod = _pod([McpPrompt(name="summarise", description="Summarise a reading", template="t", arguments=["unit"])])
    listed = (await pod._handle_mcp_message(_rpc(2, "prompts/list")))["result"]["prompts"]
    assert listed == [
        {"name": "summarise", "description": "Summarise a reading", "arguments": [{"name": "unit", "required": False}]}
    ]


@pytest.mark.anyio
async def test_a_pod_with_no_prompts_lists_none_rather_than_erroring():
    resp = await _pod([])._handle_mcp_message(_rpc(3, "prompts/list"))
    assert resp["result"] == {"prompts": []}


@pytest.mark.anyio
async def test_prompts_get_renders_declared_arguments_only():
    pod = _pod(
        [
            McpPrompt(
                name="summarise",
                description="Summarise a reading",
                # `{other}` is not a declared argument and `{}` is literal spec text: both stay.
                template="Report in {unit}. Ignore {other}. JSON looks like {}.",
                arguments=["unit"],
            )
        ]
    )
    resp = await pod._handle_mcp_message(_rpc(4, "prompts/get", {"name": "summarise", "arguments": {"unit": "°C"}}))
    assert resp["result"] == {
        "description": "Summarise a reading",
        "messages": [
            {"role": "user", "content": {"type": "text", "text": "Report in °C. Ignore {other}. JSON looks like {}."}}
        ],
    }


@pytest.mark.anyio
async def test_unknown_prompt_is_invalid_params():
    resp = await _pod([])._handle_mcp_message(_rpc(5, "prompts/get", {"name": "nope"}))
    assert resp["error"]["code"] == -32602 and "Unknown prompt" in resp["error"]["message"]


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [["unit"], {"unit": 3}, "unit"])
async def test_non_string_arguments_are_refused(bad):
    pod = _pod([McpPrompt(name="p", description="", template="t", arguments=["unit"])])
    resp = await pod._handle_mcp_message(_rpc(6, "prompts/get", {"name": "p", "arguments": bad}))
    assert resp["error"]["code"] == -32602


@pytest.mark.anyio
async def test_a_translated_specs_prompt_round_trips_through_list_and_get():
    """The prompt the translator generates from a real spec is listable and fetchable."""
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Sensor API", "version": "1.0.0", "description": "Reads temperature probes."},
        "paths": {"/items": {"get": {"operationId": "list_items", "responses": {"200": {"description": "ok"}}}}},
    }
    manifest = SpecTranslator().translate(spec, "sensor")
    pod = DevicePod(hostname="sensor", manifest=manifest, transport="sse", base_url="http://sensor.local")

    [listed] = (await pod._handle_mcp_message(_rpc(7, "prompts/list")))["result"]["prompts"]
    got = await pod._handle_mcp_message(_rpc(8, "prompts/get", {"name": listed["name"]}))
    text = got["result"]["messages"][0]["content"]["text"]
    assert "Sensor API" in text and "Reads temperature probes." in text and "list_items" in text
