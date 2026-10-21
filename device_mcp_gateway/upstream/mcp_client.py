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
from device_mcp_gateway.core import protocol

# The handshake version this gateway offers an upstream that does not speak the stateless era.
# Independent of the versions it *serves* to its own clients (core/protocol): those describe
# what we accept inbound, this is what we speak outbound.
MCP_PROTOCOL_VERSION = "2025-06-18"
#: The stateless revision this gateway speaks to an upstream that has it.
MODERN_PROTOCOL_VERSION = protocol.MODERN_PROTOCOL_VERSIONS[0]

ERA_MODERN = "modern"
ERA_HANDSHAKE = "handshake"

#: An upstream's era, per endpoint URL, for the life of the process. The spec is explicit
#: that era is a property of the server, not of a request, and probing on every connection
#: would double the round trips of every discovery cycle (roadmap trap 3). Shared across
#: client instances on purpose: discovery builds a new client per cycle, and the pod keeps
#: one, and both should learn the answer once. Invalidated when the assumption fails, or a
#: server upgrading mid-flight would wedge every device pointed at it.
_ERA_BY_URL: dict[str, str] = {}


def forget_era(url: str | None = None) -> None:
    """Drop the cached era for ``url``, or for every upstream when ``url`` is None."""
    if url is None:
        _ERA_BY_URL.clear()
    else:
        _ERA_BY_URL.pop(url, None)


