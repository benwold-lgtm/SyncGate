# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Per-device MCP SSE transport routes (mode-aware)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette import EventSourceResponse

from device_mcp_gateway import API_V1_PREFIX, metrics
from device_mcp_gateway.api.dispatch import _GATEWAY_ID, require_serving_device, spawn_timeout_watcher
from device_mcp_gateway.audit import audit_log
from device_mcp_gateway.observability import tracing
from device_mcp_gateway.ratelimit import rate_limit, rate_limit_principal
from device_mcp_gateway.rbac import SCOPE_TOOLS_CALL, require_scope
from device_mcp_gateway.registry.server import Registry
from device_mcp_gateway.security import fingerprint as fp

router = APIRouter()


@router.get("/devices/{hostname}/sse", dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def device_sse_stream(
    request: Request,
    hostname: str,
    session_id: str | None = Query(None),
    client_id: str | None = Query(None),  # deprecated alias
):
    reg: Registry = request.app.state.registry
    mode = request.app.state.mode
    device = await reg.get_device(hostname)

    device = require_serving_device(device, hostname)
    if device.transport != "sse":
        raise HTTPException(status_code=400, detail="Device transport is not SSE")

    # Bind the session to the principal that opens it (F-37) so another caller
    # holding tools:call can't post to it. ``principal`` is set by the auth
    # dependency on this protected route.
    _principal = getattr(request.state, "principal", None)
    _owner_subject = _principal.subject if _principal else "unknown"

    if mode == "distributed":
        # Distributed: server-assigned session ID; route results via Redis pub/sub
        session_router = request.app.state.session_router
        effective_id = str(uuid.uuid4())
        endpoint_url = f"{API_V1_PREFIX}/devices/{hostname}/messages?session_id={effective_id}"
        await session_router.register(effective_id, hostname, _GATEWAY_ID, owner=_owner_subject)

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
    else:
        # Embedded: in-process SSE transport via DeviceProfile.pod
        profile = reg.get_profile(hostname)
        if not profile or not profile.pod:
            raise HTTPException(status_code=404, detail="Device pod not found")

        # Always server-assigned — ignore client-supplied session_id/client_id
        # to prevent session hijacking (S2).
        effective_id = str(uuid.uuid4())
        endpoint_url = f"{API_V1_PREFIX}/devices/{hostname}/messages?session_id={effective_id}"
        transport = profile.pod._ensure_sse_transport()
        transport.register_client(effective_id, endpoint_url)
        request.app.state.session_owners[effective_id] = _owner_subject  # F-37

        async def _counted_stream():
            metrics.active_sse_connections.inc()
            try:
                async for ev in transport.event_stream(effective_id):
                    yield ev
            finally:
                metrics.active_sse_connections.dec()
                request.app.state.session_owners.pop(effective_id, None)

        return EventSourceResponse(_counted_stream())


@router.post(
    "/devices/{hostname}/messages",
    dependencies=[
        Depends(require_scope(SCOPE_TOOLS_CALL)),
        # Per-IP burst guard + per-principal fair-share for tool calls (F-16); the
        # per-principal budget is the per-identity ceiling across all its IPs.
        Depends(rate_limit("600/minute", "messages")),
        Depends(rate_limit_principal("1200/minute", "messages")),
    ],
)
async def device_sse_message(
    hostname: str,
    request: Request,
    session_id: str | None = Query(None),
    client_id: str | None = Query(None),  # deprecated alias
):
    reg: Registry = request.app.state.registry
    mode = request.app.state.mode
    cfg = request.app.state.config
    device = await reg.get_device(hostname)

    device = require_serving_device(device, hostname)
    if device.transport != "sse":
        raise HTTPException(status_code=400, detail="Device transport is not SSE")

    effective_id = session_id or client_id
    if not effective_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    payload = await request.json()
    _principal = getattr(request.state, "principal", None)
    _subject = _principal.subject if _principal else "unknown"
    _rid = getattr(request.state, "request_id", "-")

    # ADR-0015 §9: refuse the *use* methods on a device whose fingerprint changed and was
    # not approved, when the effective policy is enforce. Keyed on the JSON-RPC method, so
    # initialize/tools-list still answer and the device stays distinguishable from a dead
    # one. Checked here at admission AND again at worker dispatch: this gives the caller an
    # immediate, specific reason, while the worker check catches anything already queued
    # when the change was detected.
    _fp_reason = fp.quarantine_reason(
        device.fingerprint_state,
        device.fingerprint_policy,
        (cfg.get("security", {}) or {}).get("fingerprint_policy"),
        payload.get("method") if isinstance(payload, dict) else None,
    )
    if _fp_reason:
        raise HTTPException(status_code=409, detail=f"{hostname}: {_fp_reason}")

    # Principal↔session binding (F-37): a session may only be posted to by the
    # principal that opened it. Distributed mode stores the owner on the Redis
    # session hash (survives across replicas); embedded mode keeps it in-process.
    if mode == "distributed":
        _sess = await request.app.state.session_router.get(effective_id)
        _owner = _sess.get("owner") if _sess else None
    else:
        _owner = request.app.state.session_owners.get(effective_id)
    if _owner is not None and _owner != _subject:
        raise HTTPException(status_code=403, detail="session_id is bound to a different principal")

    if mode == "distributed":
        _backend = request.app.state.registry._backend
        # Admission control (F-06): if the device's call stream is backed up
        # past the watermark, the worker isn't draining it and a new call
        # would queue behind work that gets silently trimmed at MAXLEN —
        # surfacing only as a 30s client timeout. Fast-fail with 429 instead
        # so the backpressure is visible and retryable at the source.
        _backlog_limit = cfg.get("registry", {}).get("call_backlog_limit", 1000)
        if _backlog_limit > 0 and await _backend.call_backlog(hostname) >= _backlog_limit:
            metrics.calls_rejected_overload_total.labels(hostname=hostname).inc()
            audit_log(
                "tool dispatch shed: call backlog over watermark",
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
        _method = payload.get("method", "?") if isinstance(payload, dict) else "?"
        # Open a dispatch span and inject its W3C context so the worker's
        # execution span joins the same trace (F-14). No-op when tracing is off.
        with tracing.start_span(
            "mcp.tool_dispatch",
            attributes={"mcp.hostname": hostname, "mcp.method": _method, "mcp.rid": _rid},
        ):
            _carrier = tracing.inject_carrier()
            await _backend.publish_tool_call(
                hostname=hostname,
                request_id=request_id,
                session_id=effective_id,
                gateway_id=_GATEWAY_ID,
                message=payload,
                rid=_rid,
                traceparent=_carrier.get("traceparent", ""),
                subject=_subject,
            )
        # Guard against a lost call (no worker consuming): if no result is
        # marked within the timeout, emit an error event on the SSE stream
        # so the client doesn't hang forever (F6).
        if isinstance(payload, dict) and payload.get("id") is not None:
            spawn_timeout_watcher(
                request,
                request.app.state.session_router,
                effective_id,
                request_id,
                payload.get("id"),
                hostname,
                _rid,
            )
        audit_log(
            "tool dispatch",
            hostname=hostname,
            subject=_subject,
            method=payload.get("method", "?"),
            status="dispatched",
            rid=_rid,
        )
        return {"status": "accepted"}
    else:
        profile = reg.get_profile(hostname)
        if not profile or not profile.pod:
            raise HTTPException(status_code=500, detail="Pod reference lost")
        sse_transport = profile.pod.sse_transport
        if not sse_transport:
            raise HTTPException(status_code=500, detail="SSE transport not initialised")
        _t = time.perf_counter()
        response = await sse_transport.handle_message(effective_id, payload)
        _dur = (time.perf_counter() - _t) * 1000
        # Classify on the error key, not "result": in embedded mode
        # handle_message returns {"status": "accepted"} on success (the JSON-RPC
        # result goes out over the SSE stream, not in this response) and
        # {"error": ...} on failure, so it never carries a "result" key. Keying
        # on "result" marked every successful call an error, inverting both the
        # audit trail and tool_calls_total for the mode actually in use.
        _status = "error" if not response or "error" in response else "ok"
        _method = payload.get("method", "?") if isinstance(payload, dict) else "?"
        metrics.tool_calls_total.labels(hostname=hostname, method=_method, status=_status).inc()
        metrics.tool_call_duration_seconds.labels(hostname=hostname).observe(_dur / 1000.0)
        audit_log(
            "tool dispatch",
            hostname=hostname,
            subject=_subject,
            method=payload.get("method", "?"),
            status=_status,
            duration_ms=round(_dur, 1),
            rid=_rid,
            # Always empty in embedded mode (§11a refuses grants without a shared store), but
            # emitted from the same place as the distributed branch so the two records cannot
            # drift into different shapes.
        )
        return response
