# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""
Device Pod - isolated MCP server instance per OpenAPI-documented API/device.

Each pod runs its own MCP event loop serving tools, resources, and prompts.
Pods are spawned/teared by the Registry based on device health and spec availability.

Lifecycle, egress and the JSON-RPC router live in ``pods/pod_base.BasePod``; what remains
here is everything specific to dispatching a tool call as an **HTTP request built from an
OpenAPI operation** — the per-operation closures, header sanitisation (F-25), body encoding
(F-40) and the response-body cap (F-27).
"""

import json
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger
from pybreaker import CircuitBreakerError

from device_mcp_gateway import metrics
from device_mcp_gateway.audit import redact_url
from device_mcp_gateway.auth.base import AbstractAuth
from device_mcp_gateway.core.adapter import (
    ERR_CIRCUIT_OPEN,
    ERR_CONNECTION,
    ERR_INTERNAL,
    ERR_TIMEOUT,
    DeviceAdapter,
)
from device_mcp_gateway.core.backoff import send_with_retry
from device_mcp_gateway.core.errors import (
    RPC_INTERNAL_ERROR,
    RPC_INVALID_PARAMS,
    rpc_error,
)
from device_mcp_gateway.core.translator import McpTool
from device_mcp_gateway.pods.pod_base import BasePod
from device_mcp_gateway.pods.rate_limiter import TokenBucket
from device_mcp_gateway.shared.keys import device_resource_uri

# Headers a tool argument must never be able to set on the upstream request (Tier-0 F-25).
# Without this, an `in: header` parameter could overwrite the device's auth header or
# smuggle routing/cache headers, since untrusted header params were merged over auth.
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "host",
        "content-length",
        "content-type",
        "connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "via",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        # ADR-0026: the correlation id is evidence — it is what joins this gateway's
        # audit record to the device's own log — so a tool argument must not be able to
        # choose it. Deliberately overlapping: the egress hook in core/correlation.py
        # already assigns (not setdefaults) the header after this dict is built, so a
        # smuggled value would lose anyway. The overlap is kept because this check names
        # the cause at the point of the attempt ("dropping reserved header param"), and
        # an outbound path that one day bypasses the guarded client would otherwise lose
        # both defences at once.
        "x-request-id",
    }
)

# Cap the upstream response body the pod will buffer/return to the LLM (Tier-0 F-27).
# An unbounded body is both a memory-DoS vector and an oversized prompt-injection channel.
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB


def _sanitize_header_params(items: dict[str, Any]) -> dict[str, str]:
    """Drop reserved headers and reject CRLF in values (Tier-0 F-25)."""
    safe: dict[str, str] = {}
    for k, v in items.items():
        if k.lower() in _RESERVED_HEADERS:
            logger.warning(f"Dropping reserved header param '{k}' from tool call")
            continue
        sv = str(v)
        if "\r" in sv or "\n" in sv:
            logger.warning(f"Dropping header param '{k}' with CRLF in its value")
            continue
        safe[k] = sv
    return safe


class DevicePod(BasePod):
    """Manages a single per-hostname MCP server instance backed by an OpenAPI API.

    In embedded mode the pod also owns an SseTransport.
    In distributed mode the worker calls call_tool() directly and results
    are routed through Redis pub/sub — no SseTransport is created.
    """

    def _build_dispatch(self) -> None:
        # Per-device request-encoder + response-normalizer seam (F-49 / F-39 / F-40).
        self._adapter = DeviceAdapter(max_response_bytes=_MAX_RESPONSE_BYTES)
        self._register_tools()

    async def _dispatch_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run the operation's closure and serialise its result envelope as a text block.

        The envelope is this gateway's own uniform shape (F-39), not MCP content, so it is
        JSON-encoded into a single text block — unlike a proxied pod, whose upstream
        already returns MCP content blocks.
        """
        result = await self._tool_dispatch[name](**arguments)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    def _register_tools(self) -> None:
        """Register all MCP tools from the manifest as async callables."""
        self._tool_dispatch: dict[str, Any] = {}
        _get_client = self._client  # shared client for all tool closures
        # Capture once so all tool closures share the same per-pod circuit breaker
        # without it appearing in the function signature (Pydantic would warn on it).
        _pod_breaker = self._breaker
        _adapter = self._adapter  # request encoder + response normalizer (F-49)
        _retry_policy = self._retry_policy  # bounded jittered retries (F-05/F-44)
        _hostname = self.hostname

        def _count_retry(_attempt: int, reason: str) -> None:
            metrics.upstream_retries_total.labels(hostname=_hostname, reason=reason).inc()

        # Build each tool's dispatch closure via a factory so the per-tool bindings
        # (tool / auth / base_url / rate_limiter) live in the enclosing scope rather than
        # the call signature. The closure takes only **kwargs, so invoking it as
        # call_api(**arguments) can never collide with an OpenAPI parameter literally named
        # tool/auth/base_url/rate_limiter (which previously raised "got multiple values for
        # argument") — F-04.
        def _make_call_api(
            tool: McpTool,
            auth: AbstractAuth | None,
            base_url: str,
            rate_limiter: TokenBucket | None,
        ) -> Any:
            async def call_api(**kwargs: Any) -> Any:
                if rate_limiter:
                    await rate_limiter.acquire()

                # Split kwargs by declared parameter location. Collision-renamed args map
                # back to their upstream wire name for query/header (F-04); body fields are
                # mapped inside the adapter, and path params are never renamed.
                def _wire(name: str) -> str:
                    return tool.param_wire_names.get(name, name)

                path_params = {k: v for k, v in kwargs.items() if tool.param_locations.get(k) == "path"}
                body_params = {k: v for k, v in kwargs.items() if tool.param_locations.get(k) == "body"}
                query_params = {_wire(k): v for k, v in kwargs.items() if tool.param_locations.get(k) == "query"}
                header_params = {_wire(k): v for k, v in kwargs.items() if tool.param_locations.get(k) == "header"}
                # Params with no declared location fall back to method-appropriate defaults
                unlocated = {k: v for k, v in kwargs.items() if k not in tool.param_locations}
                if tool.method in ("POST", "PUT", "PATCH"):
                    body_params.update(unlocated)
                else:
                    query_params.update(unlocated)

                # URL-encode each path param so a value like '../admin' or 'a/b' can't traverse
                # or inject extra path segments (Tier-1 F-04). Format only the path template —
                # base_url is concatenated raw so a literal '{' in it isn't mis-parsed.
                encoded_path = {k: quote(str(v), safe="") for k, v in path_params.items()}
                try:
                    url = base_url + tool.path.format_map(encoded_path)
                except (KeyError, IndexError, ValueError) as exc:
                    return _adapter.error_envelope(
                        ERR_INTERNAL, f"Path template error for {tool.path}: missing/invalid {exc}", status=500
                    )
                # Start from sanitized, untrusted header params, then apply auth LAST so a
                # tool argument can never override the device's credentials (Tier-0 F-25).
                # Auth may live in a header, query param, or cookie (F-43).
                headers = _sanitize_header_params(header_params)
                auth_material = await auth.apply() if auth else None
                # Encode the body per the operation's declared content type (F-40): JSON,
                # form, multipart, or raw — instead of always sending json=.
                body_kwargs = _adapter.encode_body(tool, body_params)
                # A raw body carries its own content-type header; merge it under auth.
                body_headers = body_kwargs.pop("headers", None)
                if body_headers:
                    headers = {**body_headers, **headers}
                cookies = None
                if auth_material:
                    headers.update(auth_material.headers)
                    if auth_material.params:
                        query_params = {**query_params, **auth_material.params}
                    cookies = auth_material.cookies or None

                async def _send():
                    return await _get_client().request(
                        method=tool.method,
                        url=url,
                        headers=headers,
                        params=query_params or None,
                        cookies=cookies,
                        **body_kwargs,
                    )

                async def _call():
                    # Bounded jittered retries on transient failures — idempotent (GET)
                    # methods only; honors 429 Retry-After (F-05/F-44). Runs inside the
                    # breaker so one logical call = one breaker outcome.
                    resp = await send_with_retry(
                        _send,
                        method=tool.method,
                        policy=_retry_policy,
                        on_retry=_count_retry,
                    )
                    # Only raise on 5xx — device-side failures trip the breaker.
                    # 4xx are client/LLM errors and should not affect circuit state.
                    if 500 <= resp.status_code < 600:
                        resp.raise_for_status()
                    return resp

                try:
                    # calling() is a sync context manager that tracks state for asyncio
                    # (call_async requires Tornado and is not usable in asyncio contexts).
                    with _pod_breaker.calling():
                        resp = await _call()
                    # Normalize into the uniform result envelope (F-39): 4xx becomes an
                    # error rather than a fake success; the body cap (F-27) lives here too.
                    return _adapter.build_result(resp)
                except CircuitBreakerError:
                    logger.warning(f"Circuit breaker open for pod {redact_url(base_url)}")
                    metrics.circuit_breaker_opens_total.labels(hostname=self.hostname).inc()
                    return _adapter.error_envelope(
                        ERR_CIRCUIT_OPEN,
                        "Device unavailable: circuit breaker open (too many recent failures)",
                        status=503,
                    )
                except httpx.HTTPStatusError as e:
                    # 5xx (raised above so it trips the breaker) → normalized error envelope.
                    return _adapter.normalize_http_error(e.response)
                except httpx.TimeoutException as e:
                    return _adapter.error_envelope(ERR_TIMEOUT, f"Device request timed out: {e}", status=504)
                except httpx.RequestError as e:
                    return _adapter.error_envelope(ERR_CONNECTION, f"Device request failed: {e}", status=502)
                except Exception as e:
                    return _adapter.error_envelope(ERR_INTERNAL, str(e))

            return call_api

        for tool in self.manifest.tools:
            # Bind this tool's snapshot into its own dispatch closure; the JSON-RPC router in
            # BasePod invokes it through _tool_dispatch.
            self._tool_dispatch[tool.name] = _make_call_api(tool, self.auth, self.base_url, self._rate_limiter)
        logger.info(f"Registered {len(self.manifest.tools)} tools for pod {self.hostname}")

    async def _read_resource(self, uri: str, msg_id: Any) -> dict[str, Any]:
        """Fetch a device resource as a plain HTTP GET under ``base_url``."""
        prefix = device_resource_uri(self.hostname)
        if not uri.startswith(prefix):
            return rpc_error(RPC_INVALID_PARAMS, msg_id, message=f"Unknown resource URI: {uri}")
        path = uri[len(prefix) :]
        # Reject path traversal / off-API escapes (Tier-0 F-29): the path is appended to
        # base_url, so '..' or a scheme/host-relative path could read off the intended API.
        if path and (".." in path or not path.startswith("/")):
            return rpc_error(RPC_INVALID_PARAMS, msg_id, message=f"Invalid resource path in URI: {uri}")
        if self._rate_limiter:
            await self._rate_limiter.acquire()
        auth_material = await self.auth.apply() if self.auth else None
        headers = auth_material.headers if auth_material else {}
        try:
            resp = await self._client().get(
                f"{self.base_url}{path}",
                headers=headers,
                params=(auth_material.params or None) if auth_material else None,
                cookies=(auth_material.cookies or None) if auth_material else None,
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            text = json.dumps(resp.json()) if "json" in ct else resp.text
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"contents": [{"uri": uri, "mimeType": ct or "application/json", "text": text}]},
            }
        except Exception as e:
            logger.error(f"Resource read failed for {uri}: {e}")
            return rpc_error(RPC_INTERNAL_ERROR, msg_id, message=str(e))
