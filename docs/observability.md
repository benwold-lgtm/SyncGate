# MCP Gateway — Observability Guide

This document covers the two observability pillars:

1. **[Prometheus metrics](#prometheus-metrics)** — RED-style request/tool-call metrics
   and fleet gauges, exposed on a dedicated metrics port.
2. **Structured logs** — JSON audit/event logs and ready-to-use config for Grafana Loki,
   Splunk, and Elasticsearch (starts at [Log Architecture](#log-architecture)).

---

## Prometheus Metrics

### Exposition model

Metrics are exposed in Prometheus text format on a **dedicated metrics port**
(`metrics.port`, default `9100`) at `GET /metrics` — deliberately **not** on the API
port (`8000`). This lets a `ServiceMonitor`/scrape config target a named `metrics` port
and lets a NetworkPolicy restrict who can scrape it, without opening an unauthenticated
hole in the API surface. The API port keeps a separate, **auth-protected** JSON summary
at `GET /v1/metrics/summary`.

| Setting | Default | Override |
|---------|---------|----------|
| `metrics.enabled` | `true` | `MCP_METRICS_ENABLED=0` to disable |
| `metrics.port` | `9100` | `MCP_METRICS_PORT` |
| `metrics.gauge_refresh_interval` | `15` (s) | config only |
| `metrics.auth_token` | _unset_ | `MCP_METRICS_TOKEN` |

**Authenticating the scrape (F-36).** By default the exposition port is open and relies on
a NetworkPolicy to restrict who can reach it. Set `metrics.auth_token` (or `MCP_METRICS_TOKEN`)
to require `Authorization: Bearer <token>` on every scrape — defence-in-depth for clusters
where a NetworkPolicy alone isn't sufficient. Point Prometheus at a secret-backed
`bearer_token_file`:

```yaml
scrape_configs:
  - job_name: mcp-gateway
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/mcp-metrics-token
```

Both the **gateway** and each **worker** expose the same port (workers have no API
server, so this is their only HTTP surface). Run **one process per pod** and scale via
replicas — Prometheus aggregates across pods by the `instance` label, so the default
process-global registry needs no multiprocess mode.

### Metric reference

| Metric | Type | Labels | Emitted by | Description |
|--------|------|--------|------------|-------------|
| `mcp_http_requests_total` | counter | `method`, `route`, `status` | gateway | HTTP requests, labelled with the **route template** (`/v1/devices/{hostname}`), never the concrete path. Unmatched paths collapse to `route="__unmatched__"`. |
| `mcp_http_request_duration_seconds` | histogram | `method`, `route` | gateway | HTTP request latency. |
| `mcp_tool_calls_total` | counter | `hostname`, `method`, `status` | gateway (embedded) + worker (distributed) | MCP tool calls executed. `status` ∈ `ok`/`error`/`noresult` (`noresult` = a notification with no JSON-RPC response). |
| `mcp_tool_call_duration_seconds` | histogram | `hostname` | gateway (embedded) + worker | Tool-call execution latency. |
| `mcp_registered_devices` | gauge | — | gateway | Devices in the registry. |
| `mcp_active_pods` | gauge | — | gateway | Devices with an active pod (fleet-wide, from the registry). |
| `mcp_reachable_devices` | gauge | — | gateway | Devices currently reachable. |
| `mcp_worker_pods` | gauge | — | worker | DevicePods running on this worker. |
| `mcp_worker_pending_calls` | gauge | — | worker | Delivered-but-unacked tool calls across this worker's device streams — the **Redis-stream-lag** signal for the worker HPA. |
| `mcp_worker_assignments_lag` | gauge | — | worker | Pending entries in this worker's assignments consumer group. |
| `mcp_worker_undelivered_calls` | gauge | — | worker | Tool-call stream entries not yet delivered to this worker's group (never-read backlog). Nears the 10k MAXLEN ⇒ silent-eviction risk. |
| `mcp_reconciler_leader` | gauge | — | worker | `1` on the worker holding the reconciler lease, else `0`. `sum == 0` ⇒ orphaned-device recovery stalled (F-14). |
| `mcp_dead_letter_total` | counter | `hostname` | worker | Tool calls moved to a device's dead-letter stream because they were undeliverable. |
| `mcp_tool_call_timeouts_total` | counter | `hostname` | gateway | Calls that hit the no-worker-responded timeout (F6). |
| `mcp_calls_rejected_overload_total` | counter | `hostname` | gateway | Calls fast-failed with 429 because the device backlog exceeded the admission watermark (F-06). |
| `mcp_oidc_validation_failures_total` | counter | `reason` | gateway | JWTs the OIDC validator rejected, falling through to static break-glass keys. `reason` ∈ `jwks_unavailable`/`expired`/`invalid_token`/`bad_algorithm`/`malformed`/`other` — a fixed set, because the raw error embeds attacker-controlled JWT contents. Sustained `jwks_unavailable` = the IdP is unreachable and the deployment has **silently degraded to break-glass-keys-only**; rising `invalid_token`/`bad_algorithm` usually means someone is probing with forged tokens. |
| `mcp_circuit_breaker_opens_total` | counter | `hostname` | gateway/worker | Calls rejected because a device pod's circuit breaker was open. |
| `mcp_break_glass_uses_total` | counter | `subject` | gateway | Requests authenticated with a `break_glass: true` credential (ADR-0023 §2). Every increment is a person using the emergency path; the matching high-severity `auth.break_glass` audit record names them and the request. |
| `mcp_break_glass_activations_total` | counter | `subject` | gateway | Break-glass **activations** — a use following a quiet gap (`gateway.break_glass_session_gap_minutes`, default 60), so one incident worked through over hours counts once however many calls it takes. This, not call volume, is the signal ADR-0023 §3 asks to watch, and it is what `MCPBreakGlassActivated` pages on. |
| `mcp_break_glass_review_flags_total` | counter | `subject` | gateway | Activations that crossed the reactivation-frequency threshold (default 3 per 30 days). **Flagging only — nothing is ever blocked**: a call budget that cut off a legitimate incident response mid-session is the one failure a break-glass mechanism cannot afford. |
| `mcp_break_glass_expiry_timestamp_seconds` | gauge | `subject` | gateway | Unix timestamp at which a configured break-glass credential expires (ADR-0023 §3). Absolute rather than days-remaining, deliberately: it is set at startup, and a countdown would quietly become wrong on a gateway that has not restarted in a month — which is exactly when a lapsing credential is discovered dead during the incident it exists for. An expired entry keeps reporting its past expiry so the alert stays firing after the key has been dropped. |

The standard `prometheus_client` process/runtime collectors (`process_*`,
`python_gc_*`) are also exported.

> **The break-glass metrics are a notification channel, not a dashboard.** ADR-0017 §4
> requires that break-glass use produce "a notification the tenant receives, not a log line
> they may one day read", and in this deployment the metrics plane is what delivers that:
> `deploy/kubernetes/prometheus-rules.yaml` ships `MCPBreakGlassActivated` (`severity: page`,
> with **no `for:`** — every SLO and operational alert in that file waits 2–15 minutes to filter
> transients, and an activation is not a transient), `MCPBreakGlassBecomingRoutine`, and the two credential-expiry alerts. If
> you do not run Alertmanager, wire the `auth.break_glass.activated` audit record
> (`severity: critical`) into whatever does page, because nothing else will.

> **Cardinality:** HTTP metrics use the route **template**, and tool-call metrics use
> `hostname` (bounded by your device count). Avoid adding unbounded labels (raw paths,
> request IDs, session IDs) — they will blow up Prometheus.

### Scrape configuration

The gateway and worker Deployments already carry pod annotations
(`prometheus.io/scrape: "true"`, `prometheus.io/port: "9100"`, `prometheus.io/path: /metrics`).

**Plain Prometheus** (`kubernetes_sd` pod role + annotation relabel):

```yaml
scrape_configs:
  - job_name: mcp-gateway-pods
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names: [mcp-gateway]
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
        action: replace
        target_label: __metrics_path__
        regex: (.+)
      - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
        action: replace
        regex: ([^:]+)(?::\d+)?;(\d+)
        replacement: $1:$2
        target_label: __address__
      - source_labels: [__meta_kubernetes_pod_label_app]
        target_label: app
      - source_labels: [__meta_kubernetes_pod_name]
        target_label: instance
```

**Prometheus Operator** (`ServiceMonitor`, targeting the named `metrics` port on the
gateway Service and the headless `device-mcp-worker-metrics` Service):

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: device-mcp-gateway
  namespace: mcp-gateway
  labels:
    release: prometheus   # match your Prometheus Operator's serviceMonitorSelector
spec:
  namespaceSelector:
    matchNames: [mcp-gateway]
  selector:
    matchLabels:
      app.kubernetes.io/name: device-mcp-gateway   # gateway Service
  endpoints:
    - port: metrics
      path: /metrics
      interval: 30s
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: device-mcp-worker
  namespace: mcp-gateway
  labels:
    release: prometheus
spec:
  namespaceSelector:
    matchNames: [mcp-gateway]
  selector:
    matchLabels:
      app.kubernetes.io/name: device-mcp-worker     # device-mcp-worker-metrics Service
  endpoints:
    - port: metrics
      path: /metrics
      interval: 30s
```

### Grafana starter queries (PromQL)

```promql
# Request rate by route
sum by (route) (rate(mcp_http_requests_total[5m]))

# Error ratio (5xx) across the gateway
sum(rate(mcp_http_requests_total{status=~"5.."}[5m]))
  / sum(rate(mcp_http_requests_total[5m]))

# p95 HTTP latency by route
histogram_quantile(0.95, sum by (le, route) (rate(mcp_http_request_duration_seconds_bucket[5m])))

# Tool-call throughput and error rate by device
sum by (hostname) (rate(mcp_tool_calls_total[5m]))
sum by (hostname) (rate(mcp_tool_calls_total{status="error"}[5m]))

# p95 tool-call latency by device
histogram_quantile(0.95, sum by (le, hostname) (rate(mcp_tool_call_duration_seconds_bucket[5m])))

# Fleet health
mcp_registered_devices
mcp_reachable_devices

# Worker backlog (HPA signal) — average pending calls per worker
avg(mcp_worker_pending_calls)
```

### Worker autoscaling on Redis-stream lag

`mcp_worker_pending_calls` is the intended worker HPA signal. Kubernetes can't read a
Prometheus gauge directly — bridge it with **prometheus-adapter** (exposes it as an
External/Object metric) or **KEDA** (`prometheus` scaler). The
`device-mcp-worker` HPA in [`deploy/kubernetes/hpa.yaml`](../deploy/kubernetes/hpa.yaml)
ships with the External-metric block commented out; uncomment it once the adapter is
installed.

---

## SLOs & error budgets

Recording rules (the SLIs) and burn-rate + operational alerts ship as a
`PrometheusRule` in [`deploy/kubernetes/prometheus-rules.yaml`](../deploy/kubernetes/prometheus-rules.yaml)
(consumed by the Prometheus Operator; lift the `groups:` into a `rule_files:` entry
if you don't run the operator). The targets below are **starting points** — tune them
after a load baseline exists.

| SLI | Definition (recording rule) | Starting SLO |
|-----|-----------------------------|--------------|
| Tool-call success rate | `slo:tool_call_success:ratio_rate5m` = `ok / (ok + error)` from `mcp_tool_calls_total` (excludes `noresult`) | **99.5%** over 28d |
| Dispatch reliability | `slo:dispatch_reliability:ratio_rate5m` = `1 − (timeouts + dead_letters) / dispatched` | **99.9%** over 28d |
| Tool-call latency p99 | `slo:tool_call_latency:p99_5m` from `mcp_tool_call_duration_seconds_bucket` | tune to baseline |

**Caveat on success rate:** the `status` label on `mcp_tool_calls_total` is
`ok` / `error` / `noresult`; `error` currently lumps upstream *client-fault* 4xx in
with server/dispatch faults, so the success SLI is slightly pessimistic. Splitting
them needs a finer status label — tracked as a future refinement.

**Burn-rate alerts** use the multi-window Google-SRE method: a fast window (1h, 14.4×
burn) pages on a sharp outage, a slow window (6h, 6×) tickets on a slow leak — both
gated by a shorter confirmation window so a blip doesn't flap.

**Operational alerts** (the otherwise-silent failure modes): no live workers
(`absent(mcp_worker_pods)`), no reconciler leader (`sum(mcp_reconciler_leader) == 0`),
DLQ growing (`rate(mcp_dead_letter_total[5m]) > 0`), undelivered backlog nearing the
10k stream MAXLEN (`max(mcp_worker_undelivered_calls) > 8000` — silent-eviction risk),
admission shedding (`rate(mcp_calls_rejected_overload_total[5m]) > 0`, F-06), and
circuit breakers opening.

### Working a dead-letter alert (F-10)

When `MCPDeadLetterGrowing` fires, the device's undeliverable calls are in
`device:{hostname}:calls:dead`. Inspect, replay, or drain them over the API
(distributed mode):

```bash
# Inspect — newest first (id, reason, ts, method, rid, request_id, session_id)
curl -H "Authorization: Bearer $KEY" .../v1/devices/$H/deadletter

# Replay — re-publish onto the live call stream and remove from the DLQ.
# Most DLQ entries are "no active pod" from a pod-replace window; once a pod is
# back, replaying re-runs them. Add {"ids":[...]} to replay specific entries.
curl -X POST -H "Authorization: Bearer $KEY" .../v1/devices/$H/deadletter/replay

# Drain — drop the whole queue (or {"ids":[...]} for specific entries)
curl -X DELETE -H "Authorization: Bearer $KEY" .../v1/devices/$H/deadletter
```

Replay preserves the original `request_id`/`session_id`/`rid`/`traceparent`; a
result for an already-expired session is best-effort, but the call still
re-executes. The gateway does not auto-replay — it's an operator action.

---

## Distributed Tracing (OpenTelemetry)

**Off by default.** Tracing is a no-op until you both install the extra and enable it:

```bash
pip install '.[otel]'
```
```yaml
tracing:
  enabled: true
  otlp_endpoint: "http://otel-collector:4318/v1/traces"   # OTLP/HTTP
  service_name: "device-mcp-gateway"
  sample_ratio: 1.0
```

When on, a tool call is **one trace end to end**: the gateway opens an
`mcp.tool_dispatch` span and injects its W3C `traceparent` into the Redis call-stream
entry; the worker extracts it and runs the `mcp.tool_call` execution span as a child.
The existing correlation id (`rid`) is attached as the `mcp.rid` span attribute, so a
trace links straight back to the JSON logs. Spans export to any OTLP/HTTP collector
(e.g. an OpenTelemetry Collector sidecar). Setup failures or a missing extra downgrade
to disabled with a warning — they never block startup or add latency when off.

---

## Log Architecture

The gateway uses two log sinks:

| Sink | Format | Purpose |
|------|--------|---------|
| **stderr** | Human-readable colored text | Interactive consoles, `kubectl logs`, local dev |
| **File** (`logs/gateway.log`) | Newline-delimited JSON (default) | External collectors — Fluent Bit, Promtail, Splunk UF |

Both sinks run simultaneously. The file format is controlled by `logging.json_logs` in
`config.yaml` (default: `true`). Setting it to `false` switches the file to plain text
for local development without a collector.

In Kubernetes, the recommended pattern is to mount `logs/` as an `emptyDir` volume shared
with a Fluent Bit or Promtail sidecar. See the [Kubernetes section](#kubernetes) below.

---

## Log Record Format

Each JSON log record is one line:

```json
{
  "text": "2026-06-05T12:34:56.123456+00:00 | INFO | device_mcp_gateway.main:log_requests:212 - GET /health -> 200 (1.2ms) rid=f3a8b1c2",
  "record": {
    "elapsed": { "repr": "0:00:01.234567", "seconds": 1.234567 },
    "exception": null,
    "extra": {
      "event": "audit",
      "hostname": "array-1",
      "subject": "key:ops-dashboard",
      "method": "tools/call",
      "status": "ok",
      "duration_ms": 42.3,
      "rid": "f3a8b1c2-d4e5-6f78-9012-3456789abcde"
    },
    "file": { "name": "main.py", "path": "/app/device_mcp_gateway/main.py" },
    "function": "device_sse_message",
    "level": { "name": "INFO", "no": 20 },
    "line": 580,
    "message": "tool dispatch",
    "module": "main",
    "name": "device_mcp_gateway.main",
    "process": { "id": 1, "name": "MainProcess" },
    "thread": { "id": 140234567890, "name": "MainThread" },
    "time": { "repr": "2026-06-05 12:34:56.123456+00:00", "timestamp": 1748981696.123456 }
  }
}
```

All structured fields added via `logger.bind()` appear under `record.extra`.

---

## Key Fields for Querying

| Field | Path in JSON | Description |
|-------|-------------|-------------|
| `event` | `record.extra.event` | `"audit"` for tool dispatch events; absent for general logs |
| `hostname` | `record.extra.hostname` | Registered device name (e.g. `"array-1"`) |
| `subject` | `record.extra.subject` | Authenticated principal — `key:<name>` for an API key, or `anonymous` when auth is disabled |
| `method` | `record.extra.method` | MCP JSON-RPC method (e.g. `"tools/call"`, `"tools/list"`) |
| `status` | `record.extra.status` | `"ok"`, `"error"`, or `"dispatched"` (distributed mode) |
| `duration_ms` | `record.extra.duration_ms` | Tool call round-trip time in milliseconds (embedded mode only) |
| `rid` | `record.extra.rid` | Correlation ID — matches the `X-Request-Id` response header **and** the `X-Request-Id` the gateway sent to the device ([ADR-0026](adr/0026-service-identity-per-device.md)) |
| `level` | `record.level.name` | `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"` |
| `message` | `record.message` | Log message string |
| `time` | `record.time.timestamp` | Unix epoch timestamp (float) |

---

## Grafana + Loki

### Promtail sidecar configuration

Add a Promtail sidecar to the gateway Deployment. The sidecar tails the JSON log file
from the shared `logs` volume.

```yaml
# In your gateway Deployment spec, add a shared volume:
volumes:
  - name: gateway-logs
    emptyDir: {}

# In the gateway container, mount it:
containers:
  - name: gateway
    volumeMounts:
      - name: gateway-logs
        mountPath: /app/logs

# Add the Promtail sidecar:
  - name: promtail
    image: grafana/promtail:latest
    args:
      - -config.file=/etc/promtail/config.yaml
    volumeMounts:
      - name: gateway-logs
        mountPath: /app/logs
      - name: promtail-config
        mountPath: /etc/promtail
```

```yaml
# promtail-config ConfigMap
apiVersion: v1
kind: ConfigMap
metadata:
  name: promtail-config
  namespace: mcp-gateway
data:
  config.yaml: |
    server:
      http_listen_port: 9080

    clients:
      - url: http://loki:3100/loki/api/v1/push

    scrape_configs:
      - job_name: mcp-gateway
        static_configs:
          - targets: [localhost]
            labels:
              job: mcp-gateway
              namespace: mcp-gateway
              __path__: /app/logs/gateway.log

        pipeline_stages:
          - json:
              expressions:
                level: record.level.name
                message: record.message
                event: record.extra.event
                hostname: record.extra.hostname
                subject: record.extra.subject
                method: record.extra.method
                status: record.extra.status
                duration_ms: record.extra.duration_ms
                rid: record.extra.rid
                ts: record.time.timestamp

          - labels:
              level:
              event:
              hostname:

          - timestamp:
              source: ts
              format: Unix
```

### Grafana LogQL queries

```logql
# All audit events
{job="mcp-gateway"} | json | event="audit"

# Audit events for a specific device
{job="mcp-gateway"} | json | event="audit" | hostname="array-1"

# Failed tool calls
{job="mcp-gateway"} | json | event="audit" | status="error"

# Slow tool calls (> 500 ms) — embedded mode only
{job="mcp-gateway"} | json | event="audit" | duration_ms > 500

# All errors and warnings
{job="mcp-gateway"} | json | level=~"WARNING|ERROR"

# Trace a single request end-to-end by correlation ID
{job="mcp-gateway"} | json | rid="f3a8b1c2-d4e5-6f78-9012-3456789abcde"
```

---

## Splunk

### Universal Forwarder inputs.conf

If running outside Kubernetes, configure the Splunk Universal Forwarder to tail the log file:

```ini
[monitor:///app/logs/gateway.log]
index = mcp_gateway
sourcetype = _json
```

### Splunk HTTP Event Collector (HEC) — Fluent Bit

For Kubernetes, use Fluent Bit with the Splunk HEC output plugin:

```ini
[INPUT]
    Name              tail
    Path              /app/logs/gateway.log
    Parser            json
    Tag               mcp.gateway
    Refresh_Interval  5
    Mem_Buf_Limit     5MB

[OUTPUT]
    Name              splunk
    Match             mcp.*
    Host              splunk.example.com
    Port              8088
    Splunk_Token      <your-hec-token>
    Splunk_Send_Raw   off
    TLS               on
    TLS.Verify        on
```

### Splunk search queries (SPL)

```spl
# All audit events
index=mcp_gateway sourcetype=_json record.extra.event="audit"

# Audit events by device
index=mcp_gateway sourcetype=_json record.extra.event="audit" record.extra.hostname="array-1"

# Error rate per device (last 1 hour)
index=mcp_gateway sourcetype=_json record.extra.event="audit"
| eval status=spath(_raw, "record.extra.status")
| eval hostname=spath(_raw, "record.extra.hostname")
| stats count by hostname, status

# Average tool call duration per device
index=mcp_gateway sourcetype=_json record.extra.event="audit"
| eval duration=spath(_raw, "record.extra.duration_ms")
| eval hostname=spath(_raw, "record.extra.hostname")
| stats avg(duration) as avg_ms by hostname

# Trace a correlation ID
index=mcp_gateway sourcetype=_json record.extra.rid="f3a8b1c2-d4e5-6f78-9012-3456789abcde"
```

---

## Elasticsearch + Kibana

### Fluent Bit → Elasticsearch

```ini
[INPUT]
    Name              tail
    Path              /app/logs/gateway.log
    Parser            json
    Tag               mcp.gateway

[OUTPUT]
    Name              es
    Match             mcp.*
    Host              elasticsearch.example.com
    Port              9200
    Index             mcp-gateway
    Type              _doc
    Suppress_Type_Name on
    TLS               on
```

### Kibana queries (KQL)

```
# All audit events
record.extra.event : "audit"

# Failed tool calls on a specific device
record.extra.event : "audit" AND record.extra.hostname : "array-1" AND record.extra.status : "error"

# Trace by correlation ID
record.extra.rid : "f3a8b1c2-d4e5-6f78-9012-3456789abcde"
```

---

## Kubernetes

The recommended Kubernetes pattern is a Fluent Bit DaemonSet that reads container logs
from `/var/log/containers/` (captured automatically from stdout/stderr by the container
runtime). Since the gateway also writes a JSON file to `logs/`, you can use either path.

### DaemonSet approach (no sidecar needed)

Fluent Bit's Kubernetes filter automatically enriches log lines with pod name, namespace,
and container metadata. Configure an `[INPUT]` pointing to container logs and an
`[OUTPUT]` to your chosen backend (Loki, Splunk, Elastic, CloudWatch).

The stderr sink writes human-readable text, which will appear in `kubectl logs` for
operator convenience. The JSON file sink is the source for machine-readable ingestion.

### Useful kubectl commands

```bash
# Tail human-readable logs from all gateway pods
kubectl logs -f -l app=device-mcp-gateway -n mcp-gateway

# Filter audit events from a running pod (requires jq)
kubectl logs -l app=device-mcp-gateway -n mcp-gateway \
  | grep '"event": "audit"' \
  | jq '{time: .record.time.repr, hostname: .record.extra.hostname, method: .record.extra.method, status: .record.extra.status, duration_ms: .record.extra.duration_ms}'
```

---

## Disabling JSON Logs (local dev)

Set `json_logs: false` in `config.yaml` or override at runtime:

```yaml
logging:
  level: "DEBUG"
  json_logs: false
```

The file sink will write plain text. The stderr sink is always plain text regardless of
this setting.
