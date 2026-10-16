# MCP Gateway — Kubernetes Deployment Architecture

This document describes how the **Device MCP Gateway** is deployed on Kubernetes in **distributed mode** and traces the complete message path from an LLM client through the gateway to downstream device APIs.

Distributed mode is the production path: stateless gateway replicas read from Redis, while stateful workers own the DevicePods that make the actual HTTP calls to device APIs. All three tiers scale independently.

---

## Deployment Overview

```mermaid
flowchart TB
    LLM[/"LLM Client\nClaude Desktop · Cursor · API Application"/]

    subgraph CLUSTER["Kubernetes Cluster"]
        subgraph NS["Namespace: mcp-gateway"]

            ING["Ingress\nnginx.ingress.kubernetes.io\nHost: mcp-gateway.example.com\nTLS termination"]

            SVC["Service: device-mcp-gateway\ntype: ClusterIP  ·  port: 8000"]

            subgraph GW_DEP["Deployment: device-mcp-gateway  (stateless — scale freely)"]
                CTRL["FastAPI Control Plane  :8000\n─────────────────────────────────────\nPOST  /v1/devices          register device\nGET   /v1/devices/{n}/sse  open MCP stream\nPOST  /v1/devices/{n}/messages  invoke tool\nGET   /health  ·  GET  /readyz  ·  GET  /v1/metrics/summary\nProm /metrics on :9100 (dedicated port)\nRate limiting  ·  CORS  ·  X-Request-Id"]
            end

            PDB_GW["PodDisruptionBudget\ndevice-mcp-gateway-pdb\nminAvailable: 1"]
            PDB_WK["PodDisruptionBudget\ndevice-mcp-worker-pdb\nminAvailable: 1"]

            subgraph REDIS_SS["StatefulSet: redis"]
                REDIS["Redis 7\n─────────────────────────────────────\ndevice registry (Hash per device)\nassignment stream  device:assignments\ntool call streams  device:{n}:calls\nSSE session routing  session:{id}:results\nworker heartbeats  worker:{id}:heartbeat"]
            end
            REDIS_SVC["Service: redis\nClusterIP  ·  port: 6379"]
            REDIS_PVC[("PVC: redis-data\n/data — Redis AOF")]

            subgraph WK_DEP["Deployment: device-mcp-worker  (stateful — scale independently)"]
                WK["Worker\n─────────────────────────────────────\nconsumes  device:assignments  (XREADGROUP)\nspawns DevicePods per assigned device\nconsumes  device:{n}:calls  (one task/device)\nruns health loop with Redis SETNX lock\npublishes results to  session:{id}:results\nheartbeat → worker:{id}:heartbeat (TTL)"]
            end

            CM[/"ConfigMap: gateway-config\nconfig.yaml — server, registry (mode: distributed),\nredis, cors, auth, transport, discovery settings"/]
        end
    end

    subgraph APIS["External Device APIs  (in-cluster or remote)"]
        direction LR
        API1["Sensor API  :8080\n────────────────\nGET  /sensors/temp\nAuth: X-API-Key"]
        API2["Actuator API  :9090\n────────────────\nPOST /actuators/relay\nAuth: OAuth2 CC"]
    end

    LLM      -- "① MCP call  (SSE)" --> ING
    ING      -- "② TLS · route /v1/devices/..." --> SVC
    SVC      -- "③ forward" --> CTRL
    CTRL     -- "④ publish assignment / tool call" --> REDIS_SVC
    REDIS_SVC --> REDIS
    REDIS    -- "⑤ XREADGROUP" --> WK
    WK       -- "⑥ HTTP + auth + circuit breaker" --> API1
    WK       -- "⑥ HTTP + auth + circuit breaker" --> API2
    WK       -- "⑦ PUBLISH result" --> REDIS
    REDIS    -- "⑧ pub/sub" --> CTRL
    CTRL     -- "⑨ SSE message event" --> LLM

    WK_DEP -. "mounts read-only" .-> CM
    GW_DEP -. "mounts read-only" .-> CM
    REDIS_SS -. "mounts read-write" .-> REDIS_PVC

    classDef external  fill:#dbeafe,stroke:#3b82f6,color:#1e3a8a
    classDef k8sinfra  fill:#fef9c3,stroke:#ca8a04,color:#713f12
    classDef appcore   fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef redisnode fill:#fde68a,stroke:#d97706,color:#78350f
    classDef worker    fill:#f3e8ff,stroke:#9333ea,color:#581c87
    classDef storage   fill:#fee2e2,stroke:#ef4444,color:#7f1d1d
    classDef apinode   fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e
    classDef pdb       fill:#f0fdf4,stroke:#22c55e,color:#14532d

    class LLM external
    class ING,SVC,REDIS_SVC k8sinfra
    class CTRL appcore
    class REDIS redisnode
    class WK worker
    class CM,REDIS_PVC storage
    class API1,API2 apinode
    class PDB_GW,PDB_WK pdb
```

> **Response path:** The worker (⑥) calls the device API, receives the JSON body, publishes it to a Redis pub/sub channel (⑦). The gateway instance that owns the SSE session subscribes to that channel (⑧) and delivers the result as an SSE `message` event (⑨) to the LLM client.

---

## Message Flow

### Device Registration

