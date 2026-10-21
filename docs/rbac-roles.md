# RBAC — scopes, roles, and IdP mapping

The gateway authorizes on **scopes**, not role strings. Roles are named **bundles of
scopes** ([rbac.py](../device_mcp_gateway/rbac.py) `ROLE_SCOPES`); routes only ever call
`require_scope(...)`, so adding a role never touches a route. This is the living reference
for the role/scope model; the *decision* behind it is
[ADR-0007](adr/0007-federated-identity-oidc-and-gateway-rbac.md).

## Scopes (the atoms)

| Scope | Grants | Route guards (examples) |
|-------|--------|--------------------------|
| `devices:read` | See devices and their state; **propose** a device-write plan (ADR-0022) — writes nothing, reaches nothing (see below); also the `require_scope` guard on **apply** (the actual gate there is the `devices:write-planned` grant below, checked inline) | `GET /v1/devices`, `GET /v1/devices/{h}`, `…/diagnostics`, `…/tools`, `…/tools/diff`, `…/deadletter` (inspect), `GET /v1/admin/overview`, `POST /v1/devices/plans`, `POST /v1/devices/plans/apply` |
| `devices:write` | Manage the fleet; **review and approve** a proposed device-write plan (ADR-0022) | `POST/PUT/DELETE /v1/devices/{h}`, `POST …/deadletter/replay`, `DELETE …/deadletter` (drain), `GET /v1/devices/plans/{id}`, `POST /v1/devices/plans/{id}/approve` |
| `tools:call` | Invoke a device's MCP tools; mint and use fleet handles | `POST`/`DELETE /v1/devices/{h}/mcp`, `POST /v1/fleet/{handle}/mcp`, `POST`/`GET`/`DELETE /v1/fleets…`, `POST /v1/fleet/mcp` (deprecated) |
| `metrics:read` | Read operational metrics | `GET /v1/metrics/summary` |
| `backup:read` | Export an archive of the registry; preview a restore (writes nothing) | `GET /v1/admin/backup`, `POST /v1/admin/backup`, `POST /v1/admin/restore/preview` |
| `backup:write` | Apply a restore | `POST /v1/admin/restore/apply` |
| `backup:export-portable` | **Additionally** export a *portable* archive — credentials re-encrypted to a passphrase instead of the stack key | `POST /v1/admin/backup` with `kind=portable` |
| `devices:write-planned` | Apply **exactly one** reviewed, digest-bound device-write plan (ADR-0022) | `POST /v1/devices/plans/apply` |
| `support:request` | **Ask** to act on this tenant, and poll your own request's outcome (ADR-0017). Held by a **provider**, and since [ADR-0024](adr/0024-tenant-provisioning-is-a-request.md) §10 it normally arrives by redeeming an invitation rather than through a hand-provisioned RBAC entry (both still work). Confers no read, no write, no invoke, and no decision | `POST /v1/support-requests`, `GET /v1/support-requests/{id}` |
| `support:administer` | **Decide** who may act on this tenant: list, approve, reject, revoke, and the standing-consent setting (ADR-0017). The tenant's own authority | `GET /v1/support-requests`, `POST .../{id}/approve`, `POST .../{id}/reject`, `GET/POST/DELETE /v1/support-requests/standing-consent`, `GET /v1/support-grants`, `DELETE /v1/support-grants/{id}` |
| `support:administer` (enrolment, ADR-0024 §10) | **Decide who the provider is at all**: issue and withdraw invitations, list every enrolment with its `last_used_at`, revoke one, and read this tenant's own catalog configuration. Same scope as deciding on a support request, deliberately — both are administering this tenant's relationship with its provider, and an admin who could approve a request but not see who is enrolled would hold half a control | `POST/GET /v1/enrolment-invitations`, `DELETE /v1/enrolment-invitations/{code_hash}`, `GET /v1/enrolments`, `DELETE /v1/enrolments/{id}`, `GET /v1/enrolments/catalog-configuration` |
| *(none — the invitation is the credential)* | **Redeem** an invitation, once. Outside the RBAC machinery because the caller has no gateway credential yet; obtaining one is what the call does (ADR-0024 §10) | `POST /v1/enrolments/redeem` |
| `notifications:read` | Read the durable tenant-facing notification list — a break-glass activation, a frequently self-issued support grant (ADR-0017 slice 5) | `GET /v1/notifications` |

