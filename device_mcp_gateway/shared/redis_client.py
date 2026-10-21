# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Async Redis connection factory."""

import asyncio
import os
import random
import time
from typing import Any
from urllib.parse import urlparse

from loguru import logger

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

try:
    # Jitter matters more than the curve here: a failover hits every gateway replica and
    # every worker at the same instant, so un-jittered backoff has them all retry in
    # lockstep and hammer the newly promoted primary in synchronised waves.
    from redis.backoff import ExponentialWithJitterBackoff as _Backoff
except ImportError:  # pragma: no cover - older redis-py within our >=5.0 floor
    from redis.backoff import ExponentialBackoff as _Backoff  # type: ignore[assignment]


def redis_url(cfg: dict[str, Any]) -> str:
    """Resolve the Redis URL (MCP_REDIS_URL env overrides cfg)."""
    return os.getenv("MCP_REDIS_URL") or cfg.get("redis", {}).get("url", "redis://localhost:6379/0")


def assert_redis_secure(cfg: dict[str, Any]) -> None:
    """Refuse to run distributed mode against an unauthenticated Redis (Tier-0 F-24).

    Redis is the entire distributed control plane (registry, assignment/call/result
    streams, claims, leader locks). With no AUTH, anything that reaches port 6379 can
    inject tool calls or read state, so we require the Redis URL to carry a password
    (``redis://:<pw>@host`` or ``rediss://...``). TLS (``rediss://``) is additionally
    recommended for in-transit protection — see docs/kubernetes-architecture.md.

    Override for a trusted local/dev network with ``redis.allow_insecure: true``.
    """
    parsed = urlparse(redis_url(cfg))
    if parsed.password:
        return  # AUTH present
    if cfg.get("redis", {}).get("allow_insecure", False):
        logger.warning(
            "Redis AUTH disabled (redis.allow_insecure=true) — the distributed control plane is "
            "UNAUTHENTICATED. Anyone who can reach Redis can inject tool calls or read state. Set a "
            "password in MCP_REDIS_URL (redis://:<pw>@host, ideally rediss:// for TLS) in production."
        )
        return
    raise RuntimeError(
        "Refusing to start in distributed mode against an unauthenticated Redis: the URL carries no "
        "password, so the shared control plane (registry, tool-call streams, claims) is open to anyone "
        "who can reach it. Set a password — MCP_REDIS_URL=redis://:<password>@host:6379/0 (or rediss:// "
        "for TLS) — or, for a trusted local network only, set redis.allow_insecure: true to override."
    )


