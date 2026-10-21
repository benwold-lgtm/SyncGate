# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Streamable HTTP inbound transport (Phase 6, Workstream A).

The gateway's only inbound transport has been **HTTP+SSE** — `GET /sse` returning an
`endpoint` event, then `POST /messages?session_id=`. That transport is formally Deprecated
with a removal clock, and `transport != "sse"` is a hard reject elsewhere, so there is no
fallback. Streamable HTTP is owed regardless of whether we adopt revision `2026-07-28`.

**Semantics here are unchanged** — this is revision `2025-06-18` over a different transport.
Sessions still exist. The modern stateless era is Workstream B and lands on top of this.

Deliberately a **separate path** (`/mcp`) rather than content negotiation on `/messages`:
HTTP+SSE is scheduled for removal one minor release after this ships, and a separate path
makes that a deletion rather than an unpicking.

**Both modes are supported.** The mode difference is confined to one function,
``_exchange_for``: in embedded mode the pod is in-process and answering is a call, while in
distributed mode the POST may land on any replica and the answer is produced by a worker and
published to Redis, so the replica waits on ``session:{id}:results`` and correlates by
JSON-RPC id. Everything else in this module is transport, and transport does not care.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger

from device_mcp_gateway import metrics
from device_mcp_gateway.api.dispatch import _GATEWAY_ID, require_serving_device
from device_mcp_gateway.api.exchange import (
    DistributedResultExchange,
    EmbeddedResultExchange,
    ExchangeTimeout,
    ExchangeUnavailable,
    ResultExchange,
)
from device_mcp_gateway.audit import audit_log
from device_mcp_gateway.core import protocol
from device_mcp_gateway.ratelimit import rate_limit, rate_limit_principal
from device_mcp_gateway.rbac import SCOPE_TOOLS_CALL, require_scope
from device_mcp_gateway.registry.server import Registry
from device_mcp_gateway.security import fingerprint as fp

router = APIRouter()

#: Header carrying the session identifier, per the Streamable HTTP transport.
SESSION_HEADER = "Mcp-Session-Id"
#: Header a client uses to state the revision it is speaking on non-initialize requests.
PROTOCOL_HEADER = "MCP-Protocol-Version"


def _exchange_for(request: Request) -> ResultExchange:
    """The exchange implementation for this deployment — the only mode branch in this module.

    Everything else here is transport, and transport does not care which mode it is in. That
    was the point of defining the seam before the distributed implementation existed.
    """
    if request.app.state.mode == "distributed":
        return DistributedResultExchange(
            request.app.state.registry._backend,
            request.app.state.session_router,
            _GATEWAY_ID,
            request.app.state.config,
        )
    return EmbeddedResultExchange(request.app.state.registry)


def _check_accept(request: Request) -> None:
    """The transport requires the client to accept both response shapes.

    Enforced rather than assumed: a client that accepts only JSON would break the moment a
    response is streamed, and finding that out at the first long tool call is worse than
    being told at the handshake.
    """
    accept = request.headers.get("accept", "")
    if not accept or "*/*" in accept:
        return
    if "application/json" not in accept:
        raise HTTPException(
            status_code=406,
            detail="Accept must include application/json (and text/event-stream for streamed responses)",
        )


