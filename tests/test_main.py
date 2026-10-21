# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
import pytest

import device_mcp_gateway.main as gw_main
from device_mcp_gateway.rbac import ALL_SCOPES, Authenticator, Principal
from fastapi.testclient import TestClient

client = TestClient(gw_main.app)


def _enable_admin_auth(monkeypatch, token="test-secret"):
    """Swap in an enabled Authenticator with a single admin key (built directly so
    ambient MCP_* env vars can't interfere)."""
    admin = Principal(subject="key:test", scopes=ALL_SCOPES, auth_method="api_key")
    monkeypatch.setattr(gw_main.app.state, "authenticator", Authenticator({token: admin}, enabled=True))


def test_app_state_config_is_set():
    # Regression: the __main__ launch block reads app.state.config to resolve
    # host/port/log-level. create_app() must populate it or direct
    # `python -m device_mcp_gateway.main` raises AttributeError at startup.
    cfg = gw_main.app.state.config
    assert isinstance(cfg, dict)
    # Mirror the exact access pattern used in the __main__ block.
    cfg.get("server", {}).get("host", "0.0.0.0")
    cfg.get("server", {}).get("port", 8000)
    cfg.get("logging", {}).get("level", "INFO")


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "active_pods" in data
    assert "registered_devices" in data
    assert "version" in data


def test_health_does_not_require_auth(monkeypatch):
    _enable_admin_auth(monkeypatch)
    response = client.get("/health")
    assert response.status_code == 200


def test_auth_rejects_missing_token(monkeypatch):
    _enable_admin_auth(monkeypatch)
    response = client.get("/v1/devices")
    assert response.status_code == 401


