# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Phase 3 of the MCP-passthrough plan — proxying a remote MCP server.

The gateway's Tier-0 protections were built around the OpenAPI path, and several of them
live *inside the translator*. A proxied upstream never goes through the translator, so
those protections do not come along for free — they have to be re-applied deliberately, and
that is most of what this file tests:

- **F-26 text sanitisation.** Upstream tool names and descriptions are LLM-facing and
  attacker-controlled. A remote MCP server is a *less* trustworthy source than an OpenAPI
  document, not more.
- **F-09 count/size caps.** ``tools/list`` is an unbounded response from a third party.
- **F-25 header injection.** Outbound headers are built solely from ``auth.apply()`` plus
  fixed protocol headers, so no tool argument can reach the wire as a header.
- **F-02 SSRF.** The proxy path uses the same guarded client, so a rebind to an internal
  address is caught at dispatch, and redirects are never followed.

Two behaviours here are subtle enough to be worth stating outright:

1. **A JSON-RPC error in a result is not a transport failure.** It is the upstream's
   tool-level "no" — the analogue of a 4xx, which does not trip the breaker today. Only
   transport 5xx and connection failures do. Getting this wrong means one badly-behaved
   tool opens the breaker for every tool on that upstream.
2. **``tools/list`` ordering is server-controlled**, so the change-detection hash must be
   over a canonical projection. Hashing the raw response means every poll looks like a
   change: pod replaced every cycle, ``tools_revision`` inflated, and a continuous stream
   of breaking-change alerts across the fleet.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from device_mcp_gateway.core.translator import McpManifest, McpTool, ProxyToolSpec