def known_era(url: str) -> str | None:
    """The era recorded for ``url``, if one has been learned."""
    return _ERA_BY_URL.get(url)


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
    #: On a 4xx, the upstream's own JSON-RPC error as one bounded line (``-32022: Unsupported
    #: protocol version``), or None. ``message`` stays None for a 4xx so no caller changes
    #: behaviour; this exists only so an error *report* can say why, instead of just "400".
    error_detail: str | None = None
    #: On a 4xx, the JSON-RPC error code, which era detection acts on.
    error_code: int | None = None
    #: On a ``-32022``, the versions the upstream says it serves (strings only, bounded).
    supported_versions: list[str] | None = None


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
        #: The era this connection is speaking, once initialize() has decided.
        self.era: str | None = None

    @property
    def session_id(self) -> str | None:
        """The upstream's session, once ``initialize`` has established one."""
        return self._session_id

    # --- protocol calls ------------------------------------------------------

    async def initialize(self) -> dict[str, Any]:
        """Establish how to talk to this upstream, and return what it says about itself.

        Returns an ``initialize``-shaped result (``protocolVersion``, ``capabilities``,
        ``serverInfo``) whichever era answered, so callers need not care which it was.

        The probe follows the official SDK client's rule (``mcp.client._probe``): try
        ``server/discover`` at the stateless revision, and fall back to the handshake on
        anything that is not positive evidence the server is modern. The one refusal that does
        not fall back is ``-32022`` naming only modern versions this gateway does not speak:
        that is a real incompatibility, and it is reported as one rather than retried as a
        handshake the server has already said it will not do. A transport failure propagates:
        an outage is never an era verdict, so it is never cached as one.
        """
        era = _ERA_BY_URL.get(self._url)
        if era != ERA_HANDSHAKE:
            discovered = await self._discover()
            if discovered is not None:
                return discovered
            if era == ERA_MODERN:
                logger.info(f"Upstream {redact_url(self._url)} no longer speaks {MODERN_PROTOCOL_VERSION}; re-probing")
        try:
            return await self._handshake()
        except McpUpstreamError as exc:
            if era != ERA_HANDSHAKE or "-32022" not in str(exc):
                raise
            # The cached verdict said handshake and the server now refuses it: it has
            # upgraded. Learn that once, rather than failing every call until a restart.
            logger.info(f"Upstream {redact_url(self._url)} refused the handshake; re-probing its era")
            forget_era(self._url)
            discovered = await self._discover()
            if discovered is None:
                raise
            return discovered

    async def _discover(self) -> dict[str, Any] | None:
        """``server/discover``: the normalised result if the upstream is modern, else None."""
        self.era = ERA_MODERN  # so the probe is sent with the stateless envelope and headers
        resp = await self.send(self._envelope("server/discover", {}))
        result = (resp.message or {}).get("result") if resp.status_code < 400 else None
        if resp.status_code >= 400 and resp.error_code == protocol.UNSUPPORTED_PROTOCOL_VERSION:
            offered = resp.supported_versions or []
            if offered and not any(
                v in protocol.HANDSHAKE_PROTOCOL_VERSIONS or v == MCP_PROTOCOL_VERSION for v in offered
            ):
                self.era = None
                raise McpUpstreamError(
                    f"upstream {redact_url(self._url)} serves only {', '.join(offered)}; this gateway speaks "
                    f"{MODERN_PROTOCOL_VERSION} and the {MCP_PROTOCOL_VERSION} handshake"
                )
        versions = result.get("supportedVersions") if isinstance(result, dict) else None
        if not isinstance(versions, list) or MODERN_PROTOCOL_VERSION not in versions:
            # Not modern evidence — an error, an unparseable answer, or a discover that
            # advertises only handshake versions (an explicit legacy advertisement).
            self.era = None
            return None
        assert isinstance(result, dict)
        caps = result.get("capabilities")
        self.server_capabilities = caps if isinstance(caps, dict) else {}
        meta = result.get("_meta")
        info = meta.get(protocol.SERVER_INFO_META_KEY) if isinstance(meta, dict) else None
        _ERA_BY_URL[self._url] = ERA_MODERN
        self._initialized = True
        out: dict[str, Any] = {"protocolVersion": MODERN_PROTOCOL_VERSION, "capabilities": self.server_capabilities}
        if isinstance(info, dict):
            out["serverInfo"] = info
        return out

    async def _handshake(self) -> dict[str, Any]:
        self.era = ERA_HANDSHAKE
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
        _ERA_BY_URL[self._url] = ERA_HANDSHAKE
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
        self.era = None

    async def close_session(self) -> None:
        """Tell the upstream we are done with this session (HTTP DELETE, per the spec).

        Without this, every discovery cycle opens a session a stateful upstream keeps —
        at a 30s health interval that is thousands of abandoned sessions per device per
        day. Best-effort by design: a server that does not support termination answers 405,
        and an unreachable one is about to be marked unreachable anyway.
        """
        session, self._session_id, self._initialized = self._session_id, None, False
        self.era = None
        if not session:
            return  # a stateless upstream has no session to end
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
        if self.era == ERA_MODERN and resp.status_code == 400 and resp.error_code in _ERA_REFUSALS:
            # The upstream refused the stateless envelope itself (it has downgraded, or the
            # cached verdict was wrong), before doing any work — so re-probing and resending
            # once cannot double-execute. Anything else, including a 400 that might mean the
            # call was attempted, is returned as-is (F-08).
            logger.info(f"Upstream {redact_url(self._url)} refused the stateless envelope; re-probing its era")
            forget_era(self._url)
            self.reset_session()
            await self.ensure_initialized()
            resp = await self.send(self._envelope(method, params))
            return resp
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
            why = f" ({resp.error_detail})" if resp.error_detail else ""
            raise McpUpstreamError(f"{method} to {redact_url(self._url)} returned HTTP {resp.status_code}{why}")
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
                headers=await self._headers(payload),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise McpUpstreamError(f"upstream {redact_url(self._url)} timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise McpUpstreamError(f"upstream {redact_url(self._url)} unreachable: {exc}") from exc

        # A session header may be issued on any response, not only initialize — in the
        # handshake era. The stateless era has no sessions, and adopting one seen on the
        # discovery probe would send the handshake that follows already bound to it.
        session = resp.headers.get("mcp-session-id")
        if session and self.era != ERA_MODERN:
            self._session_id = session

        if 500 <= resp.status_code < 600:
            raise McpUpstreamError(f"upstream {redact_url(self._url)} returned HTTP {resp.status_code}")
        if resp.status_code >= 400:
            code, supported = _error_code(resp)
            return McpResponse(
                status_code=resp.status_code,
                message=None,
                error_detail=_error_detail(resp),
                error_code=code,
                supported_versions=supported,
            )

        message = self._decode(resp, payload.get("id"))
        if self.era == ERA_MODERN and payload.get("method") != "server/discover":
            message = _era_neutral(message)
        return McpResponse(status_code=resp.status_code, message=message)

    async def _headers(self, payload: dict[str, Any] | None = None) -> dict[str, str]:
        """Fixed protocol headers plus stored credentials — and nothing else.

        No caller-supplied value contributes here. That is the whole F-25 story on this
        path: there is no merge step for an argument to win. The stateless era's routing
        headers are the one apparent exception, and are not one: ``Mcp-Method`` is the method
        the gateway chose, and ``Mcp-Name`` repeats the tool, prompt or URI already in the body
        (which the upstream checks against it) — never a tool *argument*. No ``Mcp-Param-*``
        header is ever sent (ADR-0010).
        """
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self.era == ERA_MODERN:
            headers["mcp-protocol-version"] = MODERN_PROTOCOL_VERSION
            method = (payload or {}).get("method")
            if isinstance(method, str):
                headers["mcp-method"] = method
                name_key = protocol.NAME_BEARING_METHODS.get(method)
                name = ((payload or {}).get("params") or {}).get(name_key) if name_key else None
                if isinstance(name, str):
                    headers["mcp-name"] = _encode_header_value(name)
        else:
            headers["mcp-protocol-version"] = MCP_PROTOCOL_VERSION
            if self._session_id:
                headers["mcp-session-id"] = self._session_id
        if self._auth is not None:
            material = await self._auth.apply()
            if material:
                headers.update(material.headers)
        return headers

    def _decode(self, resp: httpx.Response, msg_id: Any = None) -> dict[str, Any] | None:
        """Decode a JSON body or a single SSE-framed message, with the size cap applied."""
        body = resp.content
        if len(body) > self._max_response_bytes:
            raise McpUpstreamError(
                f"upstream {redact_url(self._url)} returned {len(body)} bytes, over the "
                f"{self._max_response_bytes}-byte cap"
            )
        text = body.decode("utf-8", errors="replace")
        if "text/event-stream" in resp.headers.get("content-type", ""):
            framed = _sse_reply(text, msg_id)
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
        if self.era == ERA_MODERN:
            # The stateless era carries in every request what the handshake used to establish.
            params = {
                **(params or {}),
                "_meta": {
                    protocol.PROTOCOL_VERSION_META_KEY: MODERN_PROTOCOL_VERSION,
                    protocol.CLIENT_CAPABILITIES_META_KEY: {},
                    protocol.CLIENT_INFO_META_KEY: {"name": "device-mcp-gateway", "version": "1"},
                },
            }
        if params is not None:
            env["params"] = params
        return env


#: How much of an upstream's error text an operator-facing message may carry.
_ERROR_DETAIL_MAX = 200


def _error_detail(resp: httpx.Response) -> str | None:
    """The JSON-RPC error in a 4xx body, as ``code: message`` on one bounded line, or None.

    Best effort and never raising: a 4xx body is the upstream's, so it may be anything. The
    text is untrusted and goes only into an error *report* — collapsed to one line and cut to
    ``_ERROR_DETAIL_MAX`` characters, so it cannot forge log lines or flood them.
    """
    if len(resp.content) > 64 * 1024:
        return None
    try:
        body = json.loads(resp.content.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return None
    code, text = err.get("code"), err.get("message")
    detail = f"{code}: {text}" if code is not None else str(text or "")
    detail = " ".join(detail.split())
    return detail[:_ERROR_DETAIL_MAX] or None


#: Refusals of the stateless envelope itself — the upstream rejected it before doing any work.
_ERA_REFUSALS = frozenset({protocol.UNSUPPORTED_PROTOCOL_VERSION, protocol.INVALID_REQUEST})


def _era_neutral(message: dict[str, Any] | None) -> dict[str, Any] | None:
    """A modern upstream's reply with the stateless era's envelope fields taken off.

    Pods produce handshake-shaped results and the edge shapes them for whichever era the
    *gateway's* client speaks (``protocol.modern_response``). An upstream's own ``resultType:
    complete``, cache hints and ``serverInfo`` stamp are about the hop from the upstream to the
    gateway, not the one to the client: passed through, a handshake client would get fields
    from an era it does not speak, and a modern client would be told the upstream, rather than
    this gateway, is the server it is talking to. Everything else in ``_meta`` is kept. A
    ``resultType`` other than ``complete`` (an ``input_required`` round) has no handshake form,
    so it is left for the client to see.
    """
    if not isinstance(message, dict) or not isinstance(message.get("result"), dict):
        return message
    result = dict(message["result"])
    if result.get("resultType") == "complete":
        del result["resultType"]
    result.pop("ttlMs", None)
    result.pop("cacheScope", None)
    meta = result.get("_meta")
    if isinstance(meta, dict) and protocol.SERVER_INFO_META_KEY in meta:
        meta = {k: v for k, v in meta.items() if k != protocol.SERVER_INFO_META_KEY}
        if meta:
            result["_meta"] = meta
        else:
            del result["_meta"]
    return {**message, "result": result}


def _error_code(resp: httpx.Response) -> tuple[int | None, list[str] | None]:
    """The JSON-RPC error code in a 4xx body, and a ``-32022``'s ``supported`` list. Never raises."""
    if len(resp.content) > 64 * 1024:
        return None, None
    try:
        body = json.loads(resp.content.decode("utf-8", errors="replace"))
    except ValueError:
        return None, None
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict) or not isinstance(err.get("code"), int):
        return None, None
    code: int = err["code"]
    data = err.get("data")
    supported = data.get("supported") if isinstance(data, dict) else None
    if isinstance(supported, list):
        supported = [v for v in supported if isinstance(v, str) and len(v) <= 32][:16]
    else:
        supported = None
    return code, supported


def _encode_header_value(value: str) -> str:
    """The SDK's header encoding: plain printable ASCII verbatim, anything else base64-wrapped."""
    import base64
    import re

    if re.fullmatch(r"[\x20-\x7E]*", value) and value == value.strip() and not value.startswith("=?base64?"):
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def _sse_reply(text: str, msg_id: Any) -> str | None:
    """The data of the SSE event that carries the reply to ``msg_id``.

    A server may send notifications (progress, logging) on the same stream before the
    result, and the stateless era makes that routine, so the first event is not necessarily
    the answer. Picks the first event whose JSON-RPC ``id`` matches; with no id to match,
    or none that does, falls back to the first event, as before.
    """
    events = _sse_events(text)
    if msg_id is not None:
        for data in events:
            try:
                parsed = json.loads(data)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("id") == msg_id and ("result" in parsed or "error" in parsed):
                return data
    return events[0] if events else None


def _sse_events(text: str) -> list[str]:
    """Every event's joined ``data:`` payload, in order. Comment lines (``: ping``) are skipped."""
    events: list[str] = []
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("data:"):
            lines.append(raw[5:].lstrip())
        elif not raw.strip():
            if lines:
                events.append("\n".join(lines))
                lines = []
    if lines:
        events.append("\n".join(lines))
    return events


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
