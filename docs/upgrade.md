# Upgrade Guide — Device MCP Gateway

How to move a running deployment to a new version without dropping traffic or losing
data. Pairs with the [operations runbook](runbook.md) (what to do if something goes wrong
mid-upgrade) and the [ADRs](adr/) (why the compatibility rules below hold).

## Versioning & compatibility policy

The project is **`0.x`** (see `pyproject.toml`): minor releases may carry breaking
changes. Until `1.0`, **read the release notes before every upgrade** and treat each one
as potentially breaking. The compatibility surfaces that matter for a live upgrade:

| Surface | Stability | Notes |
|---------|-----------|-------|
| HTTP/MCP API (`/v1/devices…`, SSE) | additive by preference, **not guaranteed within a minor** | while the project is `0.x` a patch release may move a route; every such change is called out in the release notes and listed under [breaking API changes](#breaking-api-changes) |
| Redis data model (streams, registry keys, lease keys) | **mixed-version safe within a minor** — see below | the call-stream entry is forward/back-compatible by design |
| Config schema (`config.yaml`) | additive; unknown keys **warn, don't fail** (F-50) | a new required gate is the exception — see [breaking gates](#breaking-configuration-gates) |
| Encrypted-credential format (Fernet/MultiFernet) | stable | key rotation is orthogonal — see [secret-rotation.md](secret-rotation.md) |
| Embedded SQLite schema | additive | back up `storage.db_path` before a minor upgrade |

### Mixed-version safety (rolling upgrades)

The distributed components are designed to run **mixed-version during a rollout**:

- The Redis **call-stream entry is tolerant in both directions**. A new producer adds
  fields (e.g. `traceparent` in 1H, `subject` in the identity-propagation change) that an
  old consumer ignores; an old producer omits them and a new consumer falls back to a
  default (`subject="-"`) rather than erroring. So a new gateway can feed an old worker and
  vice versa for the duration of a roll.
- Workers are **single-owner per device** (D-2) and coordinate via Redis leases, not via
  each other — a new and an old worker can hold different devices simultaneously.

This is what makes the [rolling procedure](#rolling-upgrade-distributed-mode) safe. If a
specific release breaks mixed-version operation, its notes will say so and require a
different sequence.

---

## Pre-upgrade checklist

1. **Read the release notes** for every version between current and target.
2. **Back up the durable state:**
   - Distributed: snapshot Redis (the registry + leases); the credential ciphertext lives
     here.
   - Embedded: copy `storage.db_path` (SQLite).
   - **Back up `MCP_SECRET_KEY` out-of-band** if you haven't — losing it orphans every
     stored credential.
3. **Validate the new config against the new version** before rolling:
   ```bash
   device-mcp --config config.yaml --check-config   # loads + validates, does not serve
   ```
   Fix anything the F-50 validator flags (it warns on unknown/misplaced keys with a dotted
   path). Confirm the [breaking gates](#breaking-configuration-gates) are satisfied.
4. **Confirm headroom:** the roll briefly removes a replica from rotation. Ensure the PDB
   `minAvailable` and current replica count leave you serving.
5. **Note your current image tag** for a fast [rollback](#rollback).

---

## Rolling upgrade (distributed mode)

Because the fleet is mixed-version safe, a standard Kubernetes rolling update works. Do it
**one deployment at a time** so you can stop on the first sign of trouble:

The gateway and the worker run the **same image** — the worker just overrides the command —
so both steps below reference one image reference. Pin it by digest, and use the *same*
digest for both: they share the Redis data model, so a version skew across a schema change
is a split-brain risk. Read it with
`docker buildx imagetools inspect ghcr.io/benwold-lgtm/device-mcp-gateway:<tag>`.

```bash
IMG='ghcr.io/benwold-lgtm/device-mcp-gateway:<tag>@sha256:<digest>'

# 1. Workers first — they own the device connections; a roll rebalances devices (F-07).
#    A stopping worker hands its devices straight to a live one (see below).
#    Container name is `worker`, but the image is the gateway image.
kubectl -n mcp set image deploy/device-mcp-worker worker="$IMG"
kubectl -n mcp rollout status deploy/device-mcp-worker

# 2. Then the gateway — stateless; losing a replica only drops its in-flight SSE streams,
#    which clients reconnect and retry (F-20).
kubectl -n mcp set image deploy/device-mcp-gateway gateway="$IMG"
kubectl -n mcp rollout status deploy/device-mcp-gateway
```

Managing the cluster with kustomize instead? Update the `digest:` in
`deploy/kubernetes/kustomization.yaml`'s `images:` block and re-apply — that retargets both
deployments from one place, which is harder to get half-done than two `set image` calls.

**What a worker roll costs clients.** A worker that stops cleanly drains its in-flight calls
(`registry.shutdown_drain_timeout`, 25s), leaves the live set, releases each device and
republishes it, so a live worker takes it over within a few seconds. Calls to a device in that
window are answered **503** with `Retry-After`, not 404. Before 0.3.8 a stopping worker released
its devices without republishing them, and each one stayed unowned until the reconciler had
seen it orphaned on `reconcile_orphan_grace_cycles` sweeps — **42–113s measured**, per device,
on every roll. **The first roll *to* 0.3.8 still has that gap**, because the workers stopping
during it are the old version; the hand-off applies from the next roll on. A worker that is
killed rather than stopped (OOM, node loss) still waits for its claims to lapse — that is what
the reconciler is for.

Between the two steps, sanity-check the [post-upgrade verification](#post-upgrade-verification).
Workers-first means new execution logic lands before the new dispatch logic that may rely
on it; if a release's notes prescribe the opposite order, follow the notes.

### Watch during the roll

- `mcp_worker_pods` — devices redistribute as pods cycle; transient skew is normal (the
  rebalance, F-07, converges it).
- `MCPDeadLetterGrowing` — pod-replace windows can dead-letter a few "no active pod" calls;
  [replay them](runbook.md#work-the-dead-letter-queue) once the roll settles.
- `MCPReconcilerReassignmentChurn` — a graceful roll hands devices off directly, so from 0.3.8
  on it should barely move during one; the first roll to 0.3.8 still goes through the
  reconciler (above). Sustained churn after a roll settles is a problem (see the runbook).

---

## Breaking API changes

A gate refuses to *start*; these refuse a *call*, so nothing warns you at deploy time — a
script keeps working right up until the first time it runs. Check this list against anything
that talks to the management API directly.

| Version | Change | What to do |
|---------|--------|------------|
| **0.4.0** *(announced, not yet released)* | The HTTP+SSE endpoints are removed: `GET /v1/devices/{hostname}/sse`, `POST /v1/devices/{hostname}/messages`, `GET /v1/fleet/sse`, `POST /v1/fleet/messages`. Deprecated since 0.3.0. | Move clients to `POST /v1/devices/{hostname}/mcp` and, for fleets, to a fleet handle (next row). |
| **0.4.0** *(announced, not yet released)* | `POST`/`PUT /v1/devices` **refuses** a `transport` field with a 400, `"sse"` included. With SSE gone it selects nothing, and accepting it would claim a choice that no longer exists. Stored devices are migrated (the field is dropped on read), and a pre-0.4.0 archive restores with a warning rather than a refusal. `transport.default` / `transport.sse.*` in config are ignored with a startup warning. | Stop sending `transport` on registration and edits. **Upgrade the console (UI BFF) first:** it sends the field back on every device edit. Remove the `transport:` block from config at leisure. |
| **0.4.0** *(announced, not yet released)* | Fleets gain **handles** ([ADR-0027](adr/0027-stateless-requests-in-distributed-mode.md) §5): `POST /v1/fleets` mints one over a fixed device set, and `POST /v1/fleet/{handle}/mcp` serves it in both protocol eras. `POST /v1/fleet/mcp?devices=…` keeps working for handshake-era clients and is deprecated. A `2026-07-28` (stateless) client must use a handle. | Mint a handle per fleet and configure clients with its URL. |
| **0.3.8** | A **registered** device that is not serving now answers **503 with `Retry-After`** on `POST /v1/devices/{hostname}/mcp`, `GET /v1/devices/{hostname}/sse` and `POST /v1/devices/{hostname}/messages`, where it answered 404. **404 now means only "not registered".** The old 404 told a client a device being moved between workers did not exist, and on Streamable HTTP it also read as the spec's "session expired", so a conforming client re-initialised into it instead of backing off. | If a client or alert keys on 404 from these endpoints to mean "device unavailable", key on 503 instead, and honour `Retry-After`. Keep treating 404 as "not registered". |
| **0.3.6** | `POST /v1/admin/restore` is gone. It is now `POST /v1/admin/restore/preview` (needs `backup:read`, writes nothing, returns `plan_digest`/`plan_token`) and `POST /v1/admin/restore/apply` (needs `backup:write`, requires that `plan_token`). The `dry_run` body field no longer exists — which operation runs is which route you call, not a flag inside a body the caller controls. | Update any script or client posting to the old endpoint: preview first, then apply with the token it returned. A missing, mismatched, forged or expired (7-day) token is refused as `ERR_PLAN_STALE`. See [runbook.md](runbook.md#restore-from-a-backup). |
| **0.3.6** | `PUT /v1/devices/{hostname}` now **refuses** `expected_tls_spki_sha256` with a 400. It previously accepted the field and silently discarded it, so this breaks nothing that was working — but a caller that was sending it will now see an error where it used to see a 200. | Supply the digest when *registering* the device, or accept the key the device is presenting with `POST /v1/devices/{hostname}/fingerprint/approve`. |

**Distributed restore needs `MCP_SECRET_KEY` from 0.3.6.** `POST /v1/admin/restore/preview`
answers 503 in distributed mode without it, because a plan token minted under a process-local
key cannot be verified by the replica the apply is routed to. Set the same value on every
replica. Embedded mode is unaffected.

---

## Breaking configuration gates

Distributed mode **fails closed** (ADR-0006). When upgrading *into* a version that adds or
tightens a gate, the new process **refuses to start** until config satisfies it — this is
intentional, and the reason to validate config first:

| Gate | Requirement | Escape hatch (lab only) |
|------|-------------|-------------------------|
| **F-23** API key | distributed mode needs at least one API key | `gateway.allow_anonymous: true` |
| **F-24** Redis auth | the Redis URL must carry a password | `redis.allow_insecure: true` |
| **ADR-0013 §6** OIDC issuers | `gateway.oidc` may set `issuer` **or** `issuers`, never both | — (pick one form) |
| **ADR-0013 §5a/§5b** provider plane | a `plane: provider` issuer cannot map a group to a role granting `tools:call` or any `backup:*` scope | — (the cap is the control) |
| **ADR-0013 §11** tenant identity | a `plane: provider` issuer requires `gateway.tenant_id` | — (a gateway that cannot name itself cannot check a grant names *it*) |

If an upgrade surfaces one of these for the first time, set the **real** control (an API
key, an authenticated Redis URL) — not the escape hatch. The hatches exist for local
development; using them to clear an upgrade blocker re-opens a release-blocking
vulnerability. The [runbook](runbook.md#the-gateway-or-worker-wont-start-r2) covers the
exact refusal messages.

### The new `gateway.tenant_id` gate

Only bites a deployment that configures a **provider-plane** issuer — which, before this
release, could not honour an elevated grant at all, so nothing is being taken away. Set it to
the tenant this stack serves:

```yaml
gateway:
  tenant_id: acme
```

It is deployment-level rather than per-issuer because one gateway serves one tenant
([ADR-0004](adr/0004-single-tenant-per-stack.md)). The gate is at **startup**, not at the
first elevation, deliberately: an elevated grant is wanted during an incident, and that is
the worst possible moment to discover the gateway cannot honour one. Tenant-plane and
single-issuer deployments are unaffected and need no config change.

### Audit records gain a `grant` field — but only when one was used

Requests made under an elevated grant now carry `grant=<id>` on their audit records. Every
other record is **byte-identical** to what earlier releases wrote: the field is emitted only
when present, so existing hash chains verify across the upgrade and no parser sees a new key
unless a grant was actually exercised.

A `tools:call` grant is replayable inside its window, so one grant id legitimately appears on
several dispatch records — that is the join key for reconstructing what an elevated session
did, not a duplicate.

### The OIDC audit subject format changed

Not a startup gate — nothing refuses to boot — but it changes recorded data, so it is worth
knowing before you upgrade. The OIDC principal subject is now `oidc:{issuer}#{sub}`, where
it was `oidc:{sub}`.

`sub` is unique *within* an issuer, not globally. Once a gateway trusts a second issuer,
`admin` at the tenant IdP and `admin` at the provider IdP are two different humans, and the
old format silently puts them on the same line of the hash-chained audit. Qualifying the
subject is the fix, and it applies uniformly rather than only when a second issuer is
configured — a format that changes shape depending on how many issuers exist is exactly what
breaks a log parser the day someone adds one.

**What to check:** anything that parses or matches on the audit `subject` — SIEM rules,
saved queries, dashboards — needs to accept both forms. Records written before the upgrade
keep the short form; the chain itself is unaffected, since it commits to whole records.

### `gateway.api_key` becomes break-glass — **only if OIDC is enabled**

Not a startup gate — nothing refuses to boot, and no request is ever blocked — but in an
**OIDC-configured** deployment this changes how much noise your existing static key makes,
so read it before upgrading. If you do not configure `gateway.oidc`, **nothing on this page
applies to you**: the key stays exactly the ordinary, everyday credential it is today.

**What changed and why.** With OIDC enabled, `gateway.api_key` / `MCP_GATEWAY_API_KEY` (and
`MCP_ADMIN_KEY`) authenticate *only* when the JWT path fails or is absent. That is break-glass
in substance, whatever field it sits in, so it now gets ADR-0023's loud treatment: a
high-severity `auth.break_glass` audit record per use, an `auth.break_glass.activated` record
plus a page-severity alert per activation, and reactivation-frequency flagging. With no OIDC
there is nothing to fall back *from*, so the rule does not reach it.

**What this does not buy, which is the part worth acting on.** These keys have no configured
name, so the audit records that break-glass was used and **cannot say by whom** — the events
carry `attributable: false` for exactly this reason. They also have no `issued` date, so the
90-day expiry does not apply to them. Flagging makes them loud; only a named
`break_glass: true` `gateway.rbac` entry makes them attributable and expiring. The gateway
warns once at startup saying so.

**⚠️ The upgrade hazard, and it is not hypothetical.** If a UI/BFF relays this key for
**password sessions**, every console login is now a break-glass use: high-severity records on
each request, and an activation — hence a page — on the first request after each quiet gap.
Nothing breaks and nothing is blocked, but the signal becomes noise on ordinary traffic.

**What to do, in this order:**

1. Give the console's password path its own **named, unflagged** `gateway.rbac` entry with
   `role: console` (see `deploy/kubernetes/configmap.yaml`), and point the BFF's
   `GATEWAY_API_TOKEN` at it.
2. Verify no request still authenticates as `key:legacy` — the audit `subject` is directly
   observable proof, and a `console`-role token is additionally refused (`403`) on the
   `backup:*` routes an admin key would have been allowed.
3. Provision one `break_glass: true` entry per authorized person and remove the shared key.
   It may stay as a first-deploy bootstrap fallback, but it is not the steady-state
   break-glass path.

**What to check:** anything alerting on audit `severity` or on
`mcp_break_glass_activations_total` will start seeing this key. `deploy/kubernetes/prometheus-rules.yaml`
ships `MCPBreakGlassActivated` at `severity: page` with no `for:` delay — deliberate for a
real emergency credential, and worth confirming your console traffic has been migrated off it
first.

---

### Requiring credentials by reference — opt-in, and breaking when you opt in

`gateway.credentials.require_references: true` (or `MCP_REQUIRE_CREDENTIAL_REFS=true`) refuses
any **new** device credential supplied inline ([ADR-0018](adr/0018-device-credentials-by-reference.md)
§1). It ships **off**; upgrading never turns it on.

Migrate before you flip it, not after:

1. **Count what you have.** Every start logs the inventory:
   `N device(s) hold a credential inline (ADR-0018 §1): …`
2. **Move each secret into the store**, then update the device to reference it:
   `{"auth": {"credential_ref": "secret://<tenant>/devices/<name>#api-key"}}`, or for OAuth2
   `client_secret_ref` / `password_ref`.
   ⚠️ **Check the value, not just the path.** A reference that resolves is not a reference
   that authenticates: the gateway can only tell you the secret exists, never that it is the
   one that device accepts. Point a device at a populated-but-wrong path and everything looks
   healthy until the next call the upstream actually authenticates — and against an upstream
   that ignores the header, never. Confirm each device still reaches its upstream after the
   switch, and treat a device you cannot reach as one you have not migrated.
3. **Flip the gate** once the inventory reaches zero.

What changes when it is on, and what does not:

| | With the gate on |
|---|---|
| An existing inline device dispatching | **Unaffected.** The gate is a write-path rule; nothing stops working at a restart |
| Editing that device without touching its credential | **Allowed** — a rate-limit change is not a credential change |
| Registering, or supplying a new inline credential | **Refused, 400**, naming the field and the `*_ref` that replaces it |
| Restoring an archive containing inline credentials | **Refused per device**, and the dry run says so first |
| A `grant_type=refresh_token` device | **Unaffected.** Its token is gateway-minted and cannot be by reference (§1a); only its `client_secret` must be |

⚠️ **The archive is the one to plan for, and not only the old ones.** Flipping the gate does
not rewrite the devices already stored, so a backup taken *from the gated stack itself* still
carries their inline credentials and still fails per device on restore — measured on a live
cluster, where a fresh export of a gated stack came back `would_restore: 5, failed: 2`. The
archive only becomes restorable once the **inventory reaches zero**; that, not the gate, is
what makes a backup usable again. Until then the backup you are keeping for a disaster is one
that stack will refuse.

## Embedded mode

Single process, single SQLite file — there is no rolling story:

1. Back up `storage.db_path`.
2. Stop the process, deploy the new version, validate config (`--check-config`), start it.
3. Schema changes are additive; the backup is your rollback.

Embedded mode keeps its documented single-operator fail-open defaults (warned, not gated)
— no API-key/Redis gate applies.

---

## Secret-key rotation vs. version upgrade

These are **independent** and should not be combined in one change window. Rotating
`MCP_SECRET_KEY` follows its own zero-downtime, multi-key flow in
[secret-rotation.md](secret-rotation.md) (deploy both keys → run `device-mcp-rotate-secrets`
→ retire the old key). Do a version upgrade and a key rotation as two separate, verified
steps so that if one goes wrong you know which.

---

## Post-upgrade verification

```bash
# Liveness / readiness on each component
curl -fsS "$GW/livez" && curl -fsS "$GW/readyz"

# Workers present and devices owned
curl -s "$GW/metrics" | grep -E 'mcp_worker_pods|mcp_reconciler_leader'

# A representative tool call end-to-end (pick a known-good device + tool)
#   then grep its rid through the access log to confirm gateway→worker flow.

# Audit chain still intact across the restart (F-57)
python -m device_mcp_gateway.audit_verify logs/audit.log
```

Confirm no unexpected `MCPDeviceToolsBreakingChange` (an upgrade shouldn't change a
device's *upstream* spec), no sustained `MCPReconcilerReassignmentChurn`, and that the
error-budget burn alerts are quiet.

---

## Rollback

Mixed-version safety cuts both ways — rolling **back** is the same procedure with the old
tag:

```bash
kubectl -n mcp rollout undo deploy/device-mcp-gateway
kubectl -n mcp rollout undo deploy/device-mcp-worker
```

Caveats:

- **Config gates:** if you rolled back because a new gate blocked startup, the rollback
  removes the requirement — but fix the config so the next attempt succeeds rather than
  staying on the old version.
- **Data:** Redis/SQLite state written by the new version is read by the old version under
  the same back-compatible rules (new fields are ignored). If a release's notes flag a
  one-way data migration, restore from the pre-upgrade backup instead of rolling code back.
- **Secret keys:** a rollback does **not** undo a completed key rotation — the new key
  stays primary. Keep both keys configured until you're settled on a version.
- **Archives taken by the new version do not warn the old one.** An archive from a gateway
  with [ADR-0018](adr/0018-device-credentials-by-reference.md) §3 carries no OAuth2 refresh
  token, and says so in a per-device field a pre-§3 gateway does not read. Restoring one into
  a rolled-back gateway therefore reinstates `grant_type=refresh_token` devices **silently
  broken** — no `restored_needs_reconnect` outcome, no `credential_state`, just a device that
  fails its first tool call. Restore such an archive into a gateway at or above the version
  that produced it. (Every other grant is unaffected, and the reverse direction — an old
  archive into a new gateway — is fine.)
