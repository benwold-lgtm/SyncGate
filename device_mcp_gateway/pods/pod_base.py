# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""What every pod shares, regardless of what the upstream speaks.

A pod is one MCP server instance bound to one registered hostname. Until now there was
exactly one kind — ``DevicePod``, which translates an OpenAPI document into tools and
dispatches each call as an HTTP request. MCP passthrough adds a second kind that proxies a
*remote MCP server*, and the two have almost nothing in common at the dispatch layer while
being identical everywhere else: the same lifecycle, the same SSRF-guarded client, the same
token bucket and circuit breaker, and the same JSON-RPC method router.

``BasePod`` is that shared half. The split is deliberately drawn at **dispatch**, because
that is the only place the two genuinely differ:

- Everything protocol-shaped (``initialize`` negotiation, ``ping``, ``tools/list``,
  ``resources/list``, ``prompts/list``, argument validation, unknown-method handling) is
  manifest-driven and
  lives here, so a second pod kind cannot drift from the first or quietly lose F-28
  validation.
- ``_dispatch_tool_call`` is abstract, and returns the JSON-RPC ``result`` object rather
  than a payload to be wrapped. That matters: an OpenAPI tool call produces an HTTP
  response this gateway must serialise into a text block, while a proxied call already
  comes back as MCP content that must be passed through unmodified. A hook that returned
  "the payload" would force one of those two to lie about its own shape.

