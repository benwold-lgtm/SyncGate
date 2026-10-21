# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Stateless requests in distributed mode — ADR-0027 §1–§2, Workstream B PR 5.

The ADR's verification section, on the real-Redis tier: the mechanism under test is stream
routing and blocking reads, which a double would simulate rather than exercise. The worker side
is the real ``DeviceWorker`` consume loop and dispatcher; the gateway side is the real
``DistributedResultExchange``. Only the pod is a stand-in, and it answers with what it was
asked, so every check below is on content the device wrote rather than on a status code.
"""

from __future__ import annotations

import asyncio
import json
import random
import time

import pytest

from device_mcp_gateway.api.exchange import DistributedResultExchange
from device_mcp_gateway.core.errors import RPC_DUPLICATE, RPC_INTERNAL_ERROR, RPC_NO_WORKER
from device_mcp_gateway.shared.keys import KEYS
from device_mcp_gateway.shared.registry_backend import DeviceConfig, RedisRegistryBackend
from device_mcp_gateway.shared.session_router import SessionRouter
from device_mcp_gateway.worker.runner import DeviceWorker

pytestmark = pytest.mark.asyncio

HOST = "stateless-dist.local"
CONFIG = {"registry": {"health_check_interval": 30, "tool_call_timeout": 30}}


class _Tool:
    def __init__(self, name, method="GET"):
        self.name, self.method = name, method


class _Manifest:
    def __init__(self):
        self.tools = [_Tool("whoami", "GET"), _Tool("make_thing", "POST")]


class _EchoPod:
    """Answers with the argument it was given, after a random beat so calls interleave."""

    def __init__(self, *, fail=False):
        self.manifest = _Manifest()
        self.calls: list[dict] = []
        self.fail = fail

    async def call_tool(self, message):
        self.calls.append(message)
        if self.fail:
            raise RuntimeError("upstream exploded")
        await asyncio.sleep(random.uniform(0, 0.05))
        who = (message.get("params") or {}).get("arguments", {}).get("who")
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {"content": [{"type": "text", "text": who}]}}


async def _stack(real_redis, pod=None):
    """A real worker consuming the device's stream, and a backend with the device registered."""
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device(HOST, DeviceConfig(hostname=HOST, base_url="http://d.local"))
    w = DeviceWorker(worker_id="w-stateless", config=CONFIG, redis_client=real_redis)
    w._backend = backend
    if pod is not None:
        w._pods[HOST] = pod
    w._assigned.add(HOST)
    consumer = asyncio.create_task(w._dispatcher.consume_calls(HOST))
    return backend, w, consumer


async def _stop(w, consumer):
    w._stop_event.set()
    w._assigned.discard(HOST)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)


def _replica(real_redis, backend, gateway_id):
    return DistributedResultExchange(backend, SessionRouter(real_redis, real_redis), gateway_id, {})


def _call(who, msg_id=1, tool="whoami"):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool, "arguments": {"who": who}}}


# --- §1/§2: the disclosure, directly -----------------------------------------------------------


async def test_two_principals_with_the_same_id_each_get_their_own_answer(real_redis):
    """Two callers, both numbering from 1, concurrently, through two gateway replicas.

    Before ADR-0027 a sessionless result would have gone to one shared stream searched by the
    client's id, and each caller could have received the other's. Checked by content the
    device wrote — the caller's own name — not by the absence of an error.
    """
    pod = _EchoPod()
    backend, w, consumer = await _stack(real_redis, pod)
    try:
        replica_a = _replica(real_redis, backend, "gw-a")
        replica_b = _replica(real_redis, backend, "gw-b")
        calls = []
        for i in range(20):
            calls.append(replica_a.exchange(HOST, _call(f"alice-{i}"), session_id="", subject="key:alice", rid=f"a{i}"))
            calls.append(replica_b.exchange(HOST, _call(f"bob-{i}"), session_id="", subject="key:bob", rid=f"b{i}"))
        results = await asyncio.wait_for(asyncio.gather(*calls), timeout=30)
    finally:
        await _stop(w, consumer)

    for i in range(20):
        assert results[2 * i]["result"]["content"][0]["text"] == f"alice-{i}"
        assert results[2 * i + 1]["result"]["content"][0]["text"] == f"bob-{i}"
        assert results[2 * i]["id"] == results[2 * i + 1]["id"] == 1  # echoed, used for nothing else
    assert len(pod.calls) == 40


