# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Request/response exchange — deliver a JSON-RPC message and return its response *here*.

On Streamable HTTP the response belongs to the POST that carried the request. (HTTP+SSE,
removed in 0.4.0, acknowledged the POST and delivered the response later on a stream the
client held open elsewhere.) Answering *here* is the actual work.

In embedded mode it is trivial: the pod is in this process, so awaiting it is a call.

In distributed mode it is the hard part of Phase 6, and it is why this seam exists at all.
The POST can land on any gateway replica, while the device is owned by one worker and the
result is published to Redis. The replica handling the POST therefore has to *wait* on a
result it does not produce: on its session's stream, correlated by JSON-RPC id, or for a
stateless call on the call's own reply stream (ADR-0027 §1).

Defining the boundary now means the distributed implementation is a new subclass rather than
a rewrite of the endpoint that calls it. The endpoints depend on this interface only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from device_mcp_gateway.shared.keys import KEYS

DEFAULT_EXCHANGE_TIMEOUT = 30.0


class ExchangeTimeout(Exception):
    """No response arrived within the deadline.

    Distinct from an upstream error: the call may still be running, so the caller must not
    report it as a failed tool call. Streamable HTTP maps this to 504.
    """


class ExchangeUnavailable(Exception):
    """The device cannot serve this request at all (no pod, wrong mode, shedding).

    Carries an HTTP status so a transport can answer correctly without re-deriving why.
    """

    def __init__(self, detail: str, status_code: int = 503) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class ResultExchange(ABC):
    """Deliver one JSON-RPC message to a device and return its response.

    Implementations must be safe to call concurrently for the same device: several clients
    may hold Streamable HTTP requests against one hostname at once.
    """

    @abstractmethod
    async def exchange(
        self,
        hostname: str,
        payload: dict[str, Any],
        *,
        session_id: str,
        subject: str,
        rid: str,
        timeout: float = DEFAULT_EXCHANGE_TIMEOUT,
    ) -> dict[str, Any] | None:
        """Return the JSON-RPC response, or None when ``payload`` is a notification.

        Raises ``ExchangeTimeout`` if no response arrives in ``timeout`` seconds, and
        ``ExchangeUnavailable`` when the device cannot serve the request.
        """


class EmbeddedResultExchange(ResultExchange):
    """In-process: the pod is right here, so awaiting it is a direct call.

    No timeout is enforced here on purpose. The pod already owns the bounds that matter —
    the per-device token bucket, the circuit breaker and the upstream HTTP timeout — and
    layering a second deadline on top would report a timeout for a call the pod is about to
    answer, which is exactly the confusion ``ExchangeTimeout`` exists to avoid.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def exchange(
        self,
        hostname: str,
        payload: dict[str, Any],
        *,
        session_id: str,
        subject: str,
        rid: str,
        timeout: float = DEFAULT_EXCHANGE_TIMEOUT,
    ) -> dict[str, Any] | None:
        profile = self._registry.get_profile(hostname)
        if not profile or not profile.pod:
            raise ExchangeUnavailable(f"Device '{hostname}' has no active pod", status_code=404)
        return await profile.pod.call_tool(payload)


class DistributedResultExchange(ResultExchange):
    """Cross-replica: publish to the worker, then wait here for the answer.

    The POST can land on any gateway replica; the device is owned by exactly one worker,
    which publishes its result to ``session:{id}:results`` in Redis. So this replica has to
    wait on a result it does not produce and correlate it by JSON-RPC id.

    Ordering matters and is the whole of the correctness argument: the stream cursor is
    taken **before** publishing, so a worker fast enough to answer between the two cannot
    have its result missed. Waiting first and publishing second would be the obvious
    arrangement and would lose exactly those calls.

    **A stateless call (empty ``session_id``) takes a different route** (ADR-0027 §1). There
    is no session to name a results stream, and the shared ``session::results`` an empty id
    would produce is a cross-caller disclosure. So the call names its own single-request
    stream, ``reply:{request_id}``, as ``reply_to``, and waits on it from the start: nothing
    else writes there, so there is no cursor to take and nothing to correlate. For one
    release the entry also carries ``session_id = req:{request_id}``, which an older worker,
    which ignores ``reply_to``, turns into a results key that is still unique to the call.
    """

    def __init__(self, backend: Any, session_router: Any, gateway_id: str, config: dict[str, Any]) -> None:
        self._backend = backend
        self._sessions = session_router
        self._gateway_id = gateway_id
        self._config = config or {}

    async def exchange(
        self,
        hostname: str,
        payload: dict[str, Any],
        *,
        session_id: str,
        subject: str,
        rid: str,
        timeout: float = DEFAULT_EXCHANGE_TIMEOUT,
    ) -> dict[str, Any] | None:
        import uuid

        from device_mcp_gateway import metrics
        from device_mcp_gateway.observability import tracing

        # Admission control (F-06), same watermark as the SSE path: if the worker is not
        # draining the device's stream, a new call queues behind work that gets trimmed at
        # MAXLEN and surfaces only as a timeout. Fail fast and visibly instead.
        backlog_limit = self._config.get("registry", {}).get("call_backlog_limit", 1000)
        if backlog_limit > 0 and await self._backend.call_backlog(hostname) >= backlog_limit:
            metrics.calls_rejected_overload_total.labels(hostname=hostname).inc()
            raise ExchangeUnavailable(f"Device '{hostname}' is overloaded; retry shortly", status_code=429)

        msg_id = payload.get("id")
        request_id = str(uuid.uuid4())

        stateless = not session_id
        if stateless:
            reply_to = KEYS.reply(request_id)
            # The one-release shim for an older worker (ADR-0027 §1): unique to this call.
            wire_session = f"req:{request_id}"
            cursor = "0-0"
        else:
            reply_to = KEYS.session_results(session_id)
            wire_session = session_id
            # Cursor first — see the class docstring. A notification skips this entirely.
            cursor = "0-0" if msg_id is None else await self._sessions.results_cursor(session_id)

        with tracing.start_span(
            "mcp.tool_dispatch",
            attributes={"mcp.hostname": hostname, "mcp.method": payload.get("method", "?"), "mcp.rid": rid},
        ):
            carrier = tracing.inject_carrier()
            await self._backend.publish_tool_call(
                hostname=hostname,
                request_id=request_id,
                session_id=wire_session,
                gateway_id=self._gateway_id,
                message=payload,
                rid=rid,
                traceparent=carrier.get("traceparent", ""),
                subject=subject,
                reply_to=reply_to,
            )

        if msg_id is None:
            return None  # a notification has no answer to wait for

        if stateless:
            result = await self._sessions.await_reply([reply_to, KEYS.session_results(wire_session)], timeout=timeout)
        else:
            result = await self._sessions.await_result(session_id, msg_id, cursor=cursor, timeout=timeout)
        if result is None:
            metrics.tool_call_timeouts_total.labels(hostname=hostname).inc()
            raise ExchangeTimeout(
                f"No worker answered within {timeout:g}s (request_id={request_id}) — the call may "
                f"still be running; retry only if the operation is idempotent"
            )
        return result