Subclassing ``DevicePod`` instead would have been cheaper to write and wrong: its
``__init__`` builds a closure per OpenAPI operation, capturing ``method``/``path``/
``param_locations``, none of which a proxied tool has.
"""

from __future__ import annotations

import asyncio
import ssl
from abc import ABC, abstractmethod
from typing import Any

import httpx
import jsonschema
from loguru import logger
from pybreaker import CircuitBreaker

from device_mcp_gateway.auth.base import AbstractAuth
from device_mcp_gateway.core.backoff import RetryPolicy
from device_mcp_gateway.core.errors import (
    RPC_INTERNAL_ERROR,
    RPC_INVALID_PARAMS,
    RPC_METHOD_NOT_FOUND,
    rpc_error,
)
from device_mcp_gateway.core.translator import McpManifest
from device_mcp_gateway.pods.rate_limiter import TokenBucket
from device_mcp_gateway.pods.sse_server import SseTransport
from device_mcp_gateway.security.url_policy import build_guarded_client

# MCP protocol versions this gateway speaks, newest first. The `initialize`
# handshake echoes the client's requested version when we support it, otherwise
# it falls back to our preferred (newest) version per the MCP spec. Keeping this
# as data — rather than a hardcoded literal in the handler — means version
# support is one edit, and negotiation is testable in isolation (F-15).
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL_VERSION: str = SUPPORTED_PROTOCOL_VERSIONS[0]


def negotiate_protocol_version(requested: Any) -> str:
    """Resolve the MCP protocolVersion to advertise in the initialize response.

    Echoes the client's requested version when supported; otherwise returns our
    preferred (newest) version, signalling the client to retry on that version.
    A missing/invalid request also yields the preferred version.
    """
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PREFERRED_PROTOCOL_VERSION


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """Validate tool-call arguments against the tool's JSON schema (Tier-0 F-28).

    Returns an error string if the arguments violate the schema, else None. If the
    schema itself is not a valid JSON Schema (some flattened specs aren't), validation
    is skipped (logged) rather than blocking a legitimate call.
    """
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError:
        logger.warning("Tool input schema is not valid JSON Schema; skipping argument validation")
        return None
    try:
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))
    except Exception:
        logger.warning("Argument validation raised on this schema; skipping (fail-open)")
        return None
    if errors:
        e = errors[0]
        loc = "/".join(str(p) for p in e.path) or "(root)"
        return f"{loc}: {e.message}"
    return None


class BasePod(ABC):
    """Lifecycle, egress and the JSON-RPC router for one hostname's MCP server.

    Subclasses supply only ``_dispatch_tool_call``; everything else is shared.
    """

    def __init__(
        self,
        hostname: str,
        manifest: McpManifest,
        transport: str = "sse",
        auth: AbstractAuth | None = None,
        base_url: str = "",
        rate_limit_rps: float | None = None,
        keep_alive_interval: int = 30,
        request_timeout: float = 15,
        retry_policy: RetryPolicy | None = None,
        tls_verify: "ssl.SSLContext | bool" = True,
        allow_private: bool = False,
        allowed_ports: set[int] | None = None,
        credential_resolver: Any = None,
    ):
        self.hostname = hostname
        self.manifest = manifest
        self.transport = transport
        self.auth = auth
        self.base_url = base_url
        self._keep_alive_interval = keep_alive_interval
        self._request_timeout = request_timeout
        # Outbound TLS for calls to this upstream (F-31). True = httpx default
        # certifi server verification; an SSLContext carries a client cert and/or
        # a private CA for mutual TLS.
        self._tls_verify = tls_verify
        # SSRF egress posture for dispatch (F-02). Propagated to an auth handler that
        # makes its own outbound calls (OAuth2 token fetch) so it shares the same
        # policy as the dispatch client.
        self._allow_private = allow_private
        self._allowed_ports = allowed_ports
        # ADR-0018 §2. Attached beside the egress policy so a by-reference handler resolves
        # itself inside apply(); no dispatch call site has to remember a bind step.
        self._credential_resolver = credential_resolver
        if self.auth is not None:
            self.auth.configure_egress(allow_private=allow_private, allowed_ports=allowed_ports)
            self.auth.configure_credentials(credential_resolver)
        # Bounded jittered retries for idempotent calls (F-05/F-44).
        self._retry_policy = retry_policy or RetryPolicy()
        # One reused HTTP client per pod (created lazily) instead of one per
        # call — keeps connections/TLS alive across invocations (F8).
        self._http: httpx.AsyncClient | None = None
        self._rate_limiter = TokenBucket(rate_limit_rps) if rate_limit_rps and rate_limit_rps > 0 else None
        # Open after 5 consecutive 5xx/connection failures; reset after 60s.
        # 4xx responses do not trip the breaker (client error, not upstream failure).
        self._breaker = CircuitBreaker(fail_max=5, reset_timeout=60)
        self._running = False
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self.sse_transport: SseTransport | None = None
        # name → JSON input schema, for server-side argument validation (Tier-0 F-28).
        # Derived from the manifest, so it holds for any pod kind — a subclass that
        # forgot to populate it would silently disable validation, which is why it is
        # built here rather than alongside each kind's dispatch table.
        self._tool_schemas: dict[str, dict[str, Any]] = {t.name: t.schema for t in manifest.tools}
        # name → prompt, for prompts/get. Manifest-driven for the same reason as the schemas:
        # only a prompt that was listed can be fetched.
        self._prompts = {pr.name: pr for pr in manifest.prompts}
        self._build_dispatch()

    # ------------------------------------------------------------------
    # Subclass seam
    # ------------------------------------------------------------------

    def _build_dispatch(self) -> None:
        """Prepare whatever this pod kind needs to serve tool calls.

        Called at the end of ``__init__``. Default is a no-op: a pod kind that resolves
        tools at call time needs no build step.
        """

    @abstractmethod
    async def _dispatch_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute one tool call and return the JSON-RPC ``result`` object.

        The name is already known to be in the manifest and the arguments have already
        been schema-validated (F-28), so implementations dispatch directly.

        Returning the finished ``result`` — not a payload for the caller to wrap — is
        what lets an OpenAPI pod serialise an HTTP response into a text block while a
        proxy pod passes upstream MCP content through untouched.
        """

    async def _read_resource(self, uri: str, msg_id: Any) -> dict[str, Any]:
        """Serve ``resources/read``. Default: this pod kind exposes no readable resources.

        Overridden by pod kinds that can fetch a resource body. Kept a hook rather than a
        shared implementation because reading is inherently upstream-specific — the
        OpenAPI path appends to ``base_url`` and must defend against traversal (F-29),
        which is meaningless for an upstream that serves resources over MCP.
        """
        return rpc_error(RPC_INVALID_PARAMS, msg_id, message=f"Unknown resource URI: {uri}")

    async def _get_prompt(self, name: str, arguments: dict[str, str], msg_id: Any) -> dict[str, Any]:
        """Serve ``prompts/get`` for a prompt the manifest lists. Default: render it here.

        The name is already known to be in the manifest and ``arguments`` already checked to
        be a string map. A translated (OpenAPI) prompt is a template the gateway generated
        from the spec, so it is rendered locally with no device call; a pod kind whose
        prompts live upstream overrides this to forward the request instead.
        """
        prompt = self._prompts[name]
        text = prompt.template
        # Plain `{name}` substitution for declared arguments only. Not str.format: a spec's
        # description may contain braces, and format() would raise on them or worse.
        for arg in prompt.arguments:
            if arg in arguments:
                text = text.replace("{" + arg + "}", arguments[arg])
        result: dict[str, Any] = {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}
        if prompt.description:
            result["description"] = prompt.description
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # ------------------------------------------------------------------
    # Egress
    # ------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """Return the pod's shared HTTP client, creating it on first use."""
        if self._http is None or self._http.is_closed:
            # SSRF-guarded dispatch client (F-02/F-29):
            #  - follow_redirects=False — an upstream that 3xx-redirects an authenticated
            #    call could steer it to an internal address AND leak the stored
            #    API-key/custom auth headers to the redirect target (httpx only strips
            #    Authorization across origins, not custom headers).
            #  - the SsrfGuardTransport re-validates the target host on EVERY call, so a
            #    DNS-rebind of a registered upstream to an internal/metadata address is
            #    caught at dispatch time, not only at registration (costs one host
            #    resolution per call). The validated address is then pinned through to
            #    connect, so httpx never re-resolves — closing the validate→connect window
            #    that previously let a 0-TTL alternating record win the race.
            self._http = build_guarded_client(
                verify=self._tls_verify,
                allow_private=self._allow_private,
                allowed_ports=self._allowed_ports,
                follow_redirects=False,
                timeout=self._request_timeout,
            )
        return self._http

    async def aclose(self) -> None:
        """Close the shared HTTP client. Called when the pod is torn down."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def breaker_snapshot(self) -> dict:
        """Current circuit-breaker state for diagnostics (F-52).

        ``state`` is ``closed`` (healthy), ``open`` (shedding after too many recent
        failures), or ``half-open`` (probing recovery). ``fail_counter`` is the
        consecutive-failure count toward ``fail_max``.
        """
        return {
            "state": self._breaker.current_state,
            "fail_counter": self._breaker.fail_counter,
            "fail_max": self._breaker.fail_max,
            "reset_timeout": self._breaker.reset_timeout,
        }

    # ------------------------------------------------------------------
    # JSON-RPC router
    # ------------------------------------------------------------------

    async def _handle_mcp_message(self, message: dict) -> dict[str, Any] | None:
        """Handle an MCP JSON-RPC 2.0 message and return the response, or None for notifications."""
        msg_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params") or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": negotiate_protocol_version(params.get("protocolVersion")),
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False, "subscribe": False},
                        # Advertised for every pod kind: a pod with none answers an empty
                        # list, which is a truthful "none" rather than "method not found".
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": self.manifest.server_name,
                        "version": self.manifest.server_version,
                    },
                },
            }

        if method.startswith("notifications/"):
            return None  # notifications require no response

        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

        if method == "tools/list":
            tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.schema} for t in self.manifest.tools
            ]
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}

        if method == "tools/call":
            tool_name: str = params.get("name") or ""
            arguments = params.get("arguments") or {}
            if tool_name not in self._tool_schemas:
                return rpc_error(RPC_METHOD_NOT_FOUND, msg_id, message=f"Tool not found: {tool_name}")
            # Validate arguments against the tool's declared JSON schema before dispatch
            # (Tier-0 F-28) so malformed/over-posted params don't reach the upstream.
            if not isinstance(arguments, dict):
                return rpc_error(RPC_INVALID_PARAMS, msg_id, message="Invalid params: 'arguments' must be an object")
            schema = self._tool_schemas.get(tool_name)
            if schema is not None:
                arg_error = _validate_arguments(schema, arguments)
                if arg_error:
                    return rpc_error(RPC_INVALID_PARAMS, msg_id, message=f"Invalid params: {arg_error}")
            try:
                return {"jsonrpc": "2.0", "id": msg_id, "result": await self._dispatch_tool_call(tool_name, arguments)}
            except Exception as e:
                logger.error(f"Tool call failed for {tool_name}: {e}")
                return rpc_error(RPC_INTERNAL_ERROR, msg_id, message=str(e))

        if method == "resources/list":
            resources = [
                {"uri": r.uri, "name": r.name, "description": r.description, "mimeType": r.mime_type}
                for r in self.manifest.resources
            ]
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": resources}}

        if method == "resources/read":
            return await self._read_resource(params.get("uri") or "", msg_id)

        if method == "prompts/list":
            prompts = [
                {
                    "name": pr.name,
                    "description": pr.description,
                    "arguments": [{"name": a, "required": False} for a in pr.arguments],
                }
                for pr in self.manifest.prompts
            ]
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"prompts": prompts}}

        if method == "prompts/get":
            prompt_name = params.get("name") or ""
            if prompt_name not in self._prompts:
                # The spec's code for an unknown prompt name is -32602 (invalid params).
                return rpc_error(RPC_INVALID_PARAMS, msg_id, message=f"Unknown prompt: {prompt_name}")
            prompt_args = params.get("arguments") or {}
            if not isinstance(prompt_args, dict) or not all(isinstance(v, str) for v in prompt_args.values()):
                return rpc_error(
                    RPC_INVALID_PARAMS, msg_id, message="Invalid params: prompt 'arguments' must map names to strings"
                )
            try:
                return await self._get_prompt(prompt_name, prompt_args, msg_id)
            except Exception as e:
                logger.error(f"prompts/get failed for {prompt_name}: {e}")
                return rpc_error(RPC_INTERNAL_ERROR, msg_id, message=str(e))

        # Unknown method — only send an error if this was a request (has an id)
        if msg_id is not None:
            return rpc_error(RPC_METHOD_NOT_FOUND, msg_id, message=f"Method not found: {method}")
        return None

    async def call_tool(self, message: dict) -> dict | None:
        """Public entry-point for the worker to dispatch an MCP JSON-RPC message.

        Returns the JSON-RPC response dict, or None for notifications.
        """
        return await self._handle_mcp_message(message)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_sse_transport(self) -> SseTransport:
        if not self.sse_transport:
            self.sse_transport = SseTransport(
                self.hostname,
                self._handle_mcp_message,
                keep_alive_interval=self._keep_alive_interval,
            )
        return self.sse_transport

    async def start(self, with_sse: bool = True) -> None:
        """Start the pod.

        Args:
            with_sse: If True (default, embedded mode), start the SSE transport
                      task so the pod accepts connections on its in-process queue.
                      Pass False in distributed mode — the worker calls call_tool()
                      directly and SSE routing goes through Redis.
        """
        if self._running:
            return
        if with_sse and self.transport != "sse":
            raise ValueError(f"Unsupported transport: {self.transport}")
        self._running = True
        self._stop_event = asyncio.Event()
        if with_sse:
            self._task = asyncio.create_task(self._run_sse())
        logger.info(f"Pod started for {self.hostname}")

    async def _run_sse(self) -> None:
        transport = self._ensure_sse_transport()
        await transport.start()
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await transport.stop()

    def stop(self) -> None:
        """Gracefully stop the pod."""
        self._running = False
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info(f"Pod stopped for {self.hostname}")