def test_auth_rejects_wrong_token(monkeypatch):
    _enable_admin_auth(monkeypatch)
    response = client.get("/v1/devices", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_auth_accepts_correct_token(monkeypatch):
    _enable_admin_auth(monkeypatch)
    response = client.get("/v1/devices", headers={"Authorization": "Bearer test-secret"})
    assert response.status_code == 200


def test_auth_disabled_when_key_not_set(monkeypatch):
    monkeypatch.setattr(gw_main.app.state, "authenticator", Authenticator({}, enabled=False))
    response = client.get("/v1/devices")
    assert response.status_code == 200


def test_admin_overview_aggregate():
    # F14 UI enabler: fleet counts + device list in one call.
    resp = client.get("/v1/admin/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert "mode" in data
    assert set(data["counts"]) == {"total", "active_pods", "reachable", "unreachable"}
    assert isinstance(data["devices"], list)


def test_overview_publishes_when_a_reading_goes_stale():
    """F-71. A consumer showing device health has to know when `reachable` stopped being a
    measurement and became a memory. Publishing it here is the point: a UI that hardcoded a
    threshold would be silently wrong on any deployment that tuned the poll interval — a
    constant that looks like a measurement, which is the shape of several past defects.

    Deliberately not driven by the circuit breaker: `BreakerState` is only readable in
    embedded mode, so a fleet list cannot depend on it. Staleness works in both modes.
    """
    data = client.get("/v1/admin/overview").json()
    stale_after = data["stale_after_seconds"]
    assert stale_after is not None
    # Three missed polls, not one — a single missed check is a blip, and calling healthy
    # devices "unknown" on every hiccup trains operators to ignore the state.
    interval = gw_main.app.state.config.get("registry", {}).get("health_check_interval", 30)
    assert stale_after == interval * 3
    # It must exceed the poll interval, or every device is stale between two healthy checks.
    assert stale_after > interval


@pytest.mark.parametrize("value", ["sse", "http", "stdio"])
def test_register_with_transport_is_refused_whatever_its_value(value):
    """Roadmap decision 6: the field selects nothing since 0.4.0, so it is refused on presence —
    "sse" included — and the refusal names the removal rather than a supported value."""
    response = client.post("/v1/devices", json={"hostname": "x", "base_url": "http://x.local", "transport": value})
    assert response.status_code == 400
    assert "removed in 0.4.0" in response.json()["detail"]


def test_put_unknown_device_returns_404():
    response = client.put(
        "/v1/devices/does-not-exist",
        json={"base_url": "http://new.local", "auth_type": "none"},
    )
    assert response.status_code == 404


def test_put_with_transport_is_refused():
    client.post("/v1/devices", json={"hostname": "put-test", "base_url": "http://192.0.2.99", "auth_type": "none"})
    response = client.put(
        "/v1/devices/put-test",
        json={"base_url": "http://192.0.2.99", "auth_type": "none", "transport": "sse"},
    )
    assert response.status_code == 400
    assert "removed in 0.4.0" in response.json()["detail"]
    client.delete("/v1/devices/put-test")


def test_register_with_rate_limit_rps():
    response = client.post(
        "/v1/devices",
        json={"hostname": "rate-test", "base_url": "http://192.0.2.99", "auth_type": "none", "rate_limit_rps": 5.0},
    )
    assert response.status_code == 200

    metrics = client.get("/v1/metrics/summary").json()
    assert "rate-test" in metrics.get("device_rate_limits", {})
    assert metrics["device_rate_limits"]["rate-test"]["rate_limit_rps"] == 5.0

    client.delete("/v1/devices/rate-test")


def test_register_with_invalid_rate_limit_returns_400():
    response = client.post(
        "/v1/devices",
        json={"hostname": "x", "base_url": "http://x.local", "auth_type": "none", "rate_limit_rps": -1},
    )
    assert response.status_code == 400
    assert "rate_limit_rps" in response.json()["detail"]


def test_get_device_returns_404_for_unknown():
    response = client.get("/v1/devices/no-such-device")
    assert response.status_code == 404


def test_get_device_returns_device_data():
    client.post("/v1/devices", json={"hostname": "getone-test", "base_url": "http://192.0.2.99", "auth_type": "none"})
    response = client.get("/v1/devices/getone-test")
    assert response.status_code == 200
    data = response.json()
    assert data["hostname"] == "getone-test"
    assert data["base_url"] == "http://192.0.2.99"
    assert "reachable" in data
    assert "pod_active" in data
    assert "spawn_error" in data
    client.delete("/v1/devices/getone-test")


def test_get_device_tools_returns_404_for_unknown():
    response = client.get("/v1/devices/no-such-device/tools")
    assert response.status_code == 404


def test_get_device_tools_returns_409_when_pod_inactive():
    client.post(
        "/v1/devices",
        json={"hostname": "tools-inactive", "base_url": "http://192.0.2.99", "auth_type": "none"},
    )
    response = client.get("/v1/devices/tools-inactive/tools")
    assert response.status_code == 409
    assert "no active pod" in response.json()["detail"]
    client.delete("/v1/devices/tools-inactive")


def test_large_body_returns_413():
    response = client.post(
        "/v1/devices",
        content=b"x" * (1_048_576 + 1),
        headers={"Content-Type": "application/json", "Content-Length": str(1_048_576 + 1)},
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "bad_hostname",
    [
        "-starts-with-dash",
        "ends-with-dash-",
        ".starts-with-dot",
        "has space",
        "has/slash",
        "a" * 254,
        "",
    ],
)
def test_invalid_hostname_returns_400(bad_hostname):
    response = client.post(
        "/v1/devices",
        json={"hostname": bad_hostname, "base_url": "http://192.0.2.99", "auth_type": "none"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "good_hostname",
    ["device1", "my-device.local", "a", "sensor.lab.internal"],
)
def test_valid_hostname_accepted(good_hostname):
    response = client.post(
        "/v1/devices",
        json={"hostname": good_hostname, "base_url": "http://192.0.2.99", "auth_type": "none"},
    )
    assert response.status_code == 200
    client.delete(f"/v1/devices/{good_hostname}")


# --- F-17: /livez event-loop liveness probe ---------------------------------


def test_livez_alive_when_loop_ticking():
    """Inside the lifespan the heartbeat ticker runs, so /livez reports alive and
    does no auth/Redis work (it's the K8s liveness target)."""
    with TestClient(gw_main.app) as c:
        response = c.get("/livez")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_livez_503_when_heartbeat_stale(monkeypatch):
    """A wedged event loop can't advance the tick; /livez then flips to 503 so K8s
    restarts the pod — the 'wedged-but-serving' case /health would miss."""
    import time as _time

    # Force the last tick far enough in the past to exceed the staleness budget.
    monkeypatch.setattr(gw_main.app.state, "loop_heartbeat", _time.monotonic() - 3600)
    response = client.get("/livez")
    assert response.status_code == 503
    assert response.json()["status"] == "not alive"


def test_livez_does_not_require_auth(monkeypatch):
    _enable_admin_auth(monkeypatch)
    with TestClient(gw_main.app) as c:
        assert c.get("/livez").status_code == 200
