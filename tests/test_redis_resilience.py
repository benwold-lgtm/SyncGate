# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Redis client failover resilience (third-party review item 6).

``create_redis`` used to pass only ``socket_timeout`` and ``max_connections``. With no
``retry``, ``retry_on_error``, ``health_check_interval`` or ``socket_connect_timeout``, a
primary failover surfaced to callers as a burst of hard ``ConnectionError``s rather than a
transparent reconnect — so even a correctly built HA Redis did not actually buy HA.

These tests assert the *policy semantics* (does a transient failure recover? does a
permanent one still surface?), not merely that the kwargs are present.
"""

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError

from device_mcp_gateway.shared.redis_client import create_redis

_CFG = {"redis": {"url": "redis://localhost:6379/0"}}


def _kwargs(client):
    return client.connection_pool.connection_kwargs


@pytest.mark.asyncio
async def test_transient_connection_errors_are_retried_through_a_failover():
    """THE point of the item: a few failed attempts (a primary election in progress)
    must resolve into a successful command, not an exception in the caller's face."""
    client = await create_redis(_CFG)
    retry = _kwargs(client)["retry"]

    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RedisConnectionError("primary is failing over")
        return "OK"

    async def _noop(_exc):
        return None

    assert await retry.call_with_retry(flaky, _noop) == "OK"
    assert attempts["n"] == 3  # two failures absorbed, third succeeded


@pytest.mark.asyncio
async def test_timeouts_are_retried_too():
    """A failover shows up as a timeout at least as often as a refused connection."""
    client = await create_redis(_CFG)
    retry = _kwargs(client)["retry"]

    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RedisTimeoutError("read timed out")
        return "OK"

    async def _noop(_exc):
        return None

    assert await retry.call_with_retry(flaky, _noop) == "OK"


@pytest.mark.asyncio
async def test_a_permanent_outage_still_surfaces():
    """Retry must not mask a Redis that is genuinely gone — the caller has to find out,
    and the readiness probe/alerting depends on it."""
    client = await create_redis(_CFG)
    retry = _kwargs(client)["retry"]

    attempts = {"n": 0}

    async def always_down():
        attempts["n"] += 1
        raise RedisConnectionError("redis is gone")

    async def _noop(_exc):
        return None

    with pytest.raises(RedisConnectionError):
        await retry.call_with_retry(always_down, _noop)
    assert attempts["n"] > 1  # it did try more than once
    assert attempts["n"] < 10  # ...but gave up rather than hanging forever


@pytest.mark.asyncio
async def test_command_level_retry_is_enabled_for_connection_and_timeout_errors():
    """``retry`` alone only covers connection setup; ``retry_on_error`` is what makes an
    in-flight command survive the failover."""
    errors = _kwargs(await create_redis(_CFG))["retry_on_error"]
    assert RedisConnectionError in errors
    assert RedisTimeoutError in errors


@pytest.mark.asyncio
async def test_health_check_interval_is_set():
    """Without this, a pooled connection left half-open by a failover is only discovered
    when a real command fails on it."""
    assert _kwargs(await create_redis(_CFG))["health_check_interval"] > 0


@pytest.mark.asyncio
async def test_socket_connect_timeout_is_bounded():
    """A vanished primary must fail fast enough to retry against the new one, rather than
    hanging on the OS default TCP connect timeout."""
    connect_timeout = _kwargs(await create_redis(_CFG))["socket_connect_timeout"]
    assert 0 < connect_timeout <= 10


@pytest.mark.asyncio
async def test_resilience_settings_are_configurable():
    cfg = {
        "redis": {
            "url": "redis://localhost:6379/0",
            "retries": 4,
            "health_check_interval": 42,
            "socket_connect_timeout": 3,
        }
    }
    kw = _kwargs(await create_redis(cfg))
    assert kw["health_check_interval"] == 42
    assert kw["socket_connect_timeout"] == 3

    # Assert the retry budget behaviourally rather than via a version-specific accessor
    # (the dep floor is redis>=5.0): 4 retries must mean 5 total attempts.
    attempts = {"n": 0}

    async def down():
        attempts["n"] += 1
        raise RedisConnectionError("nope")

    async def _noop(_exc):
        return None

    with pytest.raises(RedisConnectionError):
        await kw["retry"].call_with_retry(down, _noop)
    assert attempts["n"] == 5


@pytest.mark.asyncio
async def test_retries_can_be_disabled():
    """Zero retries must mean zero — an operator who wants failures surfaced immediately
    (or is debugging) should not get silent retrying."""
    client = await create_redis({"redis": {"url": "redis://localhost:6379/0", "retries": 0}})
    retry = _kwargs(client)["retry"]

    attempts = {"n": 0}

    async def down():
        attempts["n"] += 1
        raise RedisConnectionError("nope")

    async def _noop(_exc):
        return None

    with pytest.raises(RedisConnectionError):
        await retry.call_with_retry(down, _noop)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_pubsub_client_gets_the_same_resilience():
    """The pub/sub client carries every open SSE stream; it needs the failover handling
    at least as much as the command pool does."""
    kw = _kwargs(await create_redis(_CFG, max_connections=500))
    assert kw["retry"] is not None
    assert kw["health_check_interval"] > 0


@pytest.mark.asyncio
async def test_existing_pool_and_timeout_settings_are_preserved():
    client = await create_redis({"redis": {"url": "redis://x", "socket_timeout": 9, "max_connections": 33}})
    assert client.connection_pool.max_connections == 33
    assert _kwargs(client)["socket_timeout"] == 9


# --- pool exhaustion: wait, don't fail --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_pool_blocks_rather_than_raising_when_every_connection_is_busy():
    import redis.asyncio as aioredis

    client = await create_redis({"redis": {"url": "redis://localhost:6379/0", "pool_timeout": 7}})
    assert isinstance(client.connection_pool, aioredis.BlockingConnectionPool)
    assert client.connection_pool.timeout == 7


@pytest.mark.asyncio
async def test_a_caller_waits_for_a_held_connection_instead_of_failing(real_redis):
    """The 0.4.0 live run: 40 concurrent calls to one device exhausted a worker's 20-connection
    pool, and the default pool raised MaxConnectionsError, dead-lettering calls. With one
    connection held by a blocking read, a second command must wait for it and succeed."""
    import asyncio

    from tests.conftest import TEST_REDIS_URL

    client = await create_redis({"redis": {"url": TEST_REDIS_URL, "max_connections": 1, "pool_timeout": 5}})
    try:
        held = asyncio.create_task(client.xread({"nothing-here": "$"}, block=400))
        await asyncio.sleep(0.05)  # the read now holds the only connection
        await client.set("pool-probe", "1")  # waits for it rather than raising
        assert await client.get("pool-probe") == "1"
        await held
    finally:
        await client.aclose()
