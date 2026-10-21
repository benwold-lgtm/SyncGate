# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""A deleted device must stay deleted, even while a worker is still writing to it.

Found on the lab cluster 2026-08-10. ``DELETE /v1/devices/{h}`` removed the record and
dropped the hostname from ``devices:all``; a worker that still had the device assigned
then wrote ``pod_active``/``worker_id``, and a plain ``HSET`` **re-created the hash** with
only those two fields. The wreckage was invisible to ``GET /v1/devices`` (which reads the
set) while every read *by hostname* raised ``KeyError: 'hostname'`` as a 500 — and
re-registering that hostname failed too, since registration reads the device first.

The tests that matter here run against a **real Redis**, and they perform the actual
sequence rather than pre-seeding a partial hash: a pre-seeded fixture would prove the
decoder tolerates wreckage while saying nothing about whether the write path still
creates it. ``MemoryRegistryBackend`` never had the bug (it guards with ``if cfg``),
which is why the embedded suite could not have caught this — so the parity test below
pins both backends to the same contract.
"""

from __future__ import annotations

import pytest

from device_mcp_gateway.shared.keys import KEYS
from device_mcp_gateway.shared.registry_backend import (
    DeviceConfig,
    MemoryRegistryBackend,
    RedisRegistryBackend,
)


def _device(hostname="dev1"):
    return DeviceConfig(hostname=hostname, base_url="https://dev1.example")


# ---------------------------------------------------------------------------
# The race, against a real Redis
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_field_update_after_delete_does_not_resurrect_the_device(real_redis):
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device("dev1", _device())
    await backend.delete_device("dev1")

    # Exactly what the worker does when it notices the pod is gone. Before the fix this
    # HSET re-created the key with two fields and no hostname.
    applied = await backend.update_device_fields("dev1", pod_active=False, worker_id="")

    assert applied is False, "a field update must not apply to a device that no longer exists"
    assert await real_redis.exists(KEYS.device_config("dev1")) == 0, "the record was re-created"
    assert await backend.get_device("dev1") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_hostname_can_be_registered_again_afterwards(real_redis):
    # The consequence that actually hurt: registration reads the device first, so the
    # wreckage made the hostname permanently unusable until someone DEL'd it by hand.
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device("dev1", _device())
    await backend.delete_device("dev1")
    await backend.update_device_fields("dev1", pod_active=False, worker_id="")

    await backend.set_device("dev1", _device())
    again = await backend.get_device("dev1")
    assert again is not None and again.hostname == "dev1"
    assert "dev1" in await backend.list_hostnames()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_field_update_to_a_live_device_still_applies(real_redis):
    # The positive control. A guard that rejected *every* update would pass the tests
    # above and silently stop the health loop from recording anything.
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device("dev1", _device())

    applied = await backend.update_device_fields("dev1", pod_active=True, spec_hash="abc123")

    assert applied is True
    cfg = await backend.get_device("dev1")
    assert cfg is not None
    assert cfg.pod_active is True
    assert cfg.spec_hash == "abc123"
    assert cfg.base_url == "https://dev1.example", "untouched fields must survive a partial update"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wreckage_from_an_older_version_reads_as_absent_not_as_a_500(real_redis):
    # Defence in depth, and the migration path: a record written by a version that could
    # still resurrect one must not raise out of every endpoint that touches the hostname.
    await real_redis.hset(KEYS.device_config("dev1"), mapping={"pod_active": "False", "worker_id": ""})
    backend = RedisRegistryBackend(real_redis)

    assert await backend.get_device("dev1") is None  # was: KeyError -> 500
    assert await backend.get_devices(["dev1"]) == []

    # ...and it clears itself, rather than needing a DEL by hand.
    await backend.set_device("dev1", _device())
    cfg = await backend.get_device("dev1")
    assert cfg is not None and cfg.base_url == "https://dev1.example"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_partial_record_does_not_break_the_fleet_listing(real_redis):
    # get_devices zips hostnames to pipeline results; a mis-zip here would mislabel every
    # device after the bad one, which is worse than the crash it replaced.
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device("good1", _device("good1"))
    await real_redis.hset(KEYS.device_config("wreck"), mapping={"pod_active": "False"})
    await backend.set_device("good2", _device("good2"))

    got = await backend.get_devices(["good1", "wreck", "good2"])

    assert [c.hostname for c in got] == ["good1", "good2"]


# ---------------------------------------------------------------------------
# Backend parity — the contract, not one implementation of it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_backend_also_refuses_to_create_on_update():
    backend = MemoryRegistryBackend()
    await backend.set_device("dev1", _device())
    await backend.delete_device("dev1")

    assert await backend.update_device_fields("dev1", pod_active=False) is False
    assert await backend.get_device("dev1") is None
    assert await backend.update_device_fields("never-existed", pod_active=True) is False


@pytest.mark.asyncio
async def test_memory_backend_applies_updates_to_a_live_device():
    backend = MemoryRegistryBackend()
    await backend.set_device("dev1", _device())

    assert await backend.update_device_fields("dev1", pod_active=True) is True
    cfg = await backend.get_device("dev1")
    assert cfg is not None and cfg.pod_active is True