```mermaid
sequenceDiagram
    autonumber

    actor Admin as Admin / CI Pipeline
    participant GW as Gateway
    participant REDIS as Redis
    participant WK as Worker
    participant API as Device API

    Admin->>GW: POST /v1/devices<br/>{ hostname, base_url, auth_type, auth }
    GW->>REDIS: HSET device:{hostname}:config ...<br/>SADD devices:all {hostname}
    GW->>REDIS: XADD device:assignments<br/>{ action: "assign", hostname }
    GW-->>Admin: 200 OK { status: "registered", pod_active: false }

    Note over WK: Assignment consumer wakes
    WK->>REDIS: XREADGROUP device:assignments
    WK->>API: GET /openapi.json  (spec discovery)
    API-->>WK: OpenAPI 3.x spec
    Note over WK: SpecTranslator → McpManifest<br/>Spawn DevicePod
    WK->>REDIS: HSET device:{hostname}:config pod_active true<br/>SET device:{hostname}:manifest ...
    WK->>REDIS: XACK  (assignment consumed)
    Note over WK: Start per-device call consumer task
```

#### Registration latency — provisioning is off the request path (F-11)

`POST /v1/devices` never blocks on a slow or unreachable device. In **distributed mode** the gateway only persists the device and publishes an `assign`, returning `200 { pod_active: false }` immediately; a worker does reachability + spec discovery + translation + spawn out of band (the diagram above). In **embedded mode** the gateway runs that same provisioning on a background task and waits inline only up to `registry.registration_provision_budget` (default 8 s): a fast/healthy device finishes within the budget so the response already reflects the spawned pod, while a slow device returns promptly with `"provisioning": true` and the work continues in the background (also re-checked by the health loop). Either way the caller polls `GET /v1/devices/{hostname}` for the settled `reachable` / `pod_active` / `spawn_error`.

Spec **discovery probes candidate paths concurrently** (both the embedded registry and the worker) and takes the first path that returns a valid spec, so worst-case discovery latency is one path's timeout rather than the sum across all of `spec_paths`. Translation is bounded separately by `registry.spec_translate_timeout` (F-09).

### Runtime Tool Invocation

```mermaid
sequenceDiagram
    autonumber

    participant LLM as LLM Client
    participant GW as Gateway (any replica)
    participant REDIS as Redis
    participant WK as Worker (pod owner)
    participant API as Device API

    LLM->>GW: GET /v1/devices/sensor-api/sse
    GW->>REDIS: HSET session:{uuid} hostname gateway_id TTL
    GW-->>LLM: event: endpoint<br/>data: /v1/devices/sensor-api/messages?session_id={uuid}
    Note over GW: Subscribe to session:{uuid}:results (pub/sub)

    LLM->>GW: POST /v1/devices/sensor-api/messages?session_id={uuid}<br/>{ jsonrpc: "2.0", method: "tools/call", ... }
    GW->>REDIS: XADD device:sensor-api:calls<br/>{ request_id, session_id, gateway_id, message }
    GW-->>LLM: 200 OK { status: "accepted" }

    Note over WK: Call consumer task wakes
    WK->>REDIS: XREADGROUP device:sensor-api:calls
    WK->>WK: DevicePod._handle_mcp_message()
    WK->>API: GET /sensors/temperature<br/>X-API-Key: •••<br/>(circuit breaker wraps this call)
    API-->>WK: 200 OK { "temperature": 23.4 }
    WK->>REDIS: XACK  (call consumed)<br/>PUBLISH session:{uuid}:results { jsonrpc result }

    REDIS-->>GW: pub/sub message
    GW-->>LLM: event: message<br/>data: { "jsonrpc": "2.0", "result": { "content": [...] } }
```

---

## Health, Readiness, and Disruption Safety

### Gateway probes

