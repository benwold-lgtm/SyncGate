# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for tool-set change governance surfacing (F-41):

* GET /devices/{hostname}/tools/diff — serves the last tool-set change.
* ToolSetDiff.to_record — the persisted/served change record.
* Backend persistence of the last change (Memory backend; cleared on delete).
"""

import asyncio
import copy
from contextlib import asynccontextmanager

import yaml
from fastapi.testclient import TestClient

from device_mcp_gateway.core import manifest_diff
from device_mcp_gateway.core.manifest_diff import diff_tools
from device_mcp_gateway.main import create_app
from device_mcp_gateway.rbac import Authenticator
from device_mcp_gateway.shared.registry_backend import DeviceConfig, MemoryRegistryBackend

# --- ToolSetDiff.to_record ---------------------------------------------------


def _t(name, method="GET", schema=None):
    return {"name": name, "method": method, "schema": schema or {}}


def test_to_record_shape_and_revision():
    diff = diff_tools([_t("a")], [_t("a"), _t("b", method="POST")])
    rec = diff.to_record(revision=5, at=123.0)
    assert rec["tools_revision"] == 5
    assert rec["at"] == 123.0
    assert rec["added"] == ["b"]
    assert rec["removed"] == []
    assert rec["breaking"] is False


def test_to_record_flags_breaking_with_reasons():
    diff = diff_tools([_t("a"), _t("b")], [_t("a")])  # removed "b"
    rec = diff.to_record(2, 0.0)
    assert rec["removed"] == ["b"]
    assert rec["breaking"] is True
    assert rec["breaking_reasons"]


def test_to_record_caps_name_lists():
    new = [_t(f"t{i}") for i in range(manifest_diff._MAX_NAMES + 10)]
    rec = diff_tools([], new).to_record(1, 0.0)
    assert len(rec["added"]) == manifest_diff._MAX_NAMES


# --- backend persistence -----------------------------------------------------


def test_memory_backend_persists_and_clears_tool_change():
    backend = MemoryRegistryBackend()

    async def go():
        await backend.set_device("h", DeviceConfig(hostname="h", base_url="http://h"))
        # None until a change is recorded.
        assert await backend.get_last_tool_change("h") is None
        await backend.set_last_tool_change("h", {"tools_revision": 2, "added": ["x"]})
        got = await backend.get_last_tool_change("h")
        assert got["added"] == ["x"]
        # Cleaned up with the device.
        await backend.delete_device("h")
        assert await backend.get_last_tool_change("h") is None

    asyncio.run(go())


# --- endpoint ----------------------------------------------------------------


class _StubRegistry:
    """Only the two methods the diff endpoint touches."""

    def __init__(self, device, last_change=None):
        self._device = device
        self._last_change = last_change

    async def get_device(self, hostname):
        return self._device

    async def get_last_tool_change(self, hostname):
        return self._last_change


def _app(registry, monkeypatch, mode="embedded"):
    cfg = copy.deepcopy(yaml.safe_load(open("config.yaml")))
    cfg.setdefault("registry", {})["mode"] = mode
    if mode == "distributed":
        cfg.setdefault("gateway", {})["allow_plaintext_credentials"] = True
        cfg["gateway"]["allow_anonymous"] = True
        cfg.setdefault("redis", {})["allow_insecure"] = True
    app = create_app(override_config=cfg)

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    app.router.lifespan_context = _noop_lifespan
    app.state.mode = mode
    app.state.registry = registry
    monkeypatch.setattr(app.state, "authenticator", Authenticator({}, enabled=False))
    return app


def _device(**over):
    base = dict(hostname="dev", base_url="http://dev.local", reachable=True, pod_active=True)
    base.update(over)
    return DeviceConfig(**base)


def test_diff_404_for_unknown_device(monkeypatch):
    app = _app(_StubRegistry(device=None), monkeypatch)
    with TestClient(app) as client:
        assert client.get("/v1/devices/nope/tools/diff").status_code == 404


def test_diff_null_when_no_change_recorded(monkeypatch):
    # Device exists, revision 0, no change observed yet → last_change is null.
    app = _app(_StubRegistry(_device(tools_revision=0), last_change=None), monkeypatch)
    with TestClient(app) as client:
        body = client.get("/v1/devices/dev/tools/diff").json()
    assert body["hostname"] == "dev"
    assert body["tools_revision"] == 0
    assert body["last_change"] is None


def test_diff_returns_recorded_change(monkeypatch):
    record = {
        "tools_revision": 3,
        "at": 1717500000.0,
        "added": ["new_tool"],
        "removed": ["gone_tool"],
        "changed": [],
        "breaking": True,
        "breaking_reasons": ["tool(s) removed: ['gone_tool']"],
    }
    app = _app(_StubRegistry(_device(tools_revision=3), last_change=record), monkeypatch)
    with TestClient(app) as client:
        body = client.get("/v1/devices/dev/tools/diff").json()
    assert body["tools_revision"] == 3
    assert body["last_change"]["added"] == ["new_tool"]
    assert body["last_change"]["removed"] == ["gone_tool"]
    assert body["last_change"]["breaking"] is True


def test_diff_works_in_distributed_mode(monkeypatch):
    # Endpoint is mode-agnostic (reads via the registry facade).
    record = {
        "tools_revision": 1,
        "at": 0.0,
        "added": ["a"],
        "removed": [],
        "changed": [],
        "breaking": False,
        "breaking_reasons": [],
    }
    app = _app(_StubRegistry(_device(tools_revision=1), last_change=record), monkeypatch, mode="distributed")
    with TestClient(app) as client:
        body = client.get("/v1/devices/dev/tools/diff").json()
    assert body["last_change"]["added"] == ["a"]
    assert body["last_change"]["breaking"] is False
