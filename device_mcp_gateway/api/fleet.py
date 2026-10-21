# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Fleet MCP session routes (mode-aware).

One MCP session spanning several devices, so a client (e.g. Claude Desktop via
the mcp-remote bridge) doesn't need one connection per device. Tool names are
namespaced by hostname (fleet_service.build_fleet_manifest); reuses the same
SCOPE_TOOLS_CALL as the per-device routes — there is no per-device ACL layer
today, so this doesn't expand what a caller can already reach. See ADR-0008.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from sse_starlette import EventSourceResponse

from device_mcp_gateway import API_V1_PREFIX, fleet_service, metrics
from device_mcp_gateway.api.dispatch import _GATEWAY_ID, spawn_timeout_watcher
from device_mcp_gateway.audit import audit_log
from device_mcp_gateway.core.errors import RPC_METHOD_NOT_FOUND, rpc_error
from device_mcp_gateway.fleet_service import fleet_initialize_response
from device_mcp_gateway.observability import tracing
from device_mcp_gateway.ratelimit import rate_limit, rate_limit_principal
from device_mcp_gateway.rbac import SCOPE_TOOLS_CALL, require_scope
from device_mcp_gateway.registry.server import Registry

router = APIRouter()


@router.get("/fleet/sse", dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def fleet_sse_stream(request: Request, devices: str = Query(...)):
    reg: Registry = request.app.state.registry
    mode = request.app.state.mode
    cfg = request.app.state.config

    hostnames = [h.strip() for h in devices.split(",") if h.strip()]
    if not hostnames:
        raise HTTPException(status_code=400, detail="'devices' must list at least one hostname")
    _max_devices = cfg.get("registry", {}).get("fleet_max_devices", 25)
    if len(hostnames) > _max_devices:
        raise HTTPException(status_code=400, detail=f"Too many devices requested (max {_max_devices})")

    manifest, skipped = await fleet_service.build_fleet_manifest(reg, hostnames)
    if not manifest.hostnames:
        raise HTTPException(status_code=404, detail=f"No reachable devices among: {hostnames}")
    if skipped:
        logger.warning(f"Fleet session skipped unavailable devices: {skipped}")

    _principal = getattr(request.state, "principal", None)
    _owner_subject = _principal.subject if _principal else "unknown"
    effective_id = str(uuid.uuid4())
    endpoint_url = f"{API_V1_PREFIX}/fleet/messages?session_id={effective_id}"

    if mode == "distributed":
        session_router = request.app.state.session_router
        await session_router.register(effective_id, "", _GATEWAY_ID, owner=_owner_subject)
        await session_router.set_fleet_tools(
            effective_id,
            {
                e.display_name: {
                    "hostname": e.hostname,
                    "real_name": e.real_name,
                    "description": e.description,
                    "schema": e.schema,
                }
                for e in manifest.entries
            },
        )

        async def event_generator():
            metrics.active_sse_connections.inc()
            try:
                yield {"event": "endpoint", "data": endpoint_url}
                try:
                    async for result in session_router.subscribe(effective_id):
                        yield {"event": "message", "data": json.dumps(result)}
                except asyncio.CancelledError:
                    pass
                finally:
                    await session_router.delete(effective_id)
            finally:
                metrics.active_sse_connections.dec()

        return EventSourceResponse(event_generator())

    # Embedded: no single pod owns a fleet session, so it gets its own
    # transport (rather than attaching to any one device's SseTransport)
    # whose handler fans a call out to whichever device it resolves to.
    from device_mcp_gateway.pods.sse_server import SseTransport

    # Per-device embedded sessions see tool-set changes when a pod is replaced
    # mid-session; a fleet manifest frozen at session-open would not. Rebuild it
    # on each tools/list — against the ORIGINALLY requested hostnames, so a
    # device that was down/skipped at open joins once it comes up — and keep the
    # refreshed lookup for subsequent tools/call dispatch.
    _manifest_ref = {"manifest": manifest}

    async def _handle(message: dict) -> dict | None:
        if isinstance(message, dict) and message.get("method") == "tools/list":
            fresh, _skipped = await fleet_service.build_fleet_manifest(reg, hostnames)
            _manifest_ref["manifest"] = fresh
        return await fleet_service.handle_fleet_message(reg, _manifest_ref["manifest"], message)

    transport = SseTransport(f"fleet:{effective_id}", _handle)
    transport.register_client(effective_id, endpoint_url)
    request.app.state.fleet_transports[effective_id] = transport
    request.app.state.session_owners[effective_id] = _owner_subject  # F-37

    async def _counted_stream():
        metrics.active_sse_connections.inc()
        try:
            async for ev in transport.event_stream(effective_id):
                yield ev
        finally:
            metrics.active_sse_connections.dec()
            request.app.state.session_owners.pop(effective_id, None)
            request.app.state.fleet_transports.pop(effective_id, None)

    return EventSourceResponse(_counted_stream())


@router.post(
    "/fleet/messages",
    dependencies=[
        Depends(require_scope(SCOPE_TOOLS_CALL)),
        Depends(rate_limit("600/minute", "fleet-messages")),
        Depends(rate_limit_principal("1200/minute", "fleet-messages")),
    ],
)
async def fleet_sse_message(request: Request, session_id: str = Query(...)):
    mode = request.app.state.mode
    cfg = request.app.state.config
    payload = await request.json()
    _principal = getattr(request.state, "principal", None)
    _subject = _principal.subject if _principal else "unknown"
    _rid = getattr(request.state, "request_id", "-")

    if mode == "distributed":
        reg: Registry = request.app.state.registry
        session_router = request.app.state.session_router
        _sess = await session_router.get(session_id)
        if _sess is None:
            raise HTTPException(status_code=404, detail="Fleet session not found or expired")
        _owner = _sess.get("owner")
        if _owner is not None and _owner != _subject:
            raise HTTPException(status_code=403, detail="session_id is bound to a different principal")

        tools = await session_router.get_fleet_tools(session_id)
        method = payload.get("method", "") if isinstance(payload, dict) else ""
        msg_id = payload.get("id") if isinstance(payload, dict) else None

        if method == "initialize":
            params = payload.get("params") or {}
            return fleet_initialize_response(msg_id, params)
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            tools_list = [
                {"name": name, "description": e.get("description", ""), "inputSchema": e.get("schema", {})}
                for name, e in (tools or {}).items()
            ]
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools_list}}
        if method == "tools/call":
            params = payload.get("params") or {}
            display_name = params.get("name") or ""
            entry = (tools or {}).get(display_name)
            if entry is None:
                return rpc_error(RPC_METHOD_NOT_FOUND, msg_id, message=f"Tool not found: {display_name}")
            hostname = entry["hostname"]
            rewritten = dict(payload)
            rewritten["params"] = dict(params)
            rewritten["params"]["name"] = entry["real_name"]

            # Same admission-control + dispatch + timeout-watcher sequence the
            # per-device distributed path runs (F-06 / F6), just resolved to
            # whichever hostname this fleet call landed on.
            _backend = reg._backend
            _backlog_limit = cfg.get("registry", {}).get("call_backlog_limit", 1000)
            if _backlog_limit > 0 and await _backend.call_backlog(hostname) >= _backlog_limit:
                metrics.calls_rejected_overload_total.labels(hostname=hostname).inc()
                audit_log(
                    "fleet tool dispatch shed: call backlog over watermark",
                    level="WARNING",
                    hostname=hostname,
                    subject=_subject,
                    status="rejected_overload",
                    rid=_rid,
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Device '{hostname}' is overloaded; retry shortly",
                    headers={"Retry-After": "1"},
                )

            request_id = str(uuid.uuid4())
            with tracing.start_span(
                "mcp.tool_dispatch",
                attributes={"mcp.hostname": hostname, "mcp.method": "tools/call", "mcp.rid": _rid},
            ):
                _carrier = tracing.inject_carrier()
                await _backend.publish_tool_call(
                    hostname=hostname,
                    request_id=request_id,
                    session_id=session_id,
                    gateway_id=_GATEWAY_ID,
                    message=rewritten,
                    rid=_rid,
                    traceparent=_carrier.get("traceparent", ""),
                    subject=_subject,
                )
            if msg_id is not None:
                spawn_timeout_watcher(request, session_router, session_id, request_id, msg_id, hostname, _rid)
            audit_log(
                "fleet tool dispatch",
                hostname=hostname,
                subject=_subject,
                method="tools/call",
                status="dispatched",
                rid=_rid,
            )
            return {"status": "accepted"}

        if msg_id is not None:
            return rpc_error(RPC_METHOD_NOT_FOUND, msg_id, message=f"Method not found: {method}")
        return None

    # Embedded
    transport = request.app.state.fleet_transports.get(session_id)
    if transport is None:
        raise HTTPException(status_code=404, detail="Fleet session not found or expired")

    # Principal <-> session binding (F-37), same as the per-device path.
    _owner = request.app.state.session_owners.get(session_id)
    if _owner is not None and _owner != _subject:
        raise HTTPException(status_code=403, detail="session_id is bound to a different principal")

    # Same contract as the per-device embedded path: the actual JSON-RPC
    # response (if any) is pushed onto the SSE stream by handle_message
    # itself; what it returns here is just the POST's ack/error envelope.
    response = await transport.handle_message(session_id, payload)
    audit_log(
        "fleet tool dispatch",
        subject=_subject,
        method=payload.get("method", "?") if isinstance(payload, dict) else "?",
        status="error" if isinstance(response, dict) and "error" in response else "accepted",
        rid=_rid,
    )
    return response