| Probe | Path | Behaviour |
|-------|------|-----------|
| **Liveness** | `GET /health` | Returns 200 if the process is running. In distributed mode it also reports `live_workers` and flips `status` to `"degraded"` (still 200) when no worker has a live heartbeat — a signal for the UI/operators, deliberately **not** a restart trigger (SRE #7). |
| **Readiness** | `GET /readyz` | Pings Redis (`await redis.ping()`). Returns 503 if Redis is unreachable. K8s stops routing traffic until the probe passes. **Does not** gate on worker availability — a worker outage must not pull every gateway out of the LB and break read-only endpoints. |

### Worker probes

Workers have no HTTP port. The liveness probe uses `exec`:

```yaml
livenessProbe:
  exec:
    command:
      - python
      - -c
      - |
        import os, sys, redis as r
        client = r.from_url(os.environ.get("MCP_REDIS_URL", "redis://redis:6379/0"))
        key = f"worker:{os.environ.get('WORKER_ID', 'unknown')}:heartbeat"
        sys.exit(0 if client.exists(key) else 1)
  initialDelaySeconds: 60
  periodSeconds: 30
  failureThreshold: 3
```

The heartbeat key is written by the worker's internal heartbeat loop with a TTL of `2 × health_check_interval`. A missing key means the loop has stalled — K8s will restart the pod after 3 consecutive failures (90 s).

The heartbeat is **gated on consumer-loop health** (SRE #8): the loop withholds the heartbeat (and stops refreshing device-claim leases) if a critical loop — the assignment consumer, health loop, or reconciler — has crashed, or if the assignment consumer has not made progress within `2 × health_check_interval`. So a worker whose process is alive but whose consumers are wedged now fails liveness (gets restarted) **and** lets its claims lapse so the reconciler reassigns its devices, instead of looking healthy while doing nothing.

### Rebalancing on scale-out (F-07)

Device claims are sticky — the owner refreshes its lease each heartbeat — so without rebalancing a newly added worker (e.g. from the HPA) stays idle while early workers stay hot. Each worker therefore runs a **rebalance loop** (paced by `reconcile_interval`): it computes a per-worker target = `ceil(total_devices / live_workers)` and, if it owns more than that, **sheds** the excess — releasing the claim, stopping the pod, and re-publishing an `assign`. To bias placement onto under-loaded workers, a worker **declines** an assignment while it is at/over target (the reconciler re-publishes a declined device next sweep, so it still lands), and a short `rebalance:cooldown:{hostname}` marker stops a worker from immediately re-grabbing a device it just shed. The result converges to within one device per worker, typically in a cycle or two. A shed device's pod is briefly down during the move, and any in-flight calls are reclaimed by the new owner via `XAUTOCLAIM`. Set `registry.rebalance_enabled: false` to pin devices to their first owner. The `mcp_rebalance_shed_total` counter and the per-worker `mcp_worker_pods` gauge make rebalancing activity and any residual skew visible.

### Duplicate-execution guard (F-08)

Tool calls ride a Redis Stream with **at-least-once** delivery: when a worker dies or sheds a device, the calls it had read but not acked sit in the consumer group's PEL and the new owner reclaims them via `XAUTOCLAIM`. A reclaimed call may *already have executed* on the dead worker — for a non-idempotent operation (`POST`/`PATCH`) re-running it would double-apply (e.g. a duplicate create or charge). Each worker therefore runs an **idempotency guard** keyed on the call's `request_id` before touching the upstream:

1. **Completion dedup (all methods).** If `result:{request_id}` already exists, the call finished and its result was published (the original worker just died before acking) — the reclaimed copy is dropped without re-running or re-publishing.
2. **At-most-once for non-idempotent calls.** Idempotency follows the backing HTTP method (`GET`/`HEAD`/`OPTIONS`/`TRACE`/`PUT`/`DELETE` are safe to repeat; `POST`/`PATCH` are not). Before executing a non-idempotent call the worker claims an exclusive `exec:{request_id}` marker with `SET NX`. The first executor wins; a later reclaim finds the marker set, **refuses** to re-run, and returns a `duplicate_suppressed` JSON-RPC error (code `-32002`) so the client is told definitively instead of hanging to the call timeout. Idempotent calls skip the marker and are re-run freely.

This makes a non-idempotent operation run **at most once across the fleet** even through worker death, shedding, and reclaim. It does not provide end-to-end exactly-once against the *upstream device* — that would require the device to honour an idempotency key — but it removes the gateway-introduced duplicate. The guard markers outlive the reclaim window (so a reclaim always sees them) and expire well within the per-call `request_id` lifetime (ids are unique, so a long TTL is harmless). Set `registry.idempotency_guard: false` to allow at-least-once for every method — only safe when your upstreams dedupe writes themselves. The `mcp_duplicate_calls_suppressed_total{reason}` counter exposes how often a redelivery was deduped (`already_completed`) or refused (`nonidempotent_guard`).

### Scaling a hot device — single-owner by design (F-03)

A device is owned by **exactly one worker** at a time: one lease, one consumer of its `device:{hostname}:calls` stream. Adding workers spreads *different* devices across the fleet (see rebalancing above) but does **not** split a single device's call stream across pods. A single hot device therefore scales **vertically** — its throughput is bounded by one worker's per-device concurrency cap (`registry.max_concurrent_calls_per_device`, default **20** in-flight calls) and by the upstream API itself — not horizontally.

This is a **deliberate design decision, not a gap.** Single-ownership is what makes the rest of the control plane correct and cheap:

- **At-most-once writes (F-08)** — the `exec:{request_id}` guard and `XAUTOCLAIM` reclaim assume one owner; sharding a device across pods would require cross-shard dedup.
- **Per-device ordering** — a single consumer processes a device's calls in stream order; multiple consumers would interleave.
- **One circuit breaker per device** — breaker state (open/closed, failure count) lives in the owning pod; split ownership means split-brain breaker decisions.
- **Lease failover (SRE #1/#2) and rebalancing (F-07)** — the whole device is the atomic unit that moves between workers.

Sharding one device across pods would multiply upstream connections, fracture the breaker, and complicate idempotency and ordering — substantial fragility to buy a capability that is rarely needed. For an MCP→API gateway the load is LLM-driven tool calls, and the **upstream API is almost always the first bottleneck** (most APIs rate-limit the caller, which is why the gateway honours upstream `429`/`Retry-After`, F-44). You typically hit the upstream's limit long before one worker saturates.

**If a device does run hot, scale it without sharding:**

1. **Raise the cap** — increase `registry.max_concurrent_calls_per_device` so the owning worker runs more calls in parallel.
2. **Bigger pod** — give the worker that holds the hot device more CPU/memory.
3. **Operator-level fan-out** — register the same upstream as two logical devices (e.g. `api-a` / `api-b`) so they land on different workers and the client/model spreads load across them. This shards at the *registration* boundary without touching the single-owner invariant.

**Future consideration — fairness / rate limiting (F-16, not implemented):** the concurrency cap is global to the device, so one noisy principal can monopolize a device's slots, and there is no per-principal quota or token-bucket. If multi-tenant fairness or abuse control becomes a requirement, add per-principal rate limiting as a dedicated feature — it is orthogonal to the single-owner decision above.

### PodDisruptionBudgets

Both gateway and worker have a PDB with `minAvailable: 1`. This prevents node drains and cluster upgrades from taking down all replicas simultaneously. Rolling updates (during which one pod is replaced at a time) proceed normally as long as `replicas ≥ 2`.

> With `replicas: 1`, the PDB will **block voluntary eviction** — no pod can be drained. Operators must scale to 0 or delete the PDB before draining a node that hosts a single-replica deployment.

### Worker graceful shutdown

`terminationGracePeriodSeconds: 120`. On SIGTERM:
1. A `preStop: sleep 5` hook runs first, giving Kubernetes time to stop routing new assignments to this worker.
2. SIGTERM fires; the worker stops accepting new work — it cancels the background loops (heartbeat, assignment consumer, health, reconciler) and the per-device call consumers, so no new tool calls are dispatched.
3. **In-flight tool calls are drained** (SRE #6): the worker waits up to `registry.shutdown_drain_timeout` (default 25 s) for active calls to finish before cancelling any stragglers, then tears down pods and deregisters from `workers:active`.

> Keep `terminationGracePeriodSeconds` comfortably above `shutdown_drain_timeout` (120 s vs 25 s here) so the drain completes before Kubernetes force-kills the pod.

---

## Redis availability & durability

In distributed mode Redis is the **single source of truth and a single point of failure** (SRE #9). It carries *all* shared state on the hot path:

- the device registry (`device:{h}:config`, `devices:all`),
- the assignment, per-device call, and **per-session result streams**,
- the shared rate limiter, device **claim leases**, and the **reconciler leader lock**.

**Failure behaviour.** If Redis is unreachable, gateways fail `GET /readyz` (Redis `PING`) and are pulled from the load balancer; workers retry their stream reads until it returns. No split-brain occurs because claims and the reconciler lock are Redis keys — when Redis is down, nothing is assigned or reassigned. Recovery is automatic once Redis is back.

**Durability.** The provided `redis.yaml` runs `--appendonly yes`, so the AOF persists the registry and every stream across a restart. Because tool-call results are now **Redis Streams** rather than fire-and-forget pub/sub (SRE #3), a clean restart **no longer loses in-flight results** — a reconnecting gateway re-reads the session's result stream from where it left off. Only writes within the last `appendfsync` window (default `everysec` → ≤ 1 s) can be lost on an unclean crash.

**Production recommendation.** The bundled single-node `redis.yaml` StatefulSet is fine for
dev and small/home deployments but has **no failover** — it is explicitly *not* an HA
component. Because the gateway is **fail-closed on Redis** (`/readyz` gates on `PING`),
**Redis availability is gateway availability**: an enterprise SLA effectively requires an HA
Redis. The rest of this section is how to do that.

### What the gateway needs from Redis (and what it does *not*)

Two facts about how the gateway uses Redis decide which HA topology fits:

1. **One logical primary, not sharding.** The gateway keeps all state in a **single
   keyspace** and uses **multi-key `MULTI`/`EXEC` transactions** (the atomic rate limiter),
   **Streams**, and **pub/sub** across it. It is single-owner-per-device ([ADR-0003](adr/0003-single-owner-per-device.md)) and does **not** need horizontal write-scaling — so it needs **HA (failover)**, not a sharded cluster.
2. **A single connection endpoint.** The client connects with one URL
   (`MCP_REDIS_URL` / `redis.url`, e.g. `rediss://:<pw>@host:6379/0`) — it does **not** do
   native Sentinel discovery today. So **any HA solution that presents one stable address
   which survives failover is a drop-in** (no code change). Native multi-Sentinel discovery
   would be a small client enhancement (tracked; not required).
3. **Redis 7+ (or Valkey ≥ 7.2).** The atomic rate-limiter `MULTI`/`EXEC` requires it; pick
   an HA option/tier that is on 7+.

### HA options, best fit first

**1 — Managed Redis (cloud) — least ops, drop-in.** AWS **ElastiCache** (cluster-mode
*disabled*, Multi-AZ + automatic failover), Azure **Cache for Redis** Premium (zone
redundant), or GCP **Memorystore** Standard. Each presents **one endpoint** that fails over
internally → set `redis.url` to it, enable persistence, done. Preferred when a cloud is
available.

**2 — Self-hosted Redis/Valkey + Sentinel, behind a stable endpoint — on-prem default.**
1 primary + 2 replicas + **3 Sentinels** for automatic failover. Use a maintained
operator/chart rather than hand-rolling (e.g. the **Bitnami Redis** chart with
`architecture: replication` and Sentinel enabled, or a Redis/Valkey operator). The one
design rule: front it with a **Service/proxy that always tracks the current primary** so the
gateway keeps using a single URL across failovers (the Bitnami chart provides this). Spread
replicas/Sentinels across nodes/zones (podAntiAffinity), put the data on a real
StorageClass, and keep **AOF on** (below).

**3 — Native Sentinel discovery — optional enhancement.** If you would rather not run a
primary-tracking front, the gateway client can be extended to use `redis.asyncio.Sentinel`
(connect to the Sentinels, resolve the primary) — a small change to
`device_mcp_gateway/shared/redis_client.py` plus a couple of config keys. Only worth it for
pure-Sentinel deployments without a fronting Service.

### What to avoid

- **Redis Cluster (sharding).** The gateway's **multi-key `MULTI`/`EXEC` and pub/sub on one
  keyspace** run into Cluster's same-hash-slot and sharded-pub/sub constraints, which the
  code is not written for. It adds risk and buys scale a device gateway does not need. Skip
  it unless you ever *prove* a single node can't hold the load.
- **Active-active / multi-primary (CRDB).** Unnecessary and **semantically wrong** for
  single-owner-per-device — it can break the transaction/claim guarantees. One primary is
  correct.

### Durability (separate from failover)

HA ≠ durability — and Redis here holds **authoritative** state (registry, encrypted
credentials, in-flight call/result streams), not a cache. So regardless of the option:

- Keep **AOF on** (`appendonly yes`, `appendfsync everysec`) on the primary **and** replicas;
  `redis.yaml` already does this for the bundled instance.
- Take periodic **RDB/snapshot backups** for point-in-time recovery.
- **Alert on failover events and replication lag** — failover is your availability event.

### Pools and persona

Size the command and pub/sub connection pools (`redis.max_connections`,
`redis.pubsub_max_connections`) for your gateway/worker replica count and expected concurrent
SSE streams. For packaging, make Redis a single switch: **home/Docker** uses the bundled
single instance (documented not-HA); **enterprise** defaults to **bring-your-own** endpoint
(`redis.url` → managed or Sentinel-fronted), with an optional bundled HA subchart for those
who want it in-cluster.

---

## Secret store availability

> **Status: this section is forward-looking.** `ResolverKind.NETWORKED` is declared but **not
> yet implemented** — `MountedFilesResolver` is the only backend that exists today, so there is
> currently no way to configure a networked store and nothing here can be exercised. The
> resolver-level circuit breaker and `ERR_SECRET_STORE_UNAVAILABLE` arrive with it (ADR-0018
> §7). The section is written now because the *decisions* are settled and shape the resolver's
> existing structure; treat it as the contract the first networked backend must meet, not as a
> description of running behaviour.

Only relevant if you configure a **networked** credential-resolver backend — Vault, a cloud
secret manager. The default **mounted-files** backend (Kubernetes Secret/CSI volume, and
the local file tree) has no equivalent concern: per
[ADR-0018 §7a](adr/0018-device-credentials-by-reference.md), it fails at **deploy time**
(`FailedMount`, old pods keep serving), never at dispatch time, so there is no outage window
for HA to shorten — and per §7c, the resolution cache is structurally absent for this
backend (**TTL 0, read-through**) rather than merely configured off, since it buys nothing
there and would otherwise hold a durable-shaped plaintext copy in memory for no benefit.
Nothing below applies to it.

**Failure behaviour.** Per [ADR-0018 §7](adr/0018-device-credentials-by-reference.md), the
store is a fleet-wide dependency, distinct from any one device: an unreachable store opens a
**resolver-level** circuit breaker (devices' own breakers stay closed and untouched), and
every device resolving through it reports `ERR_SECRET_STORE_UNAVAILABLE`. Readiness
deliberately does **not** fail — the gateway stays up, degraded, so the console and
diagnostics remain reachable — but dispatch for any device on that backend stops until the
store recovers.

**It fails closed, not open.** A credential past its cache TTL (300s default, networked
backends only) when the store is unreachable is refused, not served stale. This is a
considered decision, not the cautious default: fail-open would risk dispatching an
already-rotated-away credential, which produces a *worse* diagnosis than an honest
`ERR_SECRET_STORE_UNAVAILABLE` — indistinguishable, at the device, from twenty unrelated
auth failures. The corollary is direct: **outage duration and frequency are entirely what
an under-provisioned store costs you.** There is no gateway-side setting that trades
correctness back for availability here — HA on the store itself is the only lever.

### HA options, best fit first

**1 — Managed (cloud) — least ops, drop-in.** A cloud secret manager (AWS Secrets Manager,
GCP Secret Manager, Azure Key Vault) presents one endpoint with HA already part of the
service — nothing to configure beyond pointing the resolver at it. Preferred when a cloud is
available, for the same reason it's preferred for Redis.

**2 — Self-hosted Vault, HA mode — on-prem default.** Multiple Vault server nodes sharing
integrated Raft storage (or a Consul backend), with automatic leader election and failover.
Use a maintained deployment (the official Vault Helm chart in `ha` mode) rather than
hand-rolling — the same reasoning as preferring a maintained Redis chart over a bespoke one.

**3 — Single-node Vault or a single-instance secret service.** Works, but every restart or
crash is a real, visible dispatch outage for every device on that backend, for as long as
the outage lasts — the fail-closed decision above means this cost is not hidden or
softened. Acceptable only where that downtime is genuinely tolerable.

### What to avoid

- **Treating fail-open as a substitute for HA.** It isn't offered as a configuration option
  for exactly this reason: it would let an under-provisioned store's downtime be paid for in
  credential-freshness risk instead of in visible, correctly-attributed unavailability. Fix
  the store's deployment topology, not the gateway's failure mode.

---

## Catalog service (ADR-0020)

> **Status: ADR-0020 phase 1 (§1–§5) is built** — the service (`device_mcp_catalog/`),
> deployed (`deploy/kubernetes/catalog/`), device-type curation, per-tenant assignment, the
> claim flow and upgrade offers, with a real PostgreSQL connection and named-unavailable
> readiness (`/readyz`). Since ADR-0020 §7a it authenticates **two caller classes** rather
> than one shared token; see "Reachability" below, because the tenant half needs a deliberate
> change here before it works at all. §6 (provider-operated services) and per-tool→scope
> mapping remain deferred.

The provider's device catalog is a **separate process with its own PostgreSQL database**,
not a client library bolted onto the gateway the way Redis is (ADR-0020 §7: "a separate
component with its own failure domain... not reached through the [console] BFF's process
boundary"). Its own image, its own database, its own namespace, its own NetworkPolicy, its
own CI job (`.github/workflows/ci.yml`'s `catalog` job) — a fourth first-class component
rather than an add-on.

**It is applied separately, and exactly once per estate:**

```bash
kubectl apply -k deploy/kubernetes/catalog/     # namespace mcp-provider — ONE per estate
kubectl apply -k deploy/kubernetes/             # namespace mcp-<tenant> — one per TENANT
```

It used to be bundled into the gateway's own `kustomization.yaml`, for the convenience of
bringing everything up in one command. That convenience does not survive contact with more
than one tenant: this stack is deployed once *per tenant*, so bundling gave every tenant a
private copy of a store that belongs to no tenant, and "which device types exist" acquired as
many answers as there were tenants. A tenant overlay had to actively delete six objects to be
correct — which is what the bundling looked like from the tenant side. The catalog is a
provider-plane object with no tenant reach (§1); where it physically lives stays a deployment
choice (§7), and separating it is what makes choosing possible.

**It holds no tenant credential, no claim, and no device instance** (ADR-0020 §5). Its durable
content is exactly device-type definitions (with full version history) and per-tenant
assignment records — a materially smaller and less sensitive store than the gateway's own
Redis or SQLite, even though it is new operational surface. See
[ADR-0025](adr/0025-the-catalog-has-its-own-durability-story.md) for its backup/HA/restore
story (standard PostgreSQL WAL archiving + periodic base backups, kept separate from
whatever HA topology is chosen — HA solves availability, not recovery from a bad migration
or an accidental `DROP`).

**Reachability is deliberately narrow.** `deploy/kubernetes/catalog/networkpolicy.yaml`
accepts ingress on the catalog's port only from the **provider** console BFF's namespace
(`mcp-gateway-ui` by default — set this to wherever yours actually runs); the gateway and
worker have no reason to reach it and none is granted.

⚠️ **This section previously said a tenant console has no business reaching the catalog
either. That was wrong about the code and is corrected rather than quietly rewritten.** The
tenant console's catalog routes read the catalog service directly and always have — see
[ADR-0020 §7a](adr/0020-the-device-catalog.md). What was true is that this policy never let
them, and that is the only reason a single shared credential spanning the provider plane and
every tenant was never reachable in a deployment.

Enabling a tenant's claim view is therefore a **two-part change, in this order**: give that
tenant its own catalog credential (`tenant-tokens` in `catalog-secrets`), *then* add its
namespace to the policy. The reverse order is the configuration §7a exists to prevent. The
catalog refuses to start if a tenant's token is the provider's, so the first step cannot be
faked, but nothing enforces the ordering itself.

Postgres is restricted to the catalog pod alone — and standing in its own namespace this is
now an enforcement change, not only a declaration. While the catalog shared the gateway's
namespace, that namespace's blanket `allow-intra-namespace` rule already let any pod beside
it reach Postgres, so the policy documented an intent the topology did not hold to. There is
no blanket allow in `mcp-provider`, and it carries its own `default-deny`, which it
previously inherited by accident from the namespace it was borrowing.

**Before applying**, create the `catalog-secrets` Secret — both the catalog and its Postgres
mount it, and without it both sit in `CreateContainerConfigError`, which reads as a broken
image rather than a missing prerequisite. The command is in
`deploy/kubernetes/catalog/kustomization.yaml`.

**Production**: the bundled `postgres.yaml` StatefulSet is a single-replica getting-started
convenience, the same caveat `redis.yaml` carries. Point `CATALOG_DATABASE_URL` at a managed
Postgres (RDS / Cloud SQL / Azure Database for PostgreSQL) or a self-hosted HA topology
(Patroni + repmgr, or a managed operator) in production, and layer ADR-0025's WAL/PITR backup
story on top regardless of which HA topology is chosen.

---

## Observability

Each gateway and worker pod exposes Prometheus metrics on a dedicated port (`:9100`, separate from the `:8000` API). The UI/BFF and operators should treat **Prometheus and the read APIs as the observability surface — not pod log files** (see "UI/BFF sourcing" below).

### Failure-mode metrics (SRE O1)

Sites that previously only logged a failure now also increment a counter, so request loss/shedding is visible in metrics:

| Metric | Type | Incremented when |
|--------|------|------------------|
| `mcp_tool_call_timeouts_total{hostname}` | counter | the gateway's F6 watcher fires — no worker set `result:{id}` before the deadline |
| `mcp_sse_messages_dropped_total{hostname}` | counter | an embedded-mode SSE client queue is full and a response is dropped |
| `mcp_dead_letter_total{hostname}` | counter | an undeliverable tool call is moved to `device:{h}:calls:dead` |
| `mcp_circuit_breaker_opens_total{hostname}` | counter | a device pod's circuit breaker rejects a call |

### Worker backlog & HPA signal (SRE #5)

`mcp_worker_pending_calls` counts **delivered-but-unacked** (in-flight) work, but the per-device concurrency cap (SRE #5) holds excess work **undelivered** in the stream, where XPENDING can't see it. `mcp_worker_undelivered_calls` exposes that never-read backlog (XINFO GROUPS lag). **Sum the two for total work waiting → the recommended worker HPA signal.**

### Fleet gauges are leader-gated (SRE O4)

`mcp_registered_devices`, `mcp_active_pods`, and `mcp_reachable_devices` are fleet-wide. To avoid every gateway replica running a full `list_devices()` each cycle (×replicas Redis load), only the replica holding the `gateway:gauge-leader` lock computes them. **Consequence:** these gauges are populated on one replica at a time — aggregate them with `max()` across replicas in Prometheus. (Embedded mode is a single process and always refreshes.) The `/v1/admin/overview` read aggregate is likewise served from a short-TTL per-replica cache with an `ETag` (`gateway.read_cache_ttl`, default 5 s) so a polling UI doesn't trigger a fresh `list_devices()` per request.

### End-to-end tracing (SRE O2)

The gateway assigns each request an `X-Request-Id` (generated if absent) and logs it as `rid`. That `rid` is now propagated as a field on the tool-call stream and bound into the **worker's** audit log lines, so a single id traces a call across the gateway→worker hop. Filter logs in both pods by `rid=<value>` to follow one invocation end to end. The same id also leaves the cluster: every outbound call to a device carries it as `X-Request-Id`, which is how the gateway's audit record is joined to the device's own log — required, not optional, because the device sees one service identity rather than the caller ([ADR-0026](adr/0026-service-identity-per-device.md)).

### Per-call latency: Prometheus, not logs (SRE O3)

In distributed mode tool calls execute on the worker, so the **gateway's** audit log carries no `duration_ms` for them — that field is emitted by the gateway only in embedded mode. The worker does log `duration_ms`, but in a separate pod a gateway sidecar cannot read. The mode-independent source of per-call latency is the worker's Prometheus histogram **`mcp_tool_call_duration_seconds{hostname}`**. **Contract for the UI/BFF:** source distributed-mode latency from Prometheus (e.g. `histogram_quantile` over `mcp_tool_call_duration_seconds_bucket`), not from gateway logs.

### UI/BFF log sourcing (SRE O5)

If the UI runs as a gateway-pod sidecar, do **not** make it depend on tailing `logs/gateway.log`: that file rotates (50 MB × 5) and, more importantly, **worker logs — tool execution, latency, dead-letters — live in a different pod the sidecar cannot see**. The supported sourcing model is:

- **Metrics** (RED, failure-mode counters, latency histogram) from each pod's `:9100` Prometheus endpoint, aggregated by a Prometheus server;
- **Fleet/device state** from the gateway read APIs (`GET /v1/admin/overview`, `GET /v1/metrics/summary`, `GET /v1/devices`);
- **Structured logs**, if needed, shipped from *all* pods (gateway and worker) to a central log store and queried by `rid` — never read from one pod's local file.

---

## Kubernetes Resource Summary

| Kind | Name | Purpose |
|------|------|---------|
| `Namespace` | `mcp-gateway` | Isolates all resources. Set from `namespace:` in `kustomization.yaml`, the single source of truth — no resource file hardcodes it. In a multi-tenant estate this is **one namespace per tenant**, named as a pseudonym rather than after the customer ([ADR-0014](adr/0014-tenant-namespace-naming-and-network-isolation.md) §1) |
| `ConfigMap` | `gateway-config` | Non-secret `config.yaml` (mode: distributed, Redis URL, registry settings) |
| `Secret` | `gateway-secrets` | `api-key`, `secret-key`, `redis-password`, `redis-url` — injected as env vars; **never in ConfigMap**. Distributed mode requires the api-key (F-23) and an authenticated `redis-url` (F-24). |
| `Secret` | *per credential* | **Optional.** The ADR-0018 credential store, presented to both the gateway and the worker as one **`projected`** volume at `/run/secrets/mcp` (`MCP_CREDENTIAL_ROOT`), with **one source per Secret**. Not one Secret holding everything: ADR-0023 property 1 requires a break-glass credential to be an *individual* Secret whose RoleBindings grant exactly one reader, and one Secret has one ACL — so a combined Secret makes that custody property unachievable. Every source is `optional: true`, so a stack using none still starts; the resolver is built only when something holds a `secret://` reference. Each source's `items[].path` **is** what the reference resolves to: `<root>/<namespace>/<path…>/<key>`, always at least three components, which is why a flat `--from-literal` mount cannot serve one. ⚠️ `defaultMode: 0440` **plus** pod `fsGroup: 1000` are both required and were measured, not chosen — a Secret volume is root-owned and cannot be chowned to the workload, so a non-root pod reaches it as group or as world, and the resolver refuses world-readable credentials outright. Keep the `sources` list identical in both deployments: a credential projected into only one is a device that resolves on the gateway's path and fails on the worker's. |
| `StatefulSet` | `redis` | Single Redis 7 instance with AOF persistence |
| `Service` | `redis` | ClusterIP on port 6379; accessible to gateway and worker pods |
| `PersistentVolumeClaim` | `redis-data` | Persists Redis AOF data across pod restarts |
| `Deployment` | `device-mcp-gateway` | Stateless gateway — scale freely; readiness on `/readyz` |
| `Deployment` | `device-mcp-worker` | Stateful workers — scale independently; liveness via Redis heartbeat key |
| `Service` | `device-mcp-gateway` | ClusterIP on port 8000; target of the Ingress |
| `Ingress` | `device-mcp-gateway` | External HTTPS entry; TLS termination |
| `NetworkPolicy` | `default-deny-all` | **Denies all ingress and egress for every pod in the namespace.** The policy that actually provides isolation; the rest re-open one path each. Selects `podSelector: {}`, because NetworkPolicy is pod-selected and a pod matched by no policy is default-allow (F-68) |
| `NetworkPolicy` | `allow-dns-egress` | Egress to kube-system DNS. Without it *everything* breaks, presenting as an application that cannot resolve rather than as a network fault |
| `NetworkPolicy` | `allow-intra-namespace` | The stack talking to itself (gateway ↔ Redis ↔ worker). Scoped to this namespace only |
| `NetworkPolicy` | `allow-monitoring-scrape` | Ingress from the monitoring namespace to `:9100` only. The **one** standing cross-namespace path, and load-bearing for [ADR-0013](adr/0013-two-plane-tenancy-and-the-provider-plane.md) §7 |
| `NetworkPolicy` | `device-mcp-gateway` | Ingress from the ingress-controller namespace to `:8000`; egress to device APIs by port **and peer** |
| `NetworkPolicy` | `device-mcp-worker` | No API ingress; egress to device APIs. In distributed mode this is the policy that decides whether a device is reachable |
| `NetworkPolicy` | `redis` | Ingress on `:6379` from gateway and worker pods only |
| `PodDisruptionBudget` | `device-mcp-gateway-pdb` | `minAvailable: 1` for gateway |
| `PodDisruptionBudget` | `device-mcp-worker-pdb` | `minAvailable: 1` for worker |
| `PersistentVolumeClaim` | `gateway-data` | **Optional.** Embedded-mode only — SQLite persistence for gateway pod. Not applied by default. |

---

## Sample Device Registrations

Register the two devices shown in the diagrams after the gateway is running. Replace `mcp-gateway.example.com` with your actual hostname.

**Sensor API** — API key authentication:
```bash
curl -X POST https://mcp-gateway.example.com/v1/devices \
  -H "Authorization: Bearer <gateway-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "hostname":   "sensor-api",
    "base_url":   "http://sensor-svc:8080",
    "auth_type":  "api_key",
    "auth": { "api_key": "sensor-key-123", "header_name": "X-API-Key" }
  }'
```

**Actuator API** — OAuth2 client credentials:
```bash
curl -X POST https://mcp-gateway.example.com/v1/devices \
  -H "Authorization: Bearer <gateway-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "hostname":  "actuator-api",
    "base_url":  "http://actuator-svc:9090",
    "auth_type": "oauth2",
    "auth": {
      "token_endpoint": "https://auth.example.com/token",
      "client_id":      "actuator-client",
      "client_secret":  "secret",
      "scopes":         ["actuators:read", "actuators:write"]
    }
  }'
```

In distributed mode, the gateway immediately returns `{ pod_active: false }` — the pod becomes active asynchronously as a worker picks up the assignment. Poll `GET /v1/devices/{hostname}` until `pod_active: true`.

---

## Deploying with the Provided Manifests

**Cluster prerequisites:** an ingress controller (the manifests use ingress-nginx),
metrics-server (the CPU HPAs need it), and a default StorageClass (Redis PVC). The
Prometheus Operator is optional and **not** required by the default apply — `prometheus-rules.yaml`
and `servicemonitor.yaml` are excluded from `kustomization.yaml` so a cluster without the
CRDs still applies cleanly. See the README's "Cluster prerequisites" table.

```bash
# 1. The image is already pinned by DIGEST to a published multi-arch GHCR image, so there
#    is nothing to edit for a default install. To retarget it (different version, your own
#    registry, or a locally built image), edit the `images:` block in kustomization.yaml
#    ONCE — it applies to both the gateway and the worker, which share one image and must
#    stay in lockstep across a Redis schema change.
#      docker buildx imagetools inspect ghcr.io/benwold-lgtm/device-mcp-gateway:0.3.8
#    (Building from source: `docker build -t <your-registry>/device-mcp-gateway:0.3.8 .`,
#    push it somewhere the cluster can pull from, then set newName/newTag in that block;
#    kind/minikube: skip the push, `kind load docker-image my-gateway:dev`, and DROP the
#    digest — a digest pin will not resolve against a locally built image.)
#    Also customise:
#    deploy/kubernetes/ingress.yaml       — replace mcp-gateway.example.com
#    deploy/kubernetes/worker-deployment.yaml — adjust replicas and resources

# 2. Create namespace and secrets
#    Distributed mode REQUIRES an API key (else the gateway refuses to start — Tier-0 F-23)
#    and an authenticated Redis (redis-password + a redis-url that carries it — Tier-0 F-24).
kubectl create namespace mcp-gateway
REDIS_PW=$(openssl rand -hex 24)
kubectl create secret generic gateway-secrets \
  --namespace=mcp-gateway \
  --from-literal=api-key=$(openssl rand -hex 32) \
  --from-literal=secret-key=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  --from-literal=redis-password="$REDIS_PW" \
  --from-literal=redis-url="redis://:$REDIS_PW@redis:6379/0" \
  --from-literal=admin-key=$(openssl rand -hex 32) \
  --from-literal=viewer-key=$(openssl rand -hex 32)   # use rediss:// when Redis terminates TLS
#    admin-key / viewer-key are the break-glass RBAC role keys (MCP_ADMIN_KEY /
#    MCP_VIEWER_KEY). They are wired `optional: true`, so a stack running purely on OIDC
#    needs neither — but the gateway warns if OIDC is enabled with no break-glass key,
#    because an IdP outage would otherwise lock you out of your own gateway.
#    ⚠️ These are Secret KEY names, not env var names. Asking for `{.data.MCP_ADMIN_KEY}`
#    returns an empty string that base64-decodes happily into a `Bearer ` with no token,
#    which then presents as a 401 rather than as your own mistake.
#    The Ingress also needs a TLS secret (mcp-gateway-tls) — issue it via cert-manager
#    or create one manually (`kubectl create secret tls mcp-gateway-tls ...`).

# 3. Deploy everything
kubectl apply -k deploy/kubernetes/

#    ⚠️ The bundle is default-deny in both directions (ADR-0014). If your devices are on
#    a port outside 80/443/8080/8443, your ingress controller is not in `ingress-nginx`,
#    or Prometheus is not in `monitoring`, edit networkpolicy.yaml before this step — and
#    verify the pod/service CIDRs in its egress rules against YOUR cluster:
#      kubectl cluster-info dump | grep -E 'cluster-cidr|service-cluster-ip-range'

# 3a. Verify the policies are actually ENFORCED — a CNI that does not implement
#     NetworkPolicy accepts all of them and enforces none, and `kubectl get netpol`
#     looks identical either way. This must print 000/BLOCKED, never a 200:
kubectl create namespace netpol-check
kubectl run probe -n netpol-check --rm -i --restart=Never --image=curlimages/curl:8.11.1 -- \
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 \
  http://device-mcp-gateway.mcp-gateway.svc.cluster.local:8000/health
kubectl delete namespace netpol-check

# 4. Watch rollouts
kubectl rollout status deployment/device-mcp-gateway -n mcp-gateway
kubectl rollout status deployment/device-mcp-worker -n mcp-gateway

# 5. Scale
kubectl scale deployment device-mcp-gateway --replicas=3 -n mcp-gateway
kubectl scale deployment device-mcp-worker --replicas=3 -n mcp-gateway
```

### Kustomize overlays (optional)

Create environment-specific overlays to patch image tags, resource limits, or replica counts without modifying the base manifests:

```
deploy/
  kubernetes/
    base/        ← move current files here when using overlays
    overlays/
      staging/
        kustomization.yaml   # patches image tag, sets lower limits
      production/
        kustomization.yaml   # patches image tag, sets production limits, HPA
```