async def _read_payload(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON-RPC 2.0")
    if not isinstance(payload, dict):
        # Batching was removed in 2025-06-18; a list here is a client on an older revision.
        raise HTTPException(
            status_code=400,
            detail="Expected a single JSON-RPC object; batched requests are not supported",
        )
    return payload


async def _session_owner(request: Request, session_id: str) -> str | None:
    """The principal that opened ``session_id``, or None if there is no such session.

    Distributed mode keeps this on the Redis session hash so it survives across replicas —
    a Streamable HTTP request may well land somewhere other than the one that initialized.
    Embedded mode uses the same in-process map as the SSE path, so F-37 holds whichever
    transport opened the session.
    """
    if request.app.state.mode == "distributed":
        session = await request.app.state.session_router.get(session_id)
        return session.get("owner") if session else None
    return request.app.state.session_owners.get(session_id)


async def _session_create(request: Request, hostname: str, session_id: str, subject: str) -> None:
    if request.app.state.mode == "distributed":
        await request.app.state.session_router.register(session_id, hostname, _GATEWAY_ID, owner=subject)
    else:
        request.app.state.session_owners[session_id] = subject


async def _session_delete(request: Request, session_id: str) -> None:
    if request.app.state.mode == "distributed":
        await request.app.state.session_router.delete(session_id)
    else:
        request.app.state.session_owners.pop(session_id, None)


@router.post(
    "/devices/{hostname}/mcp",
    dependencies=[
        Depends(require_scope(SCOPE_TOOLS_CALL)),
        # Same budgets as the SSE message route: this is the same operation over a
        # different transport, so a client must not get a second allowance by switching.
        Depends(rate_limit("600/minute", "messages")),
        Depends(rate_limit_principal("1200/minute", "messages")),
    ],
)
async def device_streamable_post(hostname: str, request: Request, response: Response):
    """Accept one JSON-RPC message and answer it on this response."""
    _check_accept(request)

    reg: Registry = request.app.state.registry
    device = await reg.get_device(hostname)
    device = require_serving_device(device, hostname)

    payload = await _read_payload(request)
    method = payload.get("method", "")
    msg_id = payload.get("id")

    # ADR-0015 §9 — see the equivalent guard in api/sse.py.
    _fp_reason = fp.quarantine_reason(
        device.fingerprint_state,
        device.fingerprint_policy,
        (request.app.state.config.get("security", {}) or {}).get("fingerprint_policy"),
        method,
    )
    if _fp_reason:
        raise HTTPException(status_code=409, detail=f"{hostname}: {_fp_reason}")
    _principal = getattr(request.state, "principal", None)
    _subject = _principal.subject if _principal else "unknown"
    _rid = getattr(request.state, "request_id", "-")

    if protocol.is_modern_request(request.headers, payload):
        return await _serve_modern(request, hostname, payload, subject=_subject, rid=_rid)

    session_id = request.headers.get(SESSION_HEADER)

    if method == "initialize":
        # The server mints the id, as on the SSE path — a client-supplied one would let a
        # caller graft themselves onto another principal's session.
        session_id = str(uuid.uuid4())
        await _session_create(request, hostname, session_id, _subject)  # F-37
        response.headers[SESSION_HEADER] = session_id
    elif session_id is not None:
        owner = await _session_owner(request, session_id)
        if owner is None:
            # Spec: an unknown/expired session id must be rejected so the client re-initializes.
            raise HTTPException(status_code=404, detail="Unknown or expired session")
        if owner != _subject:
            raise HTTPException(status_code=403, detail="Session is bound to a different principal")
    elif msg_id is not None:
        # No session and not initializing. Refusing is not pedantry: in distributed mode the
        # session id names the Redis stream a worker publishes the result to, so an empty one
        # would put every sessionless caller's results in a single shared bucket — both a
        # correctness bug (results delivered to the wrong request) and a disclosure one.
        raise HTTPException(
            status_code=400,
            detail=f"{SESSION_HEADER} is required; send an initialize request first",
        )

    _t = time.perf_counter()
    try:
        result = await _exchange_for(request).exchange(
            hostname,
            payload,
            session_id=session_id or "",
            subject=_subject,
            rid=_rid,
        )
    except ExchangeUnavailable as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ExchangeTimeout as exc:
        # 504, not 500: the call may still be running upstream, so this is not a failed tool
        # call and a client is entitled to treat it as retryable at its own discretion.
        raise HTTPException(status_code=504, detail=str(exc) or "Timed out awaiting device response") from exc
    _record_dispatch(hostname, method, result, _t, subject=_subject, rid=_rid, era="handshake")

    if result is None:
        # A notification (no id) has no response. 202 with an empty body is what the
        # transport specifies; returning `null` would be a malformed JSON-RPC response.
        return Response(status_code=202, headers=dict(response.headers))
    if msg_id is None:
        logger.warning(f"Discarding a response to an id-less message on {hostname} (method={method!r})")
        return Response(status_code=202, headers=dict(response.headers))
    return Response(
        content=json.dumps(result),
        media_type="application/json",
        headers=dict(response.headers),
    )


def _record_dispatch(
    hostname: str, method: str, result: Any, started: float, *, subject: str, rid: str, era: str
) -> None:
    """Metrics and the audit line for one dispatched message, whichever era carried it."""
    dur_ms = (time.perf_counter() - started) * 1000
    status = "error" if isinstance(result, dict) and "error" in result else "ok"
    metrics.tool_calls_total.labels(hostname=hostname, method=method or "?", status=status).inc()
    metrics.tool_call_duration_seconds.labels(hostname=hostname).observe(dur_ms / 1000.0)
    audit_log(
        "tool dispatch",
        hostname=hostname,
        subject=subject,
        method=method or "?",
        status=status,
        duration_ms=round(dur_ms, 1),
        rid=rid,
        transport="streamable-http",
        era=era,
    )


def _modern_json(body: dict[str, Any]) -> Response:
    return Response(content=json.dumps(body), media_type="application/json", status_code=protocol.http_status_for(body))


def _server_identity(request: Request, hostname: str) -> tuple[str, str]:
    """The name and version this device's MCP server presents — what ``initialize`` would say."""
    profile = request.app.state.registry.get_profile(hostname)
    manifest = profile.pod.manifest if profile and profile.pod else None
    if manifest is None:
        return f"mcp-{hostname}", "unknown"
    return manifest.server_name, manifest.server_version


async def _serve_modern(request: Request, hostname: str, payload: dict[str, Any], *, subject: str, rid: str):
    """One stateless (2026-07-28) request: no handshake, no session, the answer on this response.

    Nothing here mints or reads a session. Principal binding (F-37) has no subject on this
    path — there is no state between requests to bind — so each request stands on its own
    authentication and scope check, which the route already applied (ADR-0027 §3).
    """
    msg_id = payload.get("id")
    version = request.headers.get(protocol.PROTOCOL_VERSION_HEADER)

    if "id" not in payload:
        # A notification. This era defines none from client to server over HTTP, and there is
        # no cross-request state for one to act on, so it is acknowledged and dropped — after
        # the one check that applies to it: is the version it names served at all.
        if version not in protocol.MODERN_PROTOCOL_VERSIONS:
            return _modern_json(protocol.unsupported_version(None, version, protocol.MODERN_PROTOCOL_VERSIONS))
        return Response(status_code=202)

    if request.app.state.mode == "distributed":
        # Distributed mode routes stateless results from 0.4.0 Workstream B PR 5 (ADR-0027 §1).
        # Until then, -32022 naming the handshake versions is the answer an auto-negotiating
        # client falls back from cleanly: it reads as "this server speaks the handshake".
        return _modern_json(protocol.unsupported_version(msg_id, version, protocol.HANDSHAKE_PROTOCOL_VERSIONS))

    for header in (protocol.PROTOCOL_VERSION_HEADER, protocol.METHOD_HEADER, protocol.NAME_HEADER):
        if len(request.headers.getlist(header)) > 1:
            # First-copy and last-copy readers would disagree about where this is going.
            return _modern_json(
                protocol.rpc_failure(msg_id, protocol.HEADER_MISMATCH, f"{header} header appears more than once")
            )
    refusal = protocol.classify_modern(payload, request.headers)
    if refusal is not None:
        return _modern_json(refusal)

    method = payload.get("method", "")
    if method not in protocol.MODERN_DEVICE_METHODS:
        return _modern_json(protocol.rpc_failure(msg_id, protocol.METHOD_NOT_FOUND, "Method not found", method))

    server_name, server_version = _server_identity(request, hostname)
    if method == "server/discover":
        result: dict[str, Any] | None = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": protocol.discover_result(
                server_name=server_name, server_version=server_version, capabilities=protocol.DEVICE_CAPABILITIES
            ),
        }
    else:
        _t = time.perf_counter()
        try:
            result = await _exchange_for(request).exchange(hostname, payload, session_id="", subject=subject, rid=rid)
        except ExchangeUnavailable as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        _record_dispatch(hostname, method, result, _t, subject=subject, rid=rid, era="stateless")
    shaped = protocol.modern_response(method, result, server_name=server_name, server_version=server_version)
    if shaped is None:
        return Response(status_code=202)
    return _modern_json(shaped)


@router.get("/devices/{hostname}/mcp", dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def device_streamable_get(hostname: str, request: Request):
    """No server-initiated stream is offered at this endpoint.

    The transport says a server that does not offer one here MUST answer 405, and we
    genuinely do not: the gateway advertises no server-initiated capability
    (`listChanged: false`, no sampling, no roots), so there is nothing to push. Answering
    405 is the honest reading, and it is what tells a conforming client to stop asking.
    """
    raise HTTPException(status_code=405, detail="This endpoint offers no server-initiated stream")


@router.delete("/devices/{hostname}/mcp", dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def device_streamable_delete(hostname: str, request: Request):
    """Explicit session termination by the client."""
    session_id = request.headers.get(SESSION_HEADER)
    if not session_id:
        raise HTTPException(status_code=400, detail=f"{SESSION_HEADER} is required to terminate a session")
    _principal = getattr(request.state, "principal", None)
    _subject = _principal.subject if _principal else "unknown"
    owner = await _session_owner(request, session_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if owner != _subject:
        raise HTTPException(status_code=403, detail="Session is bound to a different principal")
    await _session_delete(request, session_id)
    return Response(status_code=204)
