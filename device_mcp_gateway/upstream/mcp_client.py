# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""A minimal MCP Streamable HTTP client, built on the gateway's guarded egress.

**Why not the MCP SDK's client.** ``mcp.client.streamable_http`` and ``sse_client`` are
task-group context managers designed to own a connection for the lifetime of a session.
That fights the worker's dispatch model, where a call arrives off a Redis stream and must
be served without a surrounding session scope. More importantly, the SDK builds its own
httpx client through ``create_mcp_http_client``, which defaults ``follow_redirects=True`` —
precisely what ``build_guarded_client(follow_redirects=False)`` prevents on the hot path,
because httpx strips ``Authorization`` across origins but *not* custom auth headers. Using
the SDK would mean either re-deriving that guard or losing it.

What is actually needed is small: one JSON-RPC POST per call. Implementing it directly
keeps every request on the same SSRF-guarded, address-pinned, port-checked, mTLS-capable
client the rest of the gateway uses.

Two response framings are accepted, because the spec allows a server to answer the same
POST either way: a plain JSON body, or a single SSE-framed message.

**Outbound headers are built solely from fixed protocol headers plus ``auth.apply()``.**
Nothing derived from tool arguments ever reaches them — the F-25 header-injection class of
bug cannot occur on this path, and a test pins that it stays that way.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from loguru import logger

from device_mcp_gateway.audit import redact_url
from device_mcp_gateway.auth.base import AbstractAuth

# The version this gateway advertises to an upstream. Independent of the versions it
# *serves* to its own clients (pods/pod_base.SUPPORTED_PROTOCOL_VERSIONS): those describe
# what we accept inbound, this is what we speak outbound.
MCP_PROTOCOL_VERSION = "2025-06-18"

# Cap the upstream body this client will buffer (F-27 parity with the OpenAPI pod). An
# unbounded body is both a memory-DoS vector and an oversized prompt-injection channel —
# and a proxied result goes straight to the LLM.
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB


class McpUpstreamError(RuntimeError):
    """A transport-level failure talking to an upstream MCP server.

    Raised for connection errors, timeouts, 5xx and unparseable/oversized bodies — the
    failures that should count toward the circuit breaker. Deliberately *not* raised for a
    4xx (a client/config error) or for a JSON-RPC ``error`` in an otherwise fine response
    (the upstream's tool-level "no"), so neither trips the breaker.
    """


@dataclass
class McpResponse:
    """One upstream reply: the HTTP status and the decoded JSON-RPC message.

    The status is surfaced rather than folded into the message so the caller can mirror the
    OpenAPI pod's breaker policy — raise on 5xx inside the breaker, handle 4xx outside it.
    """

    status_code: int
    message: dict[str, Any] | None