from device_mcp_gateway.pods.mcp_proxy_pod import McpProxyPod
from device_mcp_gateway.upstream.mcp_client import McpUpstreamError, StreamableHttpClient
from device_mcp_gateway.security.mtls import TlsProfiles
from device_mcp_gateway.upstream.mcp_discovery import (
    build_proxy_manifest,
    canonical_tools_hash,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- helpers -----------------------------------------------------------------


def _rpc_result(result: dict, *, req_id=1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _stub_upstream(handler) -> StreamableHttpClient:
    """A client whose transport is a MockTransport, so no socket is opened."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return StreamableHttpClient(url="http://remote.local/mcp", get_client=lambda: client)


def _tool(name="echo", upstream_name=None, **kw) -> McpTool:
    return McpTool(
        name=name,
        description=kw.pop("description", "Echo"),
        schema=kw.pop("schema", {"type": "object", "properties": {}}),
        source="proxy",
        proxy=ProxyToolSpec(upstream_tool_name=upstream_name or name),
        **kw,
    )


def _manifest(*tools: McpTool) -> McpManifest:
    return McpManifest(
        server_name="mcp-remote", server_version="1.0.0", hostname="remote", tools=list(tools or (_tool(),))
    )


def _pod(handler, **kw) -> McpProxyPod:
    pod = McpProxyPod(hostname="remote", manifest=_manifest(*kw.pop("tools", ())), base_url="http://remote.local/mcp")
    pod._upstream = _stub_upstream(handler)
    return pod


# --- the manifest carries a discriminator, not a second type ------------------


def test_a_proxied_tool_is_discriminated_by_source_not_by_an_empty_method():
    """Readers must branch on ``source``. Keying off ``method == ""`` would silently
    reclassify any OpenAPI tool whose method failed to round-trip — which is exactly the
    Phase 4 idempotency trap."""
    t = _tool()
    assert t.source == "proxy"
    assert t.proxy is not None and t.proxy.upstream_tool_name == "echo"
    assert McpTool(name="x", description="", schema={}, method="GET", path="/x").source == "openapi"


def test_upstream_tool_names_are_sanitised_while_the_wire_name_round_trips():
    """The LLM-facing name is sanitised and deduped (F-04/F-26); the name actually sent
    upstream must survive verbatim, or the call goes to the wrong tool — or nowhere."""
    manifest = build_proxy_manifest(
        "remote",
        [
            {"name": "Get Weather", "description": "d", "inputSchema": {"type": "object"}},
            {"name": "get/weather", "description": "d", "inputSchema": {"type": "object"}},
        ],
    )
    names = [t.name for t in manifest.tools]
    assert names == ["get_weather", "get_weather_2"], names
    assert [t.proxy.upstream_tool_name for t in manifest.tools] == ["Get Weather", "get/weather"]


def test_f26_sanitisation_is_applied_to_upstream_names_and_descriptions():
    """Lives in the translator, which the proxy path bypasses. A remote MCP server is a
    less trustworthy source of LLM-facing text than an OpenAPI document, not more."""
    manifest = build_proxy_manifest(
        "remote",
        [
            {
                "name": "list‮data",  # bidi override, used to disguise text
                "description": "Safe​ description\x07 with⁦ hidden marks",
                "inputSchema": {"type": "object"},
            }
        ],
    )
    tool = manifest.tools[0]
    assert "‮" not in tool.name
    for ch in ("​", "\x07", "⁦"):
        assert ch not in tool.description


def test_f09_caps_the_number_of_tools_an_upstream_can_publish():
    """``tools/list`` is an unbounded response from a third party."""
    from device_mcp_gateway.core.spec_limits import SpecTooLargeError

    many = [{"name": f"t{i}", "description": "d", "inputSchema": {"type": "object"}} for i in range(50)]
    with pytest.raises(SpecTooLargeError):
        build_proxy_manifest("remote", many, max_tools=10)


def test_a_tool_with_no_usable_name_is_dropped_rather_than_named_after_nothing():
    manifest = build_proxy_manifest(
        "remote",
        [
            {"name": "///", "description": "d", "inputSchema": {"type": "object"}},
            {"name": "ok", "description": "d", "inputSchema": {"type": "object"}},
        ],
    )
    assert [t.name for t in manifest.tools] == ["ok"]


def test_a_non_object_input_schema_is_replaced_rather_than_trusted():
    """The schema is handed to jsonschema for F-28 validation and to the LLM as the tool
    contract. A non-object schema from an upstream must not become either."""
    manifest = build_proxy_manifest("remote", [{"name": "t", "description": "d", "inputSchema": "not-a-schema"}])
    assert manifest.tools[0].schema == {"type": "object"}


# --- change detection ---------------------------------------------------------


def test_reordering_tools_is_not_a_change():
    """``tools/list`` ordering is server-controlled. Hashing the raw response makes every
    poll look like a change — pod replaced every cycle, tools_revision inflated, and a
    continuous breaking-change alert storm across the whole fleet."""
    a = [
        {"name": "alpha", "description": "d1", "inputSchema": {"type": "object"}},
        {"name": "beta", "description": "d2", "inputSchema": {"type": "object"}},
    ]
    assert canonical_tools_hash(a) == canonical_tools_hash(list(reversed(a)))


def test_key_order_within_a_tool_is_not_a_change():
    a = [{"name": "t", "description": "d", "inputSchema": {"type": "object", "title": "T"}}]
    b = [{"inputSchema": {"title": "T", "type": "object"}, "description": "d", "name": "t"}]
    assert canonical_tools_hash(a) == canonical_tools_hash(b)


def test_a_changed_description_is_a_change():
    """Descriptions are LLM-facing instructions, so a silent rewrite is the rug-pull the
    change-governance machinery exists to surface."""
    a = [{"name": "t", "description": "old", "inputSchema": {"type": "object"}}]
    b = [{"name": "t", "description": "new", "inputSchema": {"type": "object"}}]
    assert canonical_tools_hash(a) != canonical_tools_hash(b)


def test_a_removed_tool_is_a_change():
    a = [
        {"name": "t", "description": "d", "inputSchema": {"type": "object"}},
        {"name": "u", "description": "d", "inputSchema": {"type": "object"}},
    ]
    assert canonical_tools_hash(a) != canonical_tools_hash(a[:1])


def test_ignored_fields_do_not_produce_phantom_changes():
    """Only the fields that define the tool contract participate. An upstream that stamps a
    timestamp or request id into each entry must not read as a change every poll."""
    a = [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]
    b = [{"name": "t", "description": "d", "inputSchema": {"type": "object"}, "_served_at": "2026-08-05T00:00:00Z"}]
    assert canonical_tools_hash(a) == canonical_tools_hash(b)


# --- the upstream client ------------------------------------------------------


async def test_a_json_body_response_is_parsed():
    async def handler(request):
        return httpx.Response(200, json=_rpc_result({"tools": []}))

    assert await _stub_upstream(handler).request("tools/list") == {"tools": []}


async def test_an_sse_framed_response_body_is_parsed():
    """Streamable HTTP allows the reply to arrive as a single SSE-framed message rather
    than a JSON body. Both are the same POST; only the framing differs."""

    async def handler(request):
        body = "event: message\ndata: " + json.dumps(_rpc_result({"tools": [{"name": "t"}]})) + "\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    result = await _stub_upstream(handler).request("tools/list")
    assert result["tools"][0]["name"] == "t"


async def test_the_session_id_is_captured_and_replayed():
    """A stateful upstream binds everything after ``initialize`` to its session header.
    Dropping it makes every later call arrive unauthenticated to that server."""
    seen: list[str | None] = []

    async def handler(request):
        seen.append(request.headers.get("mcp-session-id"))
        return httpx.Response(200, json=_rpc_result({}), headers={"Mcp-Session-Id": "sess-123"})

    up = _stub_upstream(handler)
    await up.initialize()
    await up.request("tools/list")
    # initialize, then the `initialized` notification, then the request — everything after
    # the handshake carries the session the upstream issued.
    assert seen == [None, "sess-123", "sess-123"]


async def test_a_transport_failure_raises_rather_than_returning_a_result():
    async def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(McpUpstreamError):
        await _stub_upstream(handler).request("tools/list")


async def test_a_5xx_raises_but_a_4xx_does_not():
    """Mirrors the OpenAPI pod: 5xx is an upstream failure that should count toward the
    breaker; 4xx is a client/config error that should not."""

    async def five(request):
        return httpx.Response(503, text="down")

    with pytest.raises(McpUpstreamError):
        await _stub_upstream(five).request("tools/list")

    async def four(request):
        return httpx.Response(401, text="nope")

    resp = await _stub_upstream(four).send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 401  # surfaced, not raised


async def test_an_oversized_upstream_body_is_refused(monkeypatch):
    """F-27 parity: an unbounded body is both a memory-DoS vector and an oversized
    prompt-injection channel."""

    async def handler(request):
        return httpx.Response(200, json=_rpc_result({"blob": "x" * 4000}))

    up = _stub_upstream(handler)
    up._max_response_bytes = 1000
    with pytest.raises(McpUpstreamError):
        await up.request("tools/list")


# --- the handshake ------------------------------------------------------------


def _stateful_upstream(log: list):
    """An upstream that behaves as the MCP spec permits a *stateful* server to.

    It refuses anything arriving before ``initialize`` and binds later calls to the session
    it issued. Every earlier test in this file used a stateless stub, which answers
    ``tools/call`` cold — so the stubs agreed with the implementation instead of checking
    it, and a missing handshake looked like working code.
    """

    async def handler(request):
        body = json.loads(request.content)
        log.append((body["method"], request.headers.get("mcp-session-id")))
        if body["method"] == "initialize":
            return httpx.Response(
                200, json=_rpc_result({"serverInfo": {}}, req_id=body["id"]), headers={"Mcp-Session-Id": "sess-1"}
            )
        if body["method"].startswith("notifications/"):
            return httpx.Response(202, text="")
        if not request.headers.get("mcp-session-id"):
            return httpx.Response(400, text="Bad Request: server not initialized")
        return httpx.Response(200, json=_rpc_result({"content": [{"type": "text", "text": "ok"}]}, req_id=body["id"]))

    return handler


async def test_a_stateful_upstream_is_handshaken_before_the_first_tool_call():
    """Without this, every proxied call against a stateful server fails with no session —
    while looking perfectly fine against a stateless one."""
    log: list = []
    pod = _pod(_stateful_upstream(log))
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
    )
    assert [m for m, _ in log] == ["initialize", "notifications/initialized", "tools/call"]
    assert log[-1][1] == "sess-1", "the issued session was not replayed on the tool call"
    assert resp["result"]["content"][0]["text"] == "ok"


async def test_the_handshake_happens_once_across_many_calls():
    log: list = []
    pod = _pod(_stateful_upstream(log))
    for _ in range(3):
        await pod.call_tool(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
        )
    assert [m for m, _ in log].count("initialize") == 1


async def test_concurrent_first_calls_do_not_race_several_handshakes():
    """A pod that goes from idle to a burst would otherwise open one session per in-flight
    call, and every session but the last would leak on the upstream."""
    log: list = []
    pod = _pod(_stateful_upstream(log))
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
    await asyncio.gather(*(pod.call_tool(dict(msg)) for _ in range(5)))
    assert [m for m, _ in log].count("initialize") == 1


async def test_an_expired_session_is_re_established_once():
    """404 is the spec's "session expired or unknown" signal — and it also means the request
    was not processed, so resending after a fresh handshake cannot double-execute a write."""
    calls: list = []
    state = {"valid": "sess-1", "issued": 0}

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "initialize":
            state["issued"] += 1
            state["valid"] = f"sess-{state['issued'] + 1}"
            return httpx.Response(
                200, json=_rpc_result({}, req_id=body["id"]), headers={"Mcp-Session-Id": state["valid"]}
            )
        if body["method"].startswith("notifications/"):
            return httpx.Response(202, text="")
        if request.headers.get("mcp-session-id") != state["valid"]:
            return httpx.Response(404, text="Session not found")  # upstream restarted
        return httpx.Response(200, json=_rpc_result({"content": []}, req_id=body["id"]))

    pod = _pod(handler)
    await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
    )
    state["valid"] = "sess-rotated"  # the upstream restarted behind us
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
    )
    assert calls.count("initialize") == 2, "the pod did not re-handshake after the session expired"
    assert "isError" not in resp["result"], resp


async def test_a_400_is_not_retried_because_it_is_ambiguous_about_whether_the_upstream_acted():
    """Only 404 carries "not processed". Retrying a 400 could re-run a proxied write, which
    is the F-08 double-execution class of bug."""
    attempts: list = []

    async def handler(request):
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json=_rpc_result({}, req_id=body["id"]), headers={"Mcp-Session-Id": "sess-1"})
        if body["method"].startswith("notifications/"):
            return httpx.Response(202, text="")
        attempts.append(body["method"])
        return httpx.Response(400, text="Bad Request")

    pod = _pod(handler)
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
    )
    assert attempts == ["tools/call"], f"the call was retried: {attempts}"
    assert resp["result"]["isError"] is True


async def test_a_finished_session_is_terminated_rather_than_abandoned():
    """The spec's DELETE. Discovery runs every health cycle, so without it a stateful
    upstream accumulates thousands of abandoned sessions per device per day."""
    verbs: list[str] = []

    async def handler(request):
        verbs.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(204, text="")
        return httpx.Response(200, json=_rpc_result({}, req_id=1), headers={"Mcp-Session-Id": "sess-1"})

    up = _stub_upstream(handler)
    await up.initialize()
    assert up.session_id == "sess-1"
    await up.close_session()
    assert verbs[-1] == "DELETE"
    assert up.session_id is None


async def test_session_teardown_never_masks_the_work_it_follows():
    """A server that does not support termination answers 405, and one that has already
    gone away answers nothing. Neither is a reason to fail the caller."""

    async def handler(request):
        if request.method == "DELETE":
            raise httpx.ConnectError("gone")
        return httpx.Response(200, json=_rpc_result({}, req_id=1), headers={"Mcp-Session-Id": "s"})

    up = _stub_upstream(handler)
    await up.initialize()
    await up.close_session()  # must not raise
    assert up.session_id is None


async def test_the_pod_terminates_its_session_when_it_is_torn_down():
    verbs: list[str] = []

    async def handler(request):
        verbs.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(204, text="")
        return httpx.Response(200, json=_rpc_result({"content": []}, req_id=1), headers={"Mcp-Session-Id": "sess-1"})

    pod = _pod(handler)
    await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
    )
    await pod.aclose()
    assert "DELETE" in verbs


async def test_discovery_handshakes_before_listing_tools():
    log: list = []
    up = _stub_upstream(_stateful_upstream(log))

    async def _list():
        return await up.list_tools()

    with pytest.raises(McpUpstreamError):
        # The stateful stub answers tools/list with content, not a tool list — what matters
        # here is the order of what reached the wire.
        await _list()
    assert log[0][0] == "initialize"


# --- F-25 / ADR-0010: outbound headers come only from auth + protocol ---------


async def test_tool_arguments_cannot_reach_the_wire_as_headers():
    """The executable statement of ADR-0010. Do not weaken without superseding it.

    The OpenAPI path *does* derive headers from tool parameters — an operation can declare
    `in: header`, and F-25 constrains that with a reserved-header denylist and CRLF
    rejection. The proxy path builds headers from arguments **at all**, and must not start:
    under SEP-2243 a proxied upstream would choose the header *name* as well as the model
    choosing the value, on a request that carries the device's credentials.

    That exclusion is a permanent design constraint rather than a gap awaiting the feature
    (ADR-0010 records what superseding it would require), so this test is the thing that
    keeps it true. A future change adding `x-mcp-header` support to `McpProxyPod` should
    fail here first.
    """
    seen: dict = {}

    async def handler(request):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json=_rpc_result({"content": []}))

    pod = _pod(handler)
    await pod.call_tool(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "echo",
                "arguments": {"Authorization": "Bearer attacker", "X-Custom": "v", "host": "evil.local"},
            },
        }
    )
    assert seen.get("authorization") != "Bearer attacker"
    assert "x-custom" not in seen
    assert "evil.local" not in seen.get("host", "")


async def test_stored_credentials_are_applied_to_the_upstream_call():
    seen: dict = {}

    class _Auth:
        def configure_egress(self, allow_private, allowed_ports):
            pass

        def configure_credentials(self, resolver):
            # ADR-0018 wire-up seam. Present because the base calls it unconditionally, the
            # same way it calls configure_egress — a double missing it is a double that has
            # drifted from the interface it stands in for.
            pass

        async def apply(self):
            from device_mcp_gateway.auth.base import AuthMaterial

            return AuthMaterial(headers={"X-API-Key": "s3cret"})

    async def handler(request):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json=_rpc_result({"content": []}))

    pod = McpProxyPod(hostname="remote", manifest=_manifest(), base_url="http://remote.local/mcp", auth=_Auth())
    pod._upstream = _stub_upstream(handler)
    pod._upstream._auth = pod.auth
    await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
    )
    assert seen["x-api-key"] == "s3cret"


# --- F-02: the proxy path uses the same guarded egress ------------------------


def test_the_proxy_pod_dispatch_client_is_ssrf_guarded_and_does_not_follow_redirects():
    """Inherited from BasePod, but asserted here because losing it on this path would be
    invisible: the upstream URL is operator-supplied and re-resolved on every call."""
    from device_mcp_gateway.security.url_policy import SsrfGuardTransport

    pod = McpProxyPod(hostname="remote", manifest=_manifest(), base_url="http://remote.local/mcp")
    client = pod._client()
    assert isinstance(client._transport, SsrfGuardTransport)
    assert client.follow_redirects is False


async def test_an_upstream_resolving_to_loopback_is_refused_at_dispatch():
    from device_mcp_gateway.security.url_policy import UrlPolicyError

    pod = McpProxyPod(hostname="remote", manifest=_manifest(), base_url="http://127.0.0.1:9/mcp")
    with pytest.raises((UrlPolicyError, McpUpstreamError)):
        await pod._upstream.request("tools/list")


# --- dispatch semantics -------------------------------------------------------


async def test_upstream_content_is_passed_through_untouched():
    """The upstream already returns MCP content. Re-wrapping it in a text block would
    double-encode every proxied result."""

    async def handler(request):
        return httpx.Response(200, json=_rpc_result({"content": [{"type": "text", "text": "hello"}], "isError": False}))

    pod = _pod(handler)
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
    )
    assert resp["result"] == {"content": [{"type": "text", "text": "hello"}], "isError": False}


async def test_the_upstream_wire_name_is_what_gets_called():
    seen: dict = {}

    async def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_rpc_result({"content": []}))

    pod = _pod(handler, tools=(_tool(name="get_weather", upstream_name="Get Weather"),))
    await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"q": 1}}}
    )
    assert seen["params"]["name"] == "Get Weather"
    assert seen["params"]["arguments"] == {"q": 1}


async def test_a_jsonrpc_tool_error_is_returned_and_does_not_trip_the_breaker():
    """The upstream's tool-level 'no' — the analogue of a 4xx. If this tripped the breaker,
    one badly-behaved tool would take down every tool on that upstream."""

    async def handler(request):
        body = json.loads(request.content)
        # The handshake must succeed: a *failing* initialize is a genuine upstream failure
        # and should count toward the breaker. What must not count is a tool saying no.
        if body["method"] == "initialize":
            return httpx.Response(200, json=_rpc_result({}, req_id=body["id"]))
        if body["method"].startswith("notifications/"):
            return httpx.Response(202, text="")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32602, "message": "bad arg"}}
        )

    pod = _pod(handler)
    for _ in range(8):  # well past the breaker's fail_max of 5
        resp = await pod.call_tool(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
        )
    assert pod.breaker_snapshot()["state"] == "closed"
    assert "error" in resp or resp["result"].get("isError")


async def test_repeated_transport_failures_open_the_breaker():
    async def handler(request):
        return httpx.Response(502, text="bad gateway")

    pod = _pod(handler)
    for _ in range(6):
        await pod.call_tool(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {}}}
        )
    assert pod.breaker_snapshot()["state"] == "open"


async def test_the_proxy_pod_still_validates_arguments_before_dispatching():
    """F-28 comes from BasePod, so it applies here for free — asserted because 'for free'
    is exactly the kind of claim that stops being true without anyone noticing."""
    called = False

    async def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json=_rpc_result({"content": []}))

    tool = _tool(schema={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]})
    pod = _pod(handler, tools=(tool,))
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "echo", "arguments": {"n": "no"}}}
    )
    assert resp["error"]["code"] == -32602
    assert not called, "invalid arguments were forwarded to the upstream"


async def test_an_unknown_tool_is_refused_without_calling_the_upstream():
    called = False

    async def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json=_rpc_result({"content": []}))

    pod = _pod(handler)
    resp = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
    )
    assert resp["error"]["code"] == -32601
    assert not called


async def test_the_proxy_pod_serves_tools_list_from_its_own_manifest():
    """Not a live passthrough of the upstream's list: the served list is the sanitised,
    deduped, capped manifest. Forwarding it live would hand the upstream a channel straight
    to the LLM, bypassing every check applied at discovery."""

    async def handler(request):
        return httpx.Response(200, json=_rpc_result({"tools": [{"name": "SURPRISE"}]}))

    pod = _pod(handler, tools=(_tool(name="echo"),))
    listed = await pod.call_tool({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert [t["name"] for t in listed["result"]["tools"]] == ["echo"]


async def test_a_proxied_pod_exposes_no_resources_in_v1():
    async def handler(request):
        return httpx.Response(200, json=_rpc_result({}))

    pod = _pod(handler)
    listed = await pod.call_tool({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    assert listed["result"]["resources"] == []
    read = await pod.call_tool(
        {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "device://remote/x"}}
    )
    assert read["error"]["code"] == -32602


# --- wiring: the registry must actually reach this pod kind -------------------


def _profile(kind: str, spec_data: dict | None = None):
    from device_mcp_gateway.registry.models import DeviceProfile
    from device_mcp_gateway.shared.registry_backend import DeviceConfig

    cfg = DeviceConfig(hostname="remote", base_url="http://remote.local/mcp", upstream_kind=kind)
    return DeviceProfile(config=cfg, spec_data=spec_data)


def _supervisor(profiles):
    from device_mcp_gateway.core.backoff import RetryPolicy
    from device_mcp_gateway.registry.pod_supervisor import PodSupervisor
    from device_mcp_gateway.shared.registry_backend import MemoryRegistryBackend

    class _NeverFetches:
        async def fetch_spec(self, profile):
            raise AssertionError("spec_data was already present; no fetch should happen")

    return PodSupervisor(
        backend=MemoryRegistryBackend(),
        config={"max_concurrent_pods": 50},
        tls_profiles=TlsProfiles(None),
        retry_policy=RetryPolicy(),
        spec_service=_NeverFetches(),
        profiles=profiles,
    )


async def test_the_supervisor_spawns_a_proxy_pod_for_an_mcp_upstream():
    """The branch that makes any of this reachable. Without it an MCP device would be
    handed to the OpenAPI translator, which would reject its discovery output as an invalid
    spec — the device would register and then never serve a tool."""
    profile = _profile("mcp", {"upstream_kind": "mcp", "tools": [{"name": "echo", "description": "d"}]})
    sup = _supervisor({profile.hostname: profile})

    await sup.spawn(profile)
    try:
        assert isinstance(profile.pod, McpProxyPod)
        assert [t.name for t in profile.pod.manifest.tools] == ["echo"]
        assert profile.pod.manifest.tools[0].source == "proxy"
    finally:
        await sup.kill(profile)


async def test_the_supervisor_still_spawns_a_device_pod_for_an_openapi_upstream():
    from device_mcp_gateway.pods.device_pod import DevicePod

    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/ping": {"get": {"operationId": "ping", "responses": {"200": {"description": "ok"}}}}},
    }
    profile = _profile("openapi", spec)
    sup = _supervisor({profile.hostname: profile})

    await sup.spawn(profile)
    try:
        assert isinstance(profile.pod, DevicePod)
    finally:
        await sup.kill(profile)


async def test_a_405_from_an_mcp_endpoint_is_not_scored_as_reachable():
    """``check_reachability`` scores ``status_code < 500`` as healthy, and an MCP endpoint
    answers a bare GET with 404/405. Without a kind-specific probe a dead upstream reads as
    healthy, its pod is spawned against nothing, and the fault surfaces only on first call."""
    from device_mcp_gateway.registry.server import Registry

    reg = Registry(config={"mode": "embedded", "health_check_interval": 30})
    profile = _profile("mcp")

    async def _405(request):
        return httpx.Response(405, text="method not allowed")

    stub = httpx.AsyncClient(transport=httpx.MockTransport(_405))
    # The probe goes through the per-device fingerprint-capturing client (ADR-0015 in
    # embedded mode). Injecting anywhere else lets a real DNS failure score "unreachable"
    # and pass this test for the wrong reason — which it did, once the seam moved.
    reg._spec_service.probe_client = lambda _hostname: stub

    assert await reg.check_reachability(profile) is False


async def test_a_working_initialize_is_scored_as_reachable():
    from device_mcp_gateway.registry.server import Registry

    reg = Registry(config={"mode": "embedded", "health_check_interval": 30})
    profile = _profile("mcp")

    async def _ok(request):
        return httpx.Response(200, json=_rpc_result({"serverInfo": {"name": "remote", "version": "1"}}))

    stub = httpx.AsyncClient(transport=httpx.MockTransport(_ok))
    reg._spec_service.probe_client = lambda _hostname: stub
    await reg._backend.set_device(profile.hostname, profile.config)  # a registered device

    assert await reg.check_reachability(profile) is True
    # And the declared identity from that initialize is now recorded in embedded mode.
    assert (profile.config.declared_name, profile.config.declared_version) == ("remote", "1")


def test_discovery_is_selected_by_upstream_kind():
    from device_mcp_gateway.registry.server import Registry
    from device_mcp_gateway.upstream.mcp_discovery import McpDiscoveryService

    reg = Registry(config={"mode": "embedded", "health_check_interval": 30})
    assert isinstance(reg._discovery_for(_profile("mcp")), McpDiscoveryService)
    assert reg._discovery_for(_profile("openapi")) is reg._spec_service
