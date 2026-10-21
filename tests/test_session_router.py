# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for the distributed SSE SessionRouter.

Regression coverage for S1 real-concern RC-2: register() pipelines hset + expire
so a registered session always carries a TTL and cannot leak.
"""

import pytest
import fakeredis.aioredis

from device_mcp_gateway.shared.session_router import SessionRouter


def _router():
    return SessionRouter(fakeredis.aioredis.FakeRedis(decode_responses=True))


def _norm(d):
    # This fakeredis version doesn't honour decode_responses for hash fields;
    # real Redis decodes, so production get() returns str keys. Normalise here.
    if not d:
        return d
    dec = lambda x: x.decode() if isinstance(x, bytes) else x  # noqa: E731
    return {dec(k): dec(v) for k, v in d.items()}


@pytest.mark.asyncio
async def test_register_sets_fields_and_ttl():
    router = _router()
    await router.register("sess1", "dev1", "gw-a", ttl=3600)

    assert _norm(await router.get("sess1")) == {"hostname": "dev1", "gateway_id": "gw-a"}

    # The hash must carry a TTL — never persist without expiry.
    ttl = await router._r.ttl("session:sess1")
    assert 0 < ttl <= 3600


@pytest.mark.asyncio
async def test_register_always_has_ttl_default():
    router = _router()
    await router.register("sess2", "dev2", "gw-b")  # default 24h TTL
    ttl = await router._r.ttl("session:sess2")
    assert ttl > 0  # -1 (no expiry) would mean the leak this fix prevents


@pytest.mark.asyncio
async def test_get_unknown_returns_none():
    router = _router()
    assert await router.get("missing") is None


@pytest.mark.asyncio
async def test_delete_removes_session():
    router = _router()
    await router.register("sess3", "dev3", "gw-c")
    await router.delete("sess3")
    assert await router.get("sess3") is None


# --- SRE #3: durable result delivery via a per-session stream ---------------


@pytest.mark.asyncio
async def test_delete_removes_results_stream():
    router = _router()
    await router.publish_result("s3", {"n": 1})
    assert await router._r.exists("session:s3:results") == 1
    await router.delete("s3")
    assert await router._r.exists("session:s3:results") == 0


@pytest.mark.asyncio
async def test_results_stream_is_bounded():
    """A client that never reads can't grow the stream without bound (MAXLEN)."""
    from device_mcp_gateway.shared.session_router import _RESULTS_MAXLEN

    router = _router()
    for i in range(_RESULTS_MAXLEN + 500):
        await router.publish_result("s4", {"i": i})
    assert await router._r.xlen("session:s4:results") <= _RESULTS_MAXLEN


@pytest.mark.asyncio
async def test_publish_result_sets_ttl_on_stream():
    """The results stream carries the session TTL so an abandoned session can't leak."""
    router = _router()
    await router.publish_result("s5", {"n": 1})
    assert await router._r.ttl("session:s5:results") > 0


# --- RC-3: throttle TTL refresh on busy streams ----------------------------


# --- Fleet sessions: display-name -> {hostname, real_name, ...} lookup table --