class StreamableHttpClient:
    """One MCP upstream endpoint, reached over Streamable HTTP.

    ``get_client`` is a callable rather than a client so the pod's lazily-built, shared
    guarded client is used — the same connection pool and the same egress policy as every
    other outbound call the pod makes.
    """

    def __init__(
        self,
        url: str,
        get_client: Callable[[], httpx.AsyncClient],
        auth: AbstractAuth | None = None,
        timeout: float = 15,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._url = url
        self._get_client = get_client
        self._auth = auth
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._session_id: str | None = None
        self._next_id = 0
        self._initialized = False
        self._init_lock = asyncio.Lock()
        # Filled by initialize(); empty until then.
        self.server_capabilities: dict[str, Any] = {}

    @property
    def session_id(self) -> str | None:
        """The upstream's session, once ``initialize`` has established one."""
        return self._session_id

    # --- protocol calls ------------------------------------------------------

    async def initialize(self) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "device-mcp-gateway", "version": "1"},
            },
        )
        # What the upstream says it serves. Discovery asks for resources and prompts only
        # when they are advertised: a server that does not claim a capability has none, and
        # probing it anyway would spend a round trip to learn "method not found".
        caps = result.get("capabilities") if isinstance(result, dict) else None
        self.server_capabilities = caps if isinstance(caps, dict) else {}
        # The spec says a client SHOULD follow initialize with this notification, and a
        # stateful server may refuse everything else until it arrives. No id, no reply.
        try:
            await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except McpUpstreamError as exc:
            logger.debug(f"initialized notification to {redact_url(self._url)} failed: {exc}")
        self._initialized = True
        return result

    async def ensure_initialized(self) -> None:
        """Perform the handshake once, before anything else on this connection.

        The MCP spec allows a server to be stateful and refuse every request that arrives
        before ``initialize`` — so skipping this makes *every* proxied call fail against
        such an upstream, while looking fine against a stateless one. The lock keeps a
        burst of concurrent first calls to one pod from racing several handshakes.
        """
        if self._initialized:
            return
        async with self._init_lock:
            if not self._initialized:
                await self.initialize()

    def reset_session(self) -> None:
        """Forget the session so the next call re-handshakes (upstream restarted)."""
        self._session_id = None
        self._initialized = False

    async def close_session(self) -> None:
        """Tell the upstream we are done with this session (HTTP DELETE, per the spec).

        Without this, every discovery cycle opens a session a stateful upstream keeps —
        at a 30s health interval that is thousands of abandoned sessions per device per
        day. Best-effort by design: a server that does not support termination answers 405,
        and an unreachable one is about to be marked unreachable anyway.
        """
        session, self._session_id, self._initialized = self._session_id, None, False
        if not session:
            return
        try:
            await self._get_client().request(
                "DELETE",
                self._url,
                headers={"mcp-session-id": session, "mcp-protocol-version": MCP_PROTOCOL_VERSION},
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — teardown must never mask the real work
            logger.debug(f"Session teardown for {redact_url(self._url)} failed: {exc}")

    async def list_tools(self) -> list[dict[str, Any]]:
        await self.ensure_initialized()
        result = await self.request("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise McpUpstreamError(f"tools/list from {redact_url(self._url)} did not return a tool list")
        return tools

    async def _list(self, method: str, key: str) -> list[dict[str, Any]]:
        await self.ensure_initialized()
        result = await self.request(method)
        items = result.get(key)
        if not isinstance(items, list):
            raise McpUpstreamError(f"{method} from {redact_url(self._url)} did not return a {key} list")
        return items

    async def list_resources(self) -> list[dict[str, Any]]:
        return await self._list("resources/list", "resources")

    async def list_prompts(self) -> list[dict[str, Any]]:
        return await self._list("prompts/list", "prompts")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResponse:
        """Invoke a tool. Returns the raw response so the caller owns breaker policy."""
        return await self.call("tools/call", {"name": name, "arguments": arguments})

    async def read_resource(self, uri: str) -> McpResponse:
        return await self.call("resources/read", {"uri": uri})

    async def get_prompt(self, name: str, arguments: dict[str, str]) -> McpResponse:
        return await self.call("prompts/get", {"name": name, "arguments": arguments})

    async def call(self, method: str, params: dict[str, Any]) -> McpResponse:
        """Send one forwarded request; the raw response, so the caller owns breaker policy."""
        await self.ensure_initialized()
        envelope = self._envelope(method, params)
        resp = await self.send(envelope)
        if resp.status_code == 404 and self._session_id:
            # The spec's signal that a session expired or was never known — which also
            # means this request was NOT processed, so re-handshaking and resending once
            # cannot double-execute a write. Deliberately narrow: a 400 is ambiguous about
            # whether the upstream acted, so it is never retried (F-08).
            logger.info(f"Upstream {redact_url(self._url)} rejected the session; re-initialising")
            self.reset_session()
            await self.ensure_initialized()
            resp = await self.send(envelope)
        return resp

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return its JSON-RPC ``result``.

        Raises ``McpUpstreamError`` for anything that is not a usable result — including a
        4xx, since a caller using this convenience form has no way to act on the status.
        Call ``send`` directly where 4xx must be distinguished from 5xx.
        """
        resp = await self.send(self._envelope(method, params))
        if resp.status_code >= 400:
            raise McpUpstreamError(f"{method} to {redact_url(self._url)} returned HTTP {resp.status_code}")
        if resp.message is None:
            raise McpUpstreamError(f"{method} to {redact_url(self._url)} returned no JSON-RPC message")
        if "error" in resp.message:
            err = resp.message["error"] or {}
            raise McpUpstreamError(f"{method} to {redact_url(self._url)} failed: {err.get('message', err)}")
        result = resp.message.get("result")
        if not isinstance(result, dict):
            raise McpUpstreamError(f"{method} to {redact_url(self._url)} returned no result object")
        return result

    # --- the wire ------------------------------------------------------------

    async def send(self, payload: dict[str, Any]) -> McpResponse:
        """POST one JSON-RPC message. Raises only for breaker-worthy failures."""
        try:
            resp = await self._get_client().post(
                self._url,
                json=payload,
                headers=await self._headers(),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise McpUpstreamError(f"upstream {redact_url(self._url)} timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise McpUpstreamError(f"upstream {redact_url(self._url)} unreachable: {exc}") from exc

        # A session header may be issued on any response, not only initialize.
        session = resp.headers.get("mcp-session-id")
        if session:
            self._session_id = session

        if 500 <= resp.status_code < 600:
            raise McpUpstreamError(f"upstream {redact_url(self._url)} returned HTTP {resp.status_code}")
        if resp.status_code >= 400:
            return McpResponse(status_code=resp.status_code, message=None)

        return McpResponse(status_code=resp.status_code, message=self._decode(resp))

    async def _headers(self) -> dict[str, str]:
        """Fixed protocol headers plus stored credentials — and nothing else.

        No caller-supplied value contributes here. That is the whole F-25 story on this
        path: there is no merge step for an argument to win.
        """
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        if self._auth is not None:
            material = await self._auth.apply()
            if material:
                headers.update(material.headers)
        return headers

    def _decode(self, resp: httpx.Response) -> dict[str, Any] | None:
        """Decode a JSON body or a single SSE-framed message, with the size cap applied."""
        body = resp.content
        if len(body) > self._max_response_bytes:
            raise McpUpstreamError(
                f"upstream {redact_url(self._url)} returned {len(body)} bytes, over the "
                f"{self._max_response_bytes}-byte cap"
            )
        text = body.decode("utf-8", errors="replace")
        if "text/event-stream" in resp.headers.get("content-type", ""):
            framed = _first_sse_data(text)
            if framed is None:
                raise McpUpstreamError(f"upstream {redact_url(self._url)} sent an SSE body with no data frame")
            text = framed
        if not text.strip():
            return None
        try:
            message = json.loads(text)
        except ValueError as exc:
            raise McpUpstreamError(f"upstream {redact_url(self._url)} sent an undecodable body: {exc}") from exc
        if not isinstance(message, dict):
            raise McpUpstreamError(f"upstream {redact_url(self._url)} sent a non-object JSON-RPC message")
        return message

    def _envelope(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        env: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            env["params"] = params
        return env


def _first_sse_data(text: str) -> str | None:
    """The payload of the first ``data:`` frame in an SSE body.

    A single JSON-RPC reply may be delivered as one SSE message whose data spans several
    ``data:`` lines, which the spec says to join with newlines.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("data:"):
            lines.append(raw[5:].lstrip())
        elif lines and not raw.strip():
            break  # blank line ends the first event
    if not lines:
        logger.debug("SSE body contained no data frame")
        return None
    return "\n".join(lines)
