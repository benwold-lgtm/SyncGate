# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""The MCP protocol facts the gateway serves, in one place.

Until 0.4.0 the ``initialize`` result was built by hand in four places — the device pod, the
SSE fleet, the Streamable HTTP fleet and the fleet service — each with its own copy of the
capability set and server info. Four copies of a wire shape is four places for one of them to
miss a field the next revision requires, and the modern era (``2026-07-28``) adds several. So
the versions, the negotiation and the result shapes live here, and every surface asks.

**Which versions, and why** (roadmap decision 3). Advertised:

- ``2025-11-25`` and ``2025-06-18`` on the ``initialize`` handshake. ``2025-11-25`` is what an
  SDK-default client offers, so it negotiates without a fallback round trip. Echoing it is a
  conformance claim, and ``tests/test_protocol.py`` checks every result shape the gateway
  produces against the official SDK's wire models for it rather than assuming the delta from
  ``2025-06-18`` is harmless.
- ``2026-07-28``, the stateless era, carried per request in ``_meta`` rather than negotiated.

``2025-03-26`` and ``2024-11-05`` were listed until 0.4.0 and never tested against; advertising
an untested version is a claim the gateway cannot back, so they are dropped. A client asking
for one is answered with the preferred version, which is what the handshake specifies.
"""

from __future__ import annotations

from typing import Any

#: Revisions reachable through the ``initialize`` handshake, newest first.
HANDSHAKE_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18")
#: Revisions that use the stateless per-request envelope (no handshake, no session).
MODERN_PROTOCOL_VERSIONS: tuple[str, ...] = ("2026-07-28",)
#: What an ``initialize`` that asks for something unsupported (or nothing) is answered with.
PREFERRED_HANDSHAKE_VERSION: str = HANDSHAKE_PROTOCOL_VERSIONS[0]


def negotiate_protocol_version(requested: Any) -> str:
    """The ``protocolVersion`` to answer an ``initialize`` with.

    Echoes the client's request when it is a handshake revision the gateway serves, and
    otherwise answers the preferred one, which per the spec tells the client to continue on
    that version or disconnect. A modern revision is not negotiable here: it is never reached
    through ``initialize``, so asking for it on the handshake gets the preferred version too.
    """
    if isinstance(requested, str) and requested in HANDSHAKE_PROTOCOL_VERSIONS:
        return requested
    return PREFERRED_HANDSHAKE_VERSION


def server_capabilities(*, resources: bool, prompts: bool) -> dict[str, Any]:
    """The capability object the gateway advertises.

    ``listChanged`` is ``False`` everywhere, honestly: the gateway polls upstreams for tool
    changes and records them (F-41) rather than pushing notifications (roadmap decision 4).
    A device pod advertises resources and prompts even when it has none, because an empty
    list is a truthful "none" where "method not found" would not be. A fleet serves tools only
    (ADR-0008), so it does not advertise them.
    """
    caps: dict[str, Any] = {"tools": {"listChanged": False}}
    if resources:
        caps["resources"] = {"listChanged": False, "subscribe": False}
    if prompts:
        caps["prompts"] = {"listChanged": False}
    return caps


#: A device pod's capabilities: tools, resources and prompts.
DEVICE_CAPABILITIES: dict[str, Any] = server_capabilities(resources=True, prompts=True)
#: A fleet's capabilities: tools only (ADR-0008).
FLEET_CAPABILITIES: dict[str, Any] = server_capabilities(resources=False, prompts=False)


def initialize_response(
    msg_id: Any,
    params: Any,
    *,
    server_name: str,
    server_version: str,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    """The JSON-RPC response to an ``initialize`` request, for any surface."""
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "protocolVersion": negotiate_protocol_version(requested),
            # A copy, so a caller that edits its result cannot change what the next one sends.
            "capabilities": {k: dict(v) for k, v in capabilities.items()},
            "serverInfo": {"name": server_name, "version": server_version},
        },
    }
