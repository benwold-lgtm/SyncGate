# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""A registered device that is not serving answers 503 + Retry-After, not 404.

Found on a live cluster during the v0.3.7 verification: while a device moved between
workers, every MCP endpoint answered 404 "Device pod not found or not active" — telling the
client the device did not exist. On Streamable HTTP that is worse than misleading: 404 is the
spec's signal that a *session* expired, so a conforming client re-initialises straight into
the same 404 instead of backing off. 404 is now reserved for a device that is not registered.
"""

import pytest
from fastapi.testclient import TestClient

import device_mcp_gateway.main as gw_main
from device_mcp_gateway.api.dispatch import INACTIVE_DEVICE_RETRY_AFTER

client = TestClient(gw_main.app)
ACCEPT = {"Accept": "application/json, text/event-stream"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}

# 192.0.2.0/24 is TEST-NET-1: never routable, so the device registers but never gets a pod.
INACTIVE = "inactive-status-test"


@pytest.fixture(scope="module", autouse=True)
def _inactive_device():
    r = client.post("/v1/devices", json={"hostname": INACTIVE, "base_url": "http://192.0.2.99", "auth_type": "none"})
    assert r.status_code == 200, r.text
    assert client.get(f"/v1/devices/{INACTIVE}").json()["pod_active"] is False
    yield
    client.delete(f"/v1/devices/{INACTIVE}")


def _streamable(hostname):
    return client.post(f"/v1/devices/{hostname}/mcp", json=INIT, headers=ACCEPT)


@pytest.mark.parametrize("call", [_streamable], ids=["streamable"])
def test_registered_but_not_serving_is_503_with_retry_after(call):
    r = call(INACTIVE)
    assert r.status_code == 503, r.text
    assert r.headers["Retry-After"] == str(INACTIVE_DEVICE_RETRY_AFTER)
    detail = r.json()["detail"]
    assert "registered but not serving" in detail
    assert f"/v1/devices/{INACTIVE}" in detail  # points at where the reason is visible


@pytest.mark.parametrize("call", [_streamable], ids=["streamable"])
def test_unregistered_is_still_404(call):
    r = call("no-such-device-anywhere")
    assert r.status_code == 404, r.text
    assert "not registered" in r.json()["detail"]
    assert "Retry-After" not in r.headers
