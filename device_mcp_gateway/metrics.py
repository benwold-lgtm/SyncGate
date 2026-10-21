# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""
Prometheus metrics for the gateway and workers.

Exposition is served on a **dedicated metrics port** (default 9100), separate from
the public API port — so a `ServiceMonitor`/NetworkPolicy can target a named
`metrics` port without opening an unauthenticated hole in the API surface, and the
gateway and worker share one pattern. Run **one process per pod** (scale via
replicas) so the default global registry needs no multiprocess mode.

HTTP metrics are labelled with the **route template** (`/devices/{hostname}`), never
the concrete path — concrete paths are unbounded cardinality and will OOM Prometheus.
"""

import os

import hmac
import threading
from http.server import ThreadingHTTPServer

from loguru import logger
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, Histogram, generate_latest
from prometheus_client import start_http_server as _start_http_server
from prometheus_client.exposition import MetricsHandler

__all__ = [
    "CONTENT_TYPE_LATEST",
    "generate_latest",
    "http_requests_total",
    "http_request_duration_seconds",
    "tool_calls_total",
    "tool_call_duration_seconds",
    "registered_devices",
    "active_pods",
    "reachable_devices",
    "worker_pods",
    "worker_pending_calls",
    "worker_assignments_lag",
    "worker_undelivered_calls",
    "reconciler_leader",
    "reconciler_reassignments_total",
    "gauge_leader",
    "rebalance_shed_total",
    "duplicate_calls_suppressed_total",
    "calls_rejected_overload_total",
    "worker_calls_throttled_total",
    "tool_call_timeouts_total",
    "dead_letter_total",
    "circuit_breaker_opens_total",
    "upstream_retries_total",
    "metrics_port",
    "metrics_enabled",
    "metrics_token",
    "start_metrics_server",
    "route_template",
]

# --- Default port / enable, overridable by config or env ---------------------

_DEFAULT_PORT = 9100


def metrics_port(cfg: dict | None = None, default: int = _DEFAULT_PORT) -> int:
    """Resolve the metrics port: env MCP_METRICS_PORT > config metrics.port > default."""
    env = os.getenv("MCP_METRICS_PORT")
    if env:
        return int(env)
    if cfg:
        return int(cfg.get("metrics", {}).get("port", default))
    return default


def metrics_enabled(cfg: dict | None = None) -> bool:
    """Whether to start the exposition server. env MCP_METRICS_ENABLED > config > True."""
    env = os.getenv("MCP_METRICS_ENABLED")
    if env is not None:
        return env.lower() not in ("0", "false", "no")
    if cfg:
        return bool(cfg.get("metrics", {}).get("enabled", True))
    return True


def metrics_token(cfg: dict | None = None) -> str | None:
    """Resolve an optional bearer token gating the exposition endpoint (F-36).

    env MCP_METRICS_TOKEN > config metrics.auth_token > None. When set, scrapers
    must send ``Authorization: Bearer <token>``; when unset, the endpoint is open
    and must be restricted by a NetworkPolicy (the documented default).
    """
    env = os.getenv("MCP_METRICS_TOKEN")
    if env:
        return env
    if cfg:
        return cfg.get("metrics", {}).get("auth_token") or None
    return None


# --- Instruments (registered on the process-global default REGISTRY) ---------

http_requests_total = Counter(
    "mcp_http_requests_total",
    "Total HTTP requests handled by the gateway.",
    ["method", "route", "status"],
)
http_request_duration_seconds = Histogram(
    "mcp_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "route"],
)

# Tool-call metrics are incremented where calls *execute* (embedded gateway
# dispatch and the worker), not where they are merely enqueued.
tool_calls_total = Counter(
    "mcp_tool_calls_total",
    "Total MCP tool calls executed.",
    ["hostname", "method", "status"],
)
tool_call_duration_seconds = Histogram(
    "mcp_tool_call_duration_seconds",
    "MCP tool call execution duration in seconds.",
    ["hostname"],
)
# Bounded outbound retries on idempotent calls (F-05/F-44). `reason` is the trigger
# (e.g. status_503, status_429, TimeoutException) so retry storms are diagnosable.
upstream_retries_total = Counter(
    "mcp_upstream_retries_total",
    "Total outbound upstream call retries (idempotent ops only).",
    ["hostname", "reason"],
)

# Device-fleet gauges, refreshed by a periodic task (collection is sync; the
# registry read is async, so we cannot compute these inside a Prometheus collector).
registered_devices = Gauge("mcp_registered_devices", "Number of registered devices.")
active_pods = Gauge("mcp_active_pods", "Number of devices with an active pod.")
reachable_devices = Gauge("mcp_reachable_devices", "Number of reachable devices.")

# --- Worker-side gauges (distributed mode) -----------------------------------
# Per-worker, refreshed on a timer. Prometheus distinguishes workers by the
# scrape target (instance label), so these stay single-series per worker.
worker_pods = Gauge("mcp_worker_pods", "DevicePods currently running on this worker.")
worker_pending_calls = Gauge(
    "mcp_worker_pending_calls",
    "Delivered-but-unacked tool calls across this worker's device call streams. "
    "Primary signal for the worker HPA (Redis Stream consumer lag).",
)
worker_assignments_lag = Gauge(
    "mcp_worker_assignments_lag",
    "Pending entries in this worker's assignments consumer group.",
)
# Undelivered (never-read) tool-call backlog across this worker's device streams
# (XINFO GROUPS lag). worker_pending_calls counts delivered-but-unacked (in-flight)
# work; this counts work not yet read — held back by the per-device concurrency cap
# (SRE #5). Sum the two for total work waiting → the recommended worker HPA signal.
worker_undelivered_calls = Gauge(
    "mcp_worker_undelivered_calls",
    "Tool-call stream entries not yet delivered to this worker's consumer group "
    "(never-read backlog). Add to mcp_worker_pending_calls for total work waiting.",
)
# 1 on the worker that currently holds the reconciler lease, 0 on the others.
# Summed across workers it must be exactly 1; sum == 0 means orphaned-device
# recovery has stalled (no leader) — alertable (F-14, SRE #1/#2).
reconciler_leader = Gauge(
    "mcp_reconciler_leader",
    "1 if this worker currently holds the reconciler leader lease, else 0.",
)
# Reassignment churn (F-62). The reconciler republishes an 'assign' for a device
# whose owner's claim lapsed. Under normal operation this fires once per real
# worker death; a sustained elevated rate means leases are FLAPPING — healthy
# workers being declared dead under GC pauses / Redis latency, causing transient
# double-pod churn. Alert on rate() to catch the flap (F-62 / F-21).
reconciler_reassignments_total = Counter(
    "mcp_reconciler_reassignments_total",
    "Orphaned devices the reconciler republished an assignment for, after their "
    "owner's claim lapsed past the grace window. Sustained rate ⇒ lease flapping.",
)
# 1 on the gateway replica that currently holds the device-gauge-refresh leader
# lock, 0 on the others (distributed mode only — absent in embedded). Summed
# across replicas it must be exactly 1; sum == 0 means the fleet gauges are no
# longer being refreshed by any replica — alertable coordination SPOF (F-21).
gauge_leader = Gauge(
    "mcp_gateway_gauge_leader",
    "1 if this gateway replica currently holds the device-gauge-refresh leader "
    "lock, else 0 (distributed mode only).",
)
# A device's spec changed and its generated tool set mutated (F-41). `breaking`
# distinguishes a backwards-incompatible mutation (tool/parameter removed,
# parameter newly required, HTTP method changed) — clients calling the old shape
# break — from a compatible one (tool/optional-parameter added). A sustained
# breaking rate ⇒ upstream APIs changing under live MCP clients without governance.
device_tools_changed_total = Counter(
    "mcp_device_tools_changed_total",
    "Tool-set mutations detected on a device spec change, labelled by whether the "
    "change was backwards-breaking (F-41).",
    ["hostname", "breaking"],
)

# --- Failure-mode counters (SRE O1) ------------------------------------------
# Counters at the exact sites where requests fail or are shed, so failures are
# visible in Prometheus instead of only in logs.
tool_call_timeouts_total = Counter(
    "mcp_tool_call_timeouts_total",
    "Tool calls whose waiting request hit its deadline before a worker answered (F6).",
    ["hostname"],
)
dead_letter_total = Counter(
    "mcp_dead_letter_total",
    "Tool calls moved to a device's dead-letter stream because they were undeliverable.",
    ["hostname"],
)
circuit_breaker_opens_total = Counter(
    "mcp_circuit_breaker_opens_total",
    "Tool calls rejected because a device pod's circuit breaker was open.",
    ["hostname"],
)
rebalance_shed_total = Counter(
    "mcp_rebalance_shed_total",
    "Devices shed by a worker during rebalancing to relieve load skew on scale-out (F-07).",
)
duplicate_calls_suppressed_total = Counter(
    "mcp_duplicate_calls_suppressed_total",
    "Redelivered tool calls the idempotency guard did not re-execute (F-08). "
    "reason=already_completed: the result was already published; "
    "reason=nonidempotent_guard: a non-idempotent op had already begun, so retry was refused.",
    ["hostname", "reason"],
)
calls_rejected_overload_total = Counter(
    "mcp_calls_rejected_overload_total",
    "Tool calls fast-failed with 429 because the device's call-stream backlog "
    "exceeded the admission watermark (F-06) — rejected up front instead of "
    "being silently trimmed at MAXLEN.",
    ["hostname"],
)
oidc_validation_failures_total = Counter(
    "mcp_oidc_validation_failures_total",
    "JWTs the OIDC validator rejected, falling through to static break-glass keys "
    "(review item 10). A sustained rise in reason=jwks_unavailable means the IdP or its "
    "JWKS endpoint is unreachable and the deployment has silently degraded to "
    "break-glass-keys-only; reason=invalid_token/bad_algorithm rising instead usually "
    "means someone is probing with forged tokens.",
    ["reason"],
)
# --- Break-glass (ADR-0023 §2/§3) --------------------------------------------
# Labelled by audit subject ("key:alice"). Cardinality is bounded by the number of
# configured break-glass entries and every value is operator-chosen, never caller-supplied —
# unlike the OIDC failure reasons above, which had to be a fixed allow-list precisely because
# a caller controls them.
break_glass_uses_total = Counter(
    "mcp_break_glass_uses_total",
    "Requests authenticated with a `break_glass: true` credential (ADR-0023). Every "
    "increment is a person using the emergency path, and the matching high-severity "
    "auth.break_glass audit record names them.",
    ["subject"],
)
break_glass_activations_total = Counter(
    "mcp_break_glass_activations_total",
    "Break-glass *activations* — a use that follows a quiet gap, so one incident worked "
    "through over hours counts once however many calls it takes. This, not call volume, is "
    "the signal ADR-0023 §3 asks to watch: repeated activations across separate weeks mean "
    "the credential has become routine access rather than emergency access.",
    ["subject"],
)
break_glass_expiry_timestamp_seconds = Gauge(
    "mcp_break_glass_expiry_timestamp_seconds",
    "Unix timestamp at which a configured break-glass credential expires (ADR-0023 §3). An "
    "ABSOLUTE timestamp rather than days-remaining, deliberately: this is set once at startup "
    "and a days-remaining value would quietly become wrong on a gateway that has not restarted "
    "in a month — which is precisely when a lapsing credential is discovered dead during the "
    "incident it exists for. Prometheus does the arithmetic instead. An expired entry keeps "
    "reporting its (past) expiry so the alert stays firing after the key has been dropped.",
    ["subject"],
)
break_glass_review_flags_total = Counter(
    "mcp_break_glass_review_flags_total",
    "Activations that crossed the reactivation-frequency threshold and raised a review "
    "flag. **Flagging only — nothing is ever blocked** (ADR-0023 §3): a call budget that "
    "cut off a legitimate incident response mid-session is the one failure a break-glass "
    "mechanism cannot afford.",
    ["subject"],
)

worker_calls_throttled_total = Counter(
    "mcp_worker_calls_throttled_total",
    "Tool-call dispatches that had to wait on the worker-wide concurrency cap "
    "(registry.max_concurrent_calls_per_worker, F-13) because every aggregate slot "
    "was in use. The worker-level backpressure signal — distinct from the per-device "
    "cap — indicating a worker is saturated across all the devices it hosts.",
)


def _authenticated_handler(token: str):
    """Build a MetricsHandler subclass that requires a bearer token (F-36)."""
    base = MetricsHandler.factory(REGISTRY)
    expected = f"Bearer {token}"

    class _AuthMetricsHandler(base):  # type: ignore[valid-type, misc]
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            presented = self.headers.get("Authorization", "")
            # Constant-time compare so a wrong token can't be timing-probed.
            if not hmac.compare_digest(presented, expected):
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            super().do_GET()

        def log_message(self, *args: object) -> None:  # silence per-scrape stderr noise
            pass

    return _AuthMetricsHandler


def start_metrics_server(port: int, auth_token: str | None = None) -> bool:
    """Start the Prometheus exposition HTTP server on ``port``.

    When ``auth_token`` is set, the endpoint requires ``Authorization: Bearer
    <token>`` (F-36); otherwise it is open and must be restricted by a NetworkPolicy.
    Tolerant of "address already in use": the test suite builds many app instances
    in one process, and a metrics port collision must never crash the app or a test.
    Returns True if the server started, False otherwise.
    """
    try:
        if auth_token:
            httpd = ThreadingHTTPServer(("", port), _authenticated_handler(auth_token))
            threading.Thread(target=httpd.serve_forever, daemon=True, name="metrics-exposition").start()
            logger.info(f"Prometheus metrics server listening on :{port} (bearer-token authenticated)")
        else:
            _start_http_server(port)
            logger.info(
                f"Prometheus metrics server listening on :{port} (unauthenticated — restrict via NetworkPolicy)"
            )
        return True
    except OSError as exc:  # address in use, permission, etc. — non-fatal
        logger.warning(f"Metrics server not started on :{port}: {exc}")
        return False


def _full_path_format(scope, route, path_format: str) -> str:
    """Re-attach any router prefix to a relative ``path_format``.

    FastAPI >=0.137 includes a prefixed router via a wrapper that keeps the inner
    routes' ``path_format`` *relative* (e.g. ``/devices/{hostname}`` for a route
    mounted under ``/v1``), with the matched ``Route`` on the scope being that inner
    route — so ``path_format`` alone drops the ``/v1`` and a dashboard can't tell API
    versions apart. (FastAPI <=0.136 flattened the prefix into ``path_format``, so
    there is nothing to re-attach.) The route's own anchored ``path_regex`` matches
    the concrete path *relative* to the prefix; the prefix is therefore the leftmost
    path boundary from which that regex still matches. This is version-independent:
    when ``path_format`` is already absolute the regex matches at offset 0 and the
    prefix is empty.
    """
    concrete = scope.get("path") or ""
    regex = getattr(route, "path_regex", None)
    # Fast path: an unprefixed static route's concrete path == its template.
    if concrete == path_format or not concrete or regex is None:
        return path_format
    for i, ch in enumerate(concrete):
        if ch == "/" and regex.match(concrete[i:]):
            return concrete[:i] + path_format
    return path_format


def route_template(request) -> str:
    """Return the matched route's path template (e.g. ``/v1/devices/{hostname}``).

    Starlette (1.3+) puts the matched ``Route`` on the scope, whose ``path_format``
    is the low-cardinality template directly — robust across the endpoint-wrapping
    changes that broke the older ``scope["endpoint"]`` identity match. Older
    Starlette only exposed ``scope["endpoint"]``, so we keep an endpoint→path_format
    cache as a fallback. A router ``prefix`` (e.g. ``/v1``) may live outside the inner
    route's ``path_format``, so we re-attach it via ``_full_path_format``. Unmatched
    requests (404 from scanners) collapse to ``__unmatched__`` so they cannot explode
    the label set.
    """
    route = request.scope.get("route")
    if route is not None:
        path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
        if path_format:
            return _full_path_format(request.scope, route, path_format)
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return "__unmatched__"
    app = request.scope.get("app")
    if app is None:
        return "__unmatched__"
    cache = getattr(app.state, "_metrics_route_cache", None)
    if cache is None:
        cache = {}
        for r in getattr(app.router, "routes", []):
            ep = getattr(r, "endpoint", None)
            path_format = getattr(r, "path_format", None) or getattr(r, "path", None)
            if ep is not None and path_format:
                cache[ep] = path_format
        app.state._metrics_route_cache = cache
    return cache.get(endpoint, "__unmatched__")
