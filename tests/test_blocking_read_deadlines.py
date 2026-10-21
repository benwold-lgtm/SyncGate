# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm-Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""XREAD BLOCK against the connection's socket read deadline.

Found on the live cluster, not here: a Streamable HTTP call to a device whose worker was
frozen returned **500** after 32s instead of the 504 the code intends. The cause is that
``XREAD BLOCK`` holds the connection with the server silent until the window elapses, so a
socket read timeout less than or equal to the block window fires *first* — every time, not
occasionally. Shipped defaults were ``block=5000ms`` against ``socket_timeout=5``: exactly
equal, and 6/6 idle reads raised when measured in-cluster.

The existing real-Redis tier could not catch it because its ``real_redis`` fixture builds a
client with **no** ``socket_timeout``, while ``create_redis`` sets one. So these tests build
their client the way production does — that difference is the entire bug, and a fixture that
smooths it over is how it survived to a cluster.
"""

import time

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from redis.exceptions import TimeoutError as RedisTimeoutError

from device_mcp_gateway.shared.session_router import SessionRouter, _safe_block_ms

from .conftest import TEST_REDIS_URL

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def timed_redis():
    """A client carrying a socket read deadline, as ``create_redis`` builds in production.

    Deliberately *not* the shared ``real_redis`` fixture: that one omits socket_timeout and
    would make every assertion here vacuous.
    """
    client = aioredis.from_url(TEST_REDIS_URL, decode_responses=True, socket_timeout=1)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip(f"real Redis not reachable at {TEST_REDIS_URL}")
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


# --- the sizing rule itself ---------------------------------------------------


class _Pool:
    def __init__(self, **kwargs):
        self.connection_kwargs = kwargs


class _Client:
    def __init__(self, **kwargs):
        self.connection_pool = _Pool(**kwargs)


@pytest.mark.parametrize("socket_timeout", [0.05, 0.2, 0.5, 1, 2, 5, 30])
def test_the_block_window_always_expires_before_the_socket_deadline(socket_timeout):
    """The invariant, across the range an operator might configure.

    Asserted as a strict inequality against the socket timeout rather than against a
    hard-coded number, so it keeps meaning if the ceiling is ever retuned. The very short
    deadlines matter: a fixed lower bound on the window would satisfy the sensible values
    and quietly reintroduce the overlap below it.
    """
    block_ms = _safe_block_ms(_Client(socket_timeout=socket_timeout))
    assert block_ms < socket_timeout * 1000, f"block {block_ms}ms >= socket deadline {socket_timeout * 1000}ms"
    assert block_ms > 0


def test_no_socket_deadline_means_the_full_window_is_used():
    """Nothing to collide with, so don't pay for headroom that buys nothing."""
    assert _safe_block_ms(_Client(socket_timeout=None)) == 5_000


def test_a_client_without_a_pool_falls_back_rather_than_raising():
    """Stubs and fakes in the unit suite expose no connection pool."""

    class Bare:
        pass

    assert _safe_block_ms(Bare()) == 5_000


def test_an_explicit_ceiling_is_never_widened():
    """A caller asking for a short window must get one.

    The floor that keeps a tiny socket deadline from producing a useless window must not
    override an explicitly requested ceiling — it did, which stopped a shortened
    ``_XREAD_BLOCK_MS`` from taking effect and left the idle-lease test spinning too slowly
    to observe a refresh.
    """
    assert _safe_block_ms(_Client(socket_timeout=5), ceiling=10) == 10


# --- the behaviour that actually broke ----------------------------------------


@pytest.mark.asyncio
async def test_an_unanswered_call_times_out_rather_than_raising(timed_redis):
    """The regression: this is what returned 500 instead of 504 on the cluster.

    ``await_result`` must report "no answer" by returning None, so the caller can map it to
    ExchangeTimeout → 504. Before the fix the socket deadline fired inside the blocking read
    and a raw ``redis.exceptions.TimeoutError`` escaped, which FastAPI turned into a 500 —
    telling the client the gateway was broken rather than that the device did not answer.
    """
    router = SessionRouter(timed_redis, timed_redis)
    started = time.monotonic()
    try:
        result = await router.await_result("s-unanswered", msg_id=1, cursor="0-0", timeout=2.5)
    except RedisTimeoutError as exc:
        pytest.fail(f"await_result leaked a transport error instead of reporting a timeout: {exc!r}")
    elapsed = time.monotonic() - started

    assert result is None
    # It must actually have waited: returning None instantly would "pass" while giving the
    # device no chance to answer at all.
    assert 2.0 <= elapsed < 6.0, f"waited {elapsed:.2f}s, expected to honour the 2.5s deadline"


@pytest.mark.asyncio
async def test_the_callers_timeout_is_what_decides_the_wait(timed_redis):
    """The deadline must be the caller's, not (retries x socket_timeout).

    Two different timeouts must produce two different waits. With the deadline unreachable —
    the pre-fix shape, where one blocking read consumed the whole budget through retries and
    then raised — this relationship does not hold.
    """
    router = SessionRouter(timed_redis, timed_redis)

    t0 = time.monotonic()
    assert await router.await_result("s-short", msg_id=1, cursor="0-0", timeout=1.0) is None
    short = time.monotonic() - t0

    t0 = time.monotonic()
    assert await router.await_result("s-long", msg_id=1, cursor="0-0", timeout=3.0) is None
    long = time.monotonic() - t0

    assert long > short + 1.0, f"short={short:.2f}s long={long:.2f}s — the timeout is not being honoured"


@pytest.mark.asyncio
async def test_a_result_still_arrives_promptly_under_a_socket_deadline(timed_redis):
    """Guard against "fixing" the timeout by never reading anything.

    The wait must still return the answer, and return it quickly — well inside the deadline,
    not at it.
    """
    router = SessionRouter(timed_redis, timed_redis)
    await router.publish_result("s-answered", {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})

    t0 = time.monotonic()
    result = await router.await_result("s-answered", msg_id=7, cursor="0-0", timeout=5.0)
    elapsed = time.monotonic() - t0

    assert result is not None and result["result"] == {"ok": True}
    assert elapsed < 1.0, f"took {elapsed:.2f}s to read an already-published result"
