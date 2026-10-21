# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""
Handshake-era session registry and result routing over Redis Streams.

Distributed mode only. A Streamable HTTP POST can land on any gateway replica while the
device's pod runs on one worker, so the replica handling the request waits on a stream the
worker publishes to:

  Worker → XADD session:{id}:results <json-rpc-response>   (a handshake-era session)
  Worker → XADD reply:{request_id} <json-rpc-response>    (a stateless call, ADR-0027 §1)
         → the waiting replica (XREAD BLOCK) → the POST's response body

A Stream rather than pub/sub (SRE #3): pub/sub is fire-and-forget, so a result published
before the waiter is reading would be lost. A Stream buffers it, and a waiter reads from a
cursor taken before publishing (a session's stream) or from the start (a reply stream, which
nothing else writes to). Session keys carry a TTL so abandoned sessions expire; reply streams
carry a short one and are deleted once read.

Until 0.4.0 this also fed the HTTP+SSE transport's long-lived result streams; that went with
the transport.
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger
from redis.exceptions import TimeoutError as RedisTimeoutError

from device_mcp_gateway.shared.keys import KEYS

SESSION_TTL = 86_400  # 24 h — refreshed periodically while the stream is active
# Cap a session's buffered results so a client that stops reading can't grow the
# stream without bound. Approximate trimming keeps XADD O(1).
_RESULTS_MAXLEN = 1000
# Block this long on each XREAD before looping. Short enough that client
# disconnect (task cancellation) and shutdown are observed promptly; long enough
# to avoid a busy spin. sse-starlette sends its own keep-alive pings meanwhile.
_XREAD_BLOCK_MS = 5_000
# The block window must expire strictly BEFORE the connection's socket read deadline.
# XREAD BLOCK holds the connection with the server silent until the window elapses, so
# when socket_timeout <= block the client always gives up first — deterministically, on
# every idle poll, not as a race. redis-py's retry then re-issues the command, so the
# symptom is not a fast failure but the caller's own deadline being silently replaced by
# (retries x socket_timeout) and then a raised TimeoutError. The shipped defaults were
# block=5000ms against socket_timeout=5s, i.e. exactly equal: 6/6 idle reads raised.
_BLOCK_HEADROOM_MS = 1_000


def _safe_block_ms(client: Any, ceiling: int | None = None) -> int:
    """A blocking-read window guaranteed to elapse before ``client``'s socket deadline.

    Read off the pool rather than off config so it stays correct however the client was
    built — including a ``redis.socket_timeout`` override that would otherwise silently
    reintroduce the overlap.

    ``ceiling`` defaults to the module constant *at call time*, not as a default argument:
    binding it at definition time would freeze the value and silently ignore a test that
    shortens ``_XREAD_BLOCK_MS`` to make the loop spin.
    """
    if ceiling is None:
        ceiling = _XREAD_BLOCK_MS
    try:
        socket_timeout = client.connection_pool.connection_kwargs.get("socket_timeout")
    except Exception:  # a fake/stub client in tests exposes no pool
        return ceiling
    if not socket_timeout:
        return ceiling  # no read deadline to collide with
    deadline_ms = int(socket_timeout * 1000)
    window = deadline_ms - _BLOCK_HEADROOM_MS
    if window < 250:
        # A deadline at or under the headroom itself: subtracting it would leave nothing (or
        # a negative). Take half the deadline instead — still strictly inside it, which is
        # the only property that matters, and never clamped up to a floor that would exceed
        # it. A fixed floor here would reintroduce the exact overlap this guards against.
        window = max(1, deadline_ms // 2)
    return min(ceiling, window)


def _results_key(session_id: str) -> str:
    return KEYS.session_results(session_id)


def _field(fields: dict, name: str) -> str | None:
    """Read one field out of a stream entry.

    Both clients decode: real Redis because ``decode_responses=True`` is set at the single
    construction site, and fakeredis since 2.37.1. It used to accept bytes as well, purely
    to accommodate the older fake.
    """
    return fields.get(name)


class SessionRouter:
    """Register SSE sessions and route results across gateway instances."""

    def __init__(self, redis_client: Any, pubsub_client: Any = None) -> None:
        self._r = redis_client
        # Long-lived SSE subscriptions each hold a connection for their whole
        # lifetime. Route them through a dedicated client/pool so they don't
        # exhaust the shared command pool (F3). Falls back to the command client
        # when no separate one is supplied.
        self._ps = pubsub_client if pubsub_client is not None else redis_client

    async def register(
        self,
        session_id: str,
        hostname: str,
        gateway_id: str,
        ttl: int = SESSION_TTL,
        owner: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> None:
        """Record that session_id is held by this gateway instance.

        ``owner`` is the principal subject that opened the session; it binds the
        session to that principal so another caller can't post to it (F-37).

        ``extra`` carries fields a particular session kind needs — a fleet session stores
        the device list it was opened over. It is a separate argument rather than an
        overload of ``hostname`` because that field is singular everywhere else, and a
        comma-joined list hiding in it would read as one very oddly named device.
        """
        key = KEYS.session(session_id)
        mapping = {"hostname": hostname, "gateway_id": gateway_id}
        if owner is not None:
            mapping["owner"] = owner
        if extra:
            mapping.update(extra)
        # Pipeline hset + expire so the hash never lands without a TTL — a drop
        # between two separate round-trips would otherwise leak the session key.
        pipe = self._r.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, ttl)
        await pipe.execute()
        logger.debug(f"Session registered: session_id={session_id} gateway={gateway_id}")

    async def get(self, session_id: str) -> dict | None:
        h = await self._r.hgetall(KEYS.session(session_id))
        if not h:
            return None
        return dict(h)

    async def refresh(self, session_id: str, ttl: int = SESSION_TTL) -> None:
        # Keep the session hash and its results stream on the same TTL so neither
        # outlives the other.
        pipe = self._r.pipeline()
        pipe.expire(KEYS.session(session_id), ttl)
        pipe.expire(_results_key(session_id), ttl)
        await pipe.execute()

    async def delete(self, session_id: str) -> None:
        pipe = self._r.pipeline()
        pipe.delete(KEYS.session(session_id))
        pipe.delete(_results_key(session_id))
        await pipe.execute()
        logger.debug(f"Session deleted: session_id={session_id}")

    async def results_cursor(self, session_id: str) -> str:
        """The id of the last entry currently on a session's results stream.

        Taken **before** dispatching so the wait below reads only what arrives afterwards.
        Reading from ``0`` instead would be wrong rather than merely wasteful: JSON-RPC ids
        are chosen by the client and may repeat across requests within one session, so an
        earlier result carrying the same id would be matched as this request's answer.
        """
        entries = await self._ps.xrevrange(_results_key(session_id), count=1)
        if not entries:
            return "0-0"
        entry_id = entries[0][0]
        return entry_id.decode() if isinstance(entry_id, bytes) else entry_id

    async def await_result(
        self,
        session_id: str,
        msg_id: Any,
        *,
        cursor: str,
        timeout: float,
    ) -> dict | None:
        """Wait for the result carrying ``msg_id``, or None if the deadline passes.

        This is what lets a Streamable HTTP POST answer on its own response in distributed
        mode: the replica handling the request is not the one that produces the result — a
        worker does, and publishes it here — so the replica has to wait on the stream and
        correlate by JSON-RPC id.

        Reads on the pub/sub pool, not the command pool. Each waiting request holds a
        connection for the duration of the call, the same shape as an open SSE stream, and
        the command pool (20 by default) would be exhausted by a handful of concurrent
        requests.
        """
        key = _results_key(session_id)
        last_id = cursor
        deadline = time.monotonic() + timeout
        ceiling = _safe_block_ms(self._ps)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            block_ms = max(1, min(int(remaining * 1000), ceiling))
            try:
                resp = await self._ps.xread({key: last_id}, count=10, block=block_ms)
            except RedisTimeoutError:
                # Not an answer and not a failure — just a window that elapsed. Looping
                # keeps ``timeout`` the single authority on how long this call waits, which
                # is the contract the caller maps to 504. Letting it propagate instead
                # surfaced as a 500 on the genuine no-worker path, because the deadline was
                # never reconsulted.
                continue
            if not resp:
                continue  # window elapsed with nothing new — re-check the deadline
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    last_id = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                    raw = _field(fields, "data")
                    if raw is None:
                        continue
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning(f"Non-JSON entry on {key}: {raw!r}")
                        continue
                    if isinstance(message, dict) and message.get("id") == msg_id:
                        return message
                    # Another in-flight request on this session owns that one; leave it on
                    # the stream for whoever is waiting, and keep reading.

    async def await_reply(self, keys: list[str], *, timeout: float) -> dict | None:
        """Wait for the single result on a stateless call's reply stream(s), then delete them.

        ``keys`` is the call's own ``reply:{request_id}`` and, for one release, the
        ``session:req:{request_id}:results`` an older worker publishes to instead (ADR-0027 §1).
        Both are unique to this call, so each is read from the start and the first entry on
        either **is** the answer: there is no cursor to take and nothing to correlate, because
        nothing else writes to them. The client's JSON-RPC id plays no part (§2).
        """
        deadline = time.monotonic() + timeout
        ceiling = _safe_block_ms(self._ps)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                block_ms = max(1, min(int(remaining * 1000), ceiling))
                try:
                    resp = await self._ps.xread({k: "0-0" for k in keys}, count=1, block=block_ms)
                except RedisTimeoutError:
                    continue
                for _stream, entries in resp or []:
                    for _entry_id, fields in entries:
                        raw = _field(fields, "data")
                        if raw is None:
                            continue
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning(f"Non-JSON entry on a reply stream: {raw!r}")
                            continue
                        if isinstance(message, dict):
                            return message
        finally:
            # Read once, by one waiter: delete on the way out, answered or not, so a late
            # result lands in a key that is already gone rather than one left to its TTL.
            await self._r.delete(*keys)

    async def publish_reply(self, key: str, result: dict, *, ttl: int) -> None:
        """Publish a stateless call's result to its single-request stream (ADR-0027 §1).

        ``MAXLEN 1``: the stream holds one answer. The TTL bounds a result that arrives after
        its waiter gave up, so an abandoned key expires instead of accumulating.
        """
        pipe = self._r.pipeline()
        pipe.xadd(key, {"data": json.dumps(result)}, maxlen=1, approximate=False)
        pipe.expire(key, ttl)
        await pipe.execute()

    async def publish_result(self, session_id: str, result: dict) -> None:
        """Append a JSON-RPC result to the session's durable results stream.

        XADD + EXPIRE in one pipeline so the stream is bounded in size (MAXLEN)
        and lifetime (TTL) regardless of whether a gateway ever drains it.
        """
        key = _results_key(session_id)
        pipe = self._r.pipeline()
        pipe.xadd(key, {"data": json.dumps(result)}, maxlen=_RESULTS_MAXLEN, approximate=True)
        pipe.expire(key, SESSION_TTL)
        await pipe.execute()
