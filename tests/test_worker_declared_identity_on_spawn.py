# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""A distributed OpenAPI device's declared identity is recorded on the first health cycle.

Seen on a live cluster: a plain-HTTP OpenAPI device showed no ``declared_name`` for about
five minutes after registration. The health loop learns the identity only from a spec it
polls, and it defers its first poll by a full ``spec_poll_interval`` because the spawn path
has just fetched the spec. Over plain HTTP there is no TLS dimension either, so nothing at
all was recorded until then. The spawn path now hands the identity over.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

import device_mcp_gateway.worker.runner as runner_mod
from device_mcp_gateway.core.backoff import RetryPolicy
from device_mcp_gateway.shared.registry_backend import DeviceConfig, MemoryRegistryBackend
from device_mcp_gateway.worker.health import WorkerHealthLoop
from device_mcp_gateway.worker.runner import DeviceWorker

HOST = "plain-http-sensor"
SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Sensor API", "version": "1.2.0"},
    "paths": {"/items": {"get": {"operationId": "list_items", "responses": {"200": {"description": "ok"}}}}},
}


@pytest.mark.asyncio
async def test_the_spawn_paths_spec_feeds_the_first_health_cycle(monkeypatch):
    w = DeviceWorker(
        worker_id="w1",
        config={"registry": {"health_check_interval": 30}},
        redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True),
    )
    w._backend = MemoryRegistryBackend()
    w._retry_policy = RetryPolicy()  # built in run(), which this test doesn't call
    w._health = WorkerHealthLoop("w1", w._backend, None, interval=30)
    await w._backend.set_device(HOST, DeviceConfig(hostname=HOST, base_url="http://sensor.local"))

    async def _spec(cfg):
        return SPEC

    async def _in_process(_executor, fn, **_kw):
        return fn()  # the real call crosses a process boundary a local stub can't

    monkeypatch.setattr(w, "_fetch_spec", _spec)
    monkeypatch.setattr(runner_mod, "run_translation", _in_process)

    await w._spawn_pod(HOST)
    try:
        # The health loop's comparison step, as its first cycle runs it for this device.
        await w._health._update_fingerprint(HOST, await w._backend.get_device(HOST))
        cfg = await w._backend.get_device(HOST)
        assert (cfg.declared_name, cfg.declared_version) == ("Sensor API", "1.2.0")
    finally:
        await w._kill_pod(HOST)


@pytest.mark.asyncio
async def test_a_proxied_server_is_left_to_its_handshake(monkeypatch):
    """An MCP upstream's identity is its serverInfo, which the reachability probe records;
    a snapshot has no `info` block and must not be read as one."""
    loop = WorkerHealthLoop("w1", MemoryRegistryBackend(), None, interval=30)
    w = DeviceWorker(worker_id="w1", config={}, redis_client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    w._backend = MemoryRegistryBackend()
    w._retry_policy = RetryPolicy()
    w._health = loop
    await w._backend.set_device(HOST, DeviceConfig(hostname=HOST, base_url="http://mcp.local", upstream_kind="mcp"))

    async def _snapshot(cfg):
        return {"upstream_kind": "mcp", "tools": [], "resources": [], "prompts": [], "info": {"title": "x"}}

    monkeypatch.setattr(w, "_fetch_spec", _snapshot)
    await w._spawn_pod(HOST)
    try:
        assert HOST not in loop._last_declared
    finally:
        await w._kill_pod(HOST)
