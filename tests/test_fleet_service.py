# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Unit tests for fleet_service: manifest aggregation, name namespacing/collision
handling, and JSON-RPC dispatch for a fleet (multi-device) MCP session.

No HTTP/Redis here — a fake Registry stand-in provides just the async
get_device/get_manifest and sync get_profile surface fleet_service depends on.
"""

import pytest

from device_mcp_gateway.core.errors import RPC_METHOD_NOT_FOUND
from device_mcp_gateway.fleet_service import (
    aggregate_tools_list_result,
    build_fleet_manifest,
    rewrite_tools_call,
)


class _FakeDevice:
    def __init__(self, pod_active: bool = True):
        self.pod_active = pod_active


class _FakePod:
    def __init__(self, result=None, exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.calls: list[dict] = []

    async def call_tool(self, message: dict):
        self.calls.append(message)
        if self._exc:
            raise self._exc
        if self._result is not None:
            return self._result
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }


class _FakeProfile:
    def __init__(self, pod):
        self.pod = pod


class _FakeReg:
    def __init__(self, devices: dict, manifests: dict, profiles: dict | None = None):
        self._devices = devices
        self._manifests = manifests
        self._profiles = profiles or {}

    async def get_device(self, hostname):
        active = self._devices.get(hostname)
        return None if active is None else _FakeDevice(pod_active=active)

    async def get_manifest(self, hostname):
        return self._manifests.get(hostname)

    def get_profile(self, hostname):
        return self._profiles.get(hostname)


def _tool(name, description="d", schema=None):
    return {"name": name, "description": description, "schema": schema or {}}


# --- build_fleet_manifest ------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregates_and_namespaces_across_devices():
    reg = _FakeReg(
        devices={"devb": True, "deva": True},
        manifests={
            "deva": {"tools": [_tool("get_status")]},
            "devb": {"tools": [_tool("get_status"), _tool("control_fan")]},
        },
    )
    manifest, skipped = await build_fleet_manifest(reg, ["devb", "deva"])

    assert skipped == []
    names = {e.display_name for e in manifest.entries}
    assert names == {"deva_get_status", "devb_get_status", "devb_control_fan"}
    assert manifest.by_display_name["deva_get_status"].hostname == "deva"
    assert manifest.by_display_name["deva_get_status"].real_name == "get_status"


@pytest.mark.asyncio
async def test_deterministic_hostname_ordering_regardless_of_input_order():
    reg = _FakeReg(
        devices={"a": True, "b": True},
        manifests={"a": {"tools": [_tool("t1")]}, "b": {"tools": [_tool("t1")]}},
    )
    m1, _ = await build_fleet_manifest(reg, ["b", "a"])
    m2, _ = await build_fleet_manifest(reg, ["a", "b"])
    assert [e.display_name for e in m1.entries] == [e.display_name for e in m2.entries]
    assert m1.hostnames == ["a", "b"] == m2.hostnames


@pytest.mark.asyncio
async def test_skips_unregistered_and_inactive_devices():
    reg = _FakeReg(
        devices={"good": True, "inactive": False},
        manifests={"good": {"tools": [_tool("t1")]}},
    )
    manifest, skipped = await build_fleet_manifest(reg, ["good", "inactive", "unknown"])
    assert manifest.hostnames == ["good"]
    assert set(skipped) == {"inactive", "unknown"}


@pytest.mark.asyncio
async def test_skips_device_with_no_manifest():
    reg = _FakeReg(devices={"good": True, "no-manifest": True}, manifests={"good": {"tools": [_tool("t1")]}})
    manifest, skipped = await build_fleet_manifest(reg, ["good", "no-manifest"])
    assert manifest.hostnames == ["good"]
    assert skipped == ["no-manifest"]


@pytest.mark.asyncio
async def test_all_invalid_yields_empty_manifest():
    reg = _FakeReg(devices={}, manifests={})
    manifest, skipped = await build_fleet_manifest(reg, ["ghost1", "ghost2"])
    assert manifest.entries == []
    assert manifest.hostnames == []
    assert set(skipped) == {"ghost1", "ghost2"}


@pytest.mark.asyncio
async def test_collision_across_devices_gets_numeric_suffix():
    # "a.b" and "a_b" both sanitize their "_get_status"-suffixed combined name to
    # the same string -- exercise the same numeric-suffix scheme translator.py
    # uses for within-device collisions, extended across devices.
    reg = _FakeReg(
        devices={"a.b": True, "a_b": True},
        manifests={"a.b": {"tools": [_tool("get_status")]}, "a_b": {"tools": [_tool("get_status")]}},
    )
    manifest, skipped = await build_fleet_manifest(reg, ["a.b", "a_b"])
    assert skipped == []
    display_names = [e.display_name for e in manifest.entries]
    assert display_names == ["a_b_get_status", "a_b_get_status_2"]
    # both entries still resolve back to their own distinct hostname
    resolved_hosts = {manifest.by_display_name[n].hostname for n in display_names}
    assert resolved_hosts == {"a.b", "a_b"}


# --- aggregate_tools_list_result / rewrite_tools_call --------------------------


@pytest.mark.asyncio
async def test_aggregate_tools_list_result_shape():
    reg = _FakeReg(
        devices={"a": True}, manifests={"a": {"tools": [_tool("t1", description="desc", schema={"type": "object"})]}}
    )
    manifest, _ = await build_fleet_manifest(reg, ["a"])
    result = aggregate_tools_list_result(manifest, msg_id=7)
    assert result == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"tools": [{"name": "a_t1", "description": "desc", "inputSchema": {"type": "object"}}]},
    }


@pytest.mark.asyncio
async def test_rewrite_tools_call_success_maps_back_to_device():
    reg = _FakeReg(devices={"a": True}, manifests={"a": {"tools": [_tool("get_status")]}})
    manifest, _ = await build_fleet_manifest(reg, ["a"])
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "a_get_status", "arguments": {}}}
    hostname, rewritten = rewrite_tools_call(manifest, message)
    assert hostname == "a"
    assert rewritten["params"]["name"] == "get_status"
    assert rewritten["id"] == 1
    # original message untouched
    assert message["params"]["name"] == "a_get_status"


@pytest.mark.asyncio
async def test_rewrite_tools_call_unknown_name_returns_rpc_error():
    reg = _FakeReg(devices={"a": True}, manifests={"a": {"tools": [_tool("get_status")]}})
    manifest, _ = await build_fleet_manifest(reg, ["a"])
    message = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
    result = rewrite_tools_call(manifest, message)
    assert isinstance(result, dict)
    assert result["error"]["code"] == RPC_METHOD_NOT_FOUND
    assert result["id"] == 2
