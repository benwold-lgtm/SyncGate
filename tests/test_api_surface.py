# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for F-19 — normalized API surface (typed device models + one serializer).

Every device endpoint projects a registry ``DeviceConfig`` through a single
``from_config`` serializer instead of hand-building divergent dicts: list/overview
use the lean ``DeviceSummary``, the single-device read uses the ``DeviceDetail``
superset, and register/update return a ``DeviceMutationResult`` (write envelope +
the full resulting device).
"""

from __future__ import annotations

from device_mcp_gateway.schemas import DeviceDetail, DeviceMutationResult, DeviceSummary
from device_mcp_gateway.shared.registry_backend import DeviceConfig

# --- serializer is the single source of truth -------------------------------


def _cfg(**over):
    base = dict(
        hostname="dev",
        base_url="http://dev.local",
        spec_url="http://dev.local/openapi.json",
        auth_type="api_key",
        rate_limit_rps=5.0,
        spec_hash="abc123",
        pod_active=True,
        reachable=True,
        last_check=1234.5,
        spawn_error="boom",
        worker_id="worker-7",
    )
    base.update(over)
    return DeviceConfig(**base)


def test_summary_from_config_projects_lean_fields():
    """Exact equality, not a subset check, and that is the point of this test.

    "Lean" is not a property anything else enforces — every field added to ``DeviceSummary``
    is added to the fleet-list response for every device, forever. Pinning the whole set
    makes each addition a deliberate edit here with a reason attached, rather than something
    that accretes. Two fields have earned a place since: ``upstream_kind``, because it
    changes how every other field on the row should be read, and ``credential_state``
    (ADR-0018 §3), because a device needing re-authorization after a restore is precisely
    what an operator scans a list for — and its own precedent, ``fingerprint_state``, is
    detail-only and therefore invisible in exactly that situation.
    """
    s = DeviceSummary.from_config(_cfg())
    assert s.model_dump() == {
        "hostname": "dev",
        "base_url": "http://dev.local",
        "reachable": True,
        "pod_active": True,
        "last_check": 1234.5,
        "rate_limit_rps": 5.0,
        "upstream_kind": "openapi",
        "credential_state": "ok",
    }
    # The lean summary must NOT leak detail-only fields.
    assert "spawn_error" not in s.model_dump()
    assert "spec_url" not in s.model_dump()


def test_detail_from_config_is_summary_superset():
    cfg = _cfg()
    d = DeviceDetail.from_config(cfg)
    dumped = d.model_dump()
    # Every summary field is present and equal...
    for k, v in DeviceSummary.from_config(cfg).model_dump().items():
        assert dumped[k] == v
    # ...plus the detail-only fields, all sourced from the config.
    assert dumped["spec_url"] == "http://dev.local/openapi.json"
    assert dumped["spec_hash"] == "abc123"
    assert dumped["auth_type"] == "api_key"
    assert dumped["spawn_error"] == "boom"
    assert dumped["worker_id"] == "worker-7"


def test_detail_is_a_summary_subtype():
    # Structural guarantee that detail can stand in anywhere a summary is expected.
    assert issubclass(DeviceDetail, DeviceSummary)
    assert set(DeviceSummary.model_fields) <= set(DeviceDetail.model_fields)


def test_last_check_zero_coerced_to_none():
    # A never-checked device (epoch 0.0) reports last_check=None, not a fake 1970 ts.
    assert DeviceSummary.from_config(_cfg(last_check=0.0)).last_check is None


# --- endpoint conformance ----------------------------------------------------


def _register(client, hostname):
    return client.post(
        "/v1/devices",
        json={"hostname": hostname, "base_url": "http://192.0.2.99", "auth_type": "none"},
    )


def test_register_returns_mutation_result_shape(client):
    resp = _register(client, "surface-reg")
    try:
        assert resp.status_code == 200
        body = resp.json()
        # Conforms to DeviceMutationResult: envelope + nested full device.
        DeviceMutationResult.model_validate(body)
        assert body["status"] == "registered"
        assert isinstance(body["provisioning"], bool)
        assert body["device"]["hostname"] == "surface-reg"
        assert "spawn_error" in body["device"]  # detail field present under device
    finally:
        client.delete("/v1/devices/surface-reg")


def test_update_returns_mutation_result_shape(client, mock_target_url):
    # Use the reachable mock target so the update's re-provision probe is fast
    # (an unreachable base_url would re-probe to the client read-timeout).
    client.post("/v1/devices", json={"hostname": "surface-upd", "base_url": mock_target_url, "auth_type": "none"})
    try:
        resp = client.put(
            "/v1/devices/surface-upd",
            json={"base_url": mock_target_url, "auth_type": "none", "rate_limit_rps": 7.0},
        )
        assert resp.status_code == 200
        body = resp.json()
        DeviceMutationResult.model_validate(body)
        assert body["status"] == "updated"
        assert body["device"]["base_url"] == mock_target_url
        assert body["device"]["rate_limit_rps"] == 7.0
    finally:
        client.delete("/v1/devices/surface-upd")


def test_get_one_is_detail_and_list_is_summary(client):
    _register(client, "surface-shape")
    try:
        detail = client.get("/v1/devices/surface-shape").json()
        DeviceDetail.model_validate(detail)
        # Detail exposes the superset fields.
        for k in ("spec_url", "spec_hash", "auth_type", "spawn_error", "worker_id"):
            assert k in detail

        listed = client.get("/v1/devices").json()["devices"]
        row = next(d for d in listed if d["hostname"] == "surface-shape")
        DeviceSummary.model_validate(row)
        # The lean list row does not carry detail-only fields.
        assert "spec_url" not in row
        assert "spawn_error" not in row
    finally:
        client.delete("/v1/devices/surface-shape")


def test_get_unknown_device_404(client):
    assert client.get("/v1/devices/surface-nope").status_code == 404
