# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""
Device Worker — distributed mode pod host.

Each worker process:
  1. Joins the Redis Streams consumer group on device:assignments
  2. Spawns/kills DevicePod instances for assigned devices
  3. Runs a per-device tool-call consumer loop (device:{hostname}:calls stream)
  4. Runs a health loop (WorkerHealthLoop) for assigned devices
  5. Publishes tool-call results to session:{session_id}:results pub/sub

A worker registers itself in Redis with a heartbeat key (TTL = 2 × health_interval)
and refreshes a per-device claim lease while it owns a pod. When a worker dies, its
claims lapse; a leader-elected reconciler (one worker at a time) detects devices with
no live claim, clears the stale ownership the dead worker left, and republishes their
assignments so a live worker takes over (SRE #1/#2). In-flight tool calls the dead
worker had read but not acked are recovered by the new owner via XAUTOCLAIM on the
device call stream. Recovery does not depend on the dead worker restarting with the
same WORKER_ID.

Collaborators (extracted from what used to be one 1,300-line class):
  - worker.dispatch.CallDispatcher — call consumption, backpressure, idempotency
    guard, dead-letter, and the dispatch pipeline
  - worker.reconcile.Reconciler   — leader-elected orphan healing
  - worker.rebalance.Rebalancer   — scale-out shedding / assignment declining
  - worker.health.WorkerHealthLoop — reachability + spec polling (pre-existing)
DeviceWorker keeps thin delegating wrappers for the private call surface.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
from functools import partial
from typing import Any

from loguru import logger

from device_mcp_gateway import metrics
from device_mcp_gateway.auth.base import AbstractAuth, CredentialNotBound, CredentialsChangedHook
from device_mcp_gateway.core.backoff import RetryPolicy, jittered
from device_mcp_gateway.credentials import ResolverError, build_resolver
from device_mcp_gateway.core.spec_limits import (
    DEFAULT_MAX_SPEC_BYTES,
    DEFAULT_TRANSLATE_TIMEOUT,
    SpecTooLargeError,
    fetched_spec_or_none,
    run_translation,
)
from device_mcp_gateway.observability import tracing
from device_mcp_gateway.pods.device_pod import DevicePod
from device_mcp_gateway.pods.mcp_proxy_pod import McpProxyPod
from device_mcp_gateway.pods.pod_base import BasePod
from device_mcp_gateway.upstream.mcp_client import StreamableHttpClient
from device_mcp_gateway.upstream.mcp_discovery import build_proxy_manifest_from, snapshot_upstream
from device_mcp_gateway.security.mtls import TlsProfiles
from device_mcp_gateway.shared.keys import KEYS
from device_mcp_gateway.shared.spec_source import CuratedSpecInvalid, resolve_spec_source
from device_mcp_gateway.security.url_policy import (
    build_guarded_client,
    resolve_allow_private,
    resolve_allowed_ports,
)
from device_mcp_gateway.shared.crypto import CredentialCodec
from device_mcp_gateway.shared.registry_backend import AbstractRegistryBackend
from device_mcp_gateway.shared.session_router import SessionRouter
from device_mcp_gateway.worker.dispatch import CallDispatcher
from device_mcp_gateway.worker.health import WorkerHealthLoop, _manifest_to_dict, spec_fingerprint
from device_mcp_gateway.worker.rebalance import Rebalancer
from device_mcp_gateway.worker.reconcile import Reconciler
from device_mcp_gateway.worker.spec_pool import _spec_executor, _translate_spec_sync  # noqa: F401  (re-exported)

# Broadcast unassign stream — every worker tails it independently (XREAD from "$"),
# so the worker that actually owns the pod tears it down; non-owners no-op. Kept in
# sync with registry_backend.KEYS.unassign_stream.
_HEARTBEAT_INTERVAL = 10  # seconds
# Bound for a device's dead-letter stream (undeliverable tool calls, SRE #4).
# Canonical here (tests monkeypatch it as runner._DLQ_MAXLEN); dispatch.py reads
# it lazily at call time.
_DLQ_MAXLEN = 1_000
# HTTP methods whose re-execution carries no extra side effect (RFC 7231 safe +
# idempotent). A redelivered call on one of these is safe to run again; anything
# else (POST/PATCH) is guarded against double-execution (F-08).
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"})


def _auth_from_config(auth_type: str | None, auth_config_str: str | None) -> AbstractAuth | None:
    if not auth_type or not auth_config_str:
        return None
    try:
        cfg = json.loads(auth_config_str)
    except (json.JSONDecodeError, TypeError):
        return None
    from device_mcp_gateway.auth.api_key import ApiKeyAuth
    from device_mcp_gateway.auth.oauth2 import OAuth2Auth

    if auth_type == "api_key":
        return ApiKeyAuth.from_dict(cfg)
    if auth_type == "oauth2":
        return OAuth2Auth.from_dict(cfg)
    return None


class DeviceWorker:
    """Runs DevicePod instances for assigned devices and routes tool calls."""

    def __init__(
        self,
        worker_id: str,
        config: dict[str, Any],
        redis_client: Any,
        codec: CredentialCodec | None = None,
    ) -> None:
        self._id = worker_id
        self._config = config
        # ADR-0018 §2 — see the note in pod_supervisor: built from the config this owner
        # already holds, not injected, so embedded and distributed cannot disagree about
        # whether credential resolution is available.
        self._credential_resolver = build_resolver(config)
        self._r = redis_client
        self._codec = codec or CredentialCodec(None)
        self._backend: AbstractRegistryBackend | None = None
        # Route tool-call results through the durable per-session results stream
        # (SRE #3) instead of fire-and-forget pub/sub, so a result isn't lost when
        # the subscribing gateway replica is briefly not reading.
        self._session_router = SessionRouter(redis_client)

        self._pods: dict[str, BasePod] = {}
        self._assigned: set[str] = set()
        self._call_tasks: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()

        self._keep_alive = config.get("transport", {}).get("sse", {}).get("keep_alive_interval", 30)
        # Outbound mutual-TLS for device calls (F-31), resolved PER DEVICE (TG-4
        # residual): this worker's pods (tool calls), its cold-path spec fetches, and
        # the health loop's reachability/spec GETs all resolve the same profile for a
        # given device, so an mTLS-protected device is reachable on every path — and a
        # device with its own CA does not lend that trust to the others this worker owns.
        # Falls back to httpx default certifi verification when no security.mtls block
        # is configured. Preflight builds every declared profile now, so a bad cert path
        # kills the worker at startup rather than one device at first contact.
        self._tls = TlsProfiles.from_config(config)
        _overridden = self._tls.preflight()
        if _overridden:
            logger.info(f"Per-device TLS profiles configured for {len(_overridden)} device(s)")
        # Device-claim lease TTL (RC-6). Outlives the heartbeat interval so a
        # claim refreshed each heartbeat never lapses while the pod runs, but
        # expires soon after a worker dies so another worker can take over.
        _hc = config.get("registry", {}).get("health_check_interval", 30)
        self._claim_ttl = max(_hc * 2, 60)
        # TTL for the per-call "result seen" marker the gateway's timeout watcher
        # checks (F6). Outlives the tool-call timeout so the watcher always sees it.
        self._result_marker_ttl = max(config.get("registry", {}).get("tool_call_timeout", 30) * 2, 60)
        # How often the (leader-elected) reconciler sweeps for orphaned devices (SRE #1/#2).
        self._reconcile_interval = config.get("registry", {}).get("reconcile_interval", 30)
        # Lease-flap hysteresis (F-62). A claim:{h} lease lapse is treated as the
        # owner's death — but a GC pause / Redis stall / network blip longer than the
        # claim TTL can lapse a *healthy* worker's claim, getting it declared dead and
        # its devices reassigned (transient double-pod churn). Require the device to be
        # seen orphaned across this many CONSECUTIVE leader sweeps before reassigning,
        # so a single transient lapse self-heals (the owner refreshes the claim on its
        # next heartbeat) without triggering a reassignment. ~grace × reconcile_interval
        # of additional margin on top of the claim TTL. 0/1 disables the hysteresis.
        self._orphan_grace_cycles = max(int(config.get("registry", {}).get("reconcile_orphan_grace_cycles", 2)), 1)
        # Per-device count of consecutive leader sweeps observed with no live claim.
        # Leader-local: a new leader starts fresh (counts reset), which only adds
        # safety — a just-elected leader won't reassign on its very first sweep.
        self._orphan_miss_counts: dict[str, int] = {}
        # Periodic load rebalancing on scale-out (F-07): each worker sheds its excess
        # devices over the per-worker target so new/idle workers actually pick up load.
        self._rebalance_enabled = bool(config.get("registry", {}).get("rebalance_enabled", True))
        # Only reclaim call-stream entries idle longer than this, so XAUTOCLAIM
        # never steals a call still in-flight on a healthy owner. Comfortably
        # above the tool-call timeout.
        self._reclaim_min_idle_ms = max(config.get("registry", {}).get("tool_call_timeout", 30), 30) * 1000
        # Idempotency guard (F-08): at-least-once delivery means a reclaimed call
        # (XAUTOCLAIM from a dead/shed worker's PEL) can re-run an operation that
        # already executed. Guard non-idempotent calls (POST/PATCH) so they run at
        # most once across the fleet; idempotent calls are still re-run freely.
        self._idempotency_guard = bool(config.get("registry", {}).get("idempotency_guard", True))
        # The dedup/started markers must outlive the reclaim window so a reclaim
        # still sees them. request_ids are unique per call, so a long TTL is safe.
        self._idempotency_ttl = max(self._reclaim_min_idle_ms // 1000 * 3, 120)
        # Cap concurrent in-flight tool calls per device (SRE #5). The consume loop
        # blocks on this when saturated rather than spawning unbounded tasks/outbound
        # requests, so a burst becomes visible stream lag instead of worker OOM.
        self._max_calls_per_device = config.get("registry", {}).get("max_concurrent_calls_per_device", 20)
        # Aggregate in-flight cap across ALL co-located devices on this worker (F-13).
        # The per-device cap above bounds one device; without a worker-wide ceiling a
        # worker hosting N devices would admit up to N × _max_calls_per_device calls at
        # once on a single shared event loop and HTTP pool. This semaphore is the global
        # admission gate, acquired in addition to (after) the per-device slot, so a burst
        # spread across many devices still becomes stream lag rather than unbounded
        # concurrency. Shared by every per-device consume loop; default 200.
        self._max_calls_per_worker = config.get("registry", {}).get("max_concurrent_calls_per_worker", 200)
        self._worker_call_sem = asyncio.Semaphore(self._max_calls_per_worker)
        # Spec-ingestion bounds (F-09): reject oversized specs before parse/pool and
        # cap how long one translation may hold a worker's translation-pool slot.
        self._spec_max_bytes = config.get("registry", {}).get("spec_max_bytes", DEFAULT_MAX_SPEC_BYTES)
        self._spec_translate_timeout = config.get("registry", {}).get(
            "spec_translate_timeout", DEFAULT_TRANSLATE_TIMEOUT
        )
        # Seconds to let in-flight tool calls finish on shutdown before cancelling,
        # so a rolling update doesn't error every active call (SRE #6).
        self._drain_timeout = config.get("registry", {}).get("shutdown_drain_timeout", 25)
        # In-flight dispatch tasks, tracked so shutdown can drain them (SRE #6).
        self._inflight_calls: set[asyncio.Task] = set()
        # Liveness (SRE #8): the heartbeat is withheld when a critical loop has
        # crashed or the assignment consumer has stalled, so K8s liveness fails and
        # the reconciler reassigns this worker's devices instead of it looking alive
        # while doing nothing.
        self._assignment_progress = time.monotonic()
        self._critical_tasks: list[asyncio.Task] = []
        self._liveness_staleness = max(config.get("registry", {}).get("health_check_interval", 30) * 2, 60)
        # Local liveness file for a CHEAP K8s exec probe (F-17). The old probe spawned
        # a Python interpreter + opened a Redis connection every period, per worker —
        # heavyweight and itself a failure source under Redis stress. Instead the
        # healthy heartbeat touches this file's mtime; the probe just checks the file
        # is fresh (a shell `find -mmin`), no interpreter/Redis. When loops are
        # unhealthy the heartbeat is withheld, the file goes stale, and the probe
        # fails — same semantics as before, a fraction of the cost. Default lands in
        # the system temp dir (avoids a hardcoded /tmp); override to match the probe.
        self._liveness_file = config.get("registry", {}).get("liveness_file") or os.path.join(
            tempfile.gettempdir(), "mcp-worker-alive"
        )
        # Collaborators (see module docstring). They read worker state through this
        # reference at call time, so tests that poke attributes directly (e.g.
        # ``worker._backend = ...``) stay correct without re-wiring.
        self._dispatcher = CallDispatcher(self)
        self._reconciler = Reconciler(self)
        self._rebalancer = Rebalancer(self)
        # _health is initialised in run() after the backend is available
        self._health: WorkerHealthLoop | None = None
        # Why a spec fetch last failed, handed from _fetch_spec back to the spawn path so
        # the recorded spawn_error names the cause rather than only "No spec available".
        # Declared here rather than at its reset in _spawn_pod: an attribute whose only
        # assignment is ``= None`` reads to mypy as type None, so the one assignment that
        # carries an actual reason is the one it rejects.
        self._spec_failure_reason: str | None = None

    async def run(self, backend: AbstractRegistryBackend) -> None:
        """Main entry point. Runs until SIGTERM/SIGINT or stop() is called."""
        self._backend = backend
        # Optional OTel tracing (no-op unless enabled + [otel] installed). F-14.
        tracing.init_tracing(self._config, "mcp-worker")
        _reg_cfg = self._config.get("registry", {})
        # Bounded jittered retries for idempotent outbound GETs/tool calls (F-05/F-44).
        self._retry_policy = RetryPolicy.from_config(self._config)
        self._health = WorkerHealthLoop(
            worker_id=self._id,
            backend=backend,
            redis_client=self._r,
            interval=_reg_cfg.get("health_check_interval", 30),
            spec_poll_interval=_reg_cfg.get("spec_poll_interval", 300),
            spec_cache_ttl=_reg_cfg.get("spec_cache_ttl", 3600),
            discovery_cfg=self._config.get("discovery", {}),
            lock_ttl=_reg_cfg.get("health_lock_ttl"),
            retry_policy=self._retry_policy,
            spec_max_bytes=self._spec_max_bytes,
            spec_translate_timeout=self._spec_translate_timeout,
            tls_profiles=self._tls,
            allow_private=resolve_allow_private(self._config),
            allowed_ports=resolve_allowed_ports(self._config),
            # Only the worker holds the credential codec, so it supplies the auth handler
            # the health loop needs to reach an authenticated MCP upstream.
            auth_provider=self._auth_for,
        )
        self._health.on_spec_changed = self._replace_pod
        await backend.initialize()

        # Register worker
        await self._r.sadd(KEYS.workers_active, self._id)
        logger.info(f"Worker {self._id} started")

        # Recover any devices previously assigned to this worker
        await self._recover_assigned()

        loop = asyncio.get_event_loop()
        # Handle graceful shutdown
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
            except (NotImplementedError, RuntimeError):
                pass  # Windows / some test runners

        assert self._health is not None  # set just above in run()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        assignments_task = asyncio.create_task(self._consume_assignments(), name="assignments")
        unassign_task = asyncio.create_task(self._consume_unassignments(), name="unassignments")
        health_task = asyncio.create_task(self._health.run_forever(self._assigned), name="health")
        metrics_task = asyncio.create_task(self._metrics_loop(), name="metrics")
        reconcile_task = asyncio.create_task(self._reconcile_loop(), name="reconcile")
        rebalance_task = asyncio.create_task(self._rebalance_loop(), name="rebalance")
        tasks = [
            heartbeat_task,
            assignments_task,
            unassign_task,
            health_task,
            metrics_task,
            reconcile_task,
            rebalance_task,
        ]
        # Loops whose unexpected exit means this worker can no longer do its job;
        # the heartbeat is withheld if any has crashed (SRE #8). The unassign tail is
        # critical too: without it a teardown/PUT-replace would never reach the owner.
        self._critical_tasks = [assignments_task, unassign_task, health_task, reconcile_task]
        try:
            await self._stop_event.wait()
        finally:
            # Stop accepting new work first (background loops + per-device consumers),
            # then let in-flight tool calls finish before tearing down pods (SRE #6),
            # so a rolling update doesn't error every active call.
            for t in tasks:
                t.cancel()
            for t in list(self._call_tasks.values()):
                t.cancel()
            await asyncio.gather(*tasks, *self._call_tasks.values(), return_exceptions=True)
            await self._drain_inflight_calls()
            await self._leave_and_hand_off()
            await self._health.close()
            logger.info(f"Worker {self._id} shut down")

    async def _drain_inflight_calls(self) -> None:
        """Wait for in-flight tool calls to finish, up to _drain_timeout (SRE #6).

        Called after the consume loops are cancelled, so no new calls start. Calls
        still running past the timeout are cancelled so shutdown can't hang.
        """
        pending = [t for t in self._inflight_calls if not t.done()]
        if not pending:
            return
        logger.info(f"Draining {len(pending)} in-flight tool call(s) (timeout {self._drain_timeout}s)")
        _done, still = await asyncio.wait(pending, timeout=self._drain_timeout)
        if still:
            logger.warning(f"{len(still)} tool call(s) did not finish in {self._drain_timeout}s; cancelling")
            for t in still:
                t.cancel()
            await asyncio.gather(*still, return_exceptions=True)

    async def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        ttl = self._config.get("registry", {}).get("health_check_interval", 30) * 2
        key = KEYS.worker_heartbeat(self._id)
        while not self._stop_event.is_set():
            if self._loops_healthy():
                # Re-assert set membership each beat so a worker pruned during its
                # startup race (added before its first heartbeat) re-registers.
                await self._r.sadd(KEYS.workers_active, self._id)
                await self._r.set(key, str(time.time()), ex=ttl)
                await self._refresh_claims()  # keep device-claim leases alive (RC-6)
                self._touch_liveness_file()  # cheap local liveness signal for K8s (F-17)
            else:
                # Withhold the heartbeat AND the claim refresh so K8s liveness fails
                # (pod restarts) and, meanwhile, the leases lapse so the reconciler
                # reassigns this worker's devices (SRE #8).
                logger.error("Worker loops unhealthy — withholding heartbeat and claim refresh (SRE #8)")
            await asyncio.sleep(jittered(_HEARTBEAT_INTERVAL))  # F-61: de-sync fleet heartbeats

    def _loops_healthy(self) -> bool:
        """True unless a critical loop crashed or the assignment consumer stalled."""
        if self._stop_event.is_set():
            return True  # shutting down — not a failure
        for t in self._critical_tasks:
            if t.done():
                logger.error(f"Critical worker loop '{t.get_name()}' exited unexpectedly")
                return False
        if time.monotonic() - self._assignment_progress > self._liveness_staleness:
            logger.error("Assignment consumer has not progressed; worker appears stalled")
            return False
        return True

    def _touch_liveness_file(self) -> None:
        """Bump the local liveness file's mtime so a cheap exec probe sees freshness.

        Only called on a healthy heartbeat, so withholding the heartbeat (unhealthy
        loops) also lets the file go stale → the probe fails → K8s restarts the pod
        (F-17). A filesystem hiccup here must never crash the heartbeat loop, so any
        error is logged and swallowed — the Redis heartbeat key remains the
        authoritative liveness signal for the reconciler regardless.
        """
        try:
            with open(self._liveness_file, "w") as fh:
                fh.write(str(time.time()))
        except OSError as exc:
            logger.warning(f"Could not update liveness file {self._liveness_file}: {exc}")

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def _metrics_loop(self) -> None:
        """Refresh worker gauges on a timer (pod count + Redis Stream lag).

        Prometheus exposition itself is served by a background HTTP server started
        in worker_main; this loop only keeps the gauge values current.
        """
        interval = self._config.get("metrics", {}).get("gauge_refresh_interval", 15)
        while not self._stop_event.is_set():
            try:
                await self._refresh_worker_metrics()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("worker metrics refresh failed")
            await asyncio.sleep(jittered(interval))  # F-61: de-sync metrics refresh

    async def _refresh_worker_metrics(self) -> None:
        metrics.worker_pods.set(len(self._pods))
        pending = 0
        undelivered = 0
        for hostname in list(self._assigned):
            stream, group = KEYS.device_calls(hostname), KEYS.device_calls_group(hostname)
            pending += await self._stream_pending(stream, group)
            undelivered += await self._stream_lag(stream, group)
        metrics.worker_pending_calls.set(pending)
        # Never-read backlog held off by the per-device concurrency cap (SRE #5):
        # without this, a saturated worker shows low pending while work piles up
        # undelivered in the stream, hiding the backlog from the HPA.
        metrics.worker_undelivered_calls.set(undelivered)
        metrics.worker_assignments_lag.set(await self._stream_pending(KEYS.assignments_stream, KEYS.worker_group))

    async def _stream_pending(self, stream: str, group: str) -> int:
        """Count delivered-but-unacked entries for a consumer group (XPENDING summary).

        Returns 0 on any error (missing stream/group, server differences) so a
        metrics hiccup never disrupts the worker loop.
        """
        try:
            info = await self._r.xpending(stream, group)
        except Exception:
            return 0
        if isinstance(info, dict):
            return int(info.get("pending", 0) or 0)
        # Some clients return a [count, min, max, consumers] summary list.
        try:
            return int(info[0]) if info else 0
        except (TypeError, IndexError, ValueError):
            return 0

    async def _stream_lag(self, stream: str, group: str) -> int:
        """Count entries added to the stream but not yet delivered to ``group``
        (XINFO GROUPS ``lag``). Returns 0 on any error or when Redis can't compute
        the lag, so a metrics hiccup never disrupts the worker loop (SRE #5)."""
        try:
            groups = await self._r.xinfo_groups(stream)
        except Exception:
            return 0
        for g in groups:
            if not isinstance(g, dict):
                continue
            name = g.get("name")
            if isinstance(name, bytes):
                name = name.decode()
            if name == group:
                lag = g.get("lag")
                try:
                    return int(lag) if lag is not None else 0
                except (TypeError, ValueError):
                    return 0
        return 0

    # ------------------------------------------------------------------
    # Assignment consumer
    # ------------------------------------------------------------------

    async def _consume_assignments(self) -> None:
        while not self._stop_event.is_set():
            # Mark loop progress for the liveness check (SRE #8). This loop blocks
            # ≤2s per iteration, so a stale timestamp means it's wedged.
            self._assignment_progress = time.monotonic()
            try:
                results = await self._r.xreadgroup(
                    KEYS.worker_group,
                    self._id,
                    {KEYS.assignments_stream: ">"},
                    count=10,
                    block=2000,
                )
                if not results:
                    continue
                for _stream, messages in results:
                    for msg_id, fields in messages:
                        action = fields.get("action", "")
                        hostname = fields.get("hostname", "")
                        try:
                            if action == "assign":
                                await self._spawn_pod(hostname)
                            elif action == "unassign":
                                await self._kill_pod(hostname)
                            await self._r.xack(KEYS.assignments_stream, KEYS.worker_group, msg_id)
                        except Exception:
                            logger.exception(f"Failed to process assignment {action} {hostname}")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Assignment consumer error; retrying in ~2 s")
                await asyncio.sleep(jittered(2))  # F-61: de-sync reconnect storms

    async def _consume_unassignments(self) -> None:
        """Tail the broadcast unassign stream and tear down any pod we own.

        Unlike assignments (shared competing-consumers group: one worker acts), an
        unassign must reach the *owner* of the pod. We read with a plain XREAD from
        "$" — no consumer group — so every live worker sees every unassign and the
        owner's idempotent _kill_pod tears its pod down (non-owners return early).
        Starting at "$" is correct: a freshly-started worker owns nothing, and it
        recovers its own prior devices from worker:{id}:devices, not from this stream.
        """
        last_id = "$"
        while not self._stop_event.is_set():
            self._assignment_progress = time.monotonic()
            try:
                results = await self._r.xread({KEYS.unassign_stream: last_id}, count=10, block=2000)
                if not results:
                    continue
                for _stream, messages in results:
                    for msg_id, fields in messages:
                        last_id = msg_id
                        hostname = fields.get("hostname", "")
                        try:
                            await self._kill_pod(hostname)  # idempotent: no-op unless we own it
                        except Exception:
                            logger.exception(f"Failed to process unassign for {hostname}")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unassignment consumer error; retrying in ~2 s")
                await asyncio.sleep(jittered(2))  # F-61: de-sync reconnect storms

    # ------------------------------------------------------------------
    # Pod lifecycle
    # ------------------------------------------------------------------

    async def _acquire_claim(self, hostname: str) -> bool:
        """Atomically claim a device before spawning so two workers can't both
        run a pod for it (RC-6). The local ``_assigned`` set isn't enough — it's
        per-worker — so a pending-assignment reclaim could otherwise hand the
        same device to two workers. Returns True if this worker holds the claim.
        """
        key = KEYS.claim(hostname)
        if await self._r.set(key, self._id, nx=True, ex=self._claim_ttl):
            return True
        # Key already exists — only proceed if we already own it, which happens
        # on pod replacement and on restart recovery (worker_id is stable).
        return (await self._r.get(key)) == self._id

    async def _release_claim(self, hostname: str) -> None:
        """Release this worker's claim on a device.

        Owner-checked get-then-delete (this fakeredis build has no EVAL, so no
        Lua CAS). It is only ever called for a device we actively own and keep
        refreshed via the heartbeat, so the non-atomic window — our claim
        expiring and being re-taken between the get and the delete — is not
        reachable in practice.
        """
        key = KEYS.claim(hostname)
        if (await self._r.get(key)) == self._id:
            await self._r.delete(key)

    async def _refresh_claims(self) -> None:
        """Extend the lease on every owned device claim (called per heartbeat).

        EXPIRE on a key that no longer exists is a no-op returning 0 — it cannot resurrect
        a lapsed claim. So a claim that has already expired (this worker paused past the
        TTL by a long GC pause, a stalled node, or a Redis failover that dropped the key)
        would never come back, while this worker went on serving the device: pods running
        and upstreams polled, but ``claim:{h}`` absent, so every gateway reports the device
        inactive and the reconciler sees it orphaned forever. The F-62 lease-flap hysteresis
        is written expecting exactly this to self-heal "on the owner's next heartbeat", so
        renewing nothing quietly defeated that design too.

        Re-acquiring can legitimately fail — another worker may have taken the device while
        the claim was unheld. Single-owner (ADR-0003) then outranks continuity, so stop
        serving it here rather than run a second pod against the same device.

        Ownership is checked rather than mere existence, because EXPIRE succeeds on *any*
        live key: renewing unconditionally meant a worker whose device had been reassigned
        kept extending the new owner's lease while still serving the device itself.
        """
        for hostname in list(self._assigned):
            owner = await self._r.get(KEYS.claim(hostname))
            if owner == self._id:
                await self._r.expire(KEYS.claim(hostname), self._claim_ttl)
                continue
            if owner is None and await self._acquire_claim(hostname):
                logger.warning(
                    f"Claim lease on {hostname} had lapsed; re-acquired by worker {self._id}. "
                    f"Check for a pause or Redis stall longer than the claim TTL ({self._claim_ttl}s)."
                )
                continue
            logger.warning(
                f"Claim on {hostname} is held by {owner or 'another worker'}; worker {self._id} "
                "is releasing the device to preserve single ownership."
            )
            await self._kill_pod(hostname)

    def _decrypt_auth(self, hostname: str, auth_config_str: str | None) -> str | None:
        """Decrypt a stored credential blob from Redis (distributed mode).

        On failure (key mismatch / rotation) the pod loads without credentials
        and the error is logged loudly, rather than silently treating ciphertext
        as plaintext.
        """
        if not auth_config_str or not self._codec.enabled:
            return auth_config_str
        try:
            return self._codec.decrypt(auth_config_str)
        except Exception:
            logger.error(
                f"Failed to decrypt credentials for {hostname} — key may have rotated; "
                "pod will load without credentials"
            )
            return None

    def _credential_writeback(self, hostname: str) -> CredentialsChangedHook:
        """Persist credentials an auth handler rotated at runtime back into Redis.

        Re-encrypts under the codec exactly as the registration path does, then writes
        only the ``auth_config`` field so a concurrent update to other fields is not
        clobbered. Safe against split-brain because a device has exactly one owning
        worker (the claim lease), so only that worker ever refreshes its credentials.
        """

        async def _write_back(auth: AbstractAuth) -> None:
            blob = json.dumps(auth.to_dict())
            if self._codec.enabled:
                blob = self._codec.encrypt(blob)
            if self._backend is None:
                logger.warning(f"Credentials rotated for {hostname} but no registry backend to persist them")
                return
            await self._backend.update_device_fields(hostname, auth_config=blob)
            logger.info(f"Persisted rotated credentials for {hostname}")

        return _write_back

    async def _spawn_pod(self, hostname: str) -> None:
        if hostname in self._assigned:
            logger.debug(f"Already assigned: {hostname}")
            return
        if await self._decline_assignment(hostname):
            return
        if not await self._acquire_claim(hostname):
            logger.info(f"Device {hostname} is claimed by another worker; skipping spawn")
            return
        assert self._backend is not None, "backend not initialised — call run() first"
        cfg = await self._backend.get_device(hostname)
        if cfg is None:
            logger.warning(f"No config for device {hostname}, cannot spawn pod")
            await self._release_claim(hostname)
            return

        is_proxy = cfg.upstream_kind == "mcp"
        # Fetch or build manifest
        manifest_dict = await self._backend.get_manifest(hostname)
        # The governance baseline (F-41), written below only when we actually fetched a
        # spec. On the cache-hit path we never saw one, so we have nothing honest to
        # record and the health loop seeds it on its first poll instead.
        spec_hash: str | None = None
        if manifest_dict is None:
            self._spec_failure_reason = None
            spec = await self._fetch_spec(cfg)
            if spec is None:
                reason = self._spec_failure_reason
                err = f"No spec available for {hostname}" + (f" ({reason})" if reason else "")
                logger.warning(err)
                # We reached out and got nothing back, so this IS a reachability
                # measurement — record it (F-66). A device that fails here is never
                # assigned, and the health loop only iterates *assigned* devices, so
                # this is the only moment anything will ever say whether it answered.
                # Without it the record kept its optimistic default forever: the fleet
                # listing showed a healthy device alongside this very spawn_error.
                await self._backend.update_device_fields(
                    hostname, spawn_error=err, pod_active=False, reachable=False, last_check=time.time()
                )
                await self._release_claim(hostname)
                return
            try:
                if is_proxy:
                    # Already-discovered tool definitions — nothing to parse, so this does
                    # not go through the translation pool (see registry/pod_supervisor).
                    manifest_obj = build_proxy_manifest_from(hostname, spec)
                else:
                    manifest_obj = await run_translation(
                        _spec_executor,
                        partial(_translate_spec_sync, spec, hostname),
                        timeout=self._spec_translate_timeout,
                        hostname=hostname,
                    )
            except (SpecTooLargeError, ValueError) as exc:
                err = f"Spec for {hostname} rejected: {exc} (F-09)"
                logger.warning(err)
                # Reachable, unusable: the device answered with a spec we then refused.
                # Scoring it unreachable here would blame the network for a spec fault —
                # the two are separate facts and `spawn_error` already carries the second.
                await self._backend.update_device_fields(
                    hostname, spawn_error=err, pod_active=False, reachable=True, last_check=time.time()
                )
                await self._release_claim(hostname)
                return
            manifest_dict = _manifest_to_dict(manifest_obj)
            spec_hash = spec_fingerprint(spec, is_proxy=is_proxy)
            if not is_proxy and self._health is not None:
                # The health loop defers its first spec poll because of this fetch, so hand
                # it the identity now — its next cycle records it, not one 300 s later.
                self._health.record_declared_from_spec(hostname, spec)
            ttl = self._config.get("registry", {}).get("spec_cache_ttl", 3600)
            await self._backend.set_manifest(hostname, manifest_dict, ttl=ttl)
        else:
            manifest_obj = _dict_to_manifest(manifest_dict)

        auth = self._auth_for(cfg)
        if auth is not None:
            auth.on_credentials_changed(self._credential_writeback(hostname))
        pod_cls = McpProxyPod if is_proxy else DevicePod
        pod = pod_cls(
            hostname=hostname,
            manifest=manifest_obj,
            transport=cfg.transport,
            auth=auth,
            base_url=cfg.base_url,
            rate_limit_rps=cfg.rate_limit_rps,
            keep_alive_interval=self._keep_alive,
            retry_policy=self._retry_policy,
            tls_verify=self._tls.for_device(hostname),
            allow_private=resolve_allow_private(self._config),
            allowed_ports=resolve_allowed_ports(self._config),
            credential_resolver=self._credential_resolver,
        )
        await pod.start(with_sse=False)  # distributed mode: no in-process SSE transport
        self._pods[hostname] = pod
        self._assigned.add(hostname)
        spawn_fields: dict[str, Any] = {"pod_active": True, "worker_id": self._id, "spawn_error": None}
        if spec_hash is not None:
            spawn_fields["spec_hash"] = spec_hash
            # A fetched spec means the device answered us. Claiming reachability only on
            # this branch is deliberate: the cache-hit path above spawns from a stored
            # manifest without touching the device, so it has nothing to report and the
            # health loop — which now owns this device — takes the first measurement.
            spawn_fields["reachable"] = True
            spawn_fields["last_check"] = time.time()
        await self._backend.update_device_fields(hostname, **spawn_fields)
        await self._r.sadd(KEYS.worker_devices(self._id), hostname)

        # Start call consumer task for this device
        task = asyncio.create_task(self._consume_calls(hostname), name=f"calls-{hostname}")
        self._call_tasks[hostname] = task
        logger.info(f"Pod spawned for {hostname} by worker {self._id}")

    async def _kill_pod(self, hostname: str) -> None:
        if hostname not in self._assigned:
            return
        pod = self._pods.pop(hostname, None)
        if pod:
            pod.stop()
            await pod.aclose()
        self._assigned.discard(hostname)
        task = self._call_tasks.pop(hostname, None)
        if task and not task.done():
            task.cancel()
        if self._backend:
            await self._backend.update_device_fields(hostname, pod_active=False, worker_id=None)
        await self._r.srem(KEYS.worker_devices(self._id), hostname)
        await self._release_claim(hostname)
        logger.info(f"Pod killed for {hostname} by worker {self._id}")

    async def _replace_pod(self, hostname: str) -> None:
        """Restart pod after a spec change."""
        await self._kill_pod(hostname)
        await self._spawn_pod(hostname)

    async def _leave_and_hand_off(self) -> None:
        """Graceful-stop sequence: leave the live set, then hand every device on.

        The order is the point. A receiving worker declines an assignment when it is
        at/over ceil(devices / live workers) (F-07), and a worker counts as live while it
        is in ``workers:active`` with a heartbeat. Shutdown used to leave that set only
        after tearing its pods down, so a survivor reading a hand-off computed its target
        as if we were staying and refused the very devices we were handing it, leaving
        them to the reconciler's orphan sweeps after all. The heartbeat key goes too —
        every live-count reads membership first, so that is tidiness rather than
        correctness: it stops the key lingering for its TTL vouching for a worker that left.
        """
        await self._r.srem(KEYS.workers_active, self._id)
        await self._r.delete(KEYS.worker_heartbeat(self._id))
        await self._shutdown_pods()

    async def _shutdown_pods(self) -> None:
        """Tear down every pod this worker runs, and hand each device on.

        ``_kill_pod`` already releases the claim and clears ``pod_active``/``worker_id``,
        but releasing is not reassigning. Without a hand-off a device left by a clean
        shutdown looked exactly like one left by a crash: unowned until the reconciler
        had seen it orphaned across ``reconcile_orphan_grace_cycles`` sweeps — 42–113s
        measured on a live cluster, every one answering clients 404 "not active", on
        every rolling worker restart. That hysteresis exists to rule out a *transient*
        lease lapse (F-62); a worker that is deliberately leaving has nothing to rule
        out, so it republishes ``assign`` itself and a live worker picks the device up
        on its next read. The reconciler stays the backstop for a worker that dies
        without getting here.

        The claim is released before the assign is published, so the receiver's
        ``_spawn_pod`` finds it free. This worker's own assignment consumer is already
        cancelled by now, so it cannot take its own hand-off back.
        """
        relinquished = list(self._assigned)
        for hostname in relinquished:
            await self._kill_pod(hostname)
        if not relinquished or self._backend is None:
            return
        for hostname in relinquished:
            try:
                await self._backend.publish_assignment("assign", hostname)
            except Exception:
                # Not fatal: the reconciler still reassigns it once the orphan grace runs out.
                logger.exception(f"Hand-off of {hostname} failed; leaving it to the reconciler")
        logger.info(f"Worker {self._id} handed off {len(relinquished)} device(s) on shutdown")

    # ------------------------------------------------------------------
    # Recovery on restart
    # ------------------------------------------------------------------

    def _auth_for(self, cfg: Any) -> Any:
        """Build an auth handler for ``cfg``, already wired for credential resolution.

        **The resolver is attached here, where the handler is built, rather than by each
        consumer.** The health loop reaches authenticated MCP upstreams with a handler from
        this factory and calls ``apply()`` on it, so a by-reference device whose handler
        arrived unconfigured would fail its *health check* and be marked unreachable — a
        device reported down for a reason that has nothing to do with the device.

        **This must stay the only place a handler is built in this module.** The first version
        of this factory existed and was still not enough, because three other call sites went
        straight to ``_auth_from_config``: pod spawn, MCP spec discovery, and the health
        loop's provider. Spec discovery was the one that failed on a live cluster — a
        by-reference device could not be discovered, so it never got a manifest and reported
        "No spec available", which names nothing about credentials at all.

        The lesson is narrower than "attach at construction": that only helps when there is
        **one** construction point. There were four. Adding a fifth way to build a handler
        without routing it here re-opens exactly this defect.
        """
        auth = _auth_from_config(cfg.auth_type, self._decrypt_auth(cfg.hostname, cfg.auth_config))
        if auth is not None:
            auth.configure_credentials(self._credential_resolver)
        return auth

    async def _recover_assigned(self) -> None:
        """Re-spawn pods for devices this worker owned before a restart."""
        devices = await self._r.smembers(KEYS.worker_devices(self._id))
        if not devices:
            return
        logger.info(f"Recovering {len(devices)} previously assigned device(s)")
        for hostname in devices:
            try:
                await self._spawn_pod(hostname)
            except Exception:
                logger.exception(f"Recovery failed for {hostname}")

    # ------------------------------------------------------------------
    # Spec fetching
    # ------------------------------------------------------------------

    async def _fetch_spec(self, cfg: Any) -> dict | None:
        # NOTE: deliberately different from WorkerHealthLoop._fetch_spec — this
        # (spawn path) probes discovery paths CONCURRENTLY on a fresh guarded
        # client so provisioning isn't gated by serial per-path timeouts (F-11);
        # the health loop polls serially through its shared retrying client.
        #
        # Because there are two of them, an upstream kind added to one and not the other
        # fails only on the path that was missed. This one is the *cold* path: a device
        # registered while no manifest is cached reaches its pod through here, so an MCP
        # upstream that discovers fine in the health loop would still never spawn.
        # ADR-0020 §4b / LR-46: ask the one choke point first. A device carrying a curated
        # snapshot is never fetched from — not at registration and not on any later cycle,
        # which is what makes §4's version pinning hold. Without this the cold path would
        # replace the snapshot with whatever the live endpoint serves on its next pass.
        curated = resolve_spec_source(cfg)
        if curated is not None:
            try:
                return curated.parsed()
            except CuratedSpecInvalid as exc:
                logger.error(f"Curated spec unusable for {cfg.hostname}: {exc}")
                return None
        discovery = self._config.get("discovery", {})
        # SSRF-guarded client: the worker validates every fetched URL (incl. redirects)
        # against the policy, closing the gap where workers never consulted it (F-02).
        async with build_guarded_client(
            verify=self._tls.for_device(cfg.hostname),
            allow_private=resolve_allow_private(self._config),
            allowed_ports=resolve_allowed_ports(self._config),
        ) as client:
            if getattr(cfg, "upstream_kind", "openapi") == "mcp":
                upstream = StreamableHttpClient(
                    url=cfg.base_url,
                    get_client=lambda: client,
                    auth=self._auth_for(cfg),
                    timeout=discovery.get("timeout", 10),
                )
                try:
                    return await snapshot_upstream(upstream)
                except (ResolverError, CredentialNotBound) as exc:
                    # **Do not flatten this into "no spec".** Measured on a live cluster,
                    # four distinct credential failures — resolver unwired, mount permissions,
                    # a non-ASCII value, and a missing secret — all reported identically as
                    # "No spec available", which points an operator at the upstream. ADR-0018
                    # §7 draws a careful line between a bad reference and a store outage, and
                    # that line is worth nothing if it is erased one layer above the resolver.
                    self._spec_failure_reason = f"credential: {exc}"
                    logger.warning(f"MCP discovery failed for {cfg.hostname}: {exc}")
                    return None
                except Exception as exc:
                    logger.warning(f"MCP discovery failed for {cfg.hostname}: {exc}")
                    return None
                finally:
                    await upstream.close_session()
            if cfg.spec_url:
                try:
                    resp = await client.get(cfg.spec_url, timeout=10)
                    return fetched_spec_or_none(resp, max_bytes=self._spec_max_bytes)
                except SpecTooLargeError as exc:
                    logger.warning(f"Spec fetch rejected oversized spec for {cfg.spec_url}: {exc} (F-09)")
                    return None
                except Exception:
                    pass
                return None
            paths = discovery.get("spec_paths", ["/openapi.json", "/swagger.json", "/api-docs"])
            timeout = discovery.get("timeout", 10)

            async def _probe(path: str) -> dict | None:
                try:
                    resp = await client.get(cfg.base_url.rstrip("/") + path, timeout=timeout)
                    return fetched_spec_or_none(resp, max_bytes=self._spec_max_bytes)
                except SpecTooLargeError as exc:
                    logger.warning(f"Spec discovery rejected oversized spec for {cfg.base_url}: {exc} (F-09)")
                    return None
                except Exception:
                    return None

            # Probe candidate paths concurrently and take the first valid spec, so a
            # worker's device provisioning isn't gated by serial per-path timeouts
            # (F-11). Losing probes are cancelled once we have a winner.
            tasks = [asyncio.create_task(_probe(p)) for p in paths]
            try:
                for fut in asyncio.as_completed(tasks):
                    spec = await fut
                    if spec is not None:
                        return spec
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return None

    # ------------------------------------------------------------------
    # Delegation to collaborators — kept under the pre-split private names so
    # the worker's call surface (used heavily by tests and internal callers)
    # is unchanged. New code should prefer the collaborators directly.
    # ------------------------------------------------------------------

    # CallDispatcher
    async def _consume_calls(self, hostname: str) -> None:
        await self._dispatcher.consume_calls(hostname)

    async def _schedule_dispatch(self, sem, hostname, stream, group, msg_id, fields) -> None:
        await self._dispatcher.schedule_dispatch(sem, hostname, stream, group, msg_id, fields)

    async def _dispatch_guarded(self, sem, hostname, stream, group, msg_id, fields) -> None:
        await self._dispatcher._dispatch_guarded(sem, hostname, stream, group, msg_id, fields)

    async def _dispatch_call(self, hostname, stream, group, msg_id, fields) -> None:
        await self._dispatcher.dispatch_call(hostname, stream, group, msg_id, fields)

    async def _reclaim_pending(self, hostname, stream, group, sem) -> None:
        await self._dispatcher.reclaim_pending(hostname, stream, group, sem)

    async def _dead_letter(self, hostname: str, fields: dict, reason: str) -> None:
        await self._dispatcher.dead_letter(hostname, fields, reason)

    async def _guard_duplicate(self, hostname, request_id, pod, message) -> str | None:
        return await self._dispatcher.guard_duplicate(hostname, request_id, pod, message)

    async def _already_completed(self, request_id: str) -> bool:
        return await self._dispatcher.already_completed(request_id)

    async def _begin_exec(self, request_id: str) -> bool:
        return await self._dispatcher.begin_exec(request_id)

    def _is_idempotent_call(self, pod, message) -> bool:
        return self._dispatcher.is_idempotent_call(pod, message)

    # Reconciler
    async def _reconcile_loop(self) -> None:
        await self._reconciler.reconcile_loop()

    async def _acquire_leadership(self, ttl: int) -> bool:
        return await self._reconciler.acquire_leadership(ttl)

    async def _reconcile_once(self) -> None:
        await self._reconciler.reconcile_once()

    async def _prune_dead_workers(self) -> None:
        await self._reconciler.prune_dead_workers()

    # Rebalancer
    async def _live_worker_count(self) -> int:
        return await self._rebalancer.live_worker_count()

    async def _rebalance_target(self) -> tuple[int, int]:
        return await self._rebalancer.rebalance_target()

    async def _decline_assignment(self, hostname: str) -> bool:
        return await self._rebalancer.decline_assignment(hostname)

    async def _rebalance_loop(self) -> None:
        await self._rebalancer.rebalance_loop()

    async def _rebalance_once(self) -> None:
        await self._rebalancer.rebalance_once()

    async def _shed_device(self, hostname: str, target: int) -> None:
        await self._rebalancer.shed_device(hostname, target)


# ---------------------------------------------------------------------------
# Manifest dict ↔ McpManifest conversion helpers
# ---------------------------------------------------------------------------


def _dict_to_body_spec(d: dict | None) -> Any:
    """Rebuild a RequestBodySpec from its plain-dict form (Redis round-trip).

    ``binary_fields`` was stored as a sorted list (sets aren't JSON-encodable);
    restore it to a set so the adapter's ``k in spec.binary_fields`` membership
    test behaves as it does in embedded mode (F-40).
    """
    if not d:
        return None
    from device_mcp_gateway.core.translator import JSON_CONTENT, RequestBodySpec

    return RequestBodySpec(
        content_type=d.get("content_type", JSON_CONTENT),
        binary_fields=set(d.get("binary_fields") or []),
        raw=bool(d.get("raw", False)),
        raw_field=d.get("raw_field"),
    )


def _dict_to_proxy_spec(d: Any) -> Any:
    """Rebuild a ``ProxyToolSpec`` from its serialised form.

    Dropping this is the single most expensive thing this round-trip can do to a proxied
    tool: ``upstream_tool_name`` is the name the worker puts on the wire, and it differs
    from the LLM-facing name for every tool whose name needed sanitising or deduping. Lose
    it and the worker calls the upstream with a name it has never heard of.
    """
    if not isinstance(d, dict):
        return None
    from device_mcp_gateway.core.translator import ProxyToolSpec

    return ProxyToolSpec(
        upstream_tool_name=d.get("upstream_tool_name", ""),
        idempotent_hint=bool(d.get("idempotent_hint", False)),
        read_only_hint=bool(d.get("read_only_hint", False)),
    )


def _dict_to_manifest(d: dict) -> Any:
    """Reconstruct an McpManifest from a plain dict (Redis round-trip)."""
    from device_mcp_gateway.core.translator import McpManifest, McpPrompt, McpResource, McpTool

    return McpManifest(
        server_name=d.get("server_name", ""),
        server_version=d.get("server_version", "0.0.0"),
        hostname=d.get("hostname", ""),
        tools=[
            McpTool(
                name=t["name"],
                description=t.get("description", ""),
                schema=t.get("schema", {}),
                # A proxied tool has no HTTP shape, so "" is its correct value here — the
                # old "GET" fallback existed for manifests written before `method` was
                # always serialised, and applying it to a proxied tool would make an
                # upstream write look like a safe read to the idempotency guard.
                method=t.get("method") or ("" if t.get("source") == "proxy" else "GET"),
                path=t.get("path") or ("" if t.get("source") == "proxy" else "/"),
                tags=t.get("tags", []),
                param_locations=t.get("param_locations", {}),
                # Without these two the cache-hit spawn path (manifest already in
                # Redis) rebuilt tools with no body encoding and no F-04 rename map,
                # so POST/PUT/PATCH tool calls went out malformed in distributed mode.
                request_body=_dict_to_body_spec(t.get("request_body")),
                param_wire_names=t.get("param_wire_names", {}),
                # The discriminator every reader branches on, and the upstream wire name.
                source=t.get("source", "openapi"),
                proxy=_dict_to_proxy_spec(t.get("proxy")),
            )
            for t in d.get("tools", [])
        ],
        resources=[
            McpResource(
                uri=r["uri"],
                name=r.get("name", ""),
                description=r.get("description", ""),
                mime_type=r.get("mime_type", "application/json"),
            )
            for r in d.get("resources", [])
        ],
        prompts=[
            McpPrompt(
                name=p["name"],
                description=p.get("description", ""),
                template=p.get("template", ""),
                arguments=p.get("arguments", []),
                # Both needed on the cache-hit spawn path: without upstream_name a proxied
                # prompts/get would forward the sanitised name and reach nothing.
                required_arguments=p.get("required_arguments", []),
                upstream_name=p.get("upstream_name", ""),
            )
            for p in d.get("prompts", [])
        ],
        metadata=d.get("metadata", {}),
    )
