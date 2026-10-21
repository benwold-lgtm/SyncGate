# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Phase 1 of the MCP-passthrough plan — the shared pod base.

``DevicePod`` used to be the only pod kind, so "the JSON-RPC protocol surface" and "how you
call an OpenAPI endpoint" were the same class. Passthrough adds a second kind, and the risk
of that split is not that the refactor breaks today's pod — the existing pod, adapter and
security suites cover that — but that the *second* kind quietly comes out weaker: no F-28
argument validation, a subtly different ``initialize``, a tools/call path that skips the
unknown-tool check.

So these tests pin the guarantees to ``BasePod`` itself, exercised through a deliberately
minimal subclass that implements nothing but ``_dispatch_tool_call``. Anything asserted here
holds for every pod kind by construction, including ones not written yet.
"""

from typing import Any

import pytest

from device_mcp_gateway.core.translator import McpManifest, McpResource, McpTool
from device_mcp_gateway.pods.device_pod import DevicePod
from device_mcp_gateway.pods.pod_base import BasePod

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _manifest() -> McpManifest:
    return McpManifest(
        server_name="mcp-test",
        server_version="1.0.0",
        hostname="test-device",
        tools=[
            McpTool(
                name="echo",
                description="Echo a value",
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                method="POST",
                path="/echo",
            )
        ],
        resources=[McpResource(uri="device://test-device/status", name="status", description="Device status")],
    )


class _MinimalPod(BasePod):
    """The least a pod kind can implement: dispatch, and nothing else.

    Stands in for a pod kind that is not OpenAPI-backed. If a protocol guarantee holds
    here, it holds because ``BasePod`` provides it — not because the subclass remembered.
    """

    def _build_dispatch(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def _dispatch_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        # Deliberately NOT the OpenAPI text-block shape — a proxied upstream returns MCP
        # content directly, and the base must hand it back untouched.
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


def _pod(**kw) -> _MinimalPod:
    return _MinimalPod(hostname="test-device", manifest=_manifest(), base_url="http://d.local", **kw)


# --- the seam itself ---------------------------------------------------------


def test_base_pod_cannot_be_instantiated_without_a_dispatch_implementation():
    """``_dispatch_tool_call`` is abstract on purpose: a pod kind that forgets it would
    otherwise inherit a router with nothing behind tools/call, failing only at call time."""
    with pytest.raises(TypeError):
        BasePod(hostname="test-device", manifest=_manifest())  # type: ignore[abstract]


async def test_dispatch_result_is_returned_verbatim_not_rewrapped():
    """The hook returns the finished JSON-RPC ``result``, so a proxy pod can pass upstream
    MCP content through unmodified. If the base re-wrapped it, every proxied tool result
    would arrive at the client double-encoded inside a text block."""
    pod = _pod()
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"value": "hi"}}}
    )
    assert resp["result"] == {"content": [{"type": "text", "text": "ok"}], "isError": False}
    assert pod.calls == [("echo", {"value": "hi"})]


async def test_device_pod_still_serialises_its_envelope_into_a_text_block():
    """Parity check for the OpenAPI kind: its result envelope is this gateway's own shape,
    not MCP content, so it stays JSON-encoded into one text block as before the split."""
    pod = DevicePod(hostname="test-device", manifest=_manifest(), base_url="http://d.local")

    async def fake_handler(**kwargs):
        return {"ok": True, "echoed": kwargs}

    pod._tool_dispatch["echo"] = fake_handler
    result = await pod._dispatch_tool_call("echo", {"value": "hi"})
    assert result["content"][0]["type"] == "text"
    assert '"ok": true' in result["content"][0]["text"]


# --- guarantees every pod kind inherits --------------------------------------


async def test_argument_validation_happens_in_the_base_before_dispatch():
    """F-28 belongs to the router, not to a pod kind. A subclass that never validates
    anything must still never see arguments that violate the tool's schema."""
    pod = _pod()
    resp = await pod.call_tool(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"value": 123}},  # wrong type
        }
    )
    assert resp["error"]["code"] == -32602
    assert pod.calls == [], "invalid arguments reached dispatch"


