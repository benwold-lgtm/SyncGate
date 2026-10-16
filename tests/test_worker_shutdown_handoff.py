# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""A gracefully stopping worker hands its devices to a live worker immediately.

Found on a live cluster (v0.3.7 verification): deleting a worker pod left each of its
devices unowned for 42–113s, every client call answered 404 "not active", until the
reconciler had seen the device orphaned across its grace sweeps. Two causes, both pinned
here:

1. Shutdown released the claim but never republished the assignment, so a clean stop looked
   exactly like a crash.
2. Even once republished, a survivor declines an assignment at/over
   ceil(devices / live workers) — and the stopping worker left ``workers:active`` only
   after tearing its pods down, so it still counted as live and the survivor refused the
   hand-off.
"""

import asyncio

import fakeredis.aioredis
import pytest

from device_mcp_gateway.shared.keys import KEYS
from device_mcp_gateway.shared.registry_backend import RedisRegistryBackend
from device_mcp_gateway.worker.runner import DeviceWorker

CONFIG = {"registry": {"mode": "distributed"}}


class _StubPod:
    def stop(self):
        pass

    async def aclose(self):
        pass


def _redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _live(worker: DeviceWorker) -> None:
    """Register ``worker`` as live the way its heartbeat does."""
    worker._backend = RedisRegistryBackend(worker._r)
    await worker._r.sadd(KEYS.workers_active, worker._id)
    await worker._r.set(KEYS.worker_heartbeat(worker._id), "1", ex=60)


async def _own(worker: DeviceWorker, hostname: str) -> None:
    await worker._r.sadd(KEYS.devices_set, hostname)
    await worker._r.set(KEYS.claim(hostname), worker._id)
    await worker._r.sadd(KEYS.worker_devices(worker._id), hostname)
    worker._assigned.add(hostname)
    worker._pods[hostname] = _StubPod()


async def _assign_entries(r) -> list[str]:
    return [f["hostname"] for _id, f in await r.xrange(KEYS.assignments_stream) if f.get("action") == "assign"]


@pytest.mark.asyncio
async def test_graceful_stop_republishes_every_device_and_frees_its_claim():
    r = _redis()
    leaving = DeviceWorker(worker_id="A", config=CONFIG, redis_client=r)
    await _live(leaving)
    for h in ("dev1", "dev2"):
        await _own(leaving, h)

    await leaving._leave_and_hand_off()

    assert sorted(await _assign_entries(r)) == ["dev1", "dev2"]
    for h in ("dev1", "dev2"):
        assert await r.get(KEYS.claim(h)) is None, "claim must be free before the receiver tries to take it"
    assert not leaving._assigned
    assert not await r.sismember(KEYS.workers_active, "A")
    assert not await r.exists(KEYS.worker_heartbeat("A"))


@pytest.mark.asyncio
async def test_a_stop_with_no_devices_publishes_nothing():
    r = _redis()
    leaving = DeviceWorker(worker_id="A", config=CONFIG, redis_client=r)
    await _live(leaving)
    await leaving._leave_and_hand_off()
    assert await _assign_entries(r) == []


@pytest.mark.asyncio
async def test_survivor_would_decline_while_the_leaver_still_counts_as_live():
    """Why the order matters: with the leaver's heartbeat present, a survivor at target
    declines — the exact state the live cluster was in (2 workers, 3 devices, survivor
    holding 2, target ceil(3/2) = 2)."""
    r = _redis()
    leaving = DeviceWorker(worker_id="A", config=CONFIG, redis_client=r)
    survivor = DeviceWorker(worker_id="B", config=CONFIG, redis_client=r)
    await _live(leaving)
    await _live(survivor)
    await _own(leaving, "sensor")
    await _own(survivor, "refsdk1")
    await _own(survivor, "refsdk2")

    assert await survivor._decline_assignment("sensor") is True  # leaver still "live"

    await r.delete(KEYS.worker_heartbeat("A"))  # what _leave_and_hand_off does first
    assert await survivor._decline_assignment("sensor") is False


@pytest.mark.asyncio
async def test_leaver_is_out_of_the_live_set_at_the_moment_each_hand_off_is_published():
    """The ordering, pinned where it is decided. Against real Redis a receiver can read the
    assign before the leaver's next command runs, so "leave first" must hold at publish
    time — it cannot be checked end to end on fakeredis, whose calls never suspend, so the
    whole shutdown sequence finishes before any other task is scheduled."""
    r = _redis()
    leaving = DeviceWorker(worker_id="A", config=CONFIG, redis_client=r)
    await _live(leaving)
    for h in ("dev1", "dev2"):
        await _own(leaving, h)

    seen: list[tuple[str, bool, bool, bool]] = []
    real_publish = leaving._backend.publish_assignment

    async def observing_publish(action: str, hostname: str) -> None:
        seen.append(
            (
                hostname,
                await r.sismember(KEYS.workers_active, "A"),
                bool(await r.exists(KEYS.worker_heartbeat("A"))),
                await r.get(KEYS.claim(hostname)) is not None,
            )
        )
        await real_publish(action, hostname)

    leaving._backend.publish_assignment = observing_publish  # type: ignore[method-assign]
    await leaving._leave_and_hand_off()

    assert sorted(h for h, *_ in seen) == ["dev1", "dev2"]
    for hostname, member, heartbeat, claimed in seen:
        assert not member, f"{hostname} handed off while the leaver was still in workers:active"
        assert not heartbeat, f"{hostname} handed off while the leaver's heartbeat still counted it live"
        assert not claimed, f"{hostname} handed off while its claim was still held"


@pytest.mark.asyncio
async def test_survivor_takes_the_hand_off_on_its_next_read():
    """End to end through the assignment consumer: the survivor spawns what it is handed,
    with no reconciler sweep involved."""
    r = _redis()
    leaving = DeviceWorker(worker_id="A", config=CONFIG, redis_client=r)
    survivor = DeviceWorker(worker_id="B", config=CONFIG, redis_client=r)
    await _live(leaving)
    await _live(survivor)
    await r.xgroup_create(KEYS.assignments_stream, KEYS.worker_group, id="$", mkstream=True)
    await _own(leaving, "sensor")
    await _own(survivor, "refsdk1")
    await _own(survivor, "refsdk2")

    spawned: list[str] = []
    real_spawn = survivor._spawn_pod

    async def recording_spawn(hostname: str) -> None:
        # Run the real admission checks (decline + claim), then record instead of building a pod.
        if hostname in survivor._assigned or await survivor._decline_assignment(hostname):
            return
        if await survivor._acquire_claim(hostname):
            spawned.append(hostname)

    survivor._spawn_pod = recording_spawn  # type: ignore[method-assign]

    # fakeredis answers a blocking XREADGROUP immediately and without suspending, so the
    # consumer loop would spin and starve the task doing the hand-off. Real Redis blocks;
    # give the double the one property the loop relies on — that a read yields.
    real_read = r.xreadgroup

    async def yielding_read(*args, **kwargs):
        await asyncio.sleep(0.01)
        return await real_read(*args, **kwargs)

    r.xreadgroup = yielding_read  # type: ignore[method-assign]
    consumer = asyncio.create_task(survivor._consume_assignments())
    try:
        await leaving._leave_and_hand_off()
        for _ in range(100):
            if spawned:
                break
            await asyncio.sleep(0.05)
    finally:
        survivor._stop_event.set()
        await asyncio.gather(consumer, return_exceptions=True)
        survivor._spawn_pod = real_spawn  # type: ignore[method-assign]

    assert spawned == ["sensor"]
    assert await r.get(KEYS.claim("sensor")) == "B"
