# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""A pod that proxies a remote MCP server (ADR-0009).

Sibling of ``DevicePod`` over the shared ``BasePod``: identical lifecycle, egress, token
bucket, breaker and JSON-RPC router, differing only in what a tool call *is* — one
JSON-RPC POST to the upstream instead of an HTTP request assembled from an OpenAPI
operation.

Three decisions worth stating, because each is a place the obvious implementation is wrong:

**``tools/list`` is served from the stored manifest, not forwarded live.** Forwarding would
hand the upstream a direct channel to the LLM, bypassing the sanitisation, deduping and
count cap applied at discovery — and would make every list a network round-trip. The served
list is what discovery approved; a change to it goes through the governance machinery
(``tools_revision``, the breaking-change record) like any other.

**Upstream content is returned untouched.** It is already MCP content. Re-wrapping it the
way the OpenAPI pod wraps its result envelope would double-encode every proxied result.

**A JSON-RPC error in the result does not trip the breaker.** That is the upstream's
tool-level "no" — the analogue of a 4xx, which does not trip the breaker on the OpenAPI
path either. Only transport failures and 5xx do. Getting this backwards means one
badly-behaved tool opens the breaker for every tool on that upstream.

**Resources and prompts follow the tools model** (ADR-0028, superseding ADR-0009's "tools
only"). The *lists* are served from the manifest discovery sanitised; *content* —
``resources/read``, ``prompts/get`` — is forwarded and returned untouched, exactly as a tool
result is. Only a URI or prompt the manifest lists can be fetched, so a client cannot use
the gateway to reach anything discovery did not see. Resource URIs are the upstream's own,
passed through verbatim: the gateway mints no URI shape, which was ADR-0009's objection.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from device_mcp_gateway import metrics
from device_mcp_gateway.audit import redact_url
from device_mcp_gateway.core.errors import RPC_INTERNAL_ERROR, RPC_INVALID_PARAMS, rpc_error
from device_mcp_gateway.pods.pod_base import BasePod
from device_mcp_gateway.upstream.mcp_client import McpUpstreamError, StreamableHttpClient

from pybreaker import CircuitBreakerError


class McpProxyPod(BasePod):
    """Serves a remote MCP server's tools to this gateway's clients."""

    def _build_dispatch(self) -> None:
        # LLM-facing name -> the name the upstream actually knows. Sanitising/deduping at
        # discovery means these differ whenever the upstream's name was not a valid MCP
        # tool name or collided; calling with the wrong one reaches nothing.
        self._wire_names: dict[str, str] = {
            t.name: (t.proxy.upstream_tool_name if t.proxy else t.name) for t in self.manifest.tools
        }
        # Only what discovery listed is fetchable (see the module docstring).
        self._resource_uris: set[str] = {r.uri for r in self.manifest.resources}
        self._upstream = StreamableHttpClient(
            url=self.base_url,
            get_client=self._client,
            auth=self.auth,
            timeout=self._request_timeout,
        )

    async def aclose(self) -> None:
        """Terminate the upstream session before dropping the HTTP client.

        The pod holds one session for its whole lifetime, so a pod replace on every tool-set
        change would otherwise abandon one on the upstream each time.
        """
        await self._upstream.close_session()
        await super().aclose()

    async def _dispatch_tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._rate_limiter:
            await self._rate_limiter.acquire()
        wire_name = self._wire_names.get(name, name)
        try:
            # Only transport failures and 5xx raise out of send(), so one logical call is
            # one breaker outcome — matching the OpenAPI pod's policy exactly.
            with self._breaker.calling():
                resp = await self._upstream.call_tool(wire_name, arguments)
        except CircuitBreakerError:
            logger.warning(f"Circuit breaker open for MCP upstream {redact_url(self.base_url)}")
            metrics.circuit_breaker_opens_total.labels(hostname=self.hostname).inc()
            return _error_content("Upstream unavailable: circuit breaker open (too many recent failures)")
        except McpUpstreamError as exc:
            return _error_content(f"Upstream MCP call failed: {exc}")

        if resp.status_code >= 400:
            # A 4xx is a client/config problem (bad credentials, wrong endpoint). Surfaced
            # as a tool error, deliberately outside the breaker.
            return _error_content(f"Upstream MCP server returned HTTP {resp.status_code}")
        if resp.message is None:
            return _error_content("Upstream MCP server returned an empty response")
        if "error" in resp.message:
            err = resp.message["error"] or {}
            return _error_content(f"Upstream tool error: {err.get('message', err)}", code=err.get("code"))

        result = resp.message.get("result")
        if not isinstance(result, dict):
            return _error_content("Upstream MCP server returned no result object")
        # Already MCP content — hand it back exactly as received.
        return result

    async def _read_resource(self, uri: str, msg_id: Any) -> dict[str, Any]:
        if uri not in self._resource_uris:
            return rpc_error(RPC_INVALID_PARAMS, msg_id, message=f"Unknown resource URI: {uri}")
        return await self._forward("resources/read", msg_id, lambda: self._upstream.read_resource(uri))

    async def _get_prompt(self, name: str, arguments: dict[str, str], msg_id: Any) -> dict[str, Any]:
        prompt = self._prompts[name]
        wire_name = prompt.upstream_name or name
        missing = [a for a in prompt.required_arguments if a not in arguments]
        if missing:
            # Checked here because the upstream declared it; saves a round trip that can
            # only fail, and gives the client the same -32602 a local prompt would.
            return rpc_error(RPC_INVALID_PARAMS, msg_id, message=f"Missing required prompt arguments: {missing}")
        return await self._forward("prompts/get", msg_id, lambda: self._upstream.get_prompt(wire_name, arguments))

    async def _forward(self, method: str, msg_id: Any, send: Any) -> dict[str, Any]:
        """Forward one read-type request upstream; the result is returned untouched.

        Same breaker and rate-limit policy as a tool call, but failures are JSON-RPC errors:
        unlike ``tools/call``, these methods have no ``isError`` result shape to carry a
        failure in-band.
        """
        if self._rate_limiter:
            await self._rate_limiter.acquire()
        try:
            with self._breaker.calling():
                resp = await send()
        except CircuitBreakerError:
            metrics.circuit_breaker_opens_total.labels(hostname=self.hostname).inc()
            return rpc_error(RPC_INTERNAL_ERROR, msg_id, message="Upstream unavailable: circuit breaker open")
        except McpUpstreamError as exc:
            return rpc_error(RPC_INTERNAL_ERROR, msg_id, message=f"Upstream {method} failed: {exc}")
        if resp.status_code >= 400:
            return rpc_error(RPC_INTERNAL_ERROR, msg_id, message=f"Upstream {method} returned HTTP {resp.status_code}")
        if resp.message is None:
            return rpc_error(RPC_INTERNAL_ERROR, msg_id, message=f"Upstream {method} returned an empty response")
        if "error" in resp.message:
            err = resp.message["error"] or {}
            # The upstream's code is untrusted and its numbering is not ours (-32002 meant
            # "not found" in one era and nothing in another), so it is reported in the
            # message and mapped onto one of our two codes.
            code = err.get("code")
            ours = RPC_INVALID_PARAMS if code in (-32602, -32002) else RPC_INTERNAL_ERROR
            return rpc_error(ours, msg_id, message=f"Upstream {method} error: {err.get('message', err)} (code {code})")
        result = resp.message.get("result")
        if not isinstance(result, dict):
            return rpc_error(RPC_INTERNAL_ERROR, msg_id, message=f"Upstream {method} returned no result object")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_content(message: str, code: Any = None) -> dict[str, Any]:
    """An MCP tool-error result.

    Returned as ``isError`` content rather than a JSON-RPC error so the client sees a failed
    *tool call* rather than a failed protocol request — the same distinction the OpenAPI pod
    draws with its error envelope. The shape is MCP's own, with no extension fields: an
    unused non-standard key would ship to every client while being tested by nothing.
    """
    text = message if code is None else f"{message} (code {code})"
    return {"content": [{"type": "text", "text": text}], "isError": True}
