# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tier-1 tests for the device diagnostics endpoint (F-52).

GET /devices/{hostname}/diagnostics — the "why is my device down?" read combining
registry status, last check, spec/manifest state, spawn error, and the circuit
breaker (in-process pods only).
"""

import copy
from contextlib import asynccontextmanager

import yaml
from fastapi.testclient import TestClient

from device_mcp_gateway.core.translator import McpManifest, McpTool
from device_mcp_gateway.main import create_app
from device_mcp_gateway.pods.device_pod import DevicePod
from device_mcp_gateway.rbac import Authenticator
from device_mcp_gateway.shared.registry_backend import DeviceConfig

# --- breaker snapshot (real pybreaker) ---------------------------------------


def _pod():
    manifest = McpManifest(
        server_name="m",
        server_version="1",
        hostname="dev",
        tools=[McpTool(name="t", description="d", schema={"type": "object"}, method="GET", path="/x")],
    )
    return DevicePod(hostname="dev", manifest=manifest, base_url="http://dev.local")


def test_breaker_snapshot_reports_closed_state():
    snap = _pod().breaker_snapshot()
    assert snap["state"] == "closed"
    assert snap["fail_counter"] == 0
    assert snap["fail_max"] == 5
    assert snap["reset_timeout"] == 60


# --- endpoint ----------------------------------------------------------------


class _StubProfile:
    def __init__(self, pod, pod_active=True):
        self.pod = pod
        self.pod_active = pod_active


class _StubRegistry:
    def __init__(self, device, manifest=None, profile=None):
        self._device = device
        self._manifest = manifest
        self._profile = profile

    async def get_device(self, hostname):
        return self._device

    async def get_manifest(self, hostname):
        return self._manifest

    def get_profile(self, hostname):
        return self._profile

    def tls_profile_for(self, hostname):
        return {"source": "fleet", "verify": True, "ca_bundle": None, "client_cert": False}


def _app(mode, registry, monkeypatch):
    cfg = copy.deepcopy(yaml.safe_load(open("config.yaml")))
    cfg.setdefault("registry", {})["mode"] = mode
    if mode == "distributed":
        cfg.setdefault("gateway", {})["allow_plaintext_credentials"] = True
        cfg["gateway"]["allow_anonymous"] = True
        cfg.setdefault("redis", {})["allow_insecure"] = True
    app = create_app(override_config=cfg)

    # Bare TestClient still fires the lifespan (real Redis dial / registry calls);
    # replace it with a no-op and inject the stub state directly.
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


def test_diagnostics_404_for_unknown_device(monkeypatch):
    app = _app("embedded", _StubRegistry(device=None), monkeypatch)
    with TestClient(app) as client:
        assert client.get("/v1/devices/nope/diagnostics").status_code == 404


def test_diagnostics_embedded_with_active_pod_reports_breaker(monkeypatch):
    device = _device(spec_hash="abc123", spawn_error=None)
    manifest = {"tools": [{"name": "t"}, {"name": "u"}]}
    reg = _StubRegistry(device, manifest=manifest, profile=_StubProfile(_pod(), pod_active=True))
    app = _app("embedded", reg, monkeypatch)
    with TestClient(app) as client:
        body = client.get("/v1/devices/dev/diagnostics").json()
    assert body["hostname"] == "dev"
    assert body["mode"] == "embedded"
    assert body["spec_hash"] == "abc123"
    assert body["has_manifest"] is True
    assert body["tool_count"] == 2
    assert body["breaker"]["available"] is True
    assert body["breaker"]["state"] == "closed"


def test_diagnostics_embedded_without_pod_marks_breaker_unavailable(monkeypatch):
    device = _device(pod_active=False, spawn_error="spec fetch failed")
    reg = _StubRegistry(device, manifest=None, profile=None)
    app = _app("embedded", reg, monkeypatch)
    with TestClient(app) as client:
        body = client.get("/v1/devices/dev/diagnostics").json()
    assert body["spawn_error"] == "spec fetch failed"
    assert body["has_manifest"] is False
    assert body["tool_count"] == 0
    assert body["breaker"]["available"] is False
    assert body["breaker"]["note"] == "no active pod"


def test_diagnostics_distributed_breaker_unavailable_with_note(monkeypatch):
    device = _device(worker_id="worker-7")
    reg = _StubRegistry(device, manifest={"tools": []}, profile=None)
    app = _app("distributed", reg, monkeypatch)
    with TestClient(app) as client:
        body = client.get("/v1/devices/dev/diagnostics").json()
    assert body["mode"] == "distributed"
    assert body["worker_id"] == "worker-7"
    assert body["breaker"]["available"] is False
    assert "worker" in body["breaker"]["note"]
