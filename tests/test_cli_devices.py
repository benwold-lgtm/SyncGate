# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""``device-mcp devices list|show`` — the CLI half of device inspection.

The main tests drive the CLI against the **real gateway app** in-process (the TestClient's
transport), not a stub: the requirement is that the CLI and the console read one backend,
so the test that matters is the CLI agreeing with the very endpoints the console reads. The
error paths use a mock transport because they are about how the CLI reports a refusal.
"""

import asyncio
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import device_mcp_gateway.cli as cli
import device_mcp_gateway.main as gw_main
from device_mcp_gateway.cli_devices import devices_main
from device_mcp_gateway.core.translator import (
    McpManifest,
    McpPrompt,
    McpResource,
    McpTool,
    manifest_to_dict,
)

client = TestClient(gw_main.app)
URL = ["--url", "http://testserver"]


def run(*argv: str, transport: httpx.BaseTransport | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    http = httpx.Client(transport=transport) if transport else client
    code = devices_main([*URL, *argv], out=out, err=err, client=http)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module", autouse=True)
def fleet():
    """One OpenAPI device with a cached manifest, one proxied MCP device, one never discovered."""
    devices = {
        "cli-sensor": {},
        "cli-proxy": {"upstream_kind": "mcp"},
        "cli-undiscovered": {},
    }
    for h, extra in devices.items():
        r = client.post(
            "/v1/devices", json={"hostname": h, "base_url": "http://192.0.2.20", "auth_type": "none", **extra}
        )
        assert r.status_code == 200, r.text
    backend = gw_main.app.state.registry._backend
    asyncio.run(
        backend.set_manifest(
            "cli-sensor",
            manifest_to_dict(
                McpManifest(
                    server_name="mcp-cli-sensor",
                    server_version="1.2.0",
                    hostname="cli-sensor",
                    metadata={"title": "Sensor API"},
                    tools=[
                        McpTool(
                            name="get_item",
                            description="Get one item",
                            schema={"type": "object", "properties": {"item_id": {"type": "integer"}}},
                            method="GET",
                            path="/items/{item_id}",
                        )
                    ],
                    resources=[McpResource(uri="device://cli-sensor/status", name="status", description="Status")],
                    prompts=[McpPrompt(name="what_is_sensor", description="Describe it", template="t")],
                )
            ),
            ttl=300,
        )
    )
    asyncio.run(
        backend.set_manifest(
            "cli-proxy",
            manifest_to_dict(
                McpManifest(
                    server_name="mcp-cli-proxy",
                    server_version="1.0.0",
                    hostname="cli-proxy",
                    tools=[McpTool(name="echo_message", description="Echo", schema={"type": "object"}, source="proxy")],
                )
            ),
            ttl=300,
        )
    )
    yield
    for h in devices:
        client.delete(f"/v1/devices/{h}")


def test_list_shows_every_device_of_both_kinds():
    code, out, err = run("list")
    assert code == 0, err
    lines = {line.split()[0]: line.split() for line in out.splitlines() if line.startswith("cli-")}
    assert set(lines) == {"cli-proxy", "cli-sensor", "cli-undiscovered"}
    assert lines["cli-sensor"][1] == "openapi" and lines["cli-proxy"][1] == "mcp"
    assert lines["cli-sensor"][3] == "inactive"
    assert "openapi" in out and "mcp" in out and "device(s)" in out


def test_list_json_is_exactly_what_the_console_reads():
    """The shared-backend property, pinned: the CLI's list is the console's overview."""
    code, out, _ = run("--json", "list")
    assert code == 0
    cli_devices = {d["hostname"]: d for d in json.loads(out)["devices"]}
    console_devices = {d["hostname"]: d for d in client.get("/v1/admin/overview").json()["devices"]}
    assert cli_devices == console_devices


def test_show_openapi_device_lists_tools_resources_and_prompts():
    code, out, err = run("show", "cli-sensor")
    assert code == 0, err
    assert "cli-sensor  (openapi" in out
    assert "Sensor API 1.2.0" in out
    assert "Tools (1)" in out and "get_item" in out and "GET /items/{item_id}" in out
    assert "Resources (1)" in out and "device://cli-sensor/status" in out
    assert "Prompts (1)" in out and "what_is_sensor" in out
    assert "input schema" not in out  # only with --schemas


def test_show_schemas_prints_each_input_schema():
    code, out, _ = run("show", "cli-sensor", "--schemas")
    assert code == 0
    assert "get_item input schema:" in out and '"item_id"' in out


def test_show_proxied_device_says_resources_and_prompts_were_not_discovered():
    code, out, _ = run("show", "cli-proxy")
    assert code == 0
    assert "(mcp" in out and "(proxied)" in out and "echo_message" in out
    assert "Server" not in out  # no serverInfo recorded — no invented name either
    assert "Resources  not discovered for a proxied MCP server" in out
    assert "Prompts    not discovered for a proxied MCP server" in out


def test_show_undiscovered_device_says_unknown_not_empty():
    code, out, _ = run("show", "cli-undiscovered")
    assert code == 0
    assert "No manifest cached" in out and "unknown, not empty" in out
    assert "Tools (" not in out


def test_show_json_matches_the_features_endpoint():
    code, out, _ = run("--json", "show", "cli-sensor")
    assert code == 0
    assert json.loads(out)["features"] == client.get("/v1/devices/cli-sensor/features").json()


def test_show_unregistered_device_fails_with_a_clear_message():
    code, out, err = run("show", "no-such-device")
    assert code == 1 and out == ""
    assert "device 'no-such-device' is not registered" in err


# --- error reporting, against a mock transport --------------------------------------------


def _mock(status: int, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.headers.get("authorization"))
        return httpx.Response(status, json={"detail": "nope"})

    return httpx.MockTransport(handler)


def test_unauthorized_names_the_fix():
    code, _, err = run("list", transport=_mock(401))
    assert code == 1 and "MCP_GATEWAY_TOKEN" in err


def test_forbidden_names_the_missing_scope():
    code, _, err = run("list", transport=_mock(403))
    assert code == 1 and "devices:read" in err


def test_unreachable_gateway_names_the_url():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    code, _, err = run("list", transport=httpx.MockTransport(refuse))
    assert code == 1 and "cannot reach the gateway at http://testserver" in err


def test_token_file_trailing_newline_is_not_sent(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_GATEWAY_TOKEN", raising=False)
    token = tmp_path / "token"
    token.write_text("s3cret\n")
    seen: list = []
    run("--token-file", str(token), "list", transport=_mock(200, seen))
    assert seen == ["Bearer s3cret"]


def test_token_from_environment(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_TOKEN", "from-env")
    seen: list = []
    run("list", transport=_mock(200, seen))
    assert seen == ["Bearer from-env"]


def test_usage_error_exits_2():
    with pytest.raises(SystemExit) as exc:
        devices_main(["show"])  # missing hostname
    assert exc.value.code == 2


def test_device_mcp_routes_devices_without_touching_the_server(monkeypatch):
    called: dict = {}

    def fake_devices_main(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr("device_mcp_gateway.cli_devices.devices_main", fake_devices_main)
    monkeypatch.setattr("sys.argv", ["device-mcp", "devices", "show", "x"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0 and called["argv"] == ["show", "x"]
