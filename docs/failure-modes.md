# Failure-Mode Matrix (FMEA) — Device MCP Gateway

Phase-0 artifact (F-22). The reliability counterpart to the [threat model](threat-model.md):
for each component, the ways it fails, how the failure is *detected* (metric/alert/log),
the *effect* on callers, the in-place *mitigation*, and the *operator action*. Detection
names map to real signals — `deploy/kubernetes/prometheus-rules.yaml` (alerts),
[observability.md](observability.md) (metrics), and the `event="audit"` stream.

Severity: 🔴 outage / data-affecting · 🟠 degraded · 🟡 localized.

## 1. Ingest / Gateway

| # | Failure | Sev | Detection | Effect | Mitigation (finding) | Operator action |
|---|---------|-----|-----------|--------|----------------------|-----------------|
| G1 | Gateway replica wedged but serving (event loop stalled) | 🟠 | `/livez` 503 via 1s heartbeat tick (F-17); k8s liveness restarts it | Slow/hung requests on that replica | No-I/O liveness probe restarts the pod (F-17) | Confirm restart; check for a blocking call regression |
| G2 | All gateway replicas down | 🔴 | Ingress 5xx; `absent` of HTTP metrics | Total outage | Multiple replicas + PDB `minAvailable:1` | Scale up; check crashloop/config |
| G3 | Client floods a hot device | 🟠 | `mcp_calls_rejected_overload_total` → `MCPAdmissionShedding`; 429s | Excess calls rejected with `Retry-After` (visible, not silent) | Admission control sheds past backlog watermark (F-06) | Scale workers; check the slow upstream |
| G4 | Per-IP/principal rate limit tripped | 🟡 | 429 rate; access log | That caller throttled | Per-IP + per-principal limits (F-16) | Confirm intended; raise limit if false positive |
| G5 | Hostile/huge spec on register | 🟠 | Spec-size/op-count reject logs | Registration rejected, pool protected | Size + op-count + translate-timeout bounds (F-09) | Inspect the offending spec/device |
| G6 | SSE dispatching replica dies mid-call | 🟠 | Client SSE drops; F6 timeout watcher | In-flight result may be lost to that client | Replica-pinned SSE accepted (F-20); client reconnects + retries | None routine; client retries |

## 2. Control plane / Redis

| # | Failure | Sev | Detection | Effect | Mitigation (finding) | Operator action |
|---|---------|-----|-----------|--------|----------------------|-----------------|
| R1 | Redis unreachable | 🔴 | `/readyz` 503 (gateway); worker reconnect-backoff logs | New calls fail; gateway sheds from rotation | Readiness gate pulls replica from LB; jittered reconnect (F-61) | Restore Redis; verify failover/persistence |
| R2 | Redis auth/TLS misconfigured | 🔴 | Startup **refuses to boot** (F-24) | Process won't start (fail-closed) | Hard gate on unauthenticated Redis (F-24) | Fix `redis-url`/password secret |
| R3 | Call-stream backlog nears MAXLEN (10k) | 🟠 | `mcp_worker_undelivered_calls` → `MCPUndeliveredBacklogNearMaxlen` | Oldest undelivered calls will be trimmed | Admission 429 (F-06) sheds before MAXLEN; alert fires at 8k | Scale workers; find the stuck consumer |
| R4 | Thundering herd after a Redis flap | 🟠 | Redis CPU/conn spike on recovery | Reconvergence load spike | ±20% jitter on every periodic loop/election/reconnect (F-61) | Usually self-heals; check jitter config |
| R6 | A deleted device is resurrected as a partial record by a worker still writing to it | 🟠 | `GET /devices/{h}` 500s with `KeyError: 'hostname'` while `GET /devices` looks clean; re-registering that hostname fails the same way | One hostname is unusable — unreadable and unregisterable — until the key is deleted by hand | `update_device_fields` will not create the record (`WATCH`/`MULTI`, not check-then-write), and a partial record reads as absent so leftovers clear themselves on re-registration (2026-08-10) | None routine; a leftover from before the fix logs a warning naming the key and is overwritten by re-registering the device |
| R5 | Blocking read outlives its own socket deadline | 🟠 | `redis.exceptions.TimeoutError` in gateway logs; a `500` where a `504` is expected; idle SSE streams dropping ~30s in | Unanswered tool calls report as gateway failure rather than device timeout; open SSE streams torn down mid-session | Block window derived from the connection's socket deadline, and a raised timeout treated as an elapsed window so the caller's own timeout stays authoritative (2026-08-07) | None routine; if it recurs, compare `redis.socket_timeout` against the XREAD block window — they must not be equal |

