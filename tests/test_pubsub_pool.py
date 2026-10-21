# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for the dedicated Redis pub/sub pool (S2 finding F3).

Each open SSE stream holds a Redis connection for its lifetime. Routing those
through the shared command pool (max_connections=20) capped concurrent SSE
clients per replica at ~20. SessionRouter now subscribes via a separate client
whose pool is sized independently.
"""

import asyncio

import pytest
import fakeredis.aioredis

from device_mcp_gateway.shared.redis_client import create_redis
from device_mcp_gateway.shared.session_router import SessionRouter


@pytest.mark.asyncio
async def test_create_redis_pool_size_override():
    client = await create_redis({"redis": {"url": "redis://x"}}, max_connections=500)
    assert client.connection_pool.max_connections == 500


@pytest.mark.asyncio
async def test_create_redis_default_pool_size():
    client = await create_redis({"redis": {"url": "redis://x", "max_connections": 20}})
    assert client.connection_pool.max_connections == 20


def test_session_router_uses_pubsub_client_for_subscriptions():
    command = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pubsub = fakeredis.aioredis.FakeRedis(decode_responses=True)
    router = SessionRouter(command, pubsub_client=pubsub)
    # Commands go to the command client, subscriptions to the dedicated one.
    assert router._r is command
    assert router._ps is pubsub


def test_session_router_falls_back_to_command_client():
    command = fakeredis.aioredis.FakeRedis(decode_responses=True)
    router = SessionRouter(command)
    assert router._ps is command  # back-compat: single-client deployments


@pytest.mark.asyncio
async def test_a_reply_wait_runs_on_the_pubsub_client():
    # One shared fakeredis instance passed as both clients: a result published on the command
    # client is read by a waiter on the pub/sub client (same backing store), proving the
    # blocking read uses self._ps and so cannot exhaust the command pool.
    shared = fakeredis.aioredis.FakeRedis(decode_responses=True)
    router = SessionRouter(shared, pubsub_client=shared)
    await router.publish_reply("reply:r1", {"ok": True}, ttl=60)
    assert await asyncio.wait_for(router.await_reply(["reply:r1"], timeout=2), timeout=3) == {"ok": True}