async def test_unknown_tool_is_rejected_before_dispatch():
    pod = _pod()
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
    )
    assert resp["error"]["code"] == -32601
    assert pod.calls == []


async def test_non_object_arguments_are_rejected_before_dispatch():
    pod = _pod()
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": ["not", "a dict"]}}
    )
    assert resp["error"]["code"] == -32602
    assert pod.calls == []


async def test_tool_schemas_are_derived_from_the_manifest_not_the_subclass():
    """The validation table is built in ``BasePod.__init__`` from the manifest, so it cannot
    fall out of step with what tools/list advertises."""
    pod = _pod()
    listed = await pod.call_tool({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert {t["name"] for t in listed["result"]["tools"]} == set(pod._tool_schemas)


async def test_read_only_protocol_methods_need_no_subclass_support():
    pod = _pod()
    init = await pod.call_tool({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "mcp-test"
    assert init["result"]["capabilities"]["tools"] == {"listChanged": False}

    assert (await pod.call_tool({"jsonrpc": "2.0", "id": 2, "method": "ping"}))["result"] == {}

    listed = await pod.call_tool({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert listed["result"]["resources"][0]["uri"] == "device://test-device/status"

    assert await pod.call_tool({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


async def test_unknown_method_errors_for_requests_and_stays_silent_for_notifications():
    pod = _pod()
    assert (await pod.call_tool({"jsonrpc": "2.0", "id": 9, "method": "no/such"}))["error"]["code"] == -32601
    assert await pod.call_tool({"jsonrpc": "2.0", "method": "no/such"}) is None


async def test_resources_read_defaults_to_unsupported_rather_than_reaching_the_network():
    """A pod kind that never overrides ``_read_resource`` must refuse the read, not inherit
    the OpenAPI path-append behaviour against an upstream that does not speak HTTP paths."""
    pod = _pod()
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "device://test-device/status"}}
    )
    assert resp["error"]["code"] == -32602


def test_egress_posture_is_configured_on_auth_by_the_base():
    """SSRF posture (F-02) is propagated to the auth handler in the base constructor, so an
    auth handler that makes its own outbound calls shares the pod's policy for every pod
    kind — not just the one that remembered to wire it."""

    class _Auth:
        configured: dict = {}

        def configure_egress(self, allow_private, allowed_ports):
            _Auth.configured = {"allow_private": allow_private, "allowed_ports": allowed_ports}

        def configure_credentials(self, resolver):
            # ADR-0018 wire-up seam. Present because the base calls it unconditionally, the
            # same way it calls configure_egress — a double missing it is a double that has
            # drifted from the interface it stands in for.
            pass

    _MinimalPod(
        hostname="test-device",
        manifest=_manifest(),
        auth=_Auth(),  # type: ignore[arg-type]
        allow_private=True,
        allowed_ports={8443},
    )
    assert _Auth.configured == {"allow_private": True, "allowed_ports": {8443}}


# --- Phase 3 forcing function -------------------------------------------------


def test_device_pod_and_proxy_pod_expose_the_same_protocol_surface():
    """Every caller — PodSupervisor, the worker's dispatch loop, the diagnostics endpoint —
    holds a pod through this surface alone. If either pod kind is missing any of it, those
    call sites need per-kind branches, which is exactly what the base class exists to avoid.
    """
    from device_mcp_gateway.pods.mcp_proxy_pod import McpProxyPod  # noqa: F401

    surface = {n for n in vars(BasePod) if not n.startswith("_")}
    assert surface <= {n for n in dir(McpProxyPod) if not n.startswith("_")}
    assert surface <= {n for n in dir(DevicePod) if not n.startswith("_")}
