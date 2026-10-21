# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tier-1 tests for distributed-mode admission control (F-06).

When a device's call stream backs up (slow/crashed worker), new tool calls used
to be accepted and silently trimmed at the stream's MAXLEN, surfacing only as a
30s client timeout. The gateway now reads the consumer-group lag before
publishing and fast-fails with HTTP 429 past a watermark, turning a silent drop
into a visible, retryable reject.
"""

import copy
from contextlib import asynccontextmanager

import fakeredis.aioredis
import pytest
import yaml

from device_mcp_gateway.main import create_app
from device_mcp_gateway.rbac import Authenticator
from device_mcp_gateway.shared.registry_backend import (
    DeviceConfig,
    MemoryRegistryBackend,
    RedisRegistryBackend,
)
from device_mcp_gateway.shared.session_router import SessionRouter

CALL_GROUP = "workers-{host}"


def _fake_redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _seed_backlog(backend: RedisRegistryBackend, host: str, n: int) -> None:
    """Create the worker consumer group and add n undelivered calls (lag = n)."""
    await backend._r.xgroup_create(f"device:{host}:calls", CALL_GROUP.format(host=host), id="0", mkstream=True)
    for i in range(n):
        await backend.publish_tool_call(host, f"r{i}", "s", "gw", {"method": "tools/call", "id": i})


# --- backend signal ----------------------------------------------------------


@pytest.mark.asyncio
async def test_call_backlog_grows_with_undelivered_calls():
    backend = RedisRegistryBackend(_fake_redis())
    assert await backend.call_backlog("dev1") == 0  # no stream yet
    await _seed_backlog(backend, "dev1", 5)
    assert await backend.call_backlog("dev1") > 0


@pytest.mark.asyncio
async def test_call_backlog_zero_when_stream_missing():
    backend = RedisRegistryBackend(_fake_redis())
    # Stream exists but no consumer group => nothing is "queued for a worker".
    await backend.publish_tool_call("dev2", "r0", "s", "gw", {"method": "x"})
    assert await backend.call_backlog("dev2") == 0


@pytest.mark.asyncio
async def test_memory_backend_never_sheds():
    # Embedded mode routes calls in-process; there is no queue to back up.
    assert await MemoryRegistryBackend().call_backlog("anything") == 0


# --- endpoint behaviour ------------------------------------------------------


class _StubBackend:
    """Backend whose admission signal is fixed, sidestepping the fakeredis hash-decode
    quirk that prevents a real DeviceConfig round-trip (covered by the integration
    suite). Records publishes so the accept path can be asserted."""

    def __init__(self, backlog: int, transport: str = "sse"):
        self._backlog = backlog
        self._transport = transport
        self.published: list[dict] = []

    async def call_backlog(self, hostname: str) -> int:
        return self._backlog

    async def get_device(self, hostname: str):
        return DeviceConfig(
            hostname=hostname, base_url=f"http://{hostname}", pod_active=True, transport=self._transport
        )

    async def publish_tool_call(self, **kwargs):
        self.published.append(kwargs)


class _StubRegistry:
    def __init__(self, backend):
        self._backend = backend

    async def get_device(self, hostname: str):
        return await self._backend.get_device(hostname)


def _distributed_app(backlog, backlog_limit, monkeypatch):
    cfg = copy.deepcopy(yaml.safe_load(open("config.yaml")))
    cfg.setdefault("registry", {})
    cfg["registry"]["mode"] = "distributed"
    cfg["registry"]["call_backlog_limit"] = backlog_limit
    # Distributed mode refuses to start without a secret key (creds → Redis); this
    # test never persists credentials, so opt into the documented plaintext override.
    cfg.setdefault("gateway", {})["allow_plaintext_credentials"] = True
    # Auth is exercised separately; this test injects a disabled Authenticator, so
    # allow the otherwise-refused anonymous distributed startup.
    cfg["gateway"]["allow_anonymous"] = True
    # State is stubbed in-process; the real-Redis-auth gate is irrelevant here.
    cfg.setdefault("redis", {})["allow_insecure"] = True
    app = create_app(override_config=cfg)

    # Replace the distributed lifespan (which dials real Redis) with a no-op and
    # wire the minimal state the admission path reads.
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    app.router.lifespan_context = _noop_lifespan
    backend = _StubBackend(backlog)
    app.state.redis = _fake_redis()
    app.state.session_router = SessionRouter(app.state.redis)
    app.state.registry = _StubRegistry(backend)
    monkeypatch.setattr(app.state, "authenticator", Authenticator({}, enabled=False))
    return app, backend