async def test_the_shared_bucket_is_never_written(real_redis):
    pod = _EchoPod()
    backend, w, consumer = await _stack(real_redis, pod)
    try:
        await _replica(real_redis, backend, "gw-a").exchange(HOST, _call("x"), session_id="", subject="s", rid="r")
    finally:
        await _stop(w, consumer)
    assert not await real_redis.exists(KEYS.session_results(""))


async def test_a_reply_stream_is_gone_once_read(real_redis):
    pod = _EchoPod()
    backend, w, consumer = await _stack(real_redis, pod)
    try:
        await _replica(real_redis, backend, "gw-a").exchange(HOST, _call("x"), session_id="", subject="s", rid="r")
    finally:
        await _stop(w, consumer)
    assert [k async for k in real_redis.scan_iter(match="reply:*")] == []


# --- the worker's route rule -------------------------------------------------------------------


async def _deliver(real_redis, fields):
    stream = KEYS.device_calls(HOST)
    group = KEYS.device_calls_group(HOST)
    try:
        await real_redis.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception:  # noqa: BLE001 - BUSYGROUP
        pass
    await real_redis.xadd(stream, fields)
    [(_s, [(msg_id, got)])] = await real_redis.xreadgroup(group, "t", {stream: ">"}, count=1)
    return stream, group, msg_id, got


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"session_id": ""}, "no reply route"),
        ({}, "no reply route"),
        ({"reply_to": "victim:key"}, "invalid reply route"),
        ({"reply_to": "session::results"}, "invalid reply route"),
    ],
)
async def test_an_entry_with_nowhere_to_answer_is_dead_lettered_and_not_run(real_redis, fields, reason):
    pod = _EchoPod()
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device(HOST, DeviceConfig(hostname=HOST, base_url="http://d.local"))
    w = DeviceWorker(worker_id="w", config=CONFIG, redis_client=real_redis)
    w._backend = backend
    w._pods[HOST] = pod
    entry = {"request_id": "r-none", "rid": "rid", "message": json.dumps(_call("x")), **fields}

    stream, group, msg_id, got = await _deliver(real_redis, entry)
    await w._dispatch_call(HOST, stream, group, msg_id, got)

    assert pod.calls == [], "a call with nowhere to answer must not run"
    dead = await real_redis.xrange(KEYS.device_calls_dead(HOST))
    assert dead and dead[-1][1].get("reason") == reason
    assert not await real_redis.exists(KEYS.session_results(""))
    assert await real_redis.type("victim:key") == "none"  # never written


async def test_an_old_gateways_entry_still_reaches_its_session(real_redis):
    """Workers roll first, so a new worker serves old gateways for the whole roll."""
    pod = _EchoPod()
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device(HOST, DeviceConfig(hostname=HOST, base_url="http://d.local"))
    w = DeviceWorker(worker_id="w", config=CONFIG, redis_client=real_redis)
    w._backend = backend
    w._pods[HOST] = pod
    entry = {"request_id": "r-old", "session_id": "sess-old", "rid": "rid", "message": json.dumps(_call("old"))}

    stream, group, msg_id, got = await _deliver(real_redis, entry)
    await w._dispatch_call(HOST, stream, group, msg_id, got)

    [(_id, published)] = await real_redis.xrange(KEYS.session_results("sess-old"))
    assert json.loads(published["data"])["result"]["content"][0]["text"] == "old"


