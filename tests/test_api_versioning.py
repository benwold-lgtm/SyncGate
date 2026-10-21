# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for F-15 — external REST API versioning + MCP protocol negotiation.

Part A: the device-management API is served under /v1 (hard cutover — root paths
404), while operational probes (/health, /readyz) stay unversioned so K8s liveness/
readiness and the Prometheus scrape contract are unaffected.

Part B: the MCP `initialize` handshake negotiates `protocolVersion` — it echoes the
client's requested version when supported, otherwise advertises our preferred
(newest) version, rather than hardcoding a single version.
"""

from __future__ import annotations

import pytest

from device_mcp_gateway import API_V1_PREFIX
from device_mcp_gateway.core.translator import McpManifest
from device_mcp_gateway.pods.device_pod import DevicePod
from device_mcp_gateway.core.protocol import (
    HANDSHAKE_PROTOCOL_VERSIONS as SUPPORTED_PROTOCOL_VERSIONS,
)
from device_mcp_gateway.core.protocol import (
    PREFERRED_HANDSHAKE_VERSION as PREFERRED_PROTOCOL_VERSION,
)
from device_mcp_gateway.core.protocol import negotiate_protocol_version

# --- Part A: REST /v1 cutover ------------------------------------------------


def test_management_api_is_served_under_v1(client):
    assert client.get(f"{API_V1_PREFIX}/devices").status_code == 200
    assert client.get(f"{API_V1_PREFIX}/metrics/summary").status_code == 200


def test_root_management_paths_are_gone(client):
    # Hard cutover (not a dual-mount): the old unversioned paths no longer resolve.
    assert client.get("/devices").status_code == 404
    assert client.get("/metrics/summary").status_code == 404
    assert client.get("/admin/overview").status_code == 404


def test_probes_stay_unversioned(client):
    # Probes are infra contracts wired into K8s/Prometheus — they must not move.
    assert client.get("/health").status_code == 200
    assert client.get("/readyz").status_code in (200, 503)
    assert client.get(f"{API_V1_PREFIX}/health").status_code == 404
    assert client.get(f"{API_V1_PREFIX}/readyz").status_code == 404


# --- Part B: MCP protocol-version negotiation --------------------------------


def test_preferred_is_newest_supported():
    assert PREFERRED_PROTOCOL_VERSION == SUPPORTED_PROTOCOL_VERSIONS[0]


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
def test_negotiate_echoes_supported_version(version):
    assert negotiate_protocol_version(version) == version


@pytest.mark.parametrize("requested", ["9999-01-01", "", None, 42, {"x": 1}])
def test_negotiate_falls_back_to_preferred_for_unsupported(requested):
    assert negotiate_protocol_version(requested) == PREFERRED_PROTOCOL_VERSION


def _pod() -> DevicePod:
    manifest = McpManifest(server_name="x", server_version="1.0", hostname="dev")
    return DevicePod(hostname="dev", manifest=manifest, base_url="http://dev.local")


@pytest.mark.asyncio
async def test_initialize_echoes_client_protocol_version():
    pod = _pod()
    resp = await pod._handle_mcp_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert resp["result"]["protocolVersion"] == "2025-06-18"


@pytest.mark.asyncio
async def test_initialize_falls_back_when_version_unknown_or_absent():
    pod = _pod()
    unknown = await pod._handle_mcp_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
    )
    assert unknown["result"]["protocolVersion"] == PREFERRED_PROTOCOL_VERSION

    absent = await pod._handle_mcp_message({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
    assert absent["result"]["protocolVersion"] == PREFERRED_PROTOCOL_VERSION
