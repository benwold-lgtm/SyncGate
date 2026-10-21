# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Streamable HTTP for the **fleet** surface (Phase 6, Workstream A3).

One MCP session spanning several devices (ADR-0008), over the transport A1/A2 introduced.
Stands to ``api/fleet.py`` exactly as ``api/streamable.py`` stands to ``api/sse.py``.

**This resolves the inline-vs-stream split.** On the SSE fleet surface, distributed mode
answers ``initialize``/``ping``/``tools/list`` inline on the POST body but returns
``{"status": "accepted"}`` for ``tools/call`` and delivers the real result on the stream —
while embedded mode delivers all four on the stream. Both shapes are legal MCP, but the
asymmetry cost a debugging round and is recorded as a finding in ``docs/testing-gaps.md``.
Here there is no stream to deliver anything on, so **every method answers on the POST that
asked**, identically in both modes. That is the point of the transport, not a bonus.

``tools/call`` reuses the ``ResultExchange`` seam from A1/A2 rather than re-implementing
dispatch: ``fleet_service.rewrite_tools_call`` resolves a namespaced display name to
``(hostname, rewritten)``, and the exchange takes it from there. So the fleet path gets
cross-replica result correlation, admission control and the timeout contract for free, and
there is exactly one implementation of the hard part.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from loguru import logger

from device_mcp_gateway import fleet_service, metrics
from device_mcp_gateway.api.dispatch import _GATEWAY_ID
from device_mcp_gateway.api.exchange import ExchangeTimeout, ExchangeUnavailable
from device_mcp_gateway.api.streamable import (
    SESSION_HEADER,
    _check_accept,
    _exchange_for,
    _read_payload,
)
from device_mcp_gateway.audit import audit_log
from device_mcp_gateway.core.errors import RPC_METHOD_NOT_FOUND, RPC_NO_WORKER, rpc_error
from device_mcp_gateway.fleet_service import FleetManifest, fleet_initialize_response
from device_mcp_gateway.ratelimit import rate_limit, rate_limit_principal
from device_mcp_gateway.rbac import SCOPE_TOOLS_CALL, require_scope
from device_mcp_gateway.registry.server import Registry

router = APIRouter()

#: Session-hash field holding the device list a fleet session was opened over.
_HOSTS_FIELD = "fleet_hosts"


# --- session state, mode-aware ------------------------------------------------
#
# Same split as the per-device transport: Redis in distributed mode so any replica can
# serve a request, an in-process map in embedded mode. Confined to these four helpers.


async def _fleet_create(request: Request, session_id: str, hostnames: list[str], subject: str) -> None:
    if request.app.state.mode == "distributed":
        await request.app.state.session_router.register(
            session_id,
            "",  # a fleet session is not one device's
            _GATEWAY_ID,
            owner=subject,
            extra={_HOSTS_FIELD: ",".join(hostnames)},
        )
    else:
        request.app.state.session_owners[session_id] = subject
        request.app.state.fleet_hosts[session_id] = list(hostnames)


async def _fleet_session(request: Request, session_id: str) -> tuple[str, list[str]] | None:
    """``(owner, hostnames)`` for a fleet session, or None if there is no such session."""
    if request.app.state.mode == "distributed":
        session = await request.app.state.session_router.get(session_id)
        if not session or _HOSTS_FIELD not in session:
            # No fleet-hosts field means this id is a *per-device* session. Refusing it here
            # keeps the two surfaces from being used interchangeably, which would otherwise
            # let a device session be driven through the fleet tool namespace.
            return None
        hosts = [h for h in session.get(_HOSTS_FIELD, "").split(",") if h]
        return session.get("owner") or "", hosts
    if session_id not in request.app.state.fleet_hosts:
        return None
    return request.app.state.session_owners.get(session_id) or "", request.app.state.fleet_hosts[session_id]


