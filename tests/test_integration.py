# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""
Integration Test: Register Mock OpenAPI Device -> Verify Gateway Health & Metrics
"""

import time
from unittest.mock import AsyncMock

import pytest


def test_register_and_metrics(client, mock_target_url):
    """
    1. POST /devices to register mock endpoint.
    2. Verify GET /devices shows the registered device as reachable.
    3. Verify GET /metrics reports correct counts.
    4. Verify GET /health returns healthy status.
    """
    # 1. Register device pointing to our mock API
    reg_payload = {"hostname": "mock-iot.local", "base_url": mock_target_url, "auth_type": "none"}
    reg_resp = client.post("/v1/devices", json=reg_payload)
    assert reg_resp.status_code == 200, f"Registration failed: {reg_resp.json()}"
    reg_data = reg_resp.json()
    # F-19: write returns the write envelope + the full resulting device.
    assert reg_data["status"] == "registered"
    assert "provisioning" in reg_data
    device = reg_data["device"]
    assert device["hostname"] == "mock-iot.local"
    assert "pod_active" in device
    assert "reachable" in device
    assert "spawn_error" in device
    print("[+] Device registered via /devices endpoint")

    # 2. Verify device appears in the list and is reachable
    dev_resp = client.get("/v1/devices")
    assert dev_resp.status_code == 200
    devices = dev_resp.json().get("devices", [])
    mock_dev = next((d for d in devices if d["hostname"] == "mock-iot.local"), None)
    assert mock_dev is not None, "Device not found in /devices list"
    assert mock_dev.get("reachable") is True, "Mock API should be reachable"
    print(f"[+] Device status verified: {mock_dev}")

    # 3. Verify metrics endpoint returns correct counts
    met_resp = client.get("/v1/metrics/summary")
    assert met_resp.status_code == 200
    metrics = met_resp.json()
    assert metrics.get("total_registered", 0) >= 1, f"Total registered mismatch: {metrics}"
    assert metrics.get("reachable_devices", 0) >= 1, f"Reachable mismatch: {metrics}"
    assert metrics.get("unreachable_devices", 0) == 0, f"Unreachable mismatch: {metrics}"
    print(f"[+] Metrics verified: {metrics}")

    # 4. Verify /health endpoint
    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    health = health_resp.json()
    assert health.get("status") == "healthy", f"Health status unexpected: {health}"
    assert health.get("registered_devices", 0) >= 1, f"Health devices count mismatch: {health}"
    print(f"[+] Health endpoint verified: {health}")

    # 5. Verify deregistration works
    del_resp = client.delete("/v1/devices/mock-iot.local")
    assert del_resp.status_code == 200, f"Deregistration failed: {del_resp.json()}"
    print("[+] Device deregistered successfully")

    # Verify it's gone
    dev_resp2 = client.get("/v1/devices")
    devices2 = dev_resp2.json().get("devices", [])
    remaining = [d for d in devices2 if d["hostname"] == "mock-iot.local"]
    assert len(remaining) == 0, "Device should be removed from list"
    print("[+] Device removal verified")

    print("[PASS] Full integration test passed.")


@pytest.mark.asyncio
async def test_spec_change_replaces_pod():
    """When a device's OpenAPI spec hash changes, the running pod is torn down and replaced."""
    from device_mcp_gateway.registry.server import Registry

    spec_v1 = {
        "openapi": "3.0.3",
        "info": {"title": "Sensor API", "version": "1.0.0"},
        "paths": {"/status": {"get": {"operationId": "get_status", "responses": {"200": {"description": "OK"}}}}},
    }
    spec_v2 = {
        "openapi": "3.0.3",
        "info": {"title": "Sensor API", "version": "2.0.0"},
        "paths": {
            "/status": {"get": {"operationId": "get_status", "responses": {"200": {"description": "OK"}}}},
            "/health": {"get": {"operationId": "check_health", "responses": {"200": {"description": "OK"}}}},
        },
    }

    registry = Registry(config={"spec_cache_ttl": 10, "health_check_interval": 10, "max_concurrent_pods": 10})

    async def _reachable(profile):
        # Stands in for the network hop only. The real check *records* what it found
        # (`reachable` + `last_check`) and the orchestration below reads those fields
        # back, so a stub that merely returns True tests nothing — it used to pass on
        # the dataclass default, which is the F-66 bug the defaults no longer allow.
        profile.config.reachable = True
        profile.config.last_check = time.time()
        return True

    registry.check_reachability = _reachable
    registry._spec_service._discover_spec = AsyncMock(return_value=spec_v1)

    device_cfg = await registry.register_device(hostname="spec-change-test", base_url="http://test.local")
    assert device_cfg.pod_active, "Pod should be active after initial registration"

    # Get the embedded-mode DeviceProfile (has pod reference and spec_data)
    profile = registry.get_profile("spec-change-test")
    assert profile is not None
    initial_pod = profile.pod
    assert len(profile.pod.manifest.tools) == 1, "Initial pod should have 1 tool"

    # Simulate upstream spec update
    registry._spec_service._discover_spec = AsyncMock(return_value=spec_v2)
    registry._spec_service._cache._store.clear()
    profile.config.last_check = 0.0  # force cache miss so fetch_spec re-fetches

    # Spec re-fetch is now side-effect-free (F-12); the health-loop orchestration
    # is what detects the change and replaces the pod, so drive that path.
    await registry._health_check_one(profile)

    profile = registry.get_profile("spec-change-test")  # re-fetch after pod replacement
    assert profile.pod_active, "Pod should still be active after spec change"
    assert profile.pod is not initial_pod, "A new pod should have been spawned"
    assert len(profile.pod.manifest.tools) == 2, "New pod should expose 2 tools from spec v2"
    # F-41 governance: the additive tool-set change bumped the client-pollable revision.
    assert profile.config.tools_revision == 1, "tools_revision should bump on a tool-set change"

    await registry.shutdown()
    print("[PASS] Spec change pod replacement verified.")


def test_register_unreachable_device_reports_failure(client):
    """Registering an unreachable device still returns 200; reachability is then
    determined off the request path (F-11 async registration) and surfaced as
    reachable=False / pod_active=False once provisioning settles."""
    hostname = "unreachable-device.local"
    resp = client.post(
        "/v1/devices",
        json={
            "hostname": hostname,
            "base_url": "http://192.0.2.1",  # TEST-NET, guaranteed unreachable
            "auth_type": "none",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["device"]["hostname"] == hostname
    assert data["device"]["pod_active"] is False
    # A device whose probe outlives the inline budget returns still-provisioning;
    # poll GET /devices/{h} until the background provisioning settles reachability.
    deadline = time.time() + 30
    final = data
    while time.time() < deadline:
        final = client.get(f"/v1/devices/{hostname}").json()
        if final["reachable"] is False:
            break
        time.sleep(0.5)
    assert final["reachable"] is False
    assert final["pod_active"] is False

    # Clean up
    client.delete(f"/v1/devices/{hostname}")
    print("[PASS] Unreachable device registration feedback verified.")
