# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tool-call consumption and dispatch for a DeviceWorker (SRE #1/#4/#5/#6, F-08, F-13).

Extracted from the DeviceWorker god-object. Holds the per-device consume loop,
two-level backpressure, XAUTOCLAIM recovery, the idempotency guard, dead-letter
publishing, and the core dispatch pipeline (decode → guard → pod call → result
marker → publish → audit).

The dispatcher deliberately holds a reference to its worker and reads shared
state (`_pods`, `_assigned`, `_backend`, semaphores, config scalars) at call
time rather than capturing it at construction: the worker's tests — and the
worker itself during startup — mutate those attributes directly (e.g. assigning
``worker._backend`` without calling ``run()``), and a snapshot would silently
diverge. ``DeviceWorker`` keeps thin ``_dispatch_call``-style delegating
wrappers so its private call surface is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from device_mcp_gateway import metrics
from device_mcp_gateway.security import fingerprint as fp
from device_mcp_gateway.audit import audit_log
from device_mcp_gateway.core.backoff import jittered
from device_mcp_gateway.core.correlation import use_request_id
from device_mcp_gateway.core.errors import RPC_DUPLICATE, RPC_INTERNAL_ERROR, RPC_NO_WORKER, rpc_error
from device_mcp_gateway.observability import tracing
from device_mcp_gateway.pods.pod_base import BasePod
from device_mcp_gateway.shared.keys import KEYS

if TYPE_CHECKING:  # pragma: no cover
    from device_mcp_gateway.worker.runner import DeviceWorker


#: How long a stateless call's reply stream outlives its result (ADR-0027 §1): the gateway's
#: exchange deadline plus the result-marker TTL, so a worker answering after the waiter gave up
#: writes into a key that expires instead of accumulating.
_EXCHANGE_DEADLINE_S = 30


@dataclass(frozen=True)
class ReplyRoute:
    """Where one call's result may be published — exactly one stream, or none."""

    #: ``reply`` — a stateless call's own single-request stream; ``session`` — a session's
    #: results stream, keyed by its id.
    kind: str
    key: str
    session_id: str = ""


def resolve_reply_route(fields: dict) -> tuple[ReplyRoute | None, str]:
    """The one stream this entry's answer may go to, or ``(None, reason)``.

    ADR-0027 §1 and its implementation note. ``reply_to`` wins and is used alone. Without it,
    an entry from an older gateway still carries the session it is waiting on. With neither
    there is no route — and the empty session id that would otherwise produce the shared
    ``session::results`` stream is exactly the cross-caller disclosure §1 exists to prevent.

    A ``reply_to`` is honoured only if it names one of those two kinds of stream: a stream
    entry is not the place to let anything choose an arbitrary Redis key to write to.
    """
    reply_to = fields.get("reply_to") or ""
    session_id = fields.get("session_id") or ""
    if reply_to:
        reply_prefix = KEYS.reply("")
        if reply_to.startswith(reply_prefix) and len(reply_to) > len(reply_prefix):
            return ReplyRoute("reply", reply_to), ""
        # KEYS.session_results(x) is "<prefix>session:" + x + ":results"; recover x and
        # require it non-empty, so the shared "session::results" can never be named.
        head, tail = KEYS.session_results("\0").split("\0")
        if reply_to.startswith(head) and reply_to.endswith(tail):
            middle = reply_to[len(head) : len(reply_to) - len(tail)]
            if middle:
                return ReplyRoute("session", reply_to, middle), ""
        return None, "invalid reply route"
    if session_id:
        return ReplyRoute("session", KEYS.session_results(session_id), session_id), ""
    return None, "no reply route"


def _runner_mod():
    """The runner module, resolved lazily.

    ``_DLQ_MAXLEN`` / ``_IDEMPOTENT_METHODS`` stay canonical on ``worker.runner``
    (tests monkeypatch them there by module attribute), so they must be read at
    call time from that module — a value captured at import would not see the
    patch. Lazy import also avoids a runner⇄dispatch import cycle.
    """
    from device_mcp_gateway.worker import runner

    return runner


