# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""GET /v1/devices/{hostname}/features — tools, resources and prompts for inspection.

The UI and the ``device-mcp devices`` CLI both read this endpoint. Two properties matter
beyond the shape: it answers for a device with no running pod (``/tools`` refuses with
409, which is exactly when an operator opens a device to see what is wrong), and it
distinguishes "none found" from "not looked for" via ``discovered``.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import device_mcp_gateway.main as gw_main
from device_mcp_gateway.core.translator import (
    McpManifest,
    McpPrompt,
    McpResource,
    McpTool,
    manifest_to_dict,
)

client = TestClient(gw_main.app)


def _register(hostname: str, **extra) -> None:
    # TEST-NET-1 never answers, so the device registers with no pod — the down-device case.
    body = {"hostname": hostname, "base_url": "http://192.0.2.10", "auth_type": "none", **extra}
    r = client.post("/v1/devices", json=body)
    assert r.status_code == 200, r.text
    assert client.get(f"/v1/devices/{hostname}").json()["pod_active"] is False


def _cache_manifest(hostname: str, manifest: McpManifest) -> None:
    backend = gw_main.app.state.registry._backend
    asyncio.run(backend.set_manifest(hostname, manifest_to_dict(manifest), ttl=300))


@pytest.fixture
def device():
    created: list[str] = []

    def make(hostname: str, **extra) -> str:
        _register(hostname, **extra)
        created.append(hostname)
        return hostname

    yield make
    for h in created:
        client.delete(f"/v1/devices/{h}")


def test_unknown_device_is_404():
    assert client.get("/v1/devices/no-such-device/features").status_code == 404


def test_no_manifest_says_so_instead_of_reporting_nothing(device):
    h = device("feat-nomanifest")
    body = client.get(f"/v1/devices/{h}/features").json()
    assert body["manifest_cached"] is False
    assert body["tools"] == body["resources"] == body["prompts"] == []
    assert body["server"] is None
    assert body["discovered"] == {"tools": True, "resources": True, "prompts": True}


def test_openapi_device_reports_tools_resources_and_prompts_with_no_pod(device):
    h = device("feat-openapi")
    _cache_manifest(
        h,
        McpManifest(
            # The translator's real shape: the gateway's own label as server_name, the
            # spec's identity in metadata. /features must report the latter.
            server_name=f"mcp-{h}",
            server_version="1.2.0",
            hostname=h,
            metadata={"title": "Sensor API", "description": "", "openapi_version": "3.1.0"},
            tools=[
                McpTool(
                    name="get_item",
                    description="Get one item",
                    schema={"type": "object", "properties": {"item_id": {"type": "integer"}}},
                    method="GET",
                    path="/items/{item_id}",
                )
            ],
            resources=[
                McpResource(uri=f"device://{h}/status", name="status", description="Device status"),
                McpResource(uri=f"device://{h}/items", name="items", description="All items"),
            ],
            prompts=[McpPrompt(name="what_is_sensor", description="Describe the sensor", template="You are…")],
        ),
    )
    # The point of the endpoint: /tools refuses this device, /features does not.
    assert client.get(f"/v1/devices/{h}/tools").status_code == 409
    r = client.get(f"/v1/devices/{h}/features")
    assert r.status_code == 200
    body = r.json()
    assert body["manifest_cached"] is True and body["pod_active"] is False
    assert body["upstream_kind"] == "openapi"
    assert body["server"] == {"name": "Sensor API", "version": "1.2.0"}
    [tool] = body["tools"]
    assert tool["name"] == "get_item" and tool["method"] == "GET" and tool["path"] == "/items/{item_id}"
    assert tool["schema"]["properties"]["item_id"]["type"] == "integer"  # same key as /tools
    assert tool["source"] == "openapi"
    assert [res["name"] for res in body["resources"]] == ["status", "items"]
    assert body["resources"][0]["uri"] == f"device://{h}/status"
    assert body["resources"][0]["mime_type"] == "application/json"
    assert body["prompts"] == [
        {
            "name": "what_is_sensor",
            "description": "Describe the sensor",
            "template": "You are…",
            "arguments": [],
            "required_arguments": [],
        }
    ]


def test_proxied_mcp_device_says_resources_and_prompts_were_not_looked_for(device):
    h = device("feat-mcp", upstream_kind="mcp")
    _cache_manifest(
        h,
        McpManifest(
            # mcp_discovery's real shape: gateway label and a placeholder version.
            server_name=f"mcp-{h}",
            server_version="1.0.0",
            hostname=h,
            tools=[McpTool(name="echo_message", description="Echo", schema={"type": "object"}, source="proxy")],
            metadata={"upstream_kind": "mcp"},
        ),
    )
    body = client.get(f"/v1/devices/{h}/features").json()
    # No serverInfo recorded yet: say nothing rather than the gateway's placeholder.
    assert body["server"] is None
    assert body["upstream_kind"] == "mcp"
    assert body["tools"][0]["source"] == "proxy"
    assert body["tools"][0]["method"] == "" and body["tools"][0]["path"] == ""
    # Empty lists here mean "not discovered", which `discovered` must say explicitly.
    assert body["resources"] == [] and body["prompts"] == []
    assert body["discovered"] == {"tools": True, "resources": False, "prompts": False}


def test_proxied_mcp_device_reports_the_upstreams_own_server_info_once_recorded(device):
    h = device("feat-mcp-declared", upstream_kind="mcp")
    _cache_manifest(h, McpManifest(server_name=f"mcp-{h}", server_version="1.0.0", hostname=h, tools=[]))
    backend = gw_main.app.state.registry._backend
    asyncio.run(backend.update_device_fields(h, declared_name="reference-upstream-sdk2", declared_version="2.3.0"))
    body = client.get(f"/v1/devices/{h}/features").json()
    assert body["server"] == {"name": "reference-upstream-sdk2", "version": "2.3.0"}


def test_proxied_mcp_device_reports_what_discovery_recorded(device):
    """ADR-0028: discovered comes from the manifest's own record, per kind."""
    h = device("feat-mcp-adr28", upstream_kind="mcp")
    _cache_manifest(
        h,
        McpManifest(
            server_name=f"mcp-{h}",
            server_version="1.0.0",
            hostname=h,
            resources=[McpResource(uri="ref://status", name="status", description="Status")],
            metadata={"upstream_kind": "mcp", "discovered": {"resources": True, "prompts": False}},
        ),
    )
    body = client.get(f"/v1/devices/{h}/features").json()
    assert body["discovered"] == {"tools": True, "resources": True, "prompts": False}
    assert body["resources"][0]["uri"] == "ref://status"


def test_a_proxied_prompt_reports_which_arguments_are_required(device):
    """What an MCP client sees as `required: true` on prompts/list, inspection reports too."""
    h = device("feat-mcp-prompt-args", upstream_kind="mcp")
    _cache_manifest(
        h,
        McpManifest(
            server_name=f"mcp-{h}",
            server_version="1.0.0",
            hostname=h,
            prompts=[
                McpPrompt(
                    name="summarise_reading",
                    description="Summarise",
                    template="Summarise in {unit}.",
                    arguments=["unit", "detail"],
                    required_arguments=["unit"],
                    upstream_name="Summarise_Reading",
                )
            ],
            metadata={"upstream_kind": "mcp", "discovered": {"resources": True, "prompts": True}},
        ),
    )
    [prompt] = client.get(f"/v1/devices/{h}/features").json()["prompts"]
    assert prompt["arguments"] == ["unit", "detail"] and prompt["required_arguments"] == ["unit"]
    assert "upstream_name" not in prompt  # the wire name is the gateway's business, not the reader's
