# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Endpoint fingerprinting (ADR-0015) in EMBEDDED mode.

Until now only the distributed worker ran it. An embedded gateway never observed a device's
TLS key or declared identity, never pinned, and — beneath that — never persisted any
fingerprint field after registration nor restored one on restart. These tests pin all
three layers, plus the attribution property that makes observing safe under the embedded
loop's concurrency.
"""

from types import SimpleNamespace

import pytest

from device_mcp_gateway.registry.models import DeviceProfile
from device_mcp_gateway.registry.server import Registry
from device_mcp_gateway.security import fingerprint as fp
from device_mcp_gateway.security.fingerprint_record import declared_from_spec, record_observation
from device_mcp_gateway.shared.registry_backend import DeviceConfig
from device_mcp_gateway.storage.sqlite_store import PERSISTED_RUNTIME_FIELDS, SqliteDeviceStore

KEY_A = "a" * 64
KEY_B = "b" * 64


class _Probe:
    """Stands in for the guarded client: the TLS observation lives on its transport."""

    def __init__(self, spki: str | None = None):
        self._transport = SimpleNamespace(last_observation=fp.Observation(tls_spki_sha256=spki) if spki else None)


async def _registry(tmp_path, *, store=True):
    st = SqliteDeviceStore(db_path=str(tmp_path / "devices.db")) if store else None
    if st:
        await st.initialize()
    reg = Registry(config={"mode": "embedded"}, store=st)
    return reg, st


async def _register(reg: Registry, st, hostname: str, upstream_kind: str = "openapi") -> DeviceProfile:
    cfg = DeviceConfig(hostname=hostname, base_url=f"https://{hostname}.local", upstream_kind=upstream_kind)
    profile = DeviceProfile(config=cfg)
    reg._profiles[hostname] = profile
    await reg._backend.set_device(hostname, cfg)
    if st:
        await st.save(hostname, {"base_url": cfg.base_url, "transport": "sse", "upstream_kind": upstream_kind})
    return profile


# --- observing ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_contact_pins_the_key_and_records_the_declared_identity(tmp_path):
    reg, st = await _registry(tmp_path)
    profile = await _register(reg, st, "sensor")
    profile.spec_data = {"info": {"title": "Sensor API", "version": "1.2.0"}}

    await reg._observe_fingerprint(profile, _Probe(KEY_A))

    cfg = await reg.get_device("sensor")
    assert cfg.fingerprint_state == fp.STATE_PINNED and cfg.tls_spki_sha256 == KEY_A
    assert (cfg.declared_name, cfg.declared_version) == ("Sensor API", "1.2.0")


@pytest.mark.asyncio
async def test_an_mcp_device_records_its_server_info(tmp_path):
    reg, st = await _registry(tmp_path)
    profile = await _register(reg, st, "refsdk2", upstream_kind="mcp")
    profile.declared_identity = ("reference-upstream-sdk2", "2.3.0")

    await reg._observe_fingerprint(profile, _Probe())  # plain http: no TLS dimension

    cfg = await reg.get_device("refsdk2")
    assert (cfg.declared_name, cfg.declared_version) == ("reference-upstream-sdk2", "2.3.0")
    assert cfg.tls_spki_sha256 is None  # nothing to pin over http://


@pytest.mark.asyncio
async def test_a_changed_key_is_flagged_for_approval(tmp_path):
    reg, st = await _registry(tmp_path)
    profile = await _register(reg, st, "sensor")
    await reg._observe_fingerprint(profile, _Probe(KEY_A))
    await reg._observe_fingerprint(profile, _Probe(KEY_B))

    cfg = await reg.get_device("sensor")
    assert cfg.fingerprint_state == fp.STATE_PENDING
    assert cfg.tls_spki_sha256 == KEY_A and cfg.pending_tls_spki_sha256 == KEY_B


@pytest.mark.asyncio
async def test_distributed_mode_leaves_observing_to_its_worker(tmp_path):
    reg = Registry(config={"mode": "distributed"})
    profile = DeviceProfile(config=DeviceConfig(hostname="d", base_url="https://d.local"))
    await reg._backend.set_device("d", profile.config)
    await reg._observe_fingerprint(profile, _Probe(KEY_A))
    assert profile.config.fingerprint_state == fp.STATE_UNPINNED


def test_each_device_gets_its_own_capturing_probe_client():
    """The attribution property: the embedded loop probes devices concurrently, and the
    pooled client is shared per TLS profile — reading its last observation could return
    another device's certificate. One client per device rules that out by construction."""
    reg = Registry(config={"mode": "embedded"})
    a, b = reg._spec_service.probe_client("a"), reg._spec_service.probe_client("b")
    assert a is not b
    assert reg._spec_service.probe_client("a") is a  # pooled per device
    assert a._transport._capture_fingerprint is True
    assert reg._spec_service.client("a") is reg._spec_service.client("b")  # the shared one