async def _fleet_delete(request: Request, session_id: str) -> None:
    if request.app.state.mode == "distributed":
        await request.app.state.session_router.delete(session_id)
    else:
        request.app.state.session_owners.pop(session_id, None)
        request.app.state.fleet_hosts.pop(session_id, None)


async def _publish_tools(request: Request, session_id: str, manifest: FleetManifest) -> None:
    """Publish the display-name lookup table for this fleet session.

    **Nothing on this transport reads it** — every method here rebuilds the manifest, which is
    what lets a device that was down at open join later. It is written because the *SSE* fleet
    route does read it (``api/fleet.py``), so a session opened over Streamable HTTP stays
    usable on ``POST /fleet/messages`` while HTTP+SSE is being retired. Verified, not assumed:
    ``test_a_streamable_opened_fleet_session_is_usable_on_the_sse_route``.

    Written on initialize and refreshed on every ``tools/list``, so it never lags the manifest
    this transport last reported.
    """
    if request.app.state.mode != "distributed":
        return
    await request.app.state.session_router.set_fleet_tools(
        session_id,
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


def _resolve_hostnames(devices: str | None, cfg: dict[str, Any]) -> list[str]:
    hostnames = [h.strip() for h in (devices or "").split(",") if h.strip()]
    if not hostnames:
        raise HTTPException(
            status_code=400,
            detail="'devices' must list at least one hostname on the initialize request",
        )
    max_devices = cfg.get("registry", {}).get("fleet_max_devices", 25)
    if len(hostnames) > max_devices:
        raise HTTPException(status_code=400, detail=f"Too many devices requested (max {max_devices})")
    return hostnames


@router.post(
    "/fleet/mcp",
    dependencies=[
        Depends(require_scope(SCOPE_TOOLS_CALL)),
        # The same budgets as /fleet/messages: this is the same operation over a different
        # transport, so a client must not get a second allowance by switching.
        Depends(rate_limit("600/minute", "fleet-messages")),
        Depends(rate_limit_principal("1200/minute", "fleet-messages")),
    ],
)
async def fleet_streamable_post(request: Request, response: Response, devices: str | None = Query(default=None)):
    """Accept one JSON-RPC message for a fleet session and answer it on this response."""
    _check_accept(request)

    reg: Registry = request.app.state.registry
    cfg = request.app.state.config
    payload = await _read_payload(request)
    method = payload.get("method", "")
    msg_id = payload.get("id")
    _principal = getattr(request.state, "principal", None)
    _subject = _principal.subject if _principal else "unknown"
    _rid = getattr(request.state, "request_id", "-")

    session_id = request.headers.get(SESSION_HEADER)
    hostnames: list[str]

    if method == "initialize":
        # `devices` is required here and ignored afterwards: the session carries the fleet,
        # so a later request cannot quietly widen it by passing a longer list.
        hostnames = _resolve_hostnames(devices, cfg)
        manifest, skipped = await fleet_service.build_fleet_manifest(reg, hostnames)
        if not manifest.hostnames:
            raise HTTPException(status_code=404, detail=f"No reachable devices among: {hostnames}")
        if skipped:
            logger.warning(f"Fleet session skipped unavailable devices: {skipped}")

        # The server mints the id, as everywhere else — a client-supplied one would let a
        # caller graft themselves onto another principal's session (F-37).
        session_id = str(uuid.uuid4())
        await _fleet_create(request, session_id, hostnames, _subject)
        await _publish_tools(request, session_id, manifest)
        response.headers[SESSION_HEADER] = session_id

        return _json(fleet_initialize_response(msg_id, payload.get("params")), response)

    if session_id is None:
        # Same reasoning as the per-device transport: in distributed mode the session id
        # names the Redis stream a worker publishes results to, so an empty one would put
        # every sessionless caller's results in a single shared bucket.
        raise HTTPException(
            status_code=400,
            detail=f"{SESSION_HEADER} is required; send an initialize request first",
        )

    found = await _fleet_session(request, session_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Fleet session not found or expired")
    owner, hostnames = found
    if owner and owner != _subject:
        raise HTTPException(status_code=403, detail="Session is bound to a different principal")

    if method.startswith("notifications/"):
        return Response(status_code=202, headers=dict(response.headers))
    if method == "ping":
        return _json({"jsonrpc": "2.0", "id": msg_id, "result": {}}, response)

    if method == "tools/list":
        # Rebuilt rather than served from what was cached at open, and rebuilt against the
        # ORIGINALLY requested hostnames — so a device that was down when the session opened
        # joins the fleet once it comes back, instead of being missing for the session's whole
        # life. The distributed lookup table is refreshed from the same manifest so that
        # tools/call resolves against exactly what was just listed.
        manifest, _skipped = await fleet_service.build_fleet_manifest(reg, hostnames)
        await _publish_tools(request, session_id, manifest)
        return _json(fleet_service.aggregate_tools_list_result(manifest, msg_id), response)

    if method == "tools/call":
        manifest, _skipped = await fleet_service.build_fleet_manifest(reg, hostnames)
        outcome = fleet_service.rewrite_tools_call(manifest, payload)
        if isinstance(outcome, dict):
            return _json(outcome, response)  # unknown tool name — already a JSON-RPC error
        hostname, rewritten = outcome

        _t = time.perf_counter()
        try:
            result = await _exchange_for(request).exchange(
                hostname,
                rewritten,
                session_id=session_id,
                subject=_subject,
                rid=_rid,
            )
        except ExchangeUnavailable as exc:
            if exc.status_code == 404:
                # One device going away must not read as "the fleet endpoint is gone", and
                # must leave the session usable for the others. A JSON-RPC error in a 200
                # says exactly that; an HTTP 404 would not.
                return _json(
                    rpc_error(RPC_NO_WORKER, msg_id, message=f"Device '{hostname}' is no longer active"),
                    response,
                )
            # Backpressure (429) is a transport-level signal and stays one.
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except ExchangeTimeout as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        _dur = (time.perf_counter() - _t) * 1000

        _status = "error" if isinstance(result, dict) and "error" in result else "ok"
        metrics.tool_calls_total.labels(hostname=hostname, method="tools/call", status=_status).inc()
        metrics.tool_call_duration_seconds.labels(hostname=hostname).observe(_dur / 1000.0)
        audit_log(
            "fleet tool dispatch",
            hostname=hostname,
            subject=_subject,
            method="tools/call",
            status=_status,
            duration_ms=round(_dur, 1),
            rid=_rid,
            transport="streamable-http",
        )
        if result is None:
            return Response(status_code=202, headers=dict(response.headers))
        return _json(result, response)

    if msg_id is not None:
        return _json(rpc_error(RPC_METHOD_NOT_FOUND, msg_id, message=f"Method not found: {method}"), response)
    return Response(status_code=202, headers=dict(response.headers))


def _json(body: dict[str, Any], response: Response) -> Response:
    """Serialise a JSON-RPC response, preserving headers set on the request's Response."""
    return Response(content=json.dumps(body), media_type="application/json", headers=dict(response.headers))


@router.get("/fleet/mcp", dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def fleet_streamable_get(request: Request):
    """No server-initiated stream is offered here — see the per-device route for why."""
    raise HTTPException(status_code=405, detail="This endpoint offers no server-initiated stream")


@router.delete("/fleet/mcp", dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def fleet_streamable_delete(request: Request):
    """Explicit session termination by the client."""
    session_id = request.headers.get(SESSION_HEADER)
    if not session_id:
        raise HTTPException(status_code=400, detail=f"{SESSION_HEADER} is required to terminate a session")
    _principal = getattr(request.state, "principal", None)
    _subject = _principal.subject if _principal else "unknown"
    found = await _fleet_session(request, session_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Fleet session not found or expired")
    owner, _hosts = found
    if owner and owner != _subject:
        raise HTTPException(status_code=403, detail="Session is bound to a different principal")
    await _fleet_delete(request, session_id)
    return Response(status_code=204)
