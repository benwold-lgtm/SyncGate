# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""ADR-0017 §8 — revocation interrupts in-flight work, not just future requests.

Deliberately scoped to a single gateway process, not a distributed one. In distributed mode
the gateway API tier itself is horizontally scaled (`docs/kubernetes-architecture.md`) — a
request authenticated by a support grant is handled entirely on the one replica that accepted
the connection, and `revoke_support_grant` (see `api/support_requests.py`) may land on a
*different* replica. Reaching across replicas to cancel a task on another process would need a
durable cross-replica signal (this codebase's own precedent for that,
`shared/session_router.py`'s `SessionRouter`, uses a Redis Stream rather than pub/sub for
exactly this class of problem — a fire-and-forget publish can be missed by a replica that is
mid-restart).

That machinery is not built here, and deliberately so: a call this registry cannot reach still
stops — the request handler is either already past its own bounded wait (the exchange
deadline every Streamable HTTP request carries) or is refused on its *next* call, since `check`/`check_proof` are
already live-checked per request (no separate signed token to still be valid — see
`support_grants.py`). What this registry buys is the *same-replica* case: a revoke lands on the
very process where the call is still running, and that call is cancelled immediately rather
than left to unwind on its own. That is the common case for a single-stack (embedded-mode)
deployment, and a real improvement — never a regression — everywhere else.
"""

from __future__ import annotations

import asyncio
from typing import Any


class InFlightSupportGrantCalls:
    """Tracks, per support-grant id, the asyncio tasks currently executing a request
    authenticated by that grant on THIS process. `cancel_all` only ever reaches what is
    tracked here — see the module docstring for the deliberate cross-replica limitation."""

    def __init__(self) -> None:
        self._by_grant: dict[str, set[asyncio.Task]] = {}

    def register(self, grant_id: str, task: asyncio.Task) -> None:
        self._by_grant.setdefault(grant_id, set()).add(task)

    def unregister(self, grant_id: str, task: asyncio.Task) -> None:
        tasks = self._by_grant.get(grant_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            del self._by_grant[grant_id]

    def cancel_all(self, grant_id: str) -> int:
        """Request cancellation of every task tracked for ``grant_id`` on this process.

        Returns the number signalled. Zero means nothing was running *here* — it does not
        mean nothing was running for this grant anywhere (see module docstring)."""
        tasks = self._by_grant.get(grant_id, set())
        count = 0
        for task in list(tasks):
            if not task.done():
                task.cancel()
                count += 1
        return count


def support_grant_inflight_registry(app_state: Any) -> InFlightSupportGrantCalls:
    """Lazily attach one registry per app — always in-process, never Redis-backed (see
    module docstring for why this tracker is deliberately not distributed)."""
    registry = getattr(app_state, "support_grant_inflight", None)
    if registry is None:
        registry = InFlightSupportGrantCalls()
        app_state.support_grant_inflight = registry
    return registry
