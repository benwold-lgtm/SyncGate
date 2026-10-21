# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for the failure-mode metrics (SRE O1) + the #5 lag-metric refinement.

Each failure site that previously only logged now also increments a Prometheus
counter, so request loss/shedding is visible in metrics, not just logs:

  - tool_call_timeouts_total     a request's exchange deadline passed (api/exchange.py)
  - dead_letter_total            undeliverable tool call (runner.py)
  - circuit_breaker_opens_total  device pod breaker open (device_pod.py)

Plus worker_undelivered_calls (the #5 refinement): the never-read stream backlog
held off by the per-device concurrency cap, which XPENDING alone cannot see.
"""

import asyncio

import fakeredis.aioredis
import pytest

from device_mcp_gateway import metrics
from device_mcp_gateway.core.translator import McpManifest, McpTool
from device_mcp_gateway.main import _acquire_gauge_leadership
from device_mcp_gateway.pods.device_pod import DevicePod
from device_mcp_gateway.worker.runner import DeviceWorker

CONFIG = {"registry": {"health_check_interval": 30}}


def _counter(metric, **labels):
    return metric.labels(**labels)._value.get()


# --- dead_letter_total (worker) ----------------------------------------------


@pytest.mark.asyncio
async def test_dead_letter_increments_counter():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker = DeviceWorker(worker_id="w", config=CONFIG, redis_client=r)
    before = _counter(metrics.dead_letter_total, hostname="dev-dl")

    await worker._dead_letter("dev-dl", {"request_id": "r1"}, "no active pod")

    assert _counter(metrics.dead_letter_total, hostname="dev-dl") == before + 1


# --- worker_undelivered_calls gauge (SRE #5 lag refinement) ------------------


@pytest.mark.asyncio
async def test_refresh_sets_undelivered_gauge(monkeypatch):
    """The never-read backlog (XINFO GROUPS lag) is summed across assigned
    devices into worker_undelivered_calls — the signal XPENDING misses."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker = DeviceWorker(worker_id="w", config=CONFIG, redis_client=r)
    worker._assigned = {"dev-a", "dev-b"}

    async def _fake_lag(stream, group):
        return 4

    monkeypatch.setattr(worker, "_stream_lag", _fake_lag)
    await worker._refresh_worker_metrics()

    assert metrics.worker_undelivered_calls._value.get() == 8  # 4 per assigned device


# --- circuit_breaker_opens_total (device pod) --------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_open_increments_counter():
    manifest = McpManifest(
        server_name="mcp-test",
        server_version="1.0.0",
        hostname="dev-cb",
        tools=[
            McpTool(
                name="get_item",
                description="Get item",
                schema={"type": "object", "properties": {"item_id": {"type": "integer"}}},
                method="GET",
                path="/items/{item_id}",
                param_locations={"item_id": "path"},
            )
        ],
    )
    pod = DevicePod(hostname="dev-cb", manifest=manifest, base_url="http://dev-cb.local")
    pod._breaker.open()  # force the breaker open so the next call is rejected
    before = _counter(metrics.circuit_breaker_opens_total, hostname="dev-cb")

    resp = await pod._tool_dispatch["get_item"](item_id=1)

    assert resp["ok"] is False  # normalized envelope (F-39)
    assert resp["status"] == 503
    assert resp["error"]["type"] == "circuit_open"
    assert _counter(metrics.circuit_breaker_opens_total, hostname="dev-cb") == before + 1


# --- gauge-refresh leader election (SRE O4) ----------------------------------


@pytest.mark.asyncio
async def test_gauge_leadership_single_holder():
    """Exactly one replica holds the lock; the holder refreshes it, others lose."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await _acquire_gauge_leadership(r, "replica-A", ttl=30) is True
    assert await _acquire_gauge_leadership(r, "replica-B", ttl=30) is False  # A holds it
    assert await _acquire_gauge_leadership(r, "replica-A", ttl=30) is True  # sticky refresh


@pytest.mark.asyncio
async def test_gauge_leadership_failover_after_expiry():
    """Once the holder's lock lapses, another replica can take over."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    assert await _acquire_gauge_leadership(r, "replica-A", ttl=30) is True
    await r.delete("gateway:gauge-leader")  # simulate A dying → lock lapses
    assert await _acquire_gauge_leadership(r, "replica-B", ttl=30) is True


# --- gauge_leader gauge export (F-21 — election alert) -----------------------


def _gauge_refresh_app(redis):
    from types import SimpleNamespace

    class _Reg:
        async def list_devices(self):
            return []

    return SimpleNamespace(state=SimpleNamespace(redis=redis, registry=_Reg()))


@pytest.mark.asyncio
async def test_refresh_exports_gauge_leader_when_leading(monkeypatch):
    """The replica that wins the lock exports mcp_gateway_gauge_leader=1 so a
    'no leader' alert can fire when the sum across replicas drops to 0 (F-21)."""
    from device_mcp_gateway.main import _refresh_device_gauges

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = _gauge_refresh_app(r)
    metrics.gauge_leader.set(0)

    task = asyncio.create_task(_refresh_device_gauges(app, interval=60))
    for _ in range(50):
        await asyncio.sleep(0.005)
        if metrics.gauge_leader._value.get() == 1:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert metrics.gauge_leader._value.get() == 1


@pytest.mark.asyncio
async def test_refresh_exports_zero_when_another_replica_leads(monkeypatch):
    """A replica that loses the election exports 0 — so the fleet sum stays exactly
    1 (the leader) and the absent-leader alert only fires when truly nobody leads."""
    from device_mcp_gateway.main import _refresh_device_gauges

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await r.set("gateway:gauge-leader", "another-replica", ex=60)  # someone else already leads
    app = _gauge_refresh_app(r)
    metrics.gauge_leader.set(1)  # stale value from when we were leader

    task = asyncio.create_task(_refresh_device_gauges(app, interval=60))
    for _ in range(50):
        await asyncio.sleep(0.005)
        if metrics.gauge_leader._value.get() == 0:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert metrics.gauge_leader._value.get() == 0