class CallDispatcher:
    """Consumes a device's call stream and executes tool calls on its pod."""

    def __init__(self, worker: "DeviceWorker") -> None:
        self._w = worker

    async def consume_calls(self, hostname: str) -> None:
        w = self._w
        stream = KEYS.device_calls(hostname)
        group = KEYS.device_calls_group(hostname)
        # Ensure consumer group for this device's call stream
        try:
            await w._r.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning(f"xgroup_create {stream}: {exc}")

        # Per-device concurrency cap (SRE #5). Awaiting this in the consume loop
        # applies backpressure: when slots are exhausted we stop reading new
        # entries, so they remain delivered-unacked (visible as stream lag) rather
        # than piling up as unbounded in-memory tasks.
        sem = asyncio.Semaphore(w._max_calls_per_device)

        while not w._stop_event.is_set() and hostname in w._assigned:
            try:
                # First, reclaim entries a previous owner (typically a dead worker)
                # delivered into the group's PEL but never acked, so in-flight calls
                # at crash time aren't stranded forever (SRE #1). Only entries idle
                # longer than _reclaim_min_idle_ms are taken, so this never steals a
                # call still running on a healthy owner.
                # NOTE: cross-method calls in this class go through the worker's
                # delegating wrappers (w._reclaim_pending, w._schedule_dispatch, …)
                # so an instance-level monkeypatch on the worker still intercepts
                # them — several tests patch e.g. ``worker._dispatch_call`` directly.
                await w._reclaim_pending(hostname, stream, group, sem)

                results = await w._r.xreadgroup(
                    group,
                    w._id,
                    {stream: ">"},
                    count=5,
                    block=2000,
                )
                if not results:
                    continue
                for _s, messages in results:
                    for msg_id, fields in messages:
                        await w._schedule_dispatch(sem, hostname, stream, group, msg_id, fields)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(f"Call consumer error for {hostname}; retrying")
                await asyncio.sleep(jittered(1))  # F-61: de-sync reconnect storms

    async def schedule_dispatch(
        self, sem: asyncio.Semaphore, hostname: str, stream: str, group: str, msg_id: str, fields: dict
    ) -> None:
        """Acquire a per-device AND the worker-wide slot, then dispatch (SRE #5/#6, F-13).

        Two-level backpressure: ``sem`` bounds this one device's in-flight calls; the
        shared ``_worker_call_sem`` bounds the aggregate across every device the worker
        hosts. Both are acquired by the consume loop *before* the dispatch task is
        created, so when either is exhausted the loop stops reading new entries and the
        burst stays as delivered-unacked stream lag instead of unbounded tasks. The
        device slot is taken first so a device blocked on its own cap never holds a
        scarce worker-wide slot. The task is tracked in _inflight_calls so shutdown can
        drain it, and releases both slots when done.
        """
        w = self._w
        await sem.acquire()
        # A blocked worker-wide acquire is the worker-saturation signal (F-13).
        if w._worker_call_sem.locked():
            metrics.worker_calls_throttled_total.inc()
        try:
            await w._worker_call_sem.acquire()
        except BaseException:
            sem.release()  # never strand a device slot if the worker-wide wait is cancelled
            raise
        task = asyncio.create_task(w._dispatch_guarded(sem, hostname, stream, group, msg_id, fields))
        w._inflight_calls.add(task)
        task.add_done_callback(w._inflight_calls.discard)

    async def _dispatch_guarded(
        self, sem: asyncio.Semaphore, hostname: str, stream: str, group: str, msg_id: str, fields: dict
    ) -> None:
        try:
            await self._w._dispatch_call(hostname, stream, group, msg_id, fields)
        finally:
            self._w._worker_call_sem.release()
            sem.release()

    async def reclaim_pending(self, hostname: str, stream: str, group: str, sem: asyncio.Semaphore) -> None:
        """XAUTOCLAIM idle pending entries to this worker and dispatch them.

        Recovers tool calls a now-dead worker had read (moving them into its PEL)
        but never acked before crashing. The new owner — assigned by the
        reconciler (SRE #2) — runs this and picks the stranded calls up. Tolerant
        of XAUTOCLAIM being unavailable or transient errors: a reclaim hiccup must
        never break the consume loop.
        """
        w = self._w
        try:
            claimed = await w._r.xautoclaim(
                stream, group, w._id, min_idle_time=w._reclaim_min_idle_ms, start_id="0-0", count=10
            )
        except Exception as exc:
            logger.debug(f"xautoclaim {stream}: {exc}")
            return
        # redis-py returns (next_cursor, claimed_messages[, deleted_ids]).
        messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) >= 2 else []
        for msg_id, fields in messages:
            mid = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
            logger.info(f"Reclaimed stranded call {mid} for {hostname}")
            await w._schedule_dispatch(sem, hostname, stream, group, mid, fields)

    async def dead_letter(self, hostname: str, fields: dict, reason: str) -> None:
        """Move an undeliverable tool call to the device's dead-letter stream (SRE #4).

        Bounded so it can't grow without limit. Failure to dead-letter is logged
        but never propagated — it must not break dispatch/ack.
        """
        try:
            payload = {k: str(v) for k, v in fields.items()}
            payload["reason"] = reason
            payload["ts"] = str(time.time())
            await self._w._r.xadd(
                KEYS.device_calls_dead(hostname), payload, maxlen=_runner_mod()._DLQ_MAXLEN, approximate=True
            )
            metrics.dead_letter_total.labels(hostname=hostname).inc()
        except Exception:
            logger.exception(f"Failed to dead-letter call for {hostname}")

    # ------------------------------------------------------------------
    # Idempotency guard (F-08)
    # ------------------------------------------------------------------

    async def guard_duplicate(self, hostname: str, request_id: str, pod: BasePod, message: dict) -> str | None:
        """Decide whether a (possibly redelivered) call should be (re-)executed.

        Returns None to proceed, or a reason string to suppress execution:
          - ``already_completed``: the result was already published (the original
            attempt finished but died before acking) — don't re-run or re-publish.
          - ``nonidempotent_guard``: a non-idempotent op had already begun and we
            can't prove it didn't apply — refuse rather than double-execute.

        The single-delivery happy path returns None (the markers don't yet exist),
        so this only ever suppresses a genuine duplicate/reclaim.
        """
        w = self._w
        if await w._already_completed(request_id):
            return "already_completed"
        if w._is_idempotent_call(pod, message):
            return None  # safe/idempotent method — re-running is harmless
        # Non-idempotent: claim the exclusive right to execute this request_id once.
        if await w._begin_exec(request_id):
            return None  # we are the first; proceed
        return "nonidempotent_guard"

    async def already_completed(self, request_id: str) -> bool:
        """True if a result was already recorded for this call (dedup fast path)."""
        return bool(await self._w._r.exists(KEYS.result_marker(request_id)))

    async def begin_exec(self, request_id: str) -> bool:
        """SET-NX a 'started' marker; True only for the first executor of this id.

        A subsequent reclaim of the same entry finds the marker set and refuses,
        so a non-idempotent operation runs at most once across the fleet.
        """
        w = self._w
        return bool(await w._r.set(KEYS.exec_marker(request_id), w._id, nx=True, ex=w._idempotency_ttl))

    def is_idempotent_call(self, pod: BasePod, message: dict) -> bool:
        """True if re-executing this call carries no extra side effect.

        Read-only MCP methods (tools/list, resources/read, ping, …) are inherently
        safe. For tools/call, idempotency follows the backing HTTP method. An
        unknown tool name produces a handler-not-found error with no upstream call,
        so it's safe to re-run too.

        A **proxied** tool is always treated as non-idempotent. It has no backing HTTP
        method, and the gateway has no way to know whether the upstream operation reads or
        writes — MCP's ``idempotentHint`` is advisory metadata from the upstream itself, so
        trusting it would let an untrusted server opt its own writes into re-execution.
        Refusing a redelivery costs one failed call; re-running an upstream write does not.

        This branches on ``source``, deliberately, rather than relying on a proxied tool's
        empty ``method`` falling outside the idempotent set. That happens to be safe today
        and is safe by accident: anyone who "tidies" the empty string to a ``GET`` default
        would silently make every proxied write re-executable, with nothing failing.
        """
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            return True
        params = message.get("params") or {}
        tool_name = params.get("name")
        for tool in pod.manifest.tools:
            if tool.name == tool_name:
                if getattr(tool, "source", "openapi") == "proxy":
                    return False
                return tool.method.upper() in _runner_mod()._IDEMPOTENT_METHODS
        return True

    # ------------------------------------------------------------------
    # Core dispatch pipeline
    # ------------------------------------------------------------------

    async def publish(self, route: ReplyRoute, result: dict) -> None:
        """Publish ``result`` to ``route`` — the one stream this call may answer on."""
        w = self._w
        if route.kind == "reply":
            await w._session_router.publish_reply(route.key, result, ttl=_EXCHANGE_DEADLINE_S + w._result_marker_ttl)
        else:
            await w._session_router.publish_result(route.session_id, result)

    async def dispatch_call(self, hostname: str, stream: str, group: str, msg_id: str, fields: dict) -> None:
        """Bind the gateway's correlation id, then run the pipeline (ADR-0026).

        A wrapper rather than a `with` inside the pipeline so the binding covers *every*
        outbound hop the dispatch causes — the tool call itself, but also a lazily
        fetched spec or a token refresh — and so the id is reset when this entry returns.
        The worker consumes many calls in one task; leaving the previous call's id bound
        would attach it to an unrelated later one.
        """
        with use_request_id(fields.get("rid", "")):
            await self._dispatch_call_bound(hostname, stream, group, msg_id, fields)

    async def _dispatch_call_bound(self, hostname: str, stream: str, group: str, msg_id: str, fields: dict) -> None:
        w = self._w
        session_id = fields.get("session_id", "")
        request_id = fields.get("request_id", "")
        # X-Request-Id from the gateway (SRE O2): bind it in the worker's audit log
        # so one trace id spans the gateway dispatch and the worker execution.
        rid = fields.get("rid", "-")
        # Authenticated principal that issued the call (F-30 residual): the gateway
        # authorized + logged it at the edge and rode the subject along the stream;
        # bind it here so the worker-side execution audit carries the same actor
        # attribution ("who called this tool"), not just the correlation id.
        subject = fields.get("subject") or "-"
        # Initialised before the try so the failure path below can dead-letter/notify
        # even if json.loads itself raises.
        message: Any = {}
        _method = "?"
        route, no_route = resolve_reply_route(fields)
        try:
            message = json.loads(fields.get("message", "{}"))
            _method = message.get("method", "?") if isinstance(message, dict) else "?"

            # ADR-0027 §1: an answer with nowhere to go is a gateway bug. Checked before the
            # call runs, not after — a non-idempotent call must not take effect and then have
            # its result discarded. A notification expects no answer, so it needs no route.
            if route is None and isinstance(message, dict) and message.get("id") is not None:
                logger.error(f"Refusing {_method} for {hostname}: {no_route} (request_id={request_id})")
                await w._dead_letter(hostname, fields, no_route)
                if request_id:
                    await w._r.set(KEYS.result_marker(request_id), "1", ex=w._result_marker_ttl)
                audit_log(
                    "tool dispatch", hostname=hostname, subject=subject, method=_method, status="no_route", rid=rid
                )
                return

            # ADR-0015 §9 backstop. The gateway refuses quarantined calls at admission, but
            # a call already sitting in the stream when the fingerprint change was detected
            # would otherwise still be dispatched — the exact window the gateway check
            # cannot close. Reads the CURRENT config rather than anything carried on the
            # stream entry, so a device quarantined a second ago is honoured here.
            # `_backend` is Optional on the worker (it is set during run()); this path can
            # be reached in tests that drive the dispatcher directly. No backend means no
            # fingerprint state to consult, so the backstop simply does not apply.
            cfg_now = await w._backend.get_device(hostname) if w._backend is not None else None
            if cfg_now is not None:
                fp_reason = fp.quarantine_reason(
                    cfg_now.fingerprint_state,
                    cfg_now.fingerprint_policy,
                    (w._config.get("security", {}) or {}).get("fingerprint_policy"),
                    _method,
                )
                if fp_reason:
                    logger.warning(f"Refusing {_method} for {hostname}: {fp_reason}")
                    await w._dead_letter(hostname, fields, "fingerprint quarantine")
                    msg_id_val = message.get("id") if isinstance(message, dict) else None
                    if route is not None and msg_id_val is not None:
                        await self.publish(
                            route,
                            rpc_error(
                                RPC_NO_WORKER,
                                msg_id_val,
                                rid=rid,
                                request_id=request_id,
                                message=f"{hostname}: {fp_reason}",
                            ),
                        )
                    if request_id:
                        await w._r.set(KEYS.result_marker(request_id), "1", ex=w._result_marker_ttl)
                    audit_log(
                        "tool dispatch",
                        hostname=hostname,
                        subject=subject,
                        method=_method,
                        status="fingerprint_quarantine",
                        rid=rid,
                    )
                    return

            pod = w._pods.get(hostname)
            if pod is None:
                # No pod to serve this call (e.g. a pod-replace window). Dead-letter
                # it instead of dropping silently, and tell the client rather than
                # letting it hang to the F6 timeout (SRE #4).
                logger.warning(f"No pod for {hostname}; dead-lettering call {msg_id}")
                await w._dead_letter(hostname, fields, "no active pod")
                msg_id_val = message.get("id") if isinstance(message, dict) else None
                if route is not None and msg_id_val is not None:
                    await self.publish(
                        route,
                        rpc_error(
                            RPC_NO_WORKER,
                            msg_id_val,
                            rid=rid,
                            request_id=request_id,
                            message=f"No active pod for {hostname}; call not served",
                        ),
                    )
                if request_id:
                    await w._r.set(KEYS.result_marker(request_id), "1", ex=w._result_marker_ttl)
                audit_log(
                    "tool dispatch",
                    hostname=hostname,
                    subject=subject,
                    method=_method,
                    status="dead_letter",
                    rid=rid,
                )
                return
            # Idempotency guard (F-08): a reclaimed/redelivered call may already
            # have executed. Decide whether to (re-)run before touching the upstream.
            if w._idempotency_guard and request_id:
                decision = await w._guard_duplicate(hostname, request_id, pod, message)
                if decision is not None:
                    if decision == "nonidempotent_guard":
                        # Refusing a possibly-applied non-idempotent op — tell the
                        # client definitively instead of letting it hang to timeout.
                        msg_id_val = message.get("id") if isinstance(message, dict) else None
                        if route is not None and msg_id_val is not None:
                            await self.publish(
                                route,
                                rpc_error(
                                    RPC_DUPLICATE,
                                    msg_id_val,
                                    rid=rid,
                                    request_id=request_id,
                                    message=(
                                        f"Duplicate delivery of a non-idempotent call to {hostname}; "
                                        "not re-executed to avoid a double side effect"
                                    ),
                                ),
                            )
                        await w._r.set(KEYS.result_marker(request_id), "1", ex=w._result_marker_ttl)
                    metrics.duplicate_calls_suppressed_total.labels(hostname=hostname, reason=decision).inc()
                    audit_log(
                        "tool dispatch",
                        hostname=hostname,
                        subject=subject,
                        method=_method,
                        status=f"duplicate_{decision}",
                        rid=rid,
                    )
                    return
            _t = time.perf_counter()
            # Execution span parented from the gateway's dispatch span (F-14): the
            # traceparent rode along on the stream entry, so this joins the same
            # end-to-end trace. No-op when tracing is off.
            with tracing.start_span_from_carrier(
                "mcp.tool_call",
                {"traceparent": fields.get("traceparent", "")},
                attributes={"mcp.hostname": hostname, "mcp.method": _method, "mcp.rid": rid},
            ) as _span:
                result = await pod.call_tool(message)
                _dur = time.perf_counter() - _t
                # Distributed-mode tool calls execute here on the worker; record where
                # the work actually happens (the gateway only enqueues).
                if result is None:
                    _status = "noresult"  # notification — no JSON-RPC response expected
                elif isinstance(result, dict) and "error" in result:
                    _status = "error"
                else:
                    _status = "ok"
                if _span is not None:
                    _span.set_attribute("mcp.status", _status)
            metrics.tool_calls_total.labels(hostname=hostname, method=_method, status=_status).inc()
            metrics.tool_call_duration_seconds.labels(hostname=hostname).observe(_dur)
            if result is not None and route is not None:
                await self.publish(route, result)
            # Mark the call as handled so the gateway's timeout watcher (F6)
            # stands down even when the result reached a different gateway replica.
            if request_id:
                await w._r.set(KEYS.result_marker(request_id), "1", ex=w._result_marker_ttl)
            # Distributed-mode audit log with execution latency (SRE O2/O3): the
            # gateway only logs "dispatched", so per-call latency lives here.
            audit_log(
                "tool dispatch",
                hostname=hostname,
                subject=subject,
                method=_method,
                status=_status,
                duration_ms=round(_dur * 1000, 1),
                rid=rid,
            )
        except Exception as exc:
            logger.exception(f"Tool call dispatch error for {hostname} session {session_id} rid={rid}")
            # The call was delivered but raised (e.g. an upstream failure that survived
            # the retry policy). Don't let the ack below drop it silently: dead-letter it
            # for inspect/replay, and return a definitive error to the client instead of
            # letting it hang to the F6 timeout (#10). Replay stays safe for a partially-
            # applied non-idempotent op — the F-08 guard refuses a duplicate on re-run.
            try:
                await w._dead_letter(hostname, fields, f"dispatch error: {exc}")
                metrics.tool_calls_total.labels(hostname=hostname, method=_method, status="dead_letter").inc()
                msg_id_val = message.get("id") if isinstance(message, dict) else None
                if route is not None and msg_id_val is not None:
                    await self.publish(
                        route,
                        rpc_error(
                            RPC_INTERNAL_ERROR,
                            msg_id_val,
                            rid=rid,
                            request_id=request_id,
                            message=f"Tool call to {hostname} failed and was dead-lettered",
                        ),
                    )
                if request_id:
                    await w._r.set(KEYS.result_marker(request_id), "1", ex=w._result_marker_ttl)
                audit_log(
                    "tool dispatch",
                    hostname=hostname,
                    subject=subject,
                    method=_method,
                    status="dead_letter",
                    rid=rid,
                )
            except Exception:
                logger.exception(f"Failed to dead-letter failed call {msg_id} for {hostname}")
        finally:
            await w._r.xack(stream, group, msg_id)