async def create_redis(cfg: dict[str, Any], max_connections: int | None = None) -> aioredis.Redis:
    """Create and return a shared async Redis client from config.

    Reads cfg["redis"] for url, socket_timeout, max_connections, and the failover
    resilience knobs (retries, health_check_interval, socket_connect_timeout).
    The MCP_REDIS_URL env var overrides cfg["redis"]["url"].

    Pass max_connections to override the pool size — used for the dedicated
    pub/sub client, which needs one connection per waiting request and so must
    be sized well above the command pool.

    **Failover resilience (review item 6).** This used to pass only ``socket_timeout``
    and ``max_connections``, which meant a primary failover reached callers as a burst of
    hard ``ConnectionError``s — pointing the gateway at a properly built HA Redis bought
    almost nothing, because the client never reconnected transparently. Four settings fix
    that, and they only work together:

    - ``retry`` with jittered exponential backoff — absorbs the reconnect burst and a short
      election instead of failing the first command that lands mid-failover. Jitter is the
      important part: a failover hits every replica and worker simultaneously, so
      un-jittered backoff would have them retry in lockstep against the new primary.
    - ``retry_on_error`` — ``retry`` alone governs connection *setup*; this is what lets an
      already-issued command be retried on the new primary.
    - ``health_check_interval`` — a connection left half-open by a failover is otherwise
      only discovered when a real command fails on it, so idle pooled connections would
      each fail once after every failover.
    - ``socket_connect_timeout`` — without it a vanished primary hangs on the OS default
      TCP connect timeout, which is far longer than the failover itself.

    Retrying a command can execute it twice when the failure happened after the server
    ran it but before the reply arrived. That is deliberate and safe here: the rate-limit
    ``INCR`` merely over-counts (fails closed), and dispatch writes are already covered by
    the worker's idempotency guard (``registry.idempotency_guard``, on by default). A
    permanent outage still surfaces — the retry budget is finite, so readiness probes and
    alerting still see a genuinely dead Redis.
    """
    redis_cfg = cfg.get("redis", {})
    url = redis_url(cfg)
    pool_size = max_connections if max_connections is not None else redis_cfg.get("max_connections", 20)
    retries = int(redis_cfg.get("retries", 5))
    # base=50ms, cap=1s, jittered: 5 retries spend up to ~2.5s worst case. Sized to absorb
    # the reconnect burst and a short election — NOT to block through a long Sentinel
    # promotion, which can run tens of seconds; blocking a request that long is worse than
    # failing it and letting the caller retry. Raise redis.retries if your election is
    # slower and you would rather wait.
    retry = Retry(_Backoff(base=0.05, cap=1.0), retries)
    # A BLOCKING pool: a caller that finds every connection busy waits up to
    # ``redis.pool_timeout`` for one, instead of getting ``MaxConnectionsError`` at once.
    # The default pool raises, and a worker running a device at its per-device call cap
    # (``max_concurrent_calls_per_device``, 20) beside its own consume loop, heartbeat and
    # health loop needs more than the default 20 connections for a moment — so calls failed
    # outright and were dead-lettered under a burst any stateless client produces. Found by
    # the 0.4.0 live run: 40 concurrent calls to one proxied device. Waiting is right because
    # the shortage is momentary; the wait is bounded well inside the gateway's exchange
    # deadline, so a genuinely starved pool still fails rather than hanging.
    pool = aioredis.BlockingConnectionPool.from_url(
        url,
        max_connections=pool_size,
        timeout=redis_cfg.get("pool_timeout", 10),
        socket_timeout=redis_cfg.get("socket_timeout", 5),
        socket_connect_timeout=redis_cfg.get("socket_connect_timeout", 5),
        decode_responses=True,
        retry=retry,
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
        health_check_interval=redis_cfg.get("health_check_interval", 30),
    )
    return aioredis.Redis.from_pool(pool)


async def wait_for_redis(client: aioredis.Redis, cfg: dict[str, Any], *, component: str = "gateway") -> None:
    """Block until Redis answers a PING, or give up after ``redis.startup_timeout`` seconds.

    The per-command ``retry`` budget configured above is deliberately short (~2.5 s) — it is
    sized for a failover mid-request, where blocking a caller any longer is worse than failing
    them. Startup is the opposite situation: nobody is waiting on a response, and the usual
    reason Redis is unreachable is that it simply has not finished starting yet.

    Without this, the first command a process issued propagated its ``ConnectionError`` out of
    ``asyncio.run`` as an unhandled traceback and the process exited non-zero. On Kubernetes
    that is *survivable* — kubelet restarts it and it eventually succeeds — but it is not
    harmless: it presents as ``CrashLoopBackOff`` with a stack trace, which reads as a broken
    deployment rather than an ordering wait, and the restart counter it leaves behind is the
    same signal an operator uses to spot real crashes. Observed on a live cluster as two
    restarts per worker before anything worked.

    ``redis.startup_timeout: 0`` restores fail-fast, for a deployment that would rather have
    the process die immediately than wait.
    """
    timeout = float(cfg.get("redis", {}).get("startup_timeout", 60))
    if timeout <= 0:
        await client.ping()
        return

    deadline = time.monotonic() + timeout
    delay = 0.5
    attempt = 0
    while True:
        attempt += 1
        try:
            await client.ping()
            if attempt > 1:
                logger.info(f"{component}: Redis reachable after {attempt} attempts")
            return
        except (RedisConnectionError, RedisTimeoutError, OSError) as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Deliberately still an exception: a Redis that never appears is a genuine
                # failure and must reach the exit code, the probe and the alert.
                raise RuntimeError(
                    f"{component}: Redis at {redis_url(cfg)} unreachable after {timeout:g}s "
                    f"({attempt} attempts): {exc}. It may still be starting — raise "
                    f"redis.startup_timeout if your Redis takes longer to come up."
                ) from exc
            # Jittered so a whole deployment restarting together does not reconnect in
            # lockstep, the same reason the command retry jitters.
            sleep_for = min(delay * (1 + random.random()), 5.0, remaining)
            logger.warning(
                f"{component}: Redis not reachable yet ({exc}); retrying in {sleep_for:.1f}s "
                f"({remaining:.0f}s left before giving up)"
            )
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, 5.0)
