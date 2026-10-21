# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Runtime device state that must survive an embedded restart, beyond the fingerprint.

The SQLite store has kept ``credential_state`` (ADR-0018 §3) and ``curated_spec``
(ADR-0020 §4b) columns since those ADRs, but the registry never wrote them after
registration and ``load_persisted_devices`` never read them back. So a restart cleared
*needs reconnecting* from a device nobody had reconnected — including every device a
restore had just flagged — and a restored pin went the same way, because restore wrote
through the in-memory backend only.
"""

from types import SimpleNamespace

import pytest

from device_mcp_gateway.backup.restore import CREDENTIAL_STATE_NEEDS_RECONNECT, _restore_governance
from device_mcp_gateway.registry.models import DeviceProfile
from device_mcp_gateway.registry.server import Registry
from device_mcp_gateway.security import fingerprint as fp
from device_mcp_gateway.shared.registry_backend import DeviceConfig
from device_mcp_gateway.storage.sqlite_store import SqliteDeviceStore

KEY_A = "a" * 64
CURATED = '{"openapi": "3.0.0", "info": {"title": "Curated", "version": "1"}, "paths": {}}'


async def _registry(tmp_path):
    st = SqliteDeviceStore(db_path=str(tmp_path / "devices.db"))
    await st.initialize()
    reg = Registry(config={"mode": "embedded"}, store=st)

    async def no_provision(profile, **_kw):
        return None

    # No device I/O: these tests are about what is written and read back, not provisioning.
    reg._provision_device = no_provision  # type: ignore[method-assign]
    reg._provision_in_background = no_provision  # type: ignore[method-assign]
    return reg, st


async def _register(reg: Registry, st, hostname: str) -> None:
    cfg = DeviceConfig(hostname=hostname, base_url=f"https://{hostname}.local")
    reg._profiles[hostname] = DeviceProfile(config=cfg)
    await reg._backend.set_device(hostname, cfg)
    await st.save(hostname, {"base_url": cfg.base_url})


async def _restart(tmp_path) -> Registry:
    reg, _ = await _registry(tmp_path)
    await reg.load_persisted_devices()
    return reg


@pytest.mark.asyncio
async def test_needs_reconnect_survives_a_restart(tmp_path):
    reg, st = await _registry(tmp_path)
    await _register(reg, st, "sensor")
    await reg.update_persisted_fields("sensor", credential_state=CREDENTIAL_STATE_NEEDS_RECONNECT)

    cfg = await (await _restart(tmp_path)).get_device("sensor")
    assert cfg.credential_state == CREDENTIAL_STATE_NEEDS_RECONNECT


@pytest.mark.asyncio
async def test_a_curated_spec_survives_a_restart_byte_for_byte(tmp_path):
    reg, st = await _registry(tmp_path)
    await _register(reg, st, "sensor")
    await reg.update_persisted_fields("sensor", curated_spec=CURATED)

    cfg = await (await _restart(tmp_path)).get_device("sensor")
    assert cfg.curated_spec == CURATED  # verbatim: its digest is over the stored bytes


@pytest.mark.asyncio
async def test_a_put_that_keeps_the_credential_keeps_needs_reconnect_across_a_restart(tmp_path):
    """The rebuild's `save()` writes the default back to SQLite; the carried state must
    land after it, or the restart quietly undoes the carry."""
    reg, _ = await _registry(tmp_path)
    await reg.register_device("sensor", "https://sensor.local")
    await reg.update_persisted_fields("sensor", credential_state=CREDENTIAL_STATE_NEEDS_RECONNECT)

    live = await reg.replace_device("sensor", "https://sensor.local", rate_limit_rps=5.0, keep_auth=True)
    assert live.credential_state == CREDENTIAL_STATE_NEEDS_RECONNECT

    cfg = await (await _restart(tmp_path)).get_device("sensor")
    assert cfg.credential_state == CREDENTIAL_STATE_NEEDS_RECONNECT and cfg.rate_limit_rps == 5.0


@pytest.mark.asyncio
async def test_a_put_with_a_new_credential_clears_it_for_good(tmp_path):
    """Reconnection is a PUT that supplies a credential; that clear must persist too."""
    reg, _ = await _registry(tmp_path)
    await reg.register_device("sensor", "https://sensor.local")
    await reg.update_persisted_fields("sensor", credential_state=CREDENTIAL_STATE_NEEDS_RECONNECT)

    await reg.replace_device("sensor", "https://sensor.local", keep_auth=False)

    cfg = await (await _restart(tmp_path)).get_device("sensor")
    assert cfg.credential_state == "ok"


@pytest.mark.asyncio
async def test_what_a_restore_puts_back_survives_a_restart(tmp_path):
    """Restore writes the archived pin and `needs_reconnect` after replaying registration.
    It used to write them to the backend only — memory, in embedded mode."""
    reg, st = await _registry(tmp_path)
    await _register(reg, st, "sensor")
    pin = fp.approve(KEY_A, 1.0)

    await _restore_governance(
        "sensor",
        {"tools_revision": 3},
        {},
        reg._backend,
        registry=reg,
        include_deadletters=False,
        fingerprint_fields=pin,
        needs_reconnect=True,
    )

    cfg = await (await _restart(tmp_path)).get_device("sensor")
    assert cfg.credential_state == CREDENTIAL_STATE_NEEDS_RECONNECT
    assert cfg.fingerprint_state == fp.STATE_PINNED and cfg.tls_spki_sha256 == KEY_A


@pytest.mark.asyncio
async def test_fields_the_store_does_not_keep_still_reach_the_live_record(tmp_path):
    reg, st = await _registry(tmp_path)
    await _register(reg, st, "sensor")
    await reg.update_persisted_fields("sensor", tools_revision=7)
    assert (await reg.get_device("sensor")).tools_revision == 7


@pytest.mark.asyncio
async def test_distributed_mode_writes_the_backend_only():
    written = []
    reg = Registry(config={"mode": "distributed"})
    reg._store = SimpleNamespace(update_fields=lambda *a: written.append(a))
    await reg._backend.set_device("d", DeviceConfig(hostname="d", base_url="https://d.local"))
    await reg.update_persisted_fields("d", credential_state=CREDENTIAL_STATE_NEEDS_RECONNECT)
    assert written == []
    assert (await reg.get_device("d")).credential_state == CREDENTIAL_STATE_NEEDS_RECONNECT
