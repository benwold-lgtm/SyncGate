# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for worker backpressure and graceful shutdown (SRE #4/#5/#6).

#4 — undeliverable calls are dead-lettered (not silently XACK-dropped); call
     streams are bounded by MAXLEN.
#5 — per-device concurrency cap: the consume loop blocks on a semaphore when
     saturated instead of spawning unbounded dispatch tasks.
#6 — in-flight tool calls are drained on shutdown before cancellation.
"""

import asyncio
import json

import pytest
import fakeredis.aioredis

import device_mcp_gateway.shared.registry_backend as rb
from device_mcp_gateway.worker.runner import DeviceWorker

CONFIG = {"registry": {"health_check_interval": 30}}


def _shared_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def _worker(worker_id, redis):
    return DeviceWorker(worker_id=worker_id, config=CONFIG, redis_client=redis)


# --- #4: dead-letter + bounded streams --------------------------------------


@pytest.mark.asyncio
async def test_dispatch_dead_letters_when_no_pod():
    """A call for a device this worker has no pod for is dead-lettered, the client
    is told, and the call is acked — not silently dropped."""
    r = _shared_redis()
    worker = _worker("w", r)
    stream, group = "device:devx:calls", "workers-devx"
    await r.xgroup_create(stream, group, id="0", mkstream=True)
    msg_id = await r.xadd(stream, {"x": "1"})
    await r.xreadgroup(group, "w", {stream: ">"}, count=10)  # into PEL so the ack matters

    fields = {"session_id": "s1", "request_id": "req1", "message": json.dumps({"id": 7, "method": "tools/call"})}
    await worker._dispatch_call("devx", stream, group, msg_id, fields)

    assert await r.xlen("device:devx:calls:dead") == 1  # dead-lettered, not dropped
    assert await r.exists("session:s1:results") == 1  # client got an error result
    assert await r.get("result:req1") == "1"  # F6 marker set so the watcher stands down
    assert (await r.xpending(stream, group))["pending"] == 0  # original acked


# Both streams trim with `MAXLEN ~` (approximate=True), and real Redis trims an approximate
# stream only in whole macro-nodes — `stream-node-max-entries`, 100 by default. A cap of 5
# can therefore leave up to 5 + 100 entries, and 25 writes never trim at all. These tests
# used to assert `<= cap` after 25 writes, which held only because fakeredis < 2.40 trimmed
# approximate streams exactly; 2.40.0 models the real behaviour and they went red with no
# change to the code. The property worth pinning is the one approximate trimming promises —
# the stream stays within one node of the cap however much is written — so each test writes
# well past a node and asserts that bound, plus that trimming happened at all.
_STREAM_NODE_MAX_ENTRIES = 100
_WRITES_PAST_A_NODE = 3 * _STREAM_NODE_MAX_ENTRIES


@pytest.mark.asyncio
async def test_publish_tool_call_bounds_stream(monkeypatch):
    """The pending-call stream is trimmed to MAXLEN so a backlog can't grow Redis
    without bound."""
    cap = 5
    monkeypatch.setattr(rb, "_CALL_STREAM_MAXLEN", cap)
    r = _shared_redis()
    backend = rb.RedisRegistryBackend(r)
    for i in range(_WRITES_PAST_A_NODE):
        await backend.publish_tool_call("devx", f"r{i}", "s1", "gw", {"i": i})
    length = await r.xlen("device:devx:calls")
    assert cap <= length <= cap + _STREAM_NODE_MAX_ENTRIES
    assert length < _WRITES_PAST_A_NODE


@pytest.mark.asyncio
async def test_dead_letter_stream_is_bounded(monkeypatch):
    import device_mcp_gateway.worker.runner as runner_mod

    cap = 3
    monkeypatch.setattr(runner_mod, "_DLQ_MAXLEN", cap)
    r = _shared_redis()
    worker = _worker("w", r)
    for i in range(_WRITES_PAST_A_NODE):
        await worker._dead_letter("devx", {"request_id": f"r{i}"}, "no active pod")
    length = await r.xlen("device:devx:calls:dead")
    assert cap <= length <= cap + _STREAM_NODE_MAX_ENTRIES
    assert length < _WRITES_PAST_A_NODE


# --- #5: per-device concurrency cap -----------------------------------------


@pytest.mark.asyncio
async def test_schedule_dispatch_caps_concurrency(monkeypatch):
    """With a 2-slot semaphore, only 2 dispatches run at once and a 3rd schedule
    blocks until a slot frees — the backpressure that keeps a burst off the heap."""
    worker = _worker("w", _shared_redis())
    gate = asyncio.Event()
    running = {"n": 0, "max": 0}

    async def _fake_dispatch(hostname, stream, group, msg_id, fields):
        running["n"] += 1
        running["max"] = max(running["max"], running["n"])
        await gate.wait()
        running["n"] -= 1

    monkeypatch.setattr(worker, "_dispatch_call", _fake_dispatch)
    sem = asyncio.Semaphore(2)

    await worker._schedule_dispatch(sem, "h", "s", "g", "1", {})
    await worker._schedule_dispatch(sem, "h", "s", "g", "2", {})
    await asyncio.sleep(0.02)
    assert running["n"] == 2  # both slots in use

    third = asyncio.create_task(worker._schedule_dispatch(sem, "h", "s", "g", "3", {}))
    await asyncio.sleep(0.02)
    assert not third.done()  # blocked on the (empty) semaphore
    assert running["n"] == 2  # 3rd has not started

    gate.set()  # free the running ones → slot opens for the 3rd
    await asyncio.wait_for(third, timeout=1)
    await asyncio.gather(*list(worker._inflight_calls), return_exceptions=True)
    assert running["max"] == 2  # cap never exceeded


@pytest.mark.asyncio
async def test_worker_cap_bounds_aggregate_across_devices(monkeypatch):
    """The worker-wide cap (F-13) bounds total in-flight calls across ALL devices,
    even when each device's own cap would allow far more. A burst spread over many
    devices becomes stream lag, not devices × per-device concurrency."""
    from device_mcp_gateway import metrics

    cfg = {"registry": {"health_check_interval": 30, "max_concurrent_calls_per_worker": 2}}
    worker = DeviceWorker(worker_id="w", config=cfg, redis_client=_shared_redis())
    gate = asyncio.Event()
    running = {"n": 0, "max": 0}

    async def _fake_dispatch(hostname, stream, group, msg_id, fields):
        running["n"] += 1
        running["max"] = max(running["max"], running["n"])
        await gate.wait()
        running["n"] -= 1

    monkeypatch.setattr(worker, "_dispatch_call", _fake_dispatch)
    # Two devices, each with a roomy per-device cap, so only the worker-wide cap binds.
    sem_a, sem_b = asyncio.Semaphore(10), asyncio.Semaphore(10)

    await worker._schedule_dispatch(sem_a, "a", "s", "g", "1", {})
    await worker._schedule_dispatch(sem_b, "b", "s", "g", "1", {})
    await asyncio.sleep(0.02)
    assert running["n"] == 2  # worker-wide cap (2) reached ACROSS the two devices

    throttled_before = metrics.worker_calls_throttled_total._value.get()
    # A 3rd call blocks on the worker-wide sem even though device b's own slot is free.
    third = asyncio.create_task(worker._schedule_dispatch(sem_b, "b", "s", "g", "2", {}))
    await asyncio.sleep(0.02)
    assert not third.done()  # blocked on the worker-wide cap
    assert running["n"] == 2  # aggregate cap held; device-b slot was free but worker full
    assert sem_b._value == 8  # device slot taken FIRST and held during the worker-wide wait
    assert metrics.worker_calls_throttled_total._value.get() == throttled_before + 1  # saturation signal

    gate.set()  # free a slot → the 3rd proceeds
    await asyncio.wait_for(third, timeout=1)
    await asyncio.gather(*list(worker._inflight_calls), return_exceptions=True)
    assert running["max"] == 2  # worker-wide cap never exceeded


@pytest.mark.asyncio
async def test_worker_cap_releases_both_slots_on_completion():
    """A finished dispatch frees both its device slot and its worker-wide slot, so a
    later call on another device can run (no leak of the aggregate slot)."""
    cfg = {"registry": {"health_check_interval": 30, "max_concurrent_calls_per_worker": 1}}
    worker = DeviceWorker(worker_id="w", config=cfg, redis_client=_shared_redis())

    async def _noop(*a):
        return None

    worker._dispatch_call = _noop  # type: ignore[method-assign]
    sem_a, sem_b = asyncio.Semaphore(10), asyncio.Semaphore(10)

    await worker._schedule_dispatch(sem_a, "a", "s", "g", "1", {})
    await asyncio.gather(*list(worker._inflight_calls), return_exceptions=True)
    await asyncio.sleep(0)
    # Worker-wide slot was released, so a different device can dispatch despite cap=1.
    await asyncio.wait_for(worker._schedule_dispatch(sem_b, "b", "s", "g", "1", {}), timeout=1)
    await asyncio.gather(*list(worker._inflight_calls), return_exceptions=True)
    assert worker._worker_call_sem._value == 1  # back to full capacity, nothing stranded


@pytest.mark.asyncio
async def test_schedule_dispatch_tracks_inflight():
    """Scheduled dispatches are tracked (so shutdown can drain them) and untracked
    on completion."""
    worker = _worker("w", _shared_redis())
    gate = asyncio.Event()

    async def _fake_dispatch(*a):
        await gate.wait()

    worker._dispatch_call = _fake_dispatch  # type: ignore[method-assign]
    sem = asyncio.Semaphore(5)
    await worker._schedule_dispatch(sem, "h", "s", "g", "1", {})
    assert len(worker._inflight_calls) == 1
    gate.set()
    await asyncio.gather(*list(worker._inflight_calls), return_exceptions=True)
    await asyncio.sleep(0)  # let done callbacks run
    assert len(worker._inflight_calls) == 0


# --- #6: drain in-flight calls on shutdown ----------------------------------


@pytest.mark.asyncio
async def test_drain_waits_for_calls_to_finish():
    worker = _worker("w", _shared_redis())
    worker._drain_timeout = 1
    finished = {"v": False}

    async def _quick():
        await asyncio.sleep(0.01)
        finished["v"] = True

    t = asyncio.create_task(_quick())
    worker._inflight_calls.add(t)
    t.add_done_callback(worker._inflight_calls.discard)

    await worker._drain_inflight_calls()
    assert finished["v"] is True
    assert not t.cancelled()


@pytest.mark.asyncio
async def test_drain_cancels_calls_exceeding_timeout():
    worker = _worker("w", _shared_redis())
    worker._drain_timeout = 0.05

    async def _hang():
        await asyncio.sleep(10)

    t = asyncio.create_task(_hang())
    worker._inflight_calls.add(t)

    await worker._drain_inflight_calls()
    assert t.cancelled()


@pytest.mark.asyncio
async def test_drain_noop_when_nothing_inflight():
    worker = _worker("w", _shared_redis())
    await worker._drain_inflight_calls()  # must not raise
