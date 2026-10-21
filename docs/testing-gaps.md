# Testing gaps — what we have not empirically validated

This document exists to stop **"designed and reasoned about"** from being quietly read as
**"verified."** Everything listed here is implemented and unit-tested, but has never been
exercised against the conditions it was built for, because doing so needs infrastructure
this project does not have.

That distinction matters most exactly where it is easiest to lose: a resilience feature
that has never seen a real fault is a hypothesis. Recording the gap is cheap; discovering
it during an incident is not.

**This is a living document.** When you find something you cannot properly test, add a row
— see [Adding an entry](#adding-an-entry). Removing a row requires evidence, not
confidence: link the run, the baseline, or the recording that closed it.

## Status at a glance

| ID | Gap | Blocked on | Risk if the design is wrong |
|----|-----|-----------|------------------------------|
| [TG-1](#tg-1--chaos--fault-injection-f-63) | Chaos / fault injection (E1–E10) | A live multi-node platform + fault injector | Documented failure-mode mitigations may not behave as written under real faults |
| [TG-2](#tg-2--scale-and-performance-baseline) | Scale + performance baseline | Multi-node cluster, a realistic upstream, sustained load capacity | No capacity model; HPA thresholds and pool sizes are educated guesses |
| [TG-3](#tg-3--ha-redis-failover) | HA Redis failover behaviour | A real Sentinel/Cluster deployment able to promote a replica | The retry/health-check settings may not cover a real election window |
| ~~[TG-4](#tg-4--the-kubernetes-manifests-on-a-real-cluster--closed)~~ | ~~The k8s manifests on a real cluster~~ | **CLOSED 2026-08-06** — see below | — |
| [TG-5](#tg-5--the-arm64-image-on-real-arm64-hardware) | arm64 image on real arm64 hardware | A Pi / arm64 host | QEMU-built arm64 layers can pass build and still fail at runtime |
| ~~[TG-6](#tg-6--hash-reading-redis-paths-in-the-unit-tier--closed)~~ | ~~Hash-reading Redis paths in the unit tier~~ | **CLOSED 2026-08-24** — fakeredis 2.37.1 decodes; floor raised, four workarounds removed. See below | — |
| ~~[TG-7](#tg-7--disaster-recovery-restore-into-a-genuinely-fresh-stack--closed)~~ | ~~DR: restore into a genuinely fresh stack~~ | **CLOSED 2026-08-11** — walked end to end; see below | — |
| [TG-8](#tg-8--backup-and-restore-at-fleet-scale) | Backup/restore at fleet scale | A fleet in the hundreds with workers under real assignment pressure | Export is one synchronous response over the whole registry; it may time out precisely on the fleets where the archive matters most |
| [TG-9](#tg-9--backup-across-a-key-rotation-on-a-live-distributed-stack) | Backup across a live key rotation | A rolling restart with overlapping `MCP_SECRET_KEY` sets | Double-encrypted credentials in an archive that verifies clean; surfaces only when a restored device authenticates upstream |
| [TG-10](#tg-10--network-isolation-on-a-cni-that-is-not-cilium) | Network isolation on a CNI that is not Cilium | A cluster with a different CNI (or one that ignores NetworkPolicy) | A non-enforcing CNI accepts every policy and enforces none; `kubectl get netpol` looks identical either way |

---

## TG-1 — Chaos / fault injection (F-63)

**What is unvalidated.** Every resilience claim in
[failure-modes.md](failure-modes.md): worker-kill reassignment, Redis flap recovery,
blast-radius containment on a slow upstream, breaker open/half-open transitions, lease-flap
convergence, a client's retry after a gateway kill, and degraded-mode behaviour at zero workers.

**Why we cannot test it here.** The experiments need a live multi-node platform plus a fault
injector (Toxiproxy or equivalent) sitting between the gateway, Redis and the upstreams.
Killing pods and partitioning a network are not things a single-box test suite can do
honestly — a mocked partition proves the mock, not the system.

**What exists instead.** A written experiment plan, **E1–E10**, with a predicted-behaviour
table and the specific metric each experiment should move. It is analysis, not evidence, and
the [CHANGELOG](../CHANGELOG.md) known-limitations section says so.

**What would close it.** Run E1–E10 against a real deployment and record the observed
behaviour beside the predicted one. Disagreements are the valuable output — a prediction
that holds teaches less than one that does not.

**Prerequisite.** [TG-2](#tg-2--scale-and-performance-baseline) first: chaos results are
uninterpretable without a healthy baseline to compare against.

## TG-2 — Scale and performance baseline

**What is unvalidated.** Throughput and latency under load, and therefore every number
derived from them: connection-pool sizes, the HPA thresholds, the admission-control
watermark, `max_concurrent_calls_per_device` / `_per_worker`, and the pub/sub pool
sizing (one connection per waiting request). These are reasoned defaults, not measured ones.

**Why we cannot test it here.** A meaningful baseline needs a multi-node cluster, an upstream
that behaves like a real API (latency distribution, rate limiting, connection limits), and
enough sustained load to reach saturation. Single-box numbers mostly measure the box.

**What exists instead.** A runnable load harness ([tools/loadtest/](../tools/loadtest/)) and
a methodology with a results template in [load-testing.md](load-testing.md). The template
has **no rows filled in** — that emptiness is the gap.

**What would close it.** Fill in the baseline table for at least one realistic environment,
recorded *with* its environment — a number without its context cannot be compared against a
later one. Then revisit the tuning defaults above against the measurements.

## TG-3 — HA Redis failover

**What is unvalidated.** Whether the client's failover handling actually covers a real
primary election. Redis is the entire distributed control plane, so this is the single
highest-consequence gap on the list.

**Why we cannot test it here.** It needs a genuine Sentinel or Cluster deployment that can
promote a replica. **A `docker restart` of a single Redis is not a substitute, and it is
worth being precise about why:** a clean container restart closes sockets cleanly, so
redis-py's pool simply discards dead connections and reconnects on next use. Both the old
unconfigured client *and* the current one recover from it identically — the scenario cannot
distinguish them, so passing it proves nothing about failover. The conditions that actually
exercise the new settings are a half-open connection left by an abrupt primary loss, and a
command issued *during* the election window.

**What exists instead.** Unit tests asserting the retry *policy semantics* — that transient
connection errors and timeouts recover, that a permanent outage still surfaces, that the
budget is finite — plus health-check and connect-timeout settings. The policy is tested; the
event is not.

**Do this while you are here.** A Sentinel client is built with `master_for()`, which takes
its own connection kwargs — and `decode_responses` is exactly the one that gets forgotten,
because the single existing construction site passes it as a literal and nothing enforces it.
[TG-6](#tg-6--hash-reading-redis-paths-in-the-unit-tier--closed) measures what that costs: every
device read returns `None` and the fleet endpoint reports **empty**, with a warning blaming a
delete that never happened. Adding an HA path is the change that arms it, so the guard TG-6
defers belongs in the same piece of work.

**Specifically unverified:**

- Whether the default retry budget (~2.5s, jittered) covers a real election. It is
  deliberately sized *not* to block through a long Sentinel promotion, so some hard errors
  during a slow failover are expected behaviour — but nobody has watched where that line
  actually falls.
- Whether jitter meaningfully prevents a reconnect thundering herd across many replicas and
  workers reconnecting at once. This is the reason jitter is there.
- Whether `health_check_interval` catches half-open pooled connections before real traffic
  does.

**What would close it.** Stand up Sentinel or a managed HA Redis, force a promotion under
load, and record: error count and duration during the window, reconnect latency distribution
across replicas, and whether a retried command ever double-executed (the idempotency guard
should absorb it — confirm that it does).

## TG-4 — The Kubernetes manifests on a real cluster ✅ CLOSED

**Closed 2026-08-06.** Deployed on a 3-node kind cluster (k8s 1.36) with **Cilium 1.19.5**
as the CNI — chosen because kind's default CNI silently ignores `NetworkPolicy`, which would
have made every policy result vacuous. Enforcement was verified *first* (HTTP 200 before a
deny-all, connection timeout after) so the rest of the run means something.

**What ran:** 2 gateway replicas, 2 workers, Redis, and both device kinds — a translated
OpenAPI device against a real vendor API over TLS, and a proxied MCP upstream — through to
healthy pods, passing probes, and successful tool calls via the ingress. NetworkPolicy,
ConfigMap/Secret wiring, probe timing, ordering and ingress admission all exercised.

**What it found**, none of which static checks could:

- A cold-path spec-fetch branch that left an MCP device registered, reachable, and without a
  pod (fixed, with a regression test that seeds nothing).
- **Tool-change governance never ran in distributed mode** — the `spec_hash` baseline was
  never written, so F-41 could not fire for any device (fixed; see the CHANGELOG).
- The worker egress `NetworkPolicy` allows only 53/6379/80/443/8080/8443, so a device on any
  other port is unreachable until the policy is edited. **Documented 2026-08-06** — both
  policies now say they are an allowlist to extend, and the
  [runbook](runbook.md#registering-a-device-returns-400-rejected-base_url--spec_url) gives the
  symptom (a timeout that never names the policy) and a one-liner to confirm it. The
  behaviour is unchanged and intentional; what was missing was any way to find out.
- Workers `exit(1)` on the first Redis connection failure at startup instead of retrying;
  kubelet backoff masks it. **Fixed 2026-08-06** — both the gateway and the worker now wait
  for Redis with jittered backoff up to `redis.startup_timeout`, then still fail hard so a
  genuinely dead Redis reaches the probes.
- In-cluster upstreams require `security.allow_private_targets`, which is correct behaviour
  but undocumented for k8s users — and the shipped ConfigMap's `security:` block is commented
  out, so a naive YAML patch of it silently does nothing. **Fixed 2026-08-06** — the block
  ships live with the key set explicitly, so adding a sibling key works as an operator
  expects, and the runbook covers the Service-DNS case (including that the *worker* needs the
  override too, since it does the fetching).
- The fleet endpoint answers `initialize`/`ping`/`tools/list` inline on the POST body but
  delivers `tools/call` on the SSE stream — and in embedded mode delivers all four on the
  stream. Both shapes are legal MCP, but the split cost a debugging round and was written
  down nowhere. **Documented 2026-08-06** in the [README](../README.md#multiple-devices-in-one-session-fleet).
  **Resolved for the new transport 2026-08-08**: `POST /v1/fleet/mcp` (Streamable HTTP) has no
  stream to defer anything onto, so every method — `tools/call` included — answers on the POST
  that asked, identically in both modes. **Gone in 0.4.0** with the SSE fleet route itself.
- `pod_active: true` can briefly coexist with a refusal while a worker is terminating,
  because the lease and the flag converge rather than moving together. Self-resolves in
  seconds. **Documented 2026-08-06** as a [runbook symptom](runbook.md#pod_active-true-but-the-device-answers-503-transient-after-a-worker-roll),
  so it is not mistaken for a broken device. (It was a 404 on `/sse`; since 0.3.8 it is a 503
  with `Retry-After`, and `/sse` is gone in 0.4.0.)
- TLS trust was **fleet-global** — one `ca_bundle` per process, so a self-signed device forced
  that trust set onto every outbound call the process made. **Fixed 2026-08-10**:
  `security.mtls.devices.<hostname>` overrides any of the five mTLS keys for one device,
  inheriting the rest; devices it does not name are unaffected. Every declared profile is built
  at startup, so an unreadable CA or a misspelt key stops the process instead of surfacing hours
  later as one device failing. See [security-mtls.md](security-mtls.md#per-device-trust).

  **The constraint this was built under, and kept.** Every trust decision is an
  `ssl.SSLContext` from `security/mtls.py` — `ssl.create_default_context(cafile=…)` — so
  chain building and name-constraint checking happen in **OpenSSL via the stdlib**, and
  `cryptography` is used only for Fernet. That is what keeps three standing `cryptography`
  advisories unreachable, two of which are exactly this surface: `PYSEC-2026-3553`
  (path-building DoS on duplicated self-signed intermediates) and `PYSEC-2026-3554` (a
  wildcard SAN escaping `permittedSubtrees`). Both are in `cryptography`'s
  `x509.verification` API.

  Per-device trust is therefore **one `SSLContext` per profile**, not a migration to
  `x509.verification` (`PolicyBuilder`, `Store`, the verifiers) — which would have made both
  advisories reachable on the device-facing path, and a name-constraint escape is precisely
  the failure a per-device trust feature exists to prevent. The advisories stay unreachable
  and `cryptography>=49` remains an optional bump rather than a prerequisite; see
  [dependency-advisories.md](dependency-advisories.md#standing-triage--2026-08-06).

  **How it was proven.** A two-server handshake against certificates signed by the same
  private CA: the named device verifies and connects, the unnamed one does not. The test
  carries a positive control asserting that the *same* call succeeds under the old
  fleet-global configuration — without it, a broken TLS harness would pass the test for the
  wrong reason. Three mutations (drop the overlay, drop the eager preflight build, pool one
  client for all profiles) were each confirmed to fail the suite.
- Unknown tool arguments are silently ignored rather than rejected (generated schemas carry no
  `additionalProperties: false`), so a hallucinated argument reads as success. **Fixed
  2026-08-06** — generated schemas are closed, which is a statement of fact rather than a
  strictness preference: the translator lists every argument the dispatcher can place, and
  anything else was being dropped anyway. Proxied MCP schemas are left as the upstream
  published them.

**Harness caveat, not a product finding.** Cilium's `cni.exclusive=true` removes kind's
chained `portmap` plugin, so `hostPort` ingress does not work on kind. Production uses
LoadBalancer/NodePort; the ingress routing itself (Host matching, TLS redirect, backend
selection, and a wrong Host correctly 404ing) was verified through a port-forward.

**Not closed by this:** [TG-3](#tg-3--ha-redis-failover). Redis here was a single StatefulSet,
not Sentinel, so nothing about failover was exercised.

## TG-5 — The arm64 image on real arm64 hardware

**What is unvalidated.** That the published `linux/arm64` image runs on an actual arm64 host.
The audience for arm64 is now largely [SyncGate Lite](https://github.com/benwold-lgtm/SyncGate-Lite),
which is a separate repo — but the image is built *here*, so the gap stays here too. Splitting the
product did not split the risk.

**Why we cannot test it here.** No arm64 hardware; the image is cross-built on an amd64
runner under QEMU emulation.

**What exists instead.** A successful multi-arch build and push. A QEMU-built layer can
build cleanly and still fault at runtime on real silicon, typically in a native dependency.

**What would close it.** Bringing a stack up on a Pi or other arm64 box — SyncGate Lite's
compose is the smallest way to do it — through to a registered device and a successful tool call.

## TG-6 — Hash-reading Redis paths in the unit tier — CLOSED

**✅ CLOSED 2026-08-24 — fakeredis 2.37.1 decodes hash *and* stream replies.** Measured
across every construction path, and against `RedisRegistryBackend.get_device`, which now
round-trips a device over the fake. The floor in `pyproject.toml` is raised to `>=2.37.1` so
the fix is guaranteed rather than incidental, and the four production workarounds are gone
(`breakglass.py`, `session_router.py` ×2, `worker/dispatch.py`), along with the hand-written
`_StubRedis`.

**How it was found is the part worth keeping.** A tripwire test asserting the quirk still
existed failed on its first CI run — because **CI had floated to 2.37.1 while a local venv sat
on 2.36.0**. The two environments disagreed about a behaviour four pieces of production code
depended on, and the pin (`>=2.26,<3.0.0`) allowed both. Nothing else would have reported it:
the workarounds were written to tolerate *either* behaviour, so both environments passed.

That is [[feedback-resync-the-venv]] again, inverted — the usual failure is local drift making
a fixed bug look present; here it also meant a gap documented as current had quietly closed
upstream and nobody knew. **A dependency whose behaviour code depends on needs a floor, not a
range.**

The historical account follows, since the reasoning about what a double can and cannot show
outlived the quirk.

---

**What was unvalidated.** Nothing, in the end — but not where you would expect, and the
detail matters to anyone adding a test.

**The trap.** `fakeredis` 2.36 does **not** honour `decode_responses=True` for `hgetall`,
while honouring it for `get`, `smembers` and the rest. Hash reads come back with `bytes`
keys and values:

```
get        -> 'v'
smembers   -> {'m'}
hgetall    -> {b'a': b'1'}
```

Every `DeviceConfig.from_redis_hash` consumer is therefore unreachable under fakeredis, so
`RedisRegistryBackend.get_device` — and anything downstream of it, including the worker's
`_spawn_pod` config read — **cannot be unit-tested against the fake.** This is easy to
mistake for a bug in the code under test; it is not.

⚠️ **The symptom changed, and got quieter (measured 2026-08-24).** This section used to say
the consumer raises `KeyError: 'hostname'`. It no longer does: `_config_or_none`, added with
the R6 zombie-device fix, catches a hash with no `hostname`, logs "left over from a delete
that raced a field update", and returns `None`. That guard is right for the case it was
written for and wrong for this one — **an undecoded hash has *every* field and no
`hostname`, so it takes the same branch.** A loud failure became a silent one. Nothing is
currently harmed by that (see below), but a fix that quiets an unrelated failure mode is
worth noticing when it happens.

**Three tripwires now assert the quirk still exists** (`tests/test_fakeredis_assumptions.py`).
They are meant to fail one day; when they do, the fix is to delete them along with the four
production workarounds they justify, not to adjust them.

**What exists instead.** CI runs a real `redis:7-alpine` service and the integration tier
(`tests/test_integration_redis.py`) exercises these paths for real, so they are covered —
just not by the unit tier. A test needing a device config from a backend should either use
`MemoryRegistryBackend` (when the point is the logic, not the serialisation) or the
`real_redis` fixture (when the point *is* the serialisation).

**What would close it.** A fakeredis release that decodes hash replies, or a small decoding
wrapper in `conftest.py`. Neither is urgent while CI has a real Redis; the cost of the gap
is confusion, not missing coverage.

**Measured 2026-08-24 — the same failure on real Redis, and how far it can reach.** A real
client built without `decode_responses=True` behaves identically, so this is a property of
the code, not of the double: `get_device` returns `None` for every device while
`list_hostnames()` returns `[b'...']`, and `GET /v1/devices` reports an **empty fleet** for a
registry holding 27 fields per device — with a per-device warning blaming a delete that never
happened.

It is not reachable in production today, which is why the guard below is deferred rather than
built. Checked, not assumed: there is exactly **one** client construction site
(`shared/redis_client.py`), it passes `decode_responses=True` as a literal, a URL query
parameter cannot override it on redis-py 8.1 (`?decode_responses=false` still yields `str`),
and no Sentinel path exists to construct a second client differently.

**What would arm it**, and therefore what to do this alongside: a second construction site —
an HA/Sentinel client is [TG-3](#tg-3--ha-redis-failover), and `master_for()` takes its own
connection kwargs, which is exactly where `decode_responses` gets forgotten; a redis-py change
in kwarg-versus-URL precedence, which has differed across versions and which nothing of ours
pins; or a directly injected client, which `RedisRegistryBackend(redis_client: Any)` accepts.

**Deferred fix, to land with TG-3 — unaffected by this closure.** It is about a misconfigured *real* client, not about the fake. Teach `_config_or_none` to tell the two cases apart: a
raced delete leaves one or two fields, an undecoded hash leaves all of them with `bytes` keys.
The second is a misconfigured client and should say so loudly, rather than reporting an empty
fleet.

**Risk if the design is wrong.** Low for correctness, real for velocity: the failure mode
looks like a product bug, and the natural "fix" is to weaken the test.

**Demonstrated 2026-08-10 — this stopped being hypothetical.** The R6 defect
([failure-modes.md](failure-modes.md) §2) was exactly this class: a deleted device could be
resurrected as a partial hash by a worker still writing to it, and every read of that
hostname then raised `KeyError: 'hostname'` out of `from_redis_hash` as a 500. An `hgetall`
consumer, invisible to the unit tier, found on a live cluster.

Two things this confirms about the gap as written. First, the *risk* line understates it:
the cost was not only velocity — a real defect shipped and reached a cluster. Second, the
stated workaround held. The fix's tests use the `real_redis` fixture and perform the actual
delete-then-update sequence rather than pre-seeding a partial hash, because a pre-seeded
fixture would only prove the decoder tolerates wreckage while saying nothing about whether
the write path still creates it — see
[tests/test_deleted_device_stays_deleted.py](../tests/test_deleted_device_stays_deleted.py).

So the gap is still open, and it is now known to be load-bearing rather than merely
inconvenient. Anything that reads a device config from Redis needs a test in the
integration tier; the unit tier cannot see it fail.

## TG-7 — Disaster recovery: restore into a genuinely fresh stack ✅ CLOSED

**Closed 2026-08-11.** Walked end to end on a second physical host: a single-node kind
cluster with its own Redis, its own pods and a **different `MCP_SECRET_KEY`**, restoring a
**portable** archive exported from the lab cluster. Both devices restored, both provisioned,
and — the assertion that matters — a `tools/call` on the restored `prism` device returned
live upstream data (HTTP 200, real cluster inventory) using a credential that was encrypted
under one key, re-wrapped to a passphrase, and decrypted under another. The runbook written
from the walk is in [runbook.md](runbook.md#rebuild-a-stack-from-nothing-disaster-recovery).

**The restore itself was not the hard part.** Preflight, replay, manifest rebuild and
`tools_revision` carry-over all behaved exactly as [ADR-0011](adr/0011-backup-and-restore.md)
described. Three *environmental* dependencies broke it first, and all three are the same
shape: **things the archive does not carry and the design never claimed it would.**

1. **Per-device TLS material.** `security.mtls.devices.prism.ca_bundle` points into
   `/etc/mcp/tls`, backed by a ConfigMap that is neither in the archive nor in
   `deploy/kubernetes/`. The gateway **fails closed at startup** without it —
   `CrashLoopBackOff`, `ValueError: ... cannot build TLS context — [Errno 2]`. Correct
   behaviour, and unrecoverable-looking if you have not seen it before.
2. **Three environment variables that exist only as hand-applied additions** on the source
   cluster: `MCP_ALLOW_PRIVATE_TARGETS`, `MCP_ADMIN_KEY`, `MCP_VIEWER_KEY`. The repo
   manifests wire none of them. Missing the first makes restore refuse every private-address
   device **and report it as a correct policy refusal** — a configuration gap that reads,
   in the response body, exactly like the system working as designed. Missing the second
   means no admin credential at all: 401, not 403.
3. **Non-Kubernetes DNS.** `prism.nutanix.local` resolves via a CoreDNS `hosts` block on the
   source cluster. Kubernetes service DNS in `base_url`/`spec_url` needed **no** archive
   editing — it resolves unchanged in any cluster with the same service names in the same
   namespace, which is worth knowing — but anything outside Kubernetes must be recreated.

**What this changes.** The gap was written expecting the archive format to be the risk. It
was not. The risk is the **out-of-band dependency set**, which was undocumented because
nobody had ever rebuilt from nothing. That list is now a table in the runbook, and it belongs
in the same breath as "back up `MCP_SECRET_KEY` separately."

**Not closed by this:** [TG-8](#tg-8--backup-and-restore-at-fleet-scale) (2 devices proves
nothing about 500) and [TG-9](#tg-9--backup-across-a-key-rotation-on-a-live-distributed-stack)
(no rotation was in flight). The DR stack built here is the natural place to run both.

<details>
<summary>Original entry, kept for the record</summary>

**What is unvalidated.** The claim backup exists to support: that an
[ADR-0011](adr/0011-backup-and-restore.md) archive can rebuild a **lost** stack. Not that it
parses, and not that it declines to overwrite a stack that already has the devices — that a
new stack, with new Redis and new pods and no prior knowledge of the fleet, ends up serving
the same devices with working credentials.

**Why we cannot test it here.** It needs a second, disposable stack: its own Redis, its own
gateway and worker deployments, its own `MCP_SECRET_KEY`, and no shared state with the
original. Restoring into `mcp-gw` cannot prove it — that stack already holds the devices, so
every path that matters is skipped rather than exercised. **Do not rehearse this on `mcp-gw`.**

**What exists instead.** Two partial checks, and it is worth being precise about what each
does *not* prove.

- A unit-tier export → wipe → restore roundtrip
  ([tests/test_backup_restore.py](../tests/test_backup_restore.py)) covering the wrong-key
  abort, the canary, `on_conflict`, the egress-policy refusal and `tools_revision` carry-over.
  It runs against a backend fixture, not a stack: nothing spawns a pod or calls a tool.
- A live dry run on `mcp-gw` (2026-08-11, v0.3.2) that returned both devices as
  `skipped / already registered`. That exercised the decrypt preflight against real
  ciphertext and confirmed `dry_run` defaults to true — and **nothing else**. Not one device
  was written. It is evidence about the preflight, not about recovery.

**What would close it.** Stand up an empty stack with a *different* `MCP_SECRET_KEY`, restore
a portable archive taken from another stack, and then verify the fleet **works** rather than
merely appears: pods spawn, the manifest rebuilds, a tool call against a restored device
succeeds using the restored credential. Write the runbook from what actually happened, and
record the step that was wrong the first time — there is always one, and it is the reason the
runbook is worth more than the design doc.

**Risk if the design is wrong.** Highest consequence on this list, because it is only ever
exercised on the worst day. A restore that half-works turns a recoverable outage into a
manual rebuild, and the failure is discovered under incident pressure with the original stack
already gone.

</details>

## TG-8 — Backup and restore at fleet scale

**What is unvalidated.** Every backup number at a realistic device count. Export builds the
whole registry into a **single synchronous response**, and restore replays devices through
the ordinary registration path one at a time. Both are only ever exercised at 2–3 devices,
where nothing that scales is visible.

**Why we cannot test it here.** A meaningful run needs a fleet in the hundreds and workers
under real assignment pressure. A synthetic registry of 500 rows would measure the encoder,
not the system: the interesting part is restore replaying registrations **while workers are
running**, which needs the workers.

**What exists instead.** Correctness tests at small N, and one measured figure — Argon2id at
`m=64 MiB, t=3, p=4` costs ~0.12s, **once per archive**. That cost is independent of fleet
size and is therefore the one number that does *not* need this gap closed. The per-credential
Fernet work, the response size, and the replay time all scale with N and are unmeasured.

**What would close it.** Export and restore a fleet of ~500 devices against a live stack and
record: archive size, export wall time, peak gateway memory, whether the request survives the
ingress/client timeout, restore wall time, and whether a bulk restore triggers a spawn storm
or trips admission control. If export cannot complete inside a normal HTTP timeout, that is a
design finding, not a tuning one — it means the endpoint shape is wrong.

**Risk if the design is wrong.** Backup silently becomes unavailable exactly where it matters
most: the larger the fleet, the more valuable the archive and the more likely the request
times out. A DR procedure that only works on small fleets is worse than none, because it was
tested and believed.

**Prerequisite.** [TG-2](#tg-2--scale-and-performance-baseline) — the same reason chaos needs
it. Restore timings are uninterpretable without a baseline for what the stack does when idle.

## TG-9 — Backup across a key rotation on a live distributed stack

**What is unvalidated.** How export and restore behave **mid-rotation**, when a stack holds
more than one `MCP_SECRET_KEY` and a rolling restart means gateway and worker pods can be
running different key configuration at the same moment.

**Why we cannot test it here.** The scenario is a rolling restart across two deployments with
overlapping key sets — a pod-scheduling behaviour, not a code path. A single process cannot
be in two key configurations at once, so the suite can only simulate the outcome it already
expects.

**What exists instead.** Codec-level unit tests
([tests/test_secret_rotation.py](../tests/test_secret_rotation.py)) covering `MultiFernet`
primary/secondary decryption, re-encryption under the new primary, and the SQLite and
real-Redis credential rotation paths. They establish that the *codec* is correct. They say
nothing about a stack that is half-rotated while an export runs.

This gap is recorded because the design already tripped over it once. Detecting
"already encrypted" with `codec.is_current()` is **wrong** mid-rotation: a credential under
the older key reads as plaintext and is encrypted a second time, producing an archive that
looks perfect and restores credentials that decrypt to ciphertext. The implementation uses a
`decrypt()` attempt instead — the point is that the naive check was plausible, and only the
live scenario distinguishes them.

**What would close it.** Run an export during a rolling `MCP_SECRET_KEY` rotation, and a
restore into a stack whose primary key has moved on since the archive was written. Verify
credentials decrypt to plaintext rather than to ciphertext — assert on the decrypted
*value*, not on the absence of an error, because double-encryption raises nothing.

**Risk if the design is wrong.** Silent and delayed. The archive verifies, the restore
reports success, and the damage only surfaces when a restored device tries to authenticate
upstream — by which point the archive is the sole remaining copy.

---

## TG-10 — Network isolation on a CNI that is not Cilium

**What is unvalidated.** That the [ADR-0014](adr/0014-tenant-namespace-naming-and-network-isolation.md)
policies are *enforced* on a cluster whose CNI is not Cilium — and, more sharply, that
anyone deploying them would find out if they were not.

**A CNI that does not implement NetworkPolicy accepts every object in
`deploy/kubernetes/networkpolicy.yaml` and enforces none of it.** The API server stores
them, `kubectl get netpol` lists all seven, `kubectl describe` prints the rules, and
traffic flows exactly as if the file did not exist. There is no error, no event, and no
status field distinguishing "enforced" from "inert". This is the F-68 failure shape one
level down: a control that looks present and measures as absent, where reading the
manifest cannot tell you which.

**What has been verified**, on `mcp-gw` (kind + Cilium v1.19.5, `enable-k8s-networkpolicy:
true`), 2026-08-12 — recorded so this row is not read as "network isolation is untested":

| Probe | Result |
|---|---|
| Pod in another namespace → gateway `:8000` | **blocked** (was `HTTP 200` before — the F-68 measurement) |
| Pod in another namespace → metrics `:9100` | **blocked** |
| Pod in another namespace → Redis `:6379` | **blocked** |
| Worker-labelled pod → neighbour tenant's **pod IP** `:80` | **blocked** (the `ipBlock` pod-CIDR `except`) |
| Worker-labelled pod → LAN device `:9440` | allowed |
| Worker-labelled pod → internet `:443` | allowed |
| `monitoring` namespace → `:9100` | allowed — ADR-0013 §7 read path intact |
| `monitoring` namespace → `:8000` | **blocked** — the scrape exception is metrics-only |
| Gateway `/health`, both devices, live health loop | healthy; `last_check_age_seconds` advancing, so checks are live rather than stale |

**Why we cannot test the rest here.** Only one CNI is available. The behaviours that differ
across CNIs are exactly the ones that matter: whether NetworkPolicy is enforced at all,
whether `ipBlock` `except` is honoured for pod-to-pod traffic (implementations vary — some
evaluate policy before SNAT, some after), and whether an `Egress` policy with no matching
rule fails closed. Calico, Flannel-without-Canal, kindnet, AWS VPC CNI and Cilium do not
agree on all three, and the project ships CNI-neutral manifests.

**Two residuals this leaves, both inside the Tier-1 design rather than the test:**

- **The `ipBlock` `except` list is address-based**, so it depends on the pod and service
  CIDRs being written correctly for the cluster. The shipped values are the kind/kubeadm
  defaults. Get them wrong and cross-tenant egress by pod IP is open, with nothing
  reporting it. Where the pod CIDR overlaps the range devices live on, `ipBlock` cannot
  separate them at all.
- **Tier 1 is an RBAC guarantee, not a network one** (ADR-0014 §5). NetworkPolicy is
  additive-allow, so a tenant who can create a policy in their own namespace re-opens the
  boundary by *adding* one — no deletion needed. Untested here because it needs a real
  provider/tenant RBAC split, not a policy.

**Tier 2 is now verified** (2026-08-12), so it is no longer part of this gap. With policies
from `tools/tenant_isolation_policy.py generate` applied to two labelled namespaces:
cross-tenant blocked in both directions, intra-namespace and internet egress intact, and —
the claim that matters — a tenant that created its own `podSelector: {}` allow-all
NetworkPolicy in its own namespace **still** could not reach the neighbouring tenant. The
coverage checker was confirmed to exit 1 on a deliberately deleted policy. Three earlier
policy shapes applied cleanly and enforced the wrong thing or nothing at all; see
[ADR-0014](adr/0014-tenant-namespace-naming-and-network-isolation.md) § Implementation notes.

**What would close it.** Apply the bundle to a cluster on a different CNI and re-run the
probe table above, asserting the same verdicts. The single most valuable assertion is the
first: **a cross-namespace connection must fail**, because that is the one a non-enforcing
CNI silently inverts. Note that this affects Tier 1 only — per ADR-0014 §8 a
provider-operated estate requires Tier 2, which pins the CNI to one with deny-rule
semantics, so the CNI-portability question is a single-tenant concern.

**Risk if the design is wrong.** Total and silent for the isolation property. Every
document, dashboard and review would report tenant isolation as present; a probe would show
it absent. This is the class of gap that gets discovered by an auditor or an incident rather
than by the suite — and unlike most rows here, the mitigation is cheap: one pod, one curl,
five minutes, on any cluster you actually deploy to.

---

## Adding an entry

Add a row to the table and a section, keeping the same five headings:

- **What is unvalidated** — the specific claim, not the feature name. "Worker reassignment
  completes within ~90s under load" is useful; "resilience" is not.
- **Why we cannot test it here** — the concrete missing resource. If it turns out something
  *could* be tested with what we have, that is a bug in this document; test it instead.
- **What exists instead** — the analysis, unit tests, or static checks standing in, and
  plainly what they do *not* cover.
- **What would close it** — the specific run and the evidence it should produce.
- **Risk if the design is wrong** — what breaks in production, so the row can be prioritised
  against everything else rather than sitting here indefinitely.

Two rules worth keeping:

1. **Be precise about what a partial test does and does not prove.** TG-3 is the model: a
   `docker restart` looks like a failover test and is not one. A gap recorded as "partly
   tested" without saying which part is worse than no row at all, because it reads as
   reassurance.
2. **Closing a row needs evidence, not confidence.** Link the run, the recorded baseline, or
   the incident. If the result contradicted the prediction, say so and keep the row until
   the design is fixed.
