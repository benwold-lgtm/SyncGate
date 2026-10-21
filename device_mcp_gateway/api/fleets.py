# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Fleet handles over HTTP (ADR-0027 §5).

- ``POST /v1/fleets`` mints a handle over a fixed device set. Same scope and cap as opening a
  fleet session (``tools:call``, ``registry.fleet_max_devices``).
- ``GET /v1/fleets`` lists the caller's own handles; ``DELETE /v1/fleets/{handle}`` removes one.
- ``POST /v1/fleet/{handle}/mcp`` is an ordinary MCP endpoint over that set, in **both** eras:
  a stateless request names the fleet by its URL, and an ``initialize`` opens a handshake
  session over the handle's devices.

**Bound at mint, checked on every request.** A request from anyone but the minting principal
gets 404, never 403, so the endpoint does not reveal which handles exist; and no request can
add a device — only minting a new handle can.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from device_mcp_gateway.api.streamable import _check_accept, _read_payload
from device_mcp_gateway.api.streamable_fleet import serve_fleet_handshake, serve_fleet_modern
from device_mcp_gateway.audit import AUDIT_OUTCOME_SUCCESS, audit_request
from device_mcp_gateway.core import protocol
from device_mcp_gateway.fleet_handles import FleetHandle, FleetHandleStore, looks_like_handle, mint_handle
from device_mcp_gateway.ratelimit import rate_limit, rate_limit_principal
from device_mcp_gateway.rbac import SCOPE_TOOLS_CALL, require_scope

router = APIRouter()

#: Handles one principal may hold. Handles are persistent configuration; without a cap, a
#: token that mints in a loop grows Redis or SQLite without bound.
DEFAULT_MAX_HANDLES_PER_PRINCIPAL = 100


def _subject(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.subject if principal else "unknown"


def _store(request: Request) -> FleetHandleStore:
    return request.app.state.fleet_handles


async def _owned(request: Request, handle: str) -> FleetHandle:
    """The caller's handle, or 404 — the same 404 whether it is missing or someone else's."""
    fleet = await _store(request).get(handle) if looks_like_handle(handle) else None
    if fleet is None or fleet.owner != _subject(request):
        raise HTTPException(status_code=404, detail="Fleet handle not found")
    return fleet


@router.post("/fleets", status_code=201, dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def mint_fleet(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail='Body must be JSON: {"devices": ["host", ...]}')
    devices = body.get("devices") if isinstance(body, dict) else None
    if not isinstance(devices, list) or not devices or not all(isinstance(d, str) and d.strip() for d in devices):
        raise HTTPException(status_code=400, detail="'devices' must be a non-empty list of hostnames")
    hostnames = sorted({d.strip() for d in devices})
    cfg = request.app.state.config.get("registry", {})
    max_devices = cfg.get("fleet_max_devices", 25)
    if len(hostnames) > max_devices:
        raise HTTPException(status_code=400, detail=f"Too many devices requested (max {max_devices})")

    reg = request.app.state.registry
    unknown = [h for h in hostnames if await reg.get_device(h) is None]
    if unknown:
        # Refused at mint rather than skipped: a typo would otherwise produce a fleet that is
        # silently one device short for its whole life. Membership is still re-resolved on
        # every request, so a member deregistered later is skipped then, not refused.
        raise HTTPException(status_code=400, detail=f"Not registered: {', '.join(unknown)}")

    subject = _subject(request)
    store = _store(request)
    limit = cfg.get("fleet_handles_per_principal", DEFAULT_MAX_HANDLES_PER_PRINCIPAL)
    if len(await store.list_for(subject)) >= limit:
        raise HTTPException(
            status_code=409,
            detail=f"At most {limit} fleet handles per principal; delete one with DELETE /v1/fleets/{{handle}}",
        )
    fleet = FleetHandle(handle=mint_handle(), owner=subject, devices=hostnames)
    await store.create(fleet)
    audit_request(
        request, "fleet.mint", outcome=AUDIT_OUTCOME_SUCCESS, target=fleet.handle, devices=",".join(hostnames)
    )
    return fleet.public()


@router.get("/fleets", dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def list_fleets(request: Request) -> dict[str, Any]:
    return {"fleets": [f.public() for f in await _store(request).list_for(_subject(request))]}


@router.delete("/fleets/{handle}", status_code=204, dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def delete_fleet(handle: str, request: Request) -> Response:
    fleet = await _owned(request, handle)
    await _store(request).delete(fleet.handle)
    audit_request(request, "fleet.delete", outcome=AUDIT_OUTCOME_SUCCESS, target=fleet.handle)
    return Response(status_code=204)


@router.post(
    "/fleet/{handle}/mcp",
    dependencies=[
        Depends(require_scope(SCOPE_TOOLS_CALL)),
        # The fleet surface's budgets: a handle is the same operation as ?devices=, and must
        # not be a second allowance.
        Depends(rate_limit("600/minute", "fleet-messages")),
        Depends(rate_limit_principal("1200/minute", "fleet-messages")),
    ],
)
async def fleet_handle_post(handle: str, request: Request, response: Response):
    """One MCP message for the fleet a handle names, in whichever era the client speaks."""
    _check_accept(request)
    fleet = await _owned(request, handle)
    payload = await _read_payload(request)
    if protocol.is_modern_request(request.headers, payload):
        return await serve_fleet_modern(request, payload, fleet.devices)
    return await serve_fleet_handshake(request, response, payload, lambda: list(fleet.devices))


@router.get("/fleet/{handle}/mcp", dependencies=[Depends(require_scope(SCOPE_TOOLS_CALL))])
async def fleet_handle_get(handle: str, request: Request):
    await _owned(request, handle)
    raise HTTPException(status_code=405, detail="This endpoint offers no server-initiated stream")