async def test_a_new_gateway_beside_an_old_worker_is_answered_through_the_shim(real_redis):
    """An old worker ignores reply_to and builds a key from session_id; the gateway set that to
    req:{request_id} and waits on it too, so the answer still arrives and is still unique."""
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device(HOST, DeviceConfig(hostname=HOST, base_url="http://d.local"))
    router = SessionRouter(real_redis, real_redis)
    stream = KEYS.device_calls(HOST)

    async def old_worker():
        while True:
            entries = await real_redis.xrange(stream)
            if entries:
                fields = entries[-1][1]
                msg = json.loads(fields["message"])
                # What a pre-0.4.0 worker does: publish to session:{session_id}:results.
                await router.publish_result(
                    fields["session_id"], {"jsonrpc": "2.0", "id": msg["id"], "result": {"via": "old-worker"}}
                )
                return fields
            await asyncio.sleep(0.02)

    task = asyncio.create_task(old_worker())
    result = await _replica(real_redis, backend, "gw-new").exchange(
        HOST, _call("x"), session_id="", subject="s", rid="r", timeout=10
    )
    fields = await task
    assert result["result"] == {"via": "old-worker"}
    assert fields["session_id"].startswith("req:") and fields["reply_to"].startswith("reply:")
    assert not await real_redis.exists(KEYS.session_results(fields["session_id"]))  # cleaned up


# --- error results arrive, rather than a timeout -----------------------------------------------


async def _timed(coro):
    t = time.monotonic()
    result = await coro
    return result, time.monotonic() - t


async def test_no_pod_answers_a_stateless_call_before_the_deadline(real_redis):
    backend, w, consumer = await _stack(real_redis, pod=None)
    try:
        result, took = await _timed(
            _replica(real_redis, backend, "gw").exchange(HOST, _call("x"), session_id="", subject="s", rid="r")
        )
    finally:
        await _stop(w, consumer)
    assert result["error"]["code"] == RPC_NO_WORKER and took < 10


async def test_a_dispatch_error_answers_a_stateless_call_before_the_deadline(real_redis):
    backend, w, consumer = await _stack(real_redis, _EchoPod(fail=True))
    try:
        result, took = await _timed(
            _replica(real_redis, backend, "gw").exchange(HOST, _call("x"), session_id="", subject="s", rid="r")
        )
    finally:
        await _stop(w, consumer)
    assert result["error"]["code"] == RPC_INTERNAL_ERROR and took < 10


async def test_quarantine_answers_a_stateless_call_before_the_deadline(real_redis):
    backend, w, consumer = await _stack(real_redis, _EchoPod())
    await backend.update_device_fields(HOST, fingerprint_state="pending_approval", fingerprint_policy="enforce")
    try:
        result, took = await _timed(
            _replica(real_redis, backend, "gw").exchange(HOST, _call("x"), session_id="", subject="s", rid="r")
        )
    finally:
        await _stop(w, consumer)
    assert result["error"]["code"] == RPC_NO_WORKER and "fingerprint" in result["error"]["message"].lower()
    assert took < 10


async def test_a_suppressed_duplicate_answers_on_its_reply_stream(real_redis):
    """F-08's guard refuses a redelivered non-idempotent call; that refusal must reach a
    stateless caller too, not only a session-bound one."""
    pod = _EchoPod()
    backend = RedisRegistryBackend(real_redis)
    await backend.set_device(HOST, DeviceConfig(hostname=HOST, base_url="http://d.local"))
    w = DeviceWorker(worker_id="w", config=CONFIG, redis_client=real_redis)
    w._backend = backend
    w._pods[HOST] = pod
    reply = KEYS.reply("r-dup")
    entry = {
        "request_id": "r-dup",
        "session_id": "req:r-dup",
        "reply_to": reply,
        "rid": "rid",
        "message": json.dumps(_call("x", tool="make_thing")),
    }
    await real_redis.set(KEYS.exec_marker("r-dup"), "1")  # a previous attempt began and never finished

    stream, group, msg_id, got = await _deliver(real_redis, entry)
    await w._dispatch_call(HOST, stream, group, msg_id, got)

    result = await SessionRouter(real_redis, real_redis).await_reply([reply], timeout=5)
    assert result is not None and result["error"]["code"] == RPC_DUPLICATE
    assert pod.calls == []


async def test_a_late_result_lands_in_a_key_that_expires(real_redis):
    router = SessionRouter(real_redis, real_redis)
    key = KEYS.reply("late")
    await router.publish_reply(key, {"jsonrpc": "2.0", "id": 1, "result": {}}, ttl=90)
    assert 0 < await real_redis.ttl(key) <= 90
    assert await real_redis.xlen(key) == 1
    await router.publish_reply(key, {"jsonrpc": "2.0", "id": 1, "result": {}}, ttl=90)
    assert await real_redis.xlen(key) == 1  # MAXLEN 1: a reply stream holds one answer