> **`backup:read` is not a read-only grant in the ordinary sense.** An archive contains
> every device's `base_url`, spec URL and configuration, plus its credentials as
> ciphertext. Treat it like `devices:read` over the whole fleet at once, and see
> [ADR-0011](adr/0011-backup-and-restore.md).
>
> **`backup:export-portable` is never implied by the other two.** A portable archive is a
> complete set of live device credentials behind a single passphrase, so it is
> key-independent by design — that is what makes it useful for migration and dangerous to
> hold standing permission for. It is checked *inside* the handler, because whether it is
> required depends on the requested `kind`.

> **`devices:write-planned` never appears in any role's bundle, `admin` included, and never
> will by design.** Every other scope in this table is standing: hold the role, hold the
> scope, for as long as the credential is valid. This one is minted per-plan, at Review, by
> `write_planned.WritePlannedGrantStore.issue` — scoped to one caller and one exact plan
> digest — and redeemed once (or, if the reviewer explicitly marked it repeatable, on exact
> byte-identical reapplication only) via `write_planned.check_and_consume`, never via
> `require_scope`. `caller`'s baseline stays `devices:read` + `tools:call` permanently; an
> agent that needs to register or reconfigure a device proposes a plan, a human with
> `devices:write` reviews and approves it, and only *that* digest becomes appliable. See
> [ADR-0022](adr/0022-agent-initiated-device-writes-are-plan-bound.md).
>
> **`support:administer` is a standing bundle member, unlike `devices:write-planned` above —
> deliberately.** It gates *who may administer the support mechanism itself* (raise/decide/
> list/revoke), not the support grant it produces. The grant it produces is what carries the
> tenant-vocabulary scopes a provider operator actually gets (`devices:read`, `tools:call`,
> ...) and is checked live via `support_grants.SupportGrantStore.check`, entirely separate
> from `require_scope`/`ROLE_SCOPES` — a support grant's own subject never becomes a
> `Principal` with a standing bundle. See
> [ADR-0017](adr/0017-provider-authority-is-delegated.md).
>
> **A support grant's own bearer is checked by `CompositeAuthenticator` as a third, last-
> resort path** — tried only once a token fails both the OIDC and static-key checks and
> starts with `sgr_` (`support_grants.is_support_grant_token`), so an invalid credential of
> any other shape refuses exactly as fast as before this existed. Tier 0 (§7): the bearer
> alone. Tier 1: the request submitted a `public_key` at raise time, and every call must
> additionally carry `X-Support-Timestamp` + `X-Support-Signature` — a fresh Ed25519
> signature (`support_grant_pop.py`) over `METHOD\npath+query\ntimestamp`, strictly newer
> than the last one this grant accepted (no nonce cache; see that module's docstring for
> why a monotonic timestamp closes replay without one).
>
> **Revoking a support grant interrupts a call already in flight under it, not only future
> calls (§8)** — but only on the gateway process that is actually running that call.
> `rbac.track_support_grant_inflight` (a dependency ordered right after `authenticate_request`)
> registers the current task in a per-process `support_grant_inflight.InFlightSupportGrantCalls`
> registry while a support-grant-authenticated request executes; `DELETE
> /v1/support-grants/{id}` calls `cancel_all` on it after a successful revoke. This is
> deliberately not cross-replica: in distributed mode the gateway API tier itself is
> horizontally scaled, and a revoke landing on a different replica than the one running the
> call can't reach it directly. That call still stops — refused on its very next request
> (`check`/`check_proof` are live per-request already), or via the existing F6 timeout
> watcher — just not the instant the revoke lands elsewhere. See `support_grant_inflight.py`'s
> module docstring and `docs/adr/README.md` item 7 for the full reasoning.
>
> **An enrolment credential is a FOURTH path, and it is where a provider's
> `support-requester` identity now comes from (ADR-0024 §10).** The role bundle above still
> exists and still works — an operator can mint a static key with it — but provisioning one by
> hand was step 5 of the nine manual steps §10 measured. Redeeming an invitation mints an
> `enr_`-prefixed credential instead, resolved by `enrolments.EnrolmentStore.check` on the same
> last-resort path as `sgr_` and by the same shape test, and it resolves to exactly
> `{support:request}` — written literally rather than read from `ROLE_SCOPES["support-requester"]`,
> so anything later added to that role does not silently become something every enrolled
> provider holds.
>
> **It never expires, and that is deliberate** (§10): an enrolment carries no capability beyond
> asking, so an expiry would be "a scheduled outage with a security-shaped name" rather than a
> control. Revocation is therefore the ONLY control, which is why `check` is a live lookup on
> every request with no caching and no fallback — a Redis outage refuses rather than admitting
> a credential this replica cannot confirm is still live. What replaces expiry's one virtue is
> visibility: every enrolment is listed with who approved it, when, and a `last_used_at` taken
> from real requests, so a dormant supplier relationship is found by looking rather than by
> remembering.
>
> **`notifications:read` is a separate scope from `support:administer`, deliberately.**
> `GET /v1/notifications` is a durable, tenant-facing surface (ADR-0017 slice 5) for signals
> that must not only be logged — a break-glass activation (closing a confirmed ADR-0023 gap:
> until now an activation reached an audit event and a Prometheus alert, nothing a tenant
> admin who isn't watching Prometheus would see), and a standing-consent support grant that is
> being self-issued often enough to flag (`support_grants.SelfIssueActivityTracker`, the same
> reactivation-frequency shape break-glass already uses, applied to the one support-grant path
> that is equally silent by construction — no per-instance human approval). Reading this list
> is passive fleet visibility, not administering the support mechanism, so it is gated
> separately.
>
> `/health`, `/livez`, `/readyz` and the Prometheus scrape port are unauthenticated infra
> contracts and are not scope-gated.
>
> `GET /v1/auth/me` requires authentication but **no specific scope** — it returns the
> caller's own `subject`, effective `scopes`, and `auth_method`. A UI/BFF reads it to gate
> views on the gateway's scopes (so the two never drift); see "Where roles come from" below.


## Roles (scope bundles)

Two kinds of principal: **humans** operating the UI, and **machines** (an MCP client/agent
invoking tools over `/mcp`). One scope model serves both.

| Role | `devices:read` | `devices:write` | `tools:call` | `metrics:read` | `backup:read` | `backup:write` | `backup:export-portable` | `support:request` | `support:administer` | `notifications:read` | For |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|-----|
| **admin** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Full control (human) |
| **operator** | ✅ | ✅ | — | ✅ | — | — | — | — | ✅ | ✅ | Onboard / edit / remove devices, manage the dead-letter queue, administer support access — but not invoke tools (human) |
| **viewer** | ✅ | — | — | ✅ | — | — | — | — | — | — | Read-only (human) — *current `viewer`* |
| **auditor** | — | — | — | ✅ | — | — | — | — | — | — | Observability / compliance, no device access (human). Widens to `audit:read` when that scope exists |
| **caller** (agent) | ✅ | — | ✅ | — | — | — | — | — | — | — | An MCP client/agent that discovers and invokes tools — **machine identity**, not a UI role |
| **backup** (agent) | — | — | — | — | ✅ | ✅ | — | — | — | — | A scheduled backup/restore job — **machine identity**. Deliberately not `admin`: a nightly cron entry should not also be able to invoke tools or edit the fleet, and the portable archive stays an explicit operator action rather than a standing grant |
| **console** (agent) | ✅ | ✅ | ✅ | ✅ | — | — | — | — | ✅ | ✅ | The console's BFF relaying a **password** session, which has no per-user token to pass through — **machine identity**. `operator` ∪ `caller` (both provider- and tenant-plane views live in this one process today, ADR-0017/pre-ADR-0021), and deliberately no `backup:*` (below) |

| **support-requester** (agent) | — | — | — | — | — | — | — | ✅ | — | — | The identity a **provider** presents to THIS tenant's gateway so it can ask for access and watch its own request — **machine identity**. The narrowest bundle here, and the narrowness is the point: it reads nothing, writes nothing, invokes nothing and decides nothing. Notably **not** `support:administer` — approving is the tenant's act, and a provider that could approve its own request would be asserting the authority ADR-0017 says must be delegated |

All eight roles are defined in [`ROLE_SCOPES`](../device_mcp_gateway/rbac.py) today
(`operator`/`auditor`/`caller` were added with the OIDC work, ADR-0007; `backup` with
ADR-0011; `console` with ADR-0023 slice 4). Add a role by adding one entry to `ROLE_SCOPES`
— no route changes. `devices:write-planned` has no column here, deliberately: it is not a
bundle member of any role, so the omission is not a gap to fill in later.

> **`console` exists because nothing else fitted, and that gap had a cost.** A password
> session reaches device CRUD and diagnostics, `/metrics/summary`, and the MCP invocation
> path — `operator` cannot invoke tools, `caller` cannot manage the fleet, so the console's
> BFF was given an **admin** key and with it every `backup:*` scope. The BFF compensates by
> refusing password sessions on all four backup/restore routes, in its own words because
> *"a password session proxies with the stack's admin token, which holds every `backup:*`
> scope, so admitting one here is a complete credential dump"*. That is a real guarantee
> enforced in the wrong layer: it holds only as long as no BFF route forgets the guard. A
> role that cannot express the scope moves it to the gateway, where a console-side bug
> cannot undo it.
>
> It is written in the source as the **union** `operator | caller` rather than a copied
> scope list, so that if `operator` gains a scope the console gains it too instead of
> quietly drifting behind.

Note that `operator` does **not** get the backup scopes even though it manages the fleet:
one call as `backup:read` yields every device's configuration in a single file, which is a
different exposure from editing devices one at a time.

## Where roles come from

### Federated (OIDC) — production
The IdP asserts **group membership** in a token claim (`groups` or `roles`); a
**`group → role/scopes` mapping in gateway config** is the single source of truth. The UI
reflects whatever scopes the gateway grants (via `/auth/me`), so UI and gateway permissions
can't drift. Config lives under `gateway.oidc` (alongside the static-key settings), wired by
[oidc.py](../device_mcp_gateway/oidc.py):

```yaml
gateway:
  oidc:
    enabled: true
    issuer: https://login.example.com/realms/corp   # ADFS / Entra / Okta / Keycloak …
    audience: device-mcp-gateway                     # must equal the JWT 'aud'
    groups_claim: groups
    algorithms: ["RS256"]       # asymmetric allow-list; HS*/none are refused
    group_roles:                # IdP group  → gateway role (a scope bundle)
      mcp-admins:    admin
      mcp-operators: operator
      mcp-viewers:   viewer
      mcp-auditors:  auditor
```

> **Setting up a specific IdP?** [identity-integration.md](identity-integration.md) has the
> per-vendor walkthroughs (Keycloak and Authentik verified; Entra ID, Okta and Google
> Workspace as untested integration paths), the silent failure modes, and how to prove an
> integration actually works.

A user in multiple groups gets the **union** of the mapped scopes. A valid token whose groups
map to **no** role authenticates with an **empty scope set** — every route then 403s, and the
audit shows *who* was denied. `jwks_uri` is auto-discovered from the issuer unless set
explicitly; the full knob list is in [config.yaml](../config.yaml).

The audit subject is `oidc:{issuer}#{sub}`. The issuer is part of the identity because `sub`
is unique *within* an issuer, not globally — which matters as soon as there is more than one.

#### More than one issuer

A deployment may trust more than one identity provider — two of the tenant's own IdPs, say,
during a migration. Use `issuers` instead of the single-issuer keys; setting both is refused at
startup:

```yaml
gateway:
  oidc:
    enabled: true
    issuers:
      - issuer: https://login.example.com/realms/corp
        audience: device-mcp-gateway
        group_roles:
          mcp-admins: admin
      - issuer: https://login.example.com/realms/contractors
        audience: device-mcp-gateway
        group_roles:
          mcp-operators: operator
```

Two rules, each of which exists because its absence fails **silently** — the token still
validates and the request still succeeds:

| Rule | Without it |
|---|---|
| The issuer is resolved from `iss` **first**, and the decode is pinned to that one issuer with only that issuer's keys | A token signed by issuer A's key while claiming `iss: B` is accepted — the holder of one IdP mints themselves an identity from another |
| `group_roles` is **per issuer**, with no shared or fallback mapping | An administrator of one trusted IdP creates a group named whatever another issuer's mapping keys on, adds themselves, and the gateway grants them that issuer's scopes |

> **Removed:** earlier releases described a second *provider* IdP, a per-issuer `plane` with a
> server-side scope ceiling, and elevated grants that lifted that ceiling for one request
> ([ADR-0013](adr/0013-two-plane-tenancy-and-the-provider-plane.md) §6/§6a/§11). That
> arrangement was replaced in design by
> [ADR-0017](adr/0017-provider-authority-is-delegated.md), where authority over a tenant is
> **delegated by that tenant** rather than asserted by the provider, and the implementation has
> been removed. `plane`, `step_up_acr`, `grant_claim` and `entitlement_claim` are no longer
> read; a config that still sets them is not broken, only ignored. No released version ever
> offered them.

## Future granularity

The scope set is the granularity lever; all of this is additive (no route churn):

- Split `devices:write` → `devices:create` / `devices:update` / `devices:delete`.
- Add `deadletter:manage` (separate DLQ recovery from general writes), `audit:read`.
- **Resource-/tenant-scoped** grants (e.g. `operator@tenant-a`) — the natural extension if
  multi-tenancy ([ADR-0004](adr/0004-single-tenant-per-stack.md)) resumes; the OIDC
  claim→scope mapping is designed to carry a tenant dimension even while unused.