## 3. Workers

| # | Failure | Sev | Detection | Effect | Mitigation (finding) | Operator action |
|---|---------|-----|-----------|--------|----------------------|-----------------|
| W1 | No live workers (all down/not scraping) | 🔴 | `absent(mcp_worker_pods)` → `MCPNoLiveWorkers` | Tool calls hang to timeout | Multiple workers + PDB; HPA | Restore workers; check Redis connectivity |
| W2 | Worker death → orphaned devices | 🟠 | `mcp_reconciler_reassignments_total`; reconciler leader | Devices reassigned to a live worker. **A graceful stop** (rolling update, scale-in) hands each device off itself — leaves the live set, releases the claim, republishes `assign` — so it moves in seconds. **An ungraceful death** waits for its claims to lapse (`claim_ttl`, ≥60s) plus `reconcile_orphan_grace_cycles` sweeps; measured 42–113s per device. Callers see 503 + `Retry-After` meanwhile | Lease + reconciler reassignment (F-07); shutdown hand-off (0.3.8) | None routine; watch for churn (W3) |
| W3 | Claim-lease flap (GC pause/Redis latency > TTL) | 🟠 | `mcp_reconciler_reassignments_total` → `MCPReconcilerReassignmentChurn` | Transient double-pod/churn | Reconciler hysteresis: N consecutive orphan sweeps before reassign (F-62) | Raise `claim_ttl`/`reconcile_orphan_grace_cycles`; check GC/Redis latency |
| W4 | No reconciler leader elected | 🟠 | `sum(mcp_reconciler_leader)==0` → `MCPReconcilerLeaderAbsent` | Orphan recovery stalls | Leader election with jitter (F-21/F-61) | Check workers' Redis connectivity |
| W5 | Load skew after scale-out (sticky claims) | 🟡 | `mcp_worker_pods` per-worker skew | Hot workers, idle new ones | Decentralized rebalance on scale-out (F-07) | Verify `rebalance_enabled`; inspect skew |
| W6 | Reclaimed call double-executes a write | 🟠 | `mcp_duplicate_calls_suppressed_total` | Duplicate suppressed, client told | At-most-once guard on non-idempotent methods (F-08) | None; confirm suppression metric |
| W7 | Within-worker noisy neighbor saturates the loop | 🟡 | `mcp_worker_calls_throttled_total` | Co-located devices slowed | Per-worker aggregate in-flight cap (F-13) | Raise cap or shard the hot device to its own stack |
| W8 | Claim lease lapses and is never re-acquired (pause/stall > TTL) | 🟠 | Device reports `pod_active: false` while its worker is healthy and still polling the upstream; `claim:{hostname}` absent in Redis; reconciler logs the same device orphaned every sweep | Device unreachable through the API (404) though the worker is still serving it — **does not self-heal**; before the fix only a worker restart recovered it | Heartbeat re-acquires a lapsed claim instead of renewing a key that no longer exists, and checks ownership before renewing so a reassigned device's lease is not extended by its former owner (2026-08-07). This is the self-heal W3's F-62 hysteresis is written to depend on | None routine; the warning names the device, the worker and the claim TTL — investigate a GC pause or Redis stall longer than that TTL |

## 4. Upstream device path

