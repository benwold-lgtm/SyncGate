# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Fleet MCP sessions: aggregate multiple devices' tools into one MCP session.

Every device has its own MCP endpoint (``/v1/devices/{hostname}/mcp``), so an MCP
client wanting N devices would otherwise need N separate connections. This module
builds an aggregated, hostname-namespaced view of several devices' tools for a
single session, and maps a client's ``tools/call`` back to the right device.

Deliberately mode-agnostic: it only reads via ``Registry.get_device``/
``get_manifest``, which return the same shapes in embedded and distributed mode.
Per-mode dispatch (in-process ``pod.call_tool`` vs. publishing onto a device's
Redis call-stream) stays in the route handlers, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from device_mcp_gateway import __version__
from device_mcp_gateway.core.errors import RPC_METHOD_NOT_FOUND, rpc_error
from device_mcp_gateway.core.protocol import FLEET_CAPABILITIES, initialize_response
from device_mcp_gateway.core.translator import _sanitize_name, dedupe_name


@dataclass
class FleetToolEntry:
    """One device's tool, as exposed under the fleet's namespaced tool list."""

    display_name: str  # what the MCP client sees, e.g. "unifi_list_devices"
    hostname: str
    real_name: str  # the tool's name on that device, for dispatch
    description: str
    schema: dict[str, Any]


@dataclass
class FleetManifest:
    """Aggregated tool view for one fleet session."""

    hostnames: list[str] = field(default_factory=list)
    entries: list[FleetToolEntry] = field(default_factory=list)
    by_display_name: dict[str, FleetToolEntry] = field(default_factory=dict)


async def build_fleet_manifest(reg: Any, hostnames: list[str]) -> tuple[FleetManifest, list[str]]:
    """Aggregate manifests for ``hostnames`` into one namespaced :class:`FleetManifest`.

    Hostnames are processed in sorted order so the resulting display-name
    assignment (and any collision suffixes) is deterministic for a given device
    set. An unregistered, pod-inactive, or manifest-less hostname is skipped
    (and reported back) rather than failing the whole session — a large fleet
    request shouldn't be an all-or-nothing affair over one stale device.

    Returns ``(manifest, skipped_hostnames)``.
    """
    manifest = FleetManifest()
    skipped: list[str] = []
    used_names: set[str] = set()

    for hostname in sorted(set(hostnames)):
        device = await reg.get_device(hostname)
        if not device or not device.pod_active:
            skipped.append(hostname)
            continue
        manifest_dict = await reg.get_manifest(hostname)
        if not manifest_dict:
            skipped.append(hostname)
            continue

        manifest.hostnames.append(hostname)
        for tool in manifest_dict.get("tools", []):
            base = _sanitize_name(f"{hostname}_{tool['name']}")
            display_name = dedupe_name(base, used_names)
            if display_name != base:
                logger.warning(
                    f"Fleet tool name collision: '{base}' (hostname={hostname}, "
                    f"tool={tool['name']!r}) renamed to '{display_name}'"
                )
            used_names.add(display_name)
            entry = FleetToolEntry(
                display_name=display_name,
                hostname=hostname,
                real_name=tool["name"],
                description=tool.get("description", ""),
                schema=tool.get("schema", {}),
            )
            manifest.entries.append(entry)
            manifest.by_display_name[display_name] = entry

    return manifest, skipped


def aggregate_tools_list_result(manifest: FleetManifest, msg_id: Any) -> dict[str, Any]:
    """Build the JSON-RPC ``tools/list`` response for a fleet session."""
    tools = [{"name": e.display_name, "description": e.description, "inputSchema": e.schema} for e in manifest.entries]
    return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}


def rewrite_tools_call(manifest: FleetManifest, message: dict[str, Any]) -> tuple[str, dict[str, Any]] | dict[str, Any]:
    """Resolve a fleet-session ``tools/call`` to the device that should handle it.

    Returns ``(hostname, rewritten_message)`` — ``rewritten_message`` is a copy of
    ``message`` with ``params.name`` swapped from the fleet-namespaced display name
    back to the device's own tool name, keeping the client's original ``id`` intact.
    Returns a JSON-RPC error dict directly (never a routing guess) when the display
    name isn't in this session's manifest.
    """
    msg_id = message.get("id")
    params = message.get("params") or {}
    display_name = params.get("name") or ""
    entry = manifest.by_display_name.get(display_name)
    if entry is None:
        return rpc_error(RPC_METHOD_NOT_FOUND, msg_id, message=f"Tool not found: {display_name}")

    rewritten = dict(message)
    rewritten["params"] = dict(params)
    rewritten["params"]["name"] = entry.real_name
    return entry.hostname, rewritten


def fleet_initialize_response(msg_id: Any, params: Any) -> dict[str, Any]:
    """The ``initialize`` response every fleet surface sends: tools only (ADR-0008)."""
    return initialize_response(
        msg_id, params, server_name="mcp-fleet", server_version=__version__, capabilities=FLEET_CAPABILITIES
    )
