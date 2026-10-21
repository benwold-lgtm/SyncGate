# Operations Runbook & Troubleshooting — Device MCP Gateway

The on-call companion to the [failure-mode matrix](failure-modes.md). That doc says
*what can break and how it's detected*; this one says *what to do when the pager goes
off* — triage steps and the exact commands to remediate. It is organized two ways:

1. **[Alert playbooks](#alert-playbooks)** — one entry per Prometheus alert in
   `deploy/kubernetes/prometheus-rules.yaml`.
2. **[Symptom troubleshooting](#symptom-troubleshooting)** — for reports that arrive
   without an alert ("a client says tool calls hang").

Plus the **[standard procedures](#standard-procedures)** the playbooks call into
(scale, rotate the secret key, work the DLQ, roll a restart).

Conventions used below:

```bash
KEY=...                       # a gateway API key with the needed scope
H=...                         # a device hostname
GW=https://gateway.internal   # gateway base URL
NS=mcp                        # Kubernetes namespace
```

Distributed mode is assumed (Redis + workers). Embedded mode is a single process —
most control-plane alerts don't apply; the troubleshooting section calls out which.

---

## Alert playbooks

Each alert below maps to a row in [failure-modes.md](failure-modes.md) (the `#` column).
Sev: 🔴 page · 🟠 ticket · 🟡 watch.

### `MCPNoLiveWorkers` 🔴 (W1)

No worker is scraping / all workers down → tool calls hang to timeout.

```bash
kubectl -n $NS get pods -l app=device-mcp-worker
kubectl -n $NS logs -l app=device-mcp-worker --tail=100 | grep -iE 'redis|assert_redis|refus'
```

- Workers crashlooping on the **F-24 Redis-auth gate** ("refusing to start … unauthenticated
  Redis") → the `redis-url`/`redis-password` secret is wrong or missing. Fix the secret,
  not the gate. See [Standard procedures → Fix the Redis secret](#fix-the-redis-secret).
- Workers up but `mcp_worker_pods` absent → they can't reach Redis or the metrics port
  isn't scraped. Check `kubectl -n $NS exec` → `redis-cli -a … ping` and the ServiceMonitor.
- Genuinely scaled to zero → [scale workers up](#scale-workers).

### `MCPToolCallErrorBudgetBurnFast` 🔴 / `…BurnSlow` 🟠

Tool-call success SLI (`ok/(ok+error)`) is burning the 99.5% budget. Fast = sharp
outage (page); slow = a leak (ticket).

```bash
# Which devices / error types dominate?
curl -s "$GW/metrics" | grep -E 'mcp_tool_calls_total|mcp_circuit_breaker_opens_total'
```

1. Is it one device or fleet-wide? One device + `MCPCircuitBreakersOpen` → upstream
   fault, work that device (below). Fleet-wide → look at Redis (`MCPNoLiveWorkers`,
   readiness) and a recent deploy.
2. Remember the **success-SLI caveat**: upstream client-fault **4xx counts as `error`**
   (documented in [observability.md](observability.md)), so a client sending bad args
   can burn budget without anything being "down". Check the access log for 4xx-heavy
   callers before declaring an incident.

### `MCPDispatchReliabilityLow` 🔴 (D4/D6)

`1 − (timeouts + dead_letters)/dispatched` < 99.9%. Calls are dispatched but not
completing — distinct from upstream errors.

```bash
curl -s "$GW/metrics" | grep -E 'mcp_tool_call_timeouts_total|mcp_dead_letter_total'
```

→ Usually pairs with `MCPDeadLetterGrowing` (pod-replace window) or a slow upstream
(`MCPCircuitBreakersOpen`). Work whichever co-fires.

### `MCPDeadLetterGrowing` 🟠 (D6)

Undeliverable calls are accumulating in `device:{H}:calls:dead` (typically "no active
pod" during a pod replace). Full procedure: [observability.md → Working a dead-letter
alert](observability.md#working-a-dead-letter-alert-f-10). Short form:

```bash
curl -H "Authorization: Bearer $KEY" "$GW/v1/devices/$H/deadletter"          # inspect
curl -X POST -H "Authorization: Bearer $KEY" "$GW/v1/devices/$H/deadletter/replay"  # replay once a pod is back
curl -X DELETE -H "Authorization: Bearer $KEY" "$GW/v1/devices/$H/deadletter"       # drain
```

Confirm a pod is actually serving the device (`GET /v1/devices/$H`) **before** replaying,
or the replay just dead-letters again.

### `MCPUndeliveredBacklogNearMaxlen` 🟠 (R3)

`mcp_worker_undelivered_calls` > 8000 — approaching the 10k stream MAXLEN where the
oldest undelivered calls are silently trimmed.

1. [Scale workers](#scale-workers) — the consumer group is behind.
2. Find the stuck consumer: `redis-cli -a … XINFO GROUPS device:$H:calls` (look for a
   consumer with a large `pending` / old `idle`).
3. Admission control (F-06) should already be shedding with 429s
   (`MCPAdmissionShedding`); if not, check `registry.call_backlog_limit` isn't `0`.

### `MCPAdmissionShedding` 🟡 (G3)

`mcp_calls_rejected_overload_total` rising — the gateway is fast-failing calls with
`429 + Retry-After` because a device's worker backlog passed the watermark. **This is
working-as-intended back-pressure, not an outage.** Scale workers / fix the slow upstream;
the shedding stops on its own once the backlog drains.

### `MCPCircuitBreakersOpen` 🟠 (D2)

`mcp_circuit_breaker_opens_total` rising — a device returned 5xx repeatedly and its
breaker opened (callers now get a fast 503 instead of a 15s hang).

```bash
curl -H "Authorization: Bearer $KEY" "$GW/v1/devices/$H/diagnostics"   # reachability, spawn_error
```

→ Fix the upstream. The breaker **auto-resets after 60s**; no operator action is needed
to close it. Repeated reopens = upstream still unhealthy.

### `MCPReconcilerLeaderAbsent` / `MCPGaugeLeaderAbsent` 🟠 (W4)

`sum(mcp_reconciler_leader) == 0` — no worker holds the reconciler/gauge lease, so
orphaned-device recovery (and gauge refresh) stalls.

→ Almost always a Redis-connectivity blip across all workers. Check worker→Redis
reachability; the lease re-elects with jitter (F-21/F-61) once any worker reconnects.
If it persists, restart one worker to force an election.

### `MCPReconcilerReassignmentChurn` 🟠 (W3)

`mcp_reconciler_reassignments_total` climbing — claim-leases are flapping (a GC pause or
Redis latency exceeding the claim TTL), causing device churn between workers.

→ Raise `registry.claim_ttl` and/or `registry.reconcile_orphan_grace_cycles` (hysteresis,
F-62), and investigate worker GC pauses / Redis latency. Churn is self-limiting but wastes
work and can briefly double-own a device.

### `MCPDeviceToolsBreakingChange` 🟠 (D7)

`mcp_device_tools_changed_total{breaking}` — a device's spec changed in a
backward-incompatible way; live clients pinned to the old tool surface will fail.

```bash
# What changed and who did it
curl -H "Authorization: Bearer $KEY" "$GW/v1/devices/$H" | jq '.tools_revision'
grep '"action":"device.tools_changed"' logs/audit.log | tail
```

→ Notify affected clients; clients re-poll `tools_revision` to pick up the new surface.
See [api-change-governance.md](api-change-governance.md).

---

## Symptom troubleshooting

Reports that arrive without a specific alert.

### "Tool calls hang, then time out (~30s)"

The call was accepted but never completed. In order of likelihood:

1. **No worker owns the device.** `curl .../v1/devices/$H` — is a pod assigned/reachable?
   If no live workers → `MCPNoLiveWorkers` path. If the device is unassigned, the
   reconciler should claim it; check `MCPReconcilerLeaderAbsent`.
2. **Backlog/shedding.** A 429 (not a hang) means admission control is shedding — scale
   workers. A true hang with backlog → `MCPUndeliveredBacklogNearMaxlen` path.
3. **Slow upstream.** `GET /v1/devices/$H/diagnostics` for reachability; a breaker that
   keeps reopening points at the device.
4. The client gets a structured timeout error carrying the **`rid`** — grep it in the
   access log (`rid=…`) to follow the exact call across gateway → worker.

### "A device shows `reachable: false` / its tools fail" (D1)

```bash
curl -H "Authorization: Bearer $KEY" "$GW/v1/devices/$H/diagnostics"
```

Check, in order: `base_url` reachable from a worker pod; the spec URL fetches and is
under `registry.spec_max_bytes` (F-09); credentials decrypt (no `MCP_SECRET_KEY`
mismatch — see [secret-rotation.md](secret-rotation.md)); `spawn_error` in the device
record names the failure.

### An MCP endpoint answers 503 "registered but not serving"

`POST /v1/devices/{hostname}/mcp` answers **503 with `Retry-After`** (0.3.8 on) when the device is registered but has no active pod. A 404 now
means only that the device is not registered.

The usual cause is a device moving between workers. A worker that stops cleanly hands its
devices off itself, so this lasts seconds; one that died without stopping leaves them until
its claims lapse and the reconciler reassigns them (≈1–2 minutes). If it persists with no
roll or worker loss in progress, the device itself is the problem:

```bash
curl -H "Authorization: Bearer $KEY" "$GW/v1/devices/$H" | jq '{reachable, pod_active, spawn_error, worker_id}'
```

and continue at ["A device shows `reachable: false`"](#a-device-shows-reachable-false--its-tools-fail-d1).

### "`pod_active: true` but the device answers 503" (transient, after a worker roll)

Expected during a worker rollout, and it **self-resolves in seconds** — do not start
deleting device records over it.

The device record's `pod_active` flag and the worker actually holding the device's lease
are two pieces of state that converge, not one atomic fact. While a worker is terminating,
its lease has not yet expired and the flag still reads true, but the pod that would serve
the call is gone — so a call in that window is answered 503 with `Retry-After`.

```bash
kubectl -n $NS rollout status deploy/device-mcp-worker    # is a roll actually in progress?
curl -H "Authorization: Bearer $KEY" "$GW/v1/devices/$H/diagnostics"   # watch worker_id change
```

`worker_id` moving to a different worker is the reassignment completing — the stopping
worker's own hand-off from 0.3.8 on, or the reconciler for a worker that died.
Retry the connection after that. If the 404 persists past a minute with no roll in
progress, it is not this — treat it as
["A device shows `reachable: false`"](#a-device-shows-reachable-false--its-tools-fail-d1)
and check `spawn_error`.

### "OIDC logins stopped working / everyone is on break-glass keys"

`mcp_oidc_validation_failures_total` is the signal, and the `reason` label says which
problem you have:

- **`jwks_unavailable` climbing** — the IdP or its JWKS endpoint is unreachable. The gateway
  is **still serving**, but only static break-glass keys authenticate; every federated user
  is locked out. This is the silent degradation the counter exists to expose. Check IdP
  reachability and egress policy from the gateway pods (the JWKS fetch goes through the SSRF
  guard, so an on-prem IdP on a private address needs `security.allow_private_targets`).
- **`invalid_token` / `bad_algorithm` climbing with no IdP problem** — usually someone
  probing with forged tokens. Cross-check the audit log for the source.
- **`expired` climbing** — clock skew between the IdP and the gateway more often than real
  expiry; `gateway.oidc.leeway` tolerates a little, but fix NTP rather than widen it.

The paired log line is a WARNING, rate-limited to one per minute with a suppressed count, so
a forged-JWT flood doesn't become a log flood. Absence of warnings is not absence of
failures — trust the counter.

### "The gateway won't start: weak API key"

Distributed mode refuses a static key that is short (<16 chars) or a common guessable value
(`admin`, `changeme`, …). These are full bearer credentials, and with OIDC enabled the same
key is the break-glass path that still works when the IdP is down.

Generate a real one — `openssl rand -hex 32` — and update the secret. For a trusted local
network only, `gateway.allow_weak_keys: true` overrides. Embedded mode warns instead of
refusing, so local dev is unaffected.

### "Redis failed over — what should I expect to see?"

The client absorbs the reconnect burst rather than surfacing it: commands retry with
**jittered** exponential backoff (`redis.retries`, default 5 — up to ~2.5s worst case), idle
pooled connections are health-checked (`redis.health_check_interval`, 30s), and a vanished
primary fails the TCP connect fast (`redis.socket_connect_timeout`, 5s) instead of hanging on
the OS default. Expect a brief latency bump, not a burst of errors.

Two limits worth knowing, so you don't misread what you see:

- **It is not sized to block through a long election.** A Sentinel promotion can take tens
  of seconds; the retry budget is ~2.5s by default, because stalling a request for 30s is
  worse than failing it and letting the caller retry. Hard `ConnectionError`s during a
  *long* failover are expected, not a bug. Raise `redis.retries` if you would rather wait.
- **It does not hide a dead Redis.** The budget is finite, so readiness probes and
  `MCPNoLiveWorkers` still fire on a genuine outage.

Note a deliberate trade-off: a retried command can execute twice if the failure landed after
the server ran it but before the reply arrived. That is safe here — the rate-limit `INCR`
only over-counts (fails closed), and dispatch writes are covered by
`registry.idempotency_guard` (on by default). If you turn that guard off, you are also opting
out of this protection.

### "Registering a device returns 400 Rejected base_url / spec_url"

The egress policy refused the target. The message says which rule:

- **"resolves to a blocked address"** — private/loopback/link-local. For a trusted LAN
  fleet set `security.allow_private_targets: true` (or `MCP_ALLOW_PRIVATE_TARGETS=true`).
- **"carries a non-HTTP service"** — the port is on the default denylist (22, 25, 3306,
  6379, 2375, …). The gateway only speaks HTTP to a device, so this is almost always a
  typo'd port. If the endpoint really is HTTP on that port, add it to
  `security.allowed_target_ports` — but note that setting the key at all switches to a
  **strict allowlist**, so list every port your fleet uses, including 80/443.
- **"is not in security.allowed_target_ports"** — you already set that allowlist and this
  port isn't in it.

Non-standard HTTP ports (8000, 8080, 8443) are allowed by default and need no config.

**On Kubernetes, expect to hit the first of those.** A device addressed by Service DNS
(`http://my-device.mcp-gateway.svc.cluster.local`) resolves into the service or pod CIDR,
which is private — so an in-cluster upstream is refused until you set
`security.allow_private_targets: true`. The shipped
[ConfigMap](../deploy/kubernetes/configmap.yaml) carries the key explicitly for this
reason. Set `MCP_ALLOW_PRIVATE_TARGETS=true` on the **worker** as well as the gateway: in
distributed mode the worker performs the fetch, so configuring only the gateway gets the
device registered and then fails it at health-check time.

**A device that registers cleanly and is still unreachable is usually the NetworkPolicy,
not the egress guard.** These are two independent controls and passing one says nothing
about the other. The shipped policies allow egress to 80/443/8080/8443 only, so a device
on any other port (a BMC on 623, Prism on 9440, an appliance on 8006) times out with
nothing naming the policy as the cause. Add the port to **both** policies in
[networkpolicy.yaml](../deploy/kubernetes/networkpolicy.yaml) — the worker's is the one
that matters in distributed mode:

```bash
kubectl -n $NS exec deploy/device-mcp-worker -- \
  python -c "import socket;socket.create_connection(('DEVICE_HOST', PORT), 5)"   # hangs = blocked
```

### "Clients are getting 429s"

Two different 429s — distinguish by the `Retry-After` and the metric:

- **Admission shedding** (`mcp_calls_rejected_overload_total`) — the device's worker
  backlog is too deep. Scale workers / fix the upstream.
- **Per-IP / per-principal rate limit** (F-16) — that *caller* exceeded its limit. Raise
  the limit if it's a false positive, else it's working as intended.

If the per-IP limit is tripping for *everyone at once* behind a proxy, the callers are
probably collapsing onto one bucket because the gateway is keying on the proxy's address
instead of theirs. That means either `gateway.trust_proxy_headers` is off, or
`security.trusted_proxy_cidrs` doesn't cover the address your proxy actually connects from
— the right-to-left `X-Forwarded-For` walk stops at the first untrusted hop, so an
unlisted proxy IP *is* the resolved client. Read the real address off the platform
(`kubectl get pod -n ingress-nginx -o wide`, `docker network inspect <net>`) and add that
range. Do not widen it to `0.0.0.0/0` to make the symptom go away — that hands every
client control of its own bucket again.

### "The gateway or worker won't start" (R2)

Distributed mode **fails closed** by design. Read the first error line:

| Refusal | Cause | Fix |
|---------|-------|-----|
| "refusing to start … no API keys" (F-23) | distributed mode with no auth configured | set an API key, or `gateway.allow_anonymous: true` only if you truly mean open access |
| "refusing … unauthenticated Redis" (F-24) | Redis URL has no password | [fix the Redis secret](#fix-the-redis-secret), or `redis.allow_insecure: true` for a trusted-network lab |
| "Refusing to start in distributed mode with a weak API key" | a static key is under 16 chars or a common guessable value | `openssl rand -hex 32` and update the secret; `gateway.allow_weak_keys: true` overrides for a trusted local network |
| "Refusing to start with gateway.trust_proxy_headers: true and no security.trusted_proxy_cidrs" | proxy trust enabled without saying which hops are yours — every client could then forge `X-Forwarded-For` and pick its own rate-limit bucket | list the ranges your proxy connects from (pod CIDR / LB range / `172.16.0.0/12` for Compose), or set `trust_proxy_headers: false` if there's no proxy in front. **Never** use `0.0.0.0/0` — that restores the bypass |
| "Invalid security.trusted_proxy_cidrs" | a malformed entry in the list | fix the CIDR the message names; entries are rejected rather than skipped, because a silently-dropped range re-opens the spoofing hole |
| config-validation **warnings** (F-50) | unknown/misplaced config keys | warnings don't block startup; fix the dotted path the warning names |

Do **not** reach for the bypass flags (`allow_anonymous`, `allow_insecure`) to clear a
prod alert — they disable a release-blocking control. They exist for local/lab only.

### "Encrypted credentials suddenly unreadable"

A `MCP_SECRET_KEY` change without rotation. The codec accepts **multiple keys** — add the
old key back (`MCP_SECRET_KEY="<new>,<old>"`) and the gateway decrypts again immediately.
Then run the zero-downtime [rotate procedure](#rotate-the-secret-key). If the key is lost
entirely, the at-rest credentials are unrecoverable — re-register the devices' credentials.

---

## Standard procedures

### Take a backup

Two archive kinds ([ADR-0011](adr/0011-backup-and-restore.md)). Pick by *what will need to
open it*, not by how sensitive it feels.

```bash
# Routine/scheduled: credentials stay encrypted under this stack's MCP_SECRET_KEY.
# Restores into this stack, or any stack sharing that key.
curl -s -H "Authorization: Bearer $KEY" "$GW/v1/admin/backup" -o fleet-$(date +%F).json

# Migration / key loss: credentials re-encrypted to a passphrase, key-independent.
# POST, not GET — the passphrase must not land in a URL, proxy log, or shell history.
curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"kind":"portable","passphrase":"<20+ chars>"}' \
  "$GW/v1/admin/backup" -o fleet-portable-$(date +%F).json
```

Add `"include_deadletters": true` mid-incident to capture undeliverable calls; they are
excluded by default because they are unbounded and mostly noise.

**An archive never contains `MCP_SECRET_KEY`.** That is the point of the ciphertext kind —
and it means a ciphertext archive alone will not rebuild a stack whose key is gone. Back
the key up out-of-band, or keep a portable archive.

Both kinds are audited (`backup.export` / `backup.export_portable`); the passphrase is
never logged.

**A ciphertext export returns `409` when no `MCP_SECRET_KEY` is set.** That is deliberate,
not a bug: with no key the archive would be labelled ciphertext and contain plaintext
credentials. Set a key, or take a portable archive.

**An archive never contains an OAuth2 refresh token**
([ADR-0018](adr/0018-device-credentials-by-reference.md) §3). It is excluded unconditionally
— it is a credential the *gateway* mints and rotates, which makes it runtime state rather
than a registration input, the same category as the claims, leases and sessions an archive
has always omitted. `client_secret`, `password`, `api_key` and every `*_ref` all still travel —
and where a device holds those by reference, what travels is the reference and not the secret.

Check `counts.needs_reconnect` on every export:

```bash
jq '.counts' fleet-$(date +%F).json
# { "devices": 50, "tool_changes": 12, "dead_letters": 0, "needs_reconnect": 3 }
```

A non-zero count is the number of `grant_type=refresh_token` devices that **will need a human
to re-authorize them** if this archive is ever restored. It is not a fault in the backup and
there is nothing to fix in it — a refresh token exists because somebody consented once, out
of band, and nothing an archive can carry re-mints one. Know the number before you need the
archive, not during the restore. (Devices on `client_credentials` or `password` are
unaffected and restore seamlessly.)

### Restore from a backup

**Preview first — the two calls are structurally separate routes** (ADR-0018 §6), not one
endpoint with a flag: `POST /admin/restore/preview` needs only `backup:read` and writes
nothing, `POST /admin/restore/apply` needs `backup:write` and is the destructive one.
Preview performs the same fail-closed preflight and the same per-device gates as apply, so
its report is a prediction rather than a parse — and because it costs no elevation, run it
as many times as you need while adjusting `on_conflict`.

**The apply must carry the `plan_token` its preview returned**, and it must be *this*
preview's token: change `archive`, `on_conflict`, `include_deadletters` or the passphrase
between the two calls and the apply is refused (`ERR_PLAN_STALE`, 409) rather than silently
applying something other than what you read. A token is valid for
`backup.plan_digest_validity_seconds` (default 7 days) — preview again if it has expired.

```bash
# 1. Preview. Reports per device: would_restore | skipped | failed. Capture plan_token.
resp=$(curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"archive\": $(cat fleet-2026-08-11.json)}" "$GW/v1/admin/restore/preview")
echo "$resp" | jq '.counts, .devices'
TOKEN=$(echo "$resp" | jq -r '.plan_token')

# 2. Apply — same archive/on_conflict as the preview, plus its plan_token.
curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"archive\": $(cat fleet-2026-08-11.json), \"on_conflict\": \"skip\", \"plan_token\": \"$TOKEN\"}" \
  "$GW/v1/admin/restore/apply" | jq '.counts'
```

A portable archive additionally needs `"passphrase": "..."`. `on_conflict` is
`skip` (default), `overwrite`, or `fail` — nothing silently overwrites live configuration.

**Reading the outcomes:**

| Outcome | Means |
|---|---|
| `restored` / `would_restore` | Replayed through the ordinary registration path |
| `restored_needs_reconnect` / `would_restore_needs_reconnect` | Replayed **and cannot authenticate** — a human must re-authorize it |
| `skipped` | Hostname already exists and `on_conflict=skip` |
| `failed` | This device did not pass registration — **see `reason`** |

**`restored_needs_reconnect` is a success that is not finished.** The device is registered,
reachable, correctly fingerprinted — and will fail its first tool call, because its OAuth2
refresh token was excluded from the archive and that token *was* its credential. The dry run
predicts this, so the count is knowable before you commit:

```bash
… "$GW/v1/admin/restore/preview" | jq '.needs_reconnect, [.devices[] | select(.outcome | test("needs_reconnect"))]'
```

Each such device stays flagged afterwards as `credential_state: "needs_reconnect"`, on both
`GET /v1/devices/{hostname}` and the fleet list — so a restore you walk away from is still
findable later. **It is not a health signal**: the device may be entirely reachable.

To clear it, re-authorize the device out of band and `PUT` the new credential:

```bash
curl -s -X PUT -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"base_url":"…","auth_type":"oauth2","auth":{…,"refresh_token":"<newly consented>"}}' \
  "$GW/v1/devices/<hostname>"
```

Supplying a credential is what clears the flag — there is no "mark reconnected" endpoint,
because the flag *means* "no human has supplied a credential". A PUT that changes something
else leaves it set, deliberately: otherwise a rate-limit edit would quietly mark a device
reconnected that nobody reconnected.

**A `failed` device is usually correct behaviour, not a broken restore.** Restore replays
each device through the real registration gates, so a device whose `base_url` the *current*
egress policy forbids is refused — the archive cannot be used to reinstate what a fresh
registration would reject (F-67). Check `reason`: if the policy tightened deliberately, the
device should stay out; if not, fix `security.allow_private_targets` /
`security.allowed_target_ports` and re-run.

**A `409` means nothing was written at all.** The preflight decrypt-tests the whole archive
— canary first, then every credential — before touching anything, so a wrong key or wrong
passphrase aborts the entire restore rather than half-applying it across two key
generations. The message names which:

- *"encrypted under a different `MCP_SECRET_KEY`"* — restore into a stack sharing that key,
  or use a portable archive.
- *"needs the passphrase it was exported with"* — portable archive, add `passphrase`.

**Check `credential_warnings` and `credential_store_error` too.** A restore resolves every
archived `credential_ref` before writing anything, so the dry run already knows:

```bash
… "$GW/v1/admin/restore/preview" | jq '.credential_store_error, .credential_warnings,
     [.devices[] | select(.credential_warning) | {hostname, credential_warning}]'
```

| What you see | What it means | What to do |
|---|---|---|
| `credential_store_error` set | **The secret store itself** is unusable on this stack — unmounted, wrong path, or wrong ownership. Every by-reference device is affected, so per-device results are deliberately **not** produced | Fix the store and re-run. Do not go checking individual references — one mount is wrong, not N references (ADR-0018 §7) |
| `credential_warnings: N` | N devices reference a secret that does not exist **in this stack's store**. The store is healthy and answering "no" | Provision those secrets, then the next dispatch picks them up. No re-registration needed |
| both quiet | Every reference in the archive resolves here | — |

A device with an unresolved reference **still restores**, deliberately: the archive carries
configuration and the configuration is valid. Refusing would mean a DR rebuild fails wholesale
whenever the registry comes back before the secret store. Provisioning the secret is a separate
operation ([ADR-0018](adr/0018-device-credentials-by-reference.md) §2a) and always was.

Unlike `credential_state: needs_reconnect`, this is **not** recorded on the device afterwards —
nothing tells the gateway when you add the secret, so a stored flag would go stale. The device's
own status reports it at dispatch until it resolves.

**Check `fingerprint_warnings` on every restore.** It counts devices whose endpoint pin
([ADR-0015](adr/0015-endpoint-fingerprinting.md)) could not be carried across intact, and
each one also carries a `fingerprint_warning` naming why. It is reported at the top level
precisely because three warnings inside a 500-device list is what gets missed mid-incident:

```bash
… "$GW/v1/admin/restore/preview" | jq '.counts, .fingerprint_warnings, [.devices[] | select(.fingerprint_warning)]'
```

Two causes, and they want different responses. **The archive and the live device disagree
on the pin** — the live pin was kept and the archived one discarded, because a restore
warns rather than re-pinning: the live value was established against the endpoint as it is
now, quite possibly by an audited approval. If the *archived* value is the correct one,
delete the device and restore it again. **The archive carries no pin at all** — either it
predates ADR-0015 (re-export from a current gateway) or the device had never been probed;
either way that device will trust-on-first-use on its next probe and record whatever
answers. Plain-`http://` devices are exempt and never warn: they have no authenticated
dimension to lose.

Restored devices come back **unprovisioned**: registration re-fetches each spec and
re-spawns each pod, so a device that is unreachable at restore time lands with a
`spawn_error` and `reachable: false` (F-66) until it can be contacted. That is a device
problem, not a restore failure.

`include_deadletters: true` restores dead letters onto the **dead-letter stream**, not the
live call stream — they stay inert until you explicitly replay them through the F-10 path.

Every restore is audited (`backup.restore`), **including dry runs**: previewing is the
natural reconnaissance step before a real one.

### Rebuild a stack from nothing (disaster recovery)

Restoring into a stack that still exists is the easy half. This is the other half: the
original is gone and you are rebuilding onto new infrastructure.

**Walked end to end on 2026-08-11** against a genuinely fresh cluster — new Redis, new pods,
a *different* `MCP_SECRET_KEY` — closing [TG-7](testing-gaps.md). The steps below are what
actually worked, including the three that failed first.

**The archive is not a stack.** It carries devices, credentials and governance history. It
does **not** carry the things below, and the gateway will not start — or will refuse every
device — without them. Back these up with, and *separately from*, the archive:

| Not in the archive | Symptom if missing | Where it lives |
|---|---|---|
| `MCP_SECRET_KEY` | 409 preflight abort on a ciphertext archive | your secret store; a **portable** archive removes this dependency |
| Per-device TLS material (`security.mtls.devices.<host>.ca_bundle`) | **CrashLoopBackOff at startup** — `ValueError: security.mtls.devices.<host>: cannot build TLS context — [Errno 2]` | mounted volume, e.g. a `prism-ca` ConfigMap at `/etc/mcp/tls` |
| `MCP_ALLOW_PRIVATE_TARGETS`, `MCP_ADMIN_KEY`, `MCP_VIEWER_KEY` | every device `failed` with a policy `reason`; or 401 with no admin credential at all | deployment env — **not wired by `deploy/kubernetes/` manifests** |
| Non-Kubernetes DNS names used in `base_url` | devices restore, then never become reachable | the CoreDNS `hosts` block, or your resolver |

> **The trap worth naming.** A stack rebuilt from the repo manifests alone has no
> `MCP_ALLOW_PRIVATE_TARGETS`, so a restore refuses every private-address device and reports
> it as a **correct policy refusal** — indistinguishable, in the response, from a deliberately
> tightened policy. You will read a configuration gap as the system working as designed.
> Check the env before concluding the archive is at fault.

**Order of operations:**

1. **Stand up an empty stack** and let it reach `healthy` with `registered_devices: 0`
   *before* restoring. A stack that cannot start is not a restore problem, and diagnosing
   both at once wastes the outage.
2. **Reproduce the upstream-reachability environment.** `base_url` and `spec_url` are
   replayed verbatim. Kubernetes service DNS
   (`svc.mcp-gateway.svc.cluster.local`) resolves **unchanged** in a new cluster provided the
   same service names exist in the same namespace — so recreate those services and the
   archive needs no editing. Anything outside Kubernetes needs its resolution recreated.
3. **Preview, then apply** — as above. A portable archive needs `"passphrase"`.
4. **Verify the fleet works, not that it appears.** This is the step that separates a real
   recovery from a green report:

```bash
# Devices present and provisioned — reachable AND pod_active, no spawn_error.
curl -s -H "Authorization: Bearer $KEY" "$GW/v1/devices" | jq '.devices[]|{hostname,reachable,pod_active,spawn_error}'

# The manifest rebuilt from the restored spec_url — tool_count > 0, has_manifest true.
curl -s -H "Authorization: Bearer $KEY" "$GW/v1/devices/<host>/diagnostics" | jq '{tool_count,has_manifest,tools_revision}'

# THE assertion: a tools/call on a device with a restored credential.
# Nothing before this proves the credential decrypted to a usable secret.
```

A restored credential that decrypts to the *wrong* value fails only here — every check above
it passes. If you verify one thing, verify this.

**What a good result looks like:** every device `restored`, then `reachable: true` and
`pod_active: true` with `has_manifest: true`, `tools_revision` carried across (not reset to
0), and a `tools/call` returning real upstream data.

### Scale workers

Each worker owns a disjoint set of devices (single-owner, D-2); scaling out triggers a
decentralized rebalance (F-07).

```bash
kubectl -n $NS scale deploy/device-mcp-worker --replicas=<n>
# or rely on the HPA; confirm the new pods pick up devices:
curl -s "$GW/metrics" | grep mcp_worker_pods
```

Scaling **in** is safe — a removed worker's devices are reclaimed by the reconciler
(F-07). Don't scale gateway via `--scale` in Compose (fixed host port); scale it via
Kubernetes/LB.

### Fix the Redis secret

```bash
kubectl -n $NS get secret mcp-redis -o jsonpath='{.data.redis-url}' | base64 -d; echo
# update redis-url (must include the password) and redis-password, then:
kubectl -n $NS rollout restart deploy/device-mcp-gateway deploy/device-mcp-worker
```

The F-24 gate (`assert_redis_secure`) refuses an unauthenticated URL — the fix is a correct
secret, not the `redis.allow_insecure` escape hatch.

### Rotate the secret key

Zero-downtime, multi-key flow — full detail in [secret-rotation.md](secret-rotation.md):

1. Deploy with **both** keys, new first (`MCP_SECRET_KEY="<new>,<old>"`).
2. `device-mcp-rotate-secrets --config config.yaml` (idempotent, loss-free; run once per stack).
3. When it reports `0 failed`, redeploy with the new key only.

### Work the dead-letter queue

See [`MCPDeadLetterGrowing`](#mcpdeadlettergrowing--d6) above and
[observability.md](observability.md#working-a-dead-letter-alert-f-10).

### Roll a restart

```bash
kubectl -n $NS rollout restart deploy/device-mcp-gateway   # gateway: stateless, safe anytime
kubectl -n $NS rollout restart deploy/device-mcp-worker    # workers: devices rebalance during the roll
```

A gateway replica that is restarted drops the requests it is waiting on (the client sees a
connection error and retries). Nothing is pinned to a replica since HTTP+SSE went: the next
request may land anywhere (F-20). For version upgrades follow [upgrade.md](upgrade.md), not a bare
restart.

### Verify the audit trail

```bash
python -m device_mcp_gateway.audit_verify logs/audit.log    # exit 0 = chain intact (F-57)
```

---

## Escalation & SPOFs

Before paging the next tier, capture the `rid` of a failing call, the firing alert, and
the output of `GET /v1/devices/$H/diagnostics` for the affected device. The single points
of failure to keep in mind during any incident (full list in
[failure-modes.md](failure-modes.md#6-single-points-of-failure)):

- **Redis** — the whole control plane. Run it HA/replicated.
- **`MCP_SECRET_KEY`** — losing it makes at-rest credentials unrecoverable. Back it up
  out-of-band.