| # | Failure | Sev | Detection | Effect | Mitigation (finding) | Operator action |
|---|---------|-----|-----------|--------|----------------------|-----------------|
| D1 | Device unreachable at register/runtime | 🟡 | `reachable:false`; `GET /devices/{h}/diagnostics`; `spawn_error` | That device's tools fail | Health loop retries; diagnostics endpoint (F-52) | Check `base_url`/network/spec |
| D2 | Device returns 5xx repeatedly | 🟠 | `mcp_circuit_breaker_opens_total` → `MCPCircuitBreakersOpen` | Breaker opens; fast 503 instead of 15s hang | Per-device circuit breaker | Fix upstream; breaker auto-resets 60s |
| D3 | Device rate-limits us (429/Retry-After) | 🟡 | `mcp_upstream_retries_total{reason}` | Calls retried/backed off | Honor `Retry-After`, capped; surface if too long (F-44) | Lower `rate_limit_rps`; coordinate quota |
| D4 | Transient upstream timeout/connection error | 🟡 | `mcp_tool_call_timeouts_total` | Idempotent GETs retried; writes single-attempt | Bounded full-jitter retry, idempotent-only (F-05) | Investigate if sustained |
| D5 | Oversized upstream response | 🟡 | `response_too_large` error type | 502 instead of buffering | 5 MiB response cap (F-27) | Paginate or filter at the device |
| D6 | Undeliverable call (no pod during replace) | 🟠 | `mcp_dead_letter_total` → `MCPDeadLetterGrowing` | Call dead-lettered, client told | DLQ + inspect/replay/drain ops (F-10) | Work the DLQ runbook (observability.md) |
| D8 | A device's TLS profile is wrong (bad CA path, wrong client identity) | 🔴 | **Process refuses to start**, naming the device and the file; before per-device trust this was a silent per-device failure | That process does not come up — probes fail, kubelet restarts it | Every declared `security.mtls.devices.*` profile is built at startup, not on first contact (2026-08-10) | Fix the path/secret mount; `GET /devices/{h}/diagnostics` `.tls` shows what each device resolves to |
| D9 | A device presents a certificate from another device's private CA | 🟡 | TLS handshake failure on that device only | That device's calls fail; the rest are unaffected | Trust is per device — a `ca_bundle` named for one device is not an anchor for any other (TG-4 residual, closed 2026-08-10) | Confirm the device's CA is the one named in **its** block, not a sibling's |
| D7 | Spec changes break the tool surface | 🟠 | `mcp_device_tools_changed_total{breaking}` → `MCPDeviceToolsBreakingChange`; `tools_revision` bump | Live clients on old tools fail | Change classified + audited; pollable `tools_revision` (F-41) | Notify clients; review the `device.tools_changed` audit |
| D10 | A device whose pod never spawns reports `reachable: true` forever | 🟡 | `spawn_error` set while `reachable` reads `true`, and `last_check` ageing without bound (observed 150s → 190s over a 40s wait) | The fleet listing and the UI showed a device as reachable that had never once been contacted — the value was a dataclass default, not a measurement | **Fixed 2026-08-11 ([F-66](findings-register.md))**. The health loop still iterates only *assigned* devices, so the fix is at the two ends instead: the defaults claim nothing (`reachable=False`, `last_check=0.0` → a null `last_check` in the API), and the spawn path writes the reachability verdict its failed spec fetch already established | `last_check: null` now means "never checked" — a real state, distinct from a check that found the device dead. A rejected spec keeps `reachable: true`: read `spawn_error` for that |

## 5. Reliability targets (SLOs)

Defined in `prometheus-rules.yaml` (recording rules) and
[observability.md](observability.md#slos--error-budgets). **Starting points — retune
against a measured load baseline** (run the [load harness](load-testing.md); F-22):

| SLI | Target (28d) | Burn-rate alerts |
|-----|--------------|------------------|
| Tool-call success `ok/(ok+error)` | 99.5% | fast 14.4×/1h (page), slow 6×/6h (ticket) |
| Dispatch reliability `1−(timeouts+dead_letters)/dispatched` | 99.9% | `MCPDispatchReliabilityLow` (<99.9%/1h, page) |
| Tool-call latency p99 | recorded `slo:tool_call_latency:p99_5m` | dashboard (no target until baselined) |

## 6. Single points of failure

| SPOF | Status |
|------|--------|
| Redis (control plane) | Single logical store; run HA/replicated. Auth+TLS gated (F-24). The whole stack depends on it |
| Reconciler / gauge leader | Election-based; gaps on flap are alerted + idempotent (F-21) — not a hard SPOF |
| SSE replica pinning | Soft statefulness; a replica loss drops its streams, clients reconnect (F-20, accepted) |
| `MCP_SECRET_KEY` | Loss makes encrypted credentials unrecoverable — back it up out-of-band; rotate via the documented zero-downtime flow (F-34) |

## 7. Maintenance

Note the mitigations below are **designed and unit-tested, not yet demonstrated under real
faults** — see [testing-gaps.md](testing-gaps.md) (TG-1).

Add a row when a new failure mode is found (incident, chaos run — F-63) or a new component
is introduced. Every row's **Detection** must name a real signal; a failure with no
detection is the gap to close first (add a metric/alert before the next release).