# --- persisting and restoring ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pin_survives_a_restart(tmp_path):
    reg, st = await _registry(tmp_path)
    profile = await _register(reg, st, "sensor")
    await reg._observe_fingerprint(profile, _Probe(KEY_A))

    # A restart: a fresh registry over the same SQLite file, without provisioning I/O.
    reg2, _ = await _registry(tmp_path)

    async def no_provision(profile):
        return None

    reg2._provision_device = no_provision  # type: ignore[method-assign]
    await reg2.load_persisted_devices()
    cfg = await reg2.get_device("sensor")
    assert cfg.fingerprint_state == fp.STATE_PINNED and cfg.tls_spki_sha256 == KEY_A


@pytest.mark.asyncio
async def test_after_a_restart_a_new_key_is_caught_not_silently_re_pinned(tmp_path):
    """The failure mode that persistence exists to prevent."""
    reg, st = await _registry(tmp_path)
    profile = await _register(reg, st, "sensor")
    await reg._observe_fingerprint(profile, _Probe(KEY_A))

    reg2, _ = await _registry(tmp_path)

    async def no_provision(profile):
        return None

    reg2._provision_device = no_provision  # type: ignore[method-assign]
    await reg2.load_persisted_devices()
    await reg2._observe_fingerprint(reg2.get_profile("sensor"), _Probe(KEY_B))
    assert (await reg2.get_device("sensor")).fingerprint_state == fp.STATE_PENDING


@pytest.mark.asyncio
async def test_an_approval_survives_a_restart(tmp_path):
    reg, st = await _registry(tmp_path)
    profile = await _register(reg, st, "sensor")
    await reg._observe_fingerprint(profile, _Probe(KEY_A))
    await reg._observe_fingerprint(profile, _Probe(KEY_B))
    await reg.update_persisted_fields("sensor", **fp.approve(KEY_B, 1.0))  # what the API calls

    rows = {r["hostname"]: r for r in await st.load_all()}
    assert rows["sensor"]["tls_spki_sha256"] == KEY_B
    assert rows["sensor"]["fingerprint_state"] == fp.STATE_PINNED


@pytest.mark.asyncio
async def test_the_store_writes_allowlisted_fingerprint_columns_only(tmp_path):
    st = SqliteDeviceStore(db_path=str(tmp_path / "d.db"))
    await st.initialize()
    await st.save("x", {"base_url": "http://x", "transport": "sse"})
    await st.update_fields("x", {"declared_name": "X", "base_url": "http://evil", "nope": 1})
    [row] = await st.load_all()
    assert row["declared_name"] == "X" and row["base_url"] == "http://x"
    assert "declared_name" in PERSISTED_RUNTIME_FIELDS and "base_url" not in PERSISTED_RUNTIME_FIELDS


# --- the shared comparison --------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_observed_writes_nothing():
    calls = []

    async def write(**fields):
        calls.append(fields)

    cfg = DeviceConfig(hostname="h", base_url="http://h")
    assert await record_observation(write, "h", cfg, fp.Observation()) is None
    assert calls == []


def test_declared_from_spec_tolerates_junk():
    assert declared_from_spec({"info": {"title": "T", "version": 2}}) == ("T", "2")
    assert declared_from_spec({"info": "nope"}) is None
    assert declared_from_spec(None) is None
    assert declared_from_spec({"info": {}}) is None


@pytest.mark.asyncio
async def test_the_health_probe_itself_observes_through_the_devices_own_client(tmp_path, monkeypatch):
    """Wiring, not just the helper: check_reachability must probe with the per-device
    capturing client and record what it saw."""
    import httpx

    reg, st = await _registry(tmp_path)
    profile = await _register(reg, st, "sensor")
    used: list[str] = []

    class FakeProbe(_Probe):
        async def get(self, url, timeout=None):
            used.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(reg._spec_service, "probe_client", lambda hostname: FakeProbe(KEY_A))
    assert await reg.check_reachability(profile) is True
    assert used == ["https://sensor.local"]
    assert (await reg.get_device("sensor")).tls_spki_sha256 == KEY_A


@pytest.mark.asyncio
async def test_an_unreachable_device_records_nothing(tmp_path, monkeypatch):
    reg, st = await _registry(tmp_path)
    profile = await _register(reg, st, "sensor")

    class DeadProbe(_Probe):
        async def get(self, url, timeout=None):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(reg._spec_service, "probe_client", lambda hostname: DeadProbe(KEY_A))
    assert await reg.check_reachability(profile) is False
    assert (await reg.get_device("sensor")).fingerprint_state == fp.STATE_UNPINNED
