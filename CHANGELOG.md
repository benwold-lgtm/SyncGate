# Changelog

All notable changes to the Device MCP Gateway are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the project is `0.x`, **minor releases may include breaking changes** — read
the notes for each release before upgrading. See [docs/upgrade.md](docs/upgrade.md).

## [Unreleased]

### Added

- **`GET /v1/devices/{hostname}/features`: a device's tools, resources and prompts in one
  read.** Resources and prompts were stored in every device's manifest and never exposed: the
  management API returned tools only. It reads the cached manifest, so unlike `/tools` (which
  answers 409 without an active pod) it works for a device that is **down**, which is when an
  operator most needs to see what it offers. `manifest_cached: false` marks "nothing known yet",
  and `discovered` marks which kinds were looked for at all. A proxied MCP server's resources and
  prompts are not collected today, so they report `false` rather than an empty list that would
  read as "none". `devices:read`, like the other device reads.
- **`device-mcp devices list` / `device-mcp devices show <hostname>`**: inspect devices from the
  command line. `list` shows every OpenAPI and MCP device with kind, health (the console's rule),
  pod, last check and credential state. `show` prints a device's tools (`--schemas` adds input
  schemas), resources and prompts. It is a client of the **gateway API** (`/v1/admin/overview`,
  `/v1/devices/{h}`, `…/features`), the same endpoints the console reads, so CLI and UI cannot
  disagree, and it needs only a `devices:read` token. A bare `device-mcp` still starts the
  gateway, unchanged.

- **MCP clients can fetch prompts: `prompts/list` and `prompts/get`**, with `prompts` advertised
  in `initialize`. OpenAPI devices have always had a prompt generated from the spec, recorded in
  the manifest and, since the entry above, listed by `/features`. But the MCP router had no
  handler for them, so a client asking got "method not found". `prompts/get` renders the
  template in the gateway with no device call, substituting declared arguments only. It uses
  plain `{name}` replacement, not `str.format`, so braces in spec text are left alone. An unknown
  prompt or non-string arguments answer `-32602`. A device with no prompts answers an empty list.

## [0.3.8] - 2026-10-16

### Read this before upgrading

- **A registered device that is not serving now answers 503 + `Retry-After`, not 404**, on
  `POST /v1/devices/{hostname}/mcp`, `GET …/sse` and `POST …/messages`. **404 now means only
  "not registered".** If a client or alert keys on 404 from those endpoints to mean "device
  unavailable", move it to 503. See [docs/upgrade.md](docs/upgrade.md#breaking-api-changes).
- **The first rolling update to 0.3.8 still has the old failover gap**: the workers stopping
  during it run 0.3.7, which releases devices without handing them off, so each moved device
  is unavailable for up to ~2 minutes. From the next roll on, a stopping worker hands its
  devices over in seconds. See
  [docs/upgrade.md](docs/upgrade.md#rolling-upgrade-distributed-mode).
- **Why a patch number, with a status-code change in it.** [docs/releasing.md](docs/releasing.md)
  §1.5 makes a new capability a minor bump, and nothing here is new: both changes correct
  behaviour found wrong on a live cluster. The 503 does change a status code a client may branch
  on, which is why it leads these notes rather than hiding in *Fixed*; `0.3.6` set the precedent
  of shipping a call-breaking fix as a patch. `0.4.0` stays reserved for the HTTP+SSE removal and
  the `2026-07-28` shift.
- **The HTTP+SSE deprecation clock is unchanged** — removal is still scheduled for **0.4.0**.

### Changed

- **Documentation and the load generator stop sending `"transport": "sse"` on device
  registration.** Every request example in `README.md` and `docs/kubernetes-architecture.md`
  carried it, and `tools/loadtest/loadgen.py` sent it on every registration. 0.4.0 refuses the
  field with a 400, so anything copied from those examples would break on upgrade; the field
  is optional with `"sse"` as its default, so omitting it works now **and** after 0.4.0. Drop it
  from your own scripts the same way. This entry changes documentation and tooling only.

### Fixed

- **"Registered but not serving" was reported as "not found".** Found in the same live run:
  while a device moved between workers, all three MCP endpoints answered 404
  `"Device pod not found or not active"`, telling the client the device did not exist. On
  Streamable HTTP it was worse than misleading — 404 is the spec's signal that a *session*
  expired, so a conforming client re-initialised straight back into the same 404 instead of
  backing off. The two cases are now separate: 404 for a device that is not registered, 503
  with `Retry-After: 5` for one that is registered with no active pod, its detail pointing at
  `GET /v1/devices/{hostname}` for `reachable` and `spawn_error`.

- **A worker that stops cleanly now hands its devices to a live worker immediately.** Found
  on a live cluster during the 0.3.7 verification: deleting a worker pod left each of its
  devices unowned for **42–113s**, and the same happens to every device a rolling update
  moves. Shutdown released each claim but never republished the assignment, so a clean stop
  looked exactly like a crash and waited out the reconciler's orphan hysteresis (F-62), which
  exists to rule out *transient* lease lapses. A second cause hid behind the first: the
  stopping worker left `workers:active` only after tearing its pods down, so a survivor still
  counted it as live, computed its per-worker target as if it were staying, and **declined**
  the hand-off (F-07). Shutdown now leaves the live set first, then releases and republishes
  each device. A worker that dies without shutting down still relies on the reconciler.

  **The first rolling update *to* this version still has the gap**, because the workers
  stopping during it run the old code. See [docs/upgrade.md](docs/upgrade.md#rolling-upgrade-distributed-mode).

## [0.3.7] - 2026-10-16

### Read this before upgrading

- **Nothing in this release changes a request, a response, a configuration key or a stored
  record.** It is a dependency and documentation release: the MCP SDK leaves the image (below),
  and the breaking changes coming in `0.4.0` are announced so callers can move first. If you do
  nothing else, read *Announced for 0.4.0* — one item (the console's BFF must stop forwarding
  `transport`) has to happen **before** you upgrade to `0.4.0`, not with it.
- **Why a patch number.** [docs/releasing.md](docs/releasing.md) §1.5 makes a new capability a
  minor bump. There is none here — no endpoint, field or behaviour is added — so this is
  honestly a patch, and it leaves `0.4.0` free for the HTTP+SSE removal and the `2026-07-28`
  shift it is committed to.
- **The HTTP+SSE deprecation clock is unchanged.** The four SSE endpoints still work and are
  still scheduled for removal in **0.4.0**.

### Changed

- **The MCP SDK (`mcp`) is no longer a runtime dependency, and moves to 2.x.** The gateway
  never used it to speak MCP — its own router (`pods/pod_base.py`) and upstream client
  (`upstream/mcp_client.py`) do that. The one runtime import was a `FastMCP` instance that
  every device pod built and registered its tools on, and that nothing ever dispatched
  through. It is deleted rather than ported to 2.x's `MCPServer`, and `mcp>=2.3.0,<3.0.0` is
  now in the `dev` extra only, as the reference implementation the coming dual-era interop
  tests check against ([roadmap](docs/roadmap-protocol-2026-07-28.md)). No request or response
  changes shape.

  `pip install -e .` now works against SDK 2.x, where previously only the `<2.0.0` cap kept it
  from failing on import.

  The image gets smaller with it: `mcp`, `httpx-sse` and `python-multipart` leave
  `requirements.txt`. `python-multipart` arrived only through `mcp`, so its form-parsing
  advisories no longer describe anything this image contains
  ([dependency-advisories.md](docs/dependency-advisories.md)). A new guard,
  `test_gateway_does_not_import_the_mcp_sdk`, fails if runtime code imports `mcp` again —
  CI installs `.[dev]` and would never notice, while the image would fail on import.

### Fixed

- **CI was red on any fresh install from 2026-10-14**, on two backpressure tests, with no change
  to the code. fakeredis 2.40.0 began trimming `MAXLEN ~` streams in whole macro-nodes, as real
  Redis does, and the tests had asserted an exact cap that real Redis never promised. They now
  assert the bound approximate trimming does give. Test-only; the production trimming and the
  documented 10k / 8k-alert thresholds are unchanged.

### Announced for 0.4.0

These are not in this release. They are listed so that callers can move before the release
that breaks them. Details are in [docs/upgrade.md](docs/upgrade.md#breaking-api-changes).

- **HTTP+SSE inbound is removed**, as scheduled since 0.3.0.
- **`transport` on device registration is refused with a 400**, `"sse"` included, as a breaking
  change rather than accepted as an alias
  ([roadmap decision 6](docs/roadmap-protocol-2026-07-28.md)). Stored devices and older
  archives are migrated, not refused. **The console's BFF must stop forwarding the field
  first**, or every device edit from the console fails.
- **Fleets become server-minted handles** for `2026-07-28` clients —
  [ADR-0027](docs/adr/0027-stateless-requests-in-distributed-mode.md) (Accepted), which also
  routes stateless results per request rather than through a session.

## [0.3.6] - 2026-09-02

### Added

- **A curated device type may now carry the provider's own spec, and may fix the address
  without minting the credential** ([ADR-0020](docs/adr/0020-the-device-catalog.md) §4a/§4b
  write path, and the new §4c). Two facts a provider knows about a *product* that the tenant
  was previously made to supply or guess.

  `curated_document` snapshots the provider's OpenAPI document into the version, as **text**
  rather than JSONB: §4b has the gateway recompute a hash from those exact bytes at claim time
  and refuse to trust the one stored beside them, and JSONB would normalise key order and
  whitespace until every recompute disagreed with every assertion. A version carries a curated
  document or a `spec_path`, never both, and never a curator-declared `tool_set` beside a
  document — refused at write time rather than resolved by a precedence rule, because a row
  that can hold a contradictory pair is a state a future bug reaches accidentally and then
  fails quietly inside.

  `host_source: provider_fixed` with `fixed_base_url` covers the case §6's table could not
  express: the provider knows where the endpoint is and the tenant still authenticates with
  **their own** credential. This is not a §6 provider-operated service — the provider mints
  nothing, holds nothing per tenant, and the catalog still carries no secrets. The claim form
  stops asking for an address on such a type, and the BFF **refuses** one sent anyway rather
  than overriding it: a guessed key position is noise a curator can correct, but a different
  address is a disagreement about where the device is.

  There is deliberately no curated TLS pin. §4c permits one only as a one-time bootstrap seed
  with no ongoing write path from the catalog, because a pin the provider could keep updating
  would deliver the very key-change event the tenant's pin exists to catch as ordinary
  configuration sync. Until that seeding exists, a host-fixed type pins on first contact like
  any other device.

  Not built: §4b's consumption path. The gateway still has no registration input that takes
  spec *content*, so a claim against a curated version cannot yet be registered.

- **A device call now carries the request id, and the identity model it compensates for is
  written down** ([ADR-0026](docs/adr/0026-service-identity-per-device.md)). A device
  authenticates *the gateway*, not the person behind the call: one service account per device,
  the same one for every user of the tenant. That is now the accepted, permanent model rather
  than an unstated limitation — accepted categorically, for every device kind, with §4 recording
  why a per-device answer and a per-capability credential split were both rejected.

  The compensating control is the join between the gateway's audit record (which names the
  person) and the device's own log (which names the service account), and that join needs one
  value on both sides. **It was not built.** The request id reached the access log, the audit
  record, the response header and the worker's execution audit, and stopped there — nothing put
  it on the wire to the device. Every outbound hop now carries `X-Request-Id` holding the id the
  caller was given, stamped at the same seam that installs the SSRF guard, so tool calls,
  resource reads and MCP-passthrough hops inherit it by construction. A tool argument cannot
  choose it, and no id is invented at egress when none is in scope — one that joins to nothing
  would be worse than a visible gap.

  Whether a device *records* the header is a property of that device, so verifying it is now a
  documented onboarding step rather than an assumption.

- **Enrolment: a tenant invites its provider, and the relationship is revocable**
  ([ADR-0024](docs/adr/0024-tenant-provisioning-is-a-request.md) §10). Connecting a tenant to
  its provider was nine manual steps across two clusters and an identity provider, three of
  which failed silently. A tenant admin now issues a one-time, short-lived invitation
  (`POST /v1/enrolment-invitations`), hands it over out of band, and the provider redeems it
  once (`POST /v1/enrolments/redeem`). Redemption mints the provider's standing credential —
  carrying `support:request` and nothing else — and records the catalog address plus this
  tenant's own credential for it, replacing steps 5 and 9 of the nine.

  **The invitation expires; the enrolment it produces does not.** An enrolment carries no
  capability beyond *asking*, so an expiry would not be a security control but a scheduled
  outage with a security-shaped name — on a timer nobody watches, the provider silently loses
  the ability to be asked for help, and the first symptom is a support request that cannot be
  raised during the incident that prompted it. The control is revocation
  (`DELETE /v1/enrolments/{id}`), which refuses the provider's very next request and closes
  the tenant's catalog access. What replaces expiry's one virtue is visibility: `GET
  /v1/enrolments` lists every relationship with who approved it, when, and a `last_used_at`
  taken from real requests rather than self-reported, so a dormant supplier relationship is
  found by looking rather than by remembering.

  Redemption is authenticated by the invitation itself and sits outside the RBAC machinery,
  because the caller has no gateway credential yet — obtaining one is what the call does. It
  is **not** an unauthenticated endpoint: §10 rejects that for the reason ADR-0017 §7a
  rejected an unauthenticated raise route.

- **The catalog mints per-tenant credentials, instead of only reading them from config**
  ([ADR-0024](docs/adr/0024-tenant-provisioning-is-a-request.md) §10,
  [ADR-0020](docs/adr/0020-the-device-catalog.md) §7a). §7a required a credential per tenant and
  had nothing that could issue one, so the caller table was the static `CATALOG_TENANT_TOKENS`
  map. `POST /tenants/{id}/credentials` (provider-only) issues one, `DELETE .../{cred_id}`
  revokes it, and `DELETE /tenants/{id}/credentials` revokes every live one — which is what
  ending an enrolment calls, as a single call rather than a client loop so it cannot
  half-happen.

  Config entries are **not** replaced and are checked first: they need no database, so an estate
  that has not adopted enrolment is unaffected by the catalog's store being unavailable. Issued
  credentials are stored as hashes, since this service only ever recognises a credential and
  never presents one. An unreachable database refuses one with
  `503 ERR_CATALOG_STORE_UNAVAILABLE`, never `401` — a 401 would have an operator diagnose an
  outage as a bad credential and re-issue one that was fine.

  *Partially built* — the provider console's side and the tenant console's UI are not in this
  change. See §10's status banner.

### Fixed

- **A refused registration left the device behind.** `_apply_register` validated
  `expected_tls_spki_sha256` and `fingerprint_policy` *after* `register_device` had written
  the device and started its pod. Both raise 400, so the caller was told a registration had
  failed that had in fact happened — with three consequences rather than one: the device was
  live and **unpinned**, on the very request whose purpose was to close the
  trust-on-first-use window; `audit_request("device.create", ...)` sits below both raises, so
  the registration that succeeded was never audited; and the retry could not work, because
  the corrected digest hit 409 and the PUT handled neither field. The only exit was to delete
  and start over.

  The trigger is a paste, not a typo: `openssl x509 -fingerprint -sha256` prints the
  colon-separated upper-case form. Both fields now parse into locals above the write, beside
  every other gate — which is what `_apply_update` always did. The two were split from one
  path "without a second copy of the gates to drift from this one", and then drifted, in the
  direction that writes.

- **A curated device type could be published with a transport no gateway can serve.**
  `transport` was the one curated field constrained on neither side: the catalog typed it
  `str`, and the gateway's `_validate_transport` accepts exactly `"sse"`. So a curator could
  publish a version with any value, it would appear in every assigned tenant's console, and
  every claim of it would 400 — each tenant separately, naming a field they never supplied
  and cannot change, while the curator, the only party who could fix it, was never told. Now
  a `Literal` at the write path. No matching DB `CHECK`, deliberately: every other constraint
  in `db.py` arrived with its column, so no row could already violate it, whereas this one
  predates the constraint and `ADD CONSTRAINT` could fail against legacy data — a catalog
  that will not start is worse than the one unclaimable type it prevents.

- **A restore preview could mint a plan token no other replica could verify.**
  `_plan_token_keys` fell back to a process-local HMAC key whenever `MCP_SECRET_KEY` was
  unset, and its comment asserted distributed mode could not reach that fallback because it
  "refuses to start without a key". It is reachable: that refusal is gated on
  `and not allow_plaintext_credentials`, an override its own error text advertises. Under it
  every replica mints tokens under a different key, so a preview on replica A and an apply
  routed to replica B answers `ERR_PLAN_STALE` — and the remedy that error prints, "preview
  again", fails again on (N-1)/N of attempts, forever.

  The refusal moves to preview, where the cause can still be named: a 503 saying
  `MCP_SECRET_KEY` is missing and must be identical on every replica. It had to *move* rather
  than gain a better message — `ERR_PLAN_STALE` is a legitimate, expected verdict, so it can
  never also mean "your stack is misconfigured". Embedded mode keeps the fallback: one
  process, and "preview again" really is the whole cost of a restart.

- **A device update accepted two fields and silently discarded them.** `POST /v1/devices`
  applies `expected_tls_spki_sha256` and `fingerprint_policy`; `PUT /v1/devices/{hostname}`
  parsed neither, so a request carrying either returned **200 with nothing changed**. Not a
  refusal — a success, reporting an outcome that had not happened. An operator tightening a
  device to `enforce` was told it worked and the device stayed on the fleet default; an
  operator pinning a key by hand was told it worked and the device stayed on
  trust-on-first-use, the field's whole purpose inverted and reported as success.

  The two now get **opposite** answers, and the asymmetry is the point.

  `fingerprint_policy` is policy, not evidence, and is **honoured** — settable, clearable
  with an explicit `null`, and audited as `device.fingerprint_policy`. Until now the only
  way to move a device from `warn` to `enforce` was to delete it and register it again: a
  cost paid to *tighten* a security control, which is the wrong direction for friction. It
  is keyed on the **presence** of the key rather than its truthiness, so an absent field and
  an explicit null stay different requests — collapsing them would make every unrelated edit
  clear the override, reintroducing the ADR-0015 carry-forward bug through the door built to
  fix its sibling.

  `expected_tls_spki_sha256` is trust, and is **refused** with a 400 naming both real paths
  (register with it, or approve the key the device is presenting now). Honouring it would be
  a quieter version of the bypass `_carry_fingerprint` already refuses to open: write the new
  key first and the probe that would have raised `key_changed` finds agreement instead — no
  verdict, no quarantine, no record that a trust decision was made. Allowing it only while a
  device is still unpinned was the other candidate and is worse: it races the health check
  that pins on first sight, and a security rule whose outcome depends on timing is one nobody
  can reason about at the point of use.

  Both checks run **before** the write, so a refused update does not apply its other fields.

- **`audit_request(..., subject=...)` raised `TypeError` instead of honouring the caller.**
  Its own comment promised that extra fields win on a key collision, "and an audit emitter
  must never be the thing that 500s a route it is only observing" — but `subject` and `rid`
  were passed both explicitly and via `**extra`, so supplying either did exactly that. Found
  by ADR-0024 §10's redemption route, which is authenticated by an invitation rather than a
  Principal and so has a subject to record that `subject_of` cannot know.

### Changed

- **The provider tier is frozen: a version tag no longer publishes the catalog image.**
  Lite and single-tenant are the supported editions, and the foundation gets finished before
  multi-tenancy is layered back onto it. The provider plane — catalog, enrolment, provider
  console, delegated support grants — is not withdrawn and nothing is deleted: `ci.yml` still
  builds and tests the catalog on every PR, and `deploy/kubernetes/catalog/` is untouched.
  What changes is only whether a tag pushes `device-mcp-catalog` to a public registry. The
  job stays runnable behind a manual `publish_catalog` input, because a job that cannot run
  is a job that silently rots — which would defeat freezing rather than deleting.

  **Not a reversal of [ADR-0013](docs/adr/0013-multi-tenancy.md).** D-1
  (single-tenant-per-stack) is unaffected; it was always the model the supported editions
  use, and a tenant stack has never needed the catalog — the base kustomization excludes it
  deliberately.

- **SyncGate Lite is its own product in its own repository.** The lite profile — its compose
  file and its deployment guide — moved to
  [SyncGate-Lite](https://github.com/benwold-lgtm/SyncGate-Lite) and is purged from this
  repo. Nothing about the gateway changes: Lite runs the same published images in embedded
  mode. The `:lite` image tag is unchanged this release, and is slated for retirement once
  that repo pins a semver tag — a moving tag has the same problem as `:latest`.

- **This repository is now `SyncGate`, and the console is `SyncGate-UI`.** URLs updated
  throughout. GitHub redirects the old paths, so existing clones and pulls keep working.

- **`POST /v1/admin/restore` is now two routes, split by scope, and the apply must
  reference the plan it was previewed from** ([ADR-0018](docs/adr/0018-device-credentials-by-reference.md)
  §6). `POST /v1/admin/restore/preview` writes nothing, needs only `backup:read`, and
  returns `plan_digest`/`plan_token`; `POST /v1/admin/restore/apply` is the destructive
  call, needs `backup:write`, and requires `plan_token` from that preview — a missing,
  mismatched, forged, or expired (default 7-day) token is refused as `ERR_PLAN_STALE`
  before anything is written. There is no `dry_run` body field on either — which operation
  runs is now which route you call, not a flag inside a body a caller controls. Restore is
  no longer single-use: preview as many times as needed while adjusting `on_conflict`, with
  no elevation held until the one call that applies. **Breaking**: update any script or
  client posting to the old single endpoint. See
  [docs/runbook.md](docs/runbook.md#restore-from-a-backup).

- **`fakeredis` floor raised to 2.37.1, and four production workarounds removed.** Earlier
  versions did not honour `decode_responses=True` for hash and stream replies, which had put
  defensive byte-decoding into `breakglass.py`, `shared/session_router.py` (twice) and
  `worker/dispatch.py` — production code accommodating a test double — plus a hand-written
  stub standing in for a client the fake could not be. 2.37.1 decodes both, verified across
  every construction path and against a real `RedisRegistryBackend` round trip, so all of it
  is gone and TG-6 is closed.

  **A range let CI and a local venv disagree about behaviour that code depended on.** CI had
  floated to 2.37.1 while a local environment sat on 2.36.0. Both passed, because the
  workarounds tolerated either. A dependency whose *behaviour* is depended upon needs a floor,
  not a range — which is what this change is.

  Two tests move onto the real backend as a result, and are stronger for it: the bulk-fetch
  test now really serialises and parses (keeping its pipeline count, which was the stub's
  other job), and the TLS-pin persistence tests distinguish "stored" from "mutated in memory"
  through an actual round trip rather than through a double built to imitate one.

### Fixed

- **A device's TLS pin no longer dies on an unrelated edit**
  ([ADR-0015](docs/adr/0015-endpoint-fingerprinting.md)). A PUT that changed only
  `rate_limit_rps` cleared `fingerprint_state`, `fingerprint_pinned_at` and
  `tls_spki_sha256`. One health check later the device **re-pinned by trust-on-first-use**
  to whatever key it next saw and reported `pinned` again, with nothing logged — an end
  state indistinguishable from a device that had never lost its pin.

  Three things made it a security bug rather than an inconvenience: §8's out-of-band
  pre-pinning (an operator verifying an SPKI by hand) was destroyed by the next edit and
  cannot be re-established by observation; the re-pin runs through the ordinary first-sight
  path, so it produces **no `key_changed` verdict and no quarantine** — the alarm ADR-0015
  exists to raise cannot fire, because the gateway believes it is meeting the device for the
  first time; and anyone holding `devices:write` could therefore clear a pin with a no-op
  edit.

  **The cause was already known on the other caller.** `plan_fingerprint_restore` writes the
  live fingerprint back precisely because `replace_device` "builds a fresh `DeviceConfig`
  from registration inputs alone". Restore compensated; the device-update route did not, and
  no test paired a pin with a PUT. The carry-forward now lives in `replace_device`, so it
  holds for every caller including ones not yet written; restore still writes afterwards and
  still wins, which is correct — it arbitrates between an archived record and a live one, a
  question the registry cannot answer.

  The pin is carried **even when `base_url` changes**, deliberately: repointing a device is
  a trust change, and the designed way to accept one is the `key_changed` → approve flow
  (§6), loud and audited. Resetting instead would be silent, and would leave "change the
  URL" as a way to launder a new key past the pin. What is preserved is the whole trust
  record, not just the pin — an *unpinned* device can still carry a per-device `enforce`
  policy, and dropping that downgrades it toward trusting more.

  Found on a live cluster during an unrelated credential migration, and verified there
  against the same reproduction after the fix.

### Added

- **The design for agent-initiated, plan-bound device writes is accepted; its HTTP surface
  is deliberately not mounted**
  ([ADR-0022](docs/adr/0022-agent-initiated-device-writes-are-plan-bound.md)). Propose,
  Review and Apply were built (`POST /v1/devices/plans`, `GET /v1/devices/plans/{id}`,
  `POST /v1/devices/plans/{id}/approve`, `POST /v1/devices/plans/apply`) and then taken back
  down before any release carried them. **No version has ever served these routes**, and this
  one does not either — they are reachable only from a test, via
  `create_app(enable_write_planned=True)`.

  Recorded here rather than dropped silently, because the reasons are the release's own
  theme. Any one of them was sufficient:

  - The Redis proposal store writes the plan as plain JSON, so a register plan would put
    `auth.api_key`/`client_secret` into Redis in the clear — exactly what the gateway refuses
    to start rather than let `register_device` do.
  - Review could not actually happen through the interface: no list route, so a reviewer
    could not discover a proposal, and no reject route.
  - Nothing calls them at either end. Unmounting removes no capability from anyone.
  - Lite and single-tenant are the supported editions; this was pure surface on the plane
    that is frozen.

  **Not a retreat from plan-binding**, and not a change to the ADR. §4's repeatable grant
  already carries routine autonomy, verified in both store implementations rather than only
  the in-memory double. Gated by a `create_app` argument rather than by config or an
  environment variable, deliberately: a deployment must not be able to expose this by setting
  a variable. Re-mounting is one line, behind four conditions written at the mount site.

- **A deployment can refuse inline device credentials** —
  `gateway.credentials.require_references` (or `MCP_REQUIRE_CREDENTIAL_REFS`), **off by
  default** ([ADR-0018](docs/adr/0018-device-credentials-by-reference.md) §1). With it on, a
  registration, an update, or a restore that supplies an `api_key`, `client_secret` or
  `password` inline is refused with a message naming the field, the `*_ref` that replaces it,
  and how to turn the gate back off.

  Off by default is not timidity: turning it on is breaking for any fleet registered before
  references existed, so it is a deployment's decision when its secret store is ready — not
  one an upgrade makes for it.

  **Three guarantees, each with a test holding it:**

  - **Existing devices keep dispatching.** The gate is on the *write* path, not in the handler
    constructor where every other ADR-0018 rule lives. That asymmetry is deliberate: the
    rehydrate path builds the same handler from an already-stored device, so a constructor
    check would turn a policy change into an outage at the next restart.
  - **An ordinary edit still works.** A PUT that changes a rate limit and carries no credential
    is not refused — otherwise the gate would freeze every legacy device in place, unable to be
    touched until its secret was migrated. A PUT that *supplies* a new inline credential is.
  - **The §1a carve-out stays reachable.** A `grant_type=refresh_token` device holds a
    gateway-minted token that cannot be held by reference, so it is never counted as an inline
    secret. Counting it would refuse the device for failing to do something the ADR calls
    impossible.

  The restore path is gated too, which is F-67's rule applied to §1: a `backup:write` holder
  must not be able to reinstate a device that a fresh registration would refuse. The **dry run
  predicts it**, so the preview and the apply agree.

  All three guarantees, and both refusals, were then **verified on a live multi-replica
  cluster** rather than only in the suite — including the one a unit test can only approximate:
  a device registered with an inline credential before the gate went on kept dispatching
  end-to-end *across a restart of every gateway and worker pod*, which is precisely the moment
  a constructor check would have taken it out.

- **A startup inventory of devices still holding a credential inline.** The number nobody could
  answer before: §1's migration is "move every device to a reference, then turn the gate on",
  and a fleet had no way to know how far through that it was — the gate could only be flipped
  and the breakage discovered. Logged at INFO with the gate off, at WARNING with it on (naming
  the devices, since those are exactly the ones that can no longer be updated or restored as
  they stand). Best-effort by construction: it reads a credential's *shape*, never its value,
  and a stack never fails to start because a diagnostic could not be produced.


- **An OAuth2 device can hold its `client_secret` and `password` by reference**
  ([ADR-0018](docs/adr/0018-device-credentials-by-reference.md) §1). §1a's table has always
  listed them as by-reference alongside the API key; the code had no such thing — `OAuth2Auth`
  carried no reference field at all and `client_secret` was mandatory and inline. That is the
  concrete reason §3's archive simplification is blocked: an archive of an OAuth2 fleet is
  still a credential dump whatever else §3 says about archives.

  ```jsonc
  "auth": {
    "token_endpoint": "https://idp.example.com/token",
    "client_id": "gateway",
    "client_secret_ref": "secret://t-3f9a1c2b/devices/erp#client-secret",
    // password grant only:
    "grant_type": "password", "username": "svc",
    "password_ref": "secret://t-3f9a1c2b/devices/erp#password"
  }
  ```

  **Two references, not one, and that is the decision rather than an accident of naming.**
  A single `credential_ref` was enough while an API key was the only by-reference case. These
  two secrets are provisioned *and rotated* independently by the tenant, so folding them into
  one store path with two fragments would tie two different rotation schedules to one
  location — the opposite of what §1 buys.

  Everything that needs "every reference this device depends on" now reads one accessor,
  `AbstractAuth.credential_refs()`, rather than keeping its own list of field names — a second
  list is exactly how a handler gets added and silently skipped by one caller. The restore-time
  resolvability check reads it, so an unresolvable OAuth2 fleet is reported rather than passing
  as fine.

  **There is deliberately no `refresh_token_ref`** (§1a). The gateway is the only party present
  when a provider rotates one, so there is nobody else to write it and a reference model cannot
  describe it. It stays encrypted at rest, and `MCP_SECRET_KEY` remains a named, bounded,
  permanent exception rather than a debt. A test asserts the field's absence so symmetry does
  not "fix" it later.

  Inline `client_secret` still works untouched — this permits a reference, it does not yet
  require one. Supplying both is refused rather than resolved by precedence: any precedence
  rule makes the losing value invisible, so a reference that silently never took effect would
  look exactly like one that did. A bound handler re-serialises its **reference**, never the
  material it resolved, so an update after a dispatch cannot write the secret back into the
  registry.


- **A restore now says when this stack cannot resolve a device's credential reference**
  ([ADR-0018](docs/adr/0018-device-credentials-by-reference.md) §3). §3 states it as a
  consequence — restoring into a different stack "requires that stack to be able to resolve
  the references, which is an honest and visible failure rather than a silent one" — and it
  was neither: `restore.py` never touched the resolver, so a device whose `credential_ref`
  the target could not resolve was reported `restored` and failed at its first tool call.

  Every archived reference is now resolved **before anything is written**, so the **dry run**
  reports it while the restore can still be stopped. New on the report: `credential_warnings`
  (a count), a per-device `credential_warning`, and `credential_store_error`.

  **The two failure kinds stay two** (§7). A bad reference is one device's problem and is
  reported per device; an unreachable secret store is the *fleet's*, and is reported once in
  `credential_store_error` with **no** per-device results at all. Reporting an unmounted
  volume as N independent bad references is the exact misdiagnosis §7 is written against — it
  sends an operator to check N references when one mount is wrong. "No resolver configured on
  this stack" is a third, separately-worded case, because an operator told "store unavailable"
  would go looking for a mount that was never meant to exist.

  **The device still restores.** Refusing would couple a restore to the order the secret store
  came back in, so a DR rebuild of a large fleet would fail wholesale because the registry was
  restored first. The archive carries configuration, the configuration is valid, and the
  missing part is a secret somebody provisions separately (§2a).

  Deliberately **not** a persistent device field, unlike *needs reconnecting*: that one is
  cleared by the very act that fixes it (a human supplying a credential), whereas a missing
  secret is fixed by putting it in the store — and nothing tells the gateway that happened, so
  a stored flag would go stale and start lying. A skipped device is not warned about either:
  its reference is the live record's business, not this restore's.

### Changed

- **A backup archive no longer contains a live device credential**
  ([ADR-0018](docs/adr/0018-device-credentials-by-reference.md) §3). The OAuth2 refresh token
  is **excluded from every archive, unconditionally** — not encrypted more carefully,
  excluded. Everything that makes a device a device still travels: its registration, its
  `credential_ref`, and the operator-provisioned `client_secret` / `password` it points at.

  This applies a rule the archive already had rather than adding an exception to it. Exports
  have always omitted claims, leases, worker membership, streams, sessions, idempotency
  markers and rate-limit counters on the stated principle that an archive carries
  **registration inputs, not runtime state**. A refresh token is gateway-accumulated runtime
  state; it was slipping through only because it is stored inside `auth_config` next to
  genuine inputs.

  **What it costs, by grant, stated plainly:**

  | Grant | After a restore |
  |---|---|
  | `client_credentials` | **Seamless.** `client_secret` survived; the gateway re-runs the token exchange on first use |
  | `password` | **Seamless.** `username` and `password` survived; same path |
  | `refresh_token` | **Needs re-authorization by a human** |

  No archive design can avoid the third row: a refresh token exists because somebody
  consented once, out of band, and nothing that can be put in an archive re-mints a token
  that required consent. Excluding it stops the archive *pretending* otherwise while carrying
  a copy that many providers have already invalidated server-side.

- **A restore says which devices need a human, and the device keeps saying so.** Two new
  per-device outcomes — `restored_needs_reconnect` and `would_restore_needs_reconnect` — plus
  a top-level `needs_reconnect` count on the restore report, so a **dry run** predicts the
  cost while it can still be stopped. The affected device is then left in a new persistent
  state, `credential_state: "needs_reconnect"`, which appears on **both** the device detail
  view and the fleet **list**.

  On the list deliberately: `fingerprint_state` is the precedent for this kind of field and
  carries a defect worth not repeating — it is absent from the list projection, so a device
  awaiting approval is invisible until somebody opens it. A device needing reconnection after
  a restore is precisely what an operator scans a list for.

  `credential_state` is **not a health reading** and clients must not render it as one. Such
  a device may be perfectly reachable, its pod running, its spec fetching; what it cannot do
  is authenticate. It clears when a new credential actually arrives — a PUT that supplies one
  — and survives a PUT that does not, so changing a rate limit cannot silently mark a device
  reconnected that nobody reconnected.

### Fixed

- **The test suite no longer poisons itself through a shared on-disk registry.** The embedded
  backup tests built apps whose SQLite store defaulted to the *relative* `./data/devices.db`,
  so every run wrote its fixture devices into the repository working directory and left them
  there. They then loaded on the next run of any test that actually enters the app's lifespan,
  which probed each one — turning a 0.7s startup into 18s and failing `test_main.py`'s
  `test_livez_does_not_require_auth` with `Semaphore is bound to a different event loop`.

  Contributors would have seen a test fail that passes on a fresh clone, with the cause
  sitting in a gitignored directory. Each app now gets its own throwaway working directory.
  This was pre-existing rather than new: `origin/main` fails identically once the leaked
  database is put in place, and passes without it.

- An `auth_config` the exporting stack cannot parse is now **omitted from the archive** rather
  than copied through unexamined. The exclusion above can only be *proved* on a payload the
  exporter could read, and such a device is already dispatching without credentials on the
  stack it came from.

### Added

- **Break-glass access is individually attributable, expiring, and loud**
  ([ADR-0023](docs/adr/0023-gateway-break-glass-attribution.md)). A `gateway.rbac` entry may
  now carry `break_glass: true`, which moves it from "a shared admin key" to one
  individually-generated, individually-held credential per authorized person. The flag is
  **selective by design** — CI keys, test fixtures and machine credentials stay exactly as
  they are, because making routine automation loud, flagged and expiring would be noise for no
  security benefit.

  A flagged entry is held to three rules, all fatal at startup with **no `allow_...`
  override**: `name` is mandatory (without it the audit subject falls back to the *role*, so
  two people with two different credentials both appear as `key:admin` — the
  shared-anonymous-credential problem arriving through an omitted field); `key` is a
  `secret://` reference resolved through the [ADR-0018](docs/adr/0018-device-credentials-by-reference.md)
  credential store, never a literal (a literal fails "never in configuration" however
  carefully the value was generated — the document still carries the credential); and
  `issued` (YYYY-MM-DD) is mandatory, because an entry with no issue date has indefinite
  validity, which is the thing the lifetime rule exists to prevent.

  Lifetime defaults to 90 days (`gateway.break_glass_expiry_days`), with escalating notice at
  14 days and again at 3 — never a silent cutoff, because a break-glass credential that
  expires quietly is discovered dead at the worst possible moment. **An expired credential
  stops working; it does not stop the gateway** — refusing to boot would turn a
  credential-hygiene lapse into an outage of the mechanism that exists for outages. It is
  dropped, logged at ERROR, and everything else keeps serving; requests are then refused with
  401 and never served anonymously.

  Every use emits a dedicated `auth.break_glass` audit record (`severity: high`, WARNING),
  distinct from ordinary static-key authentication rather than folded into request logging
  where it would read as unremarkable. Every **activation** — a use following a quiet gap,
  default 60 minutes — additionally emits `auth.break_glass.activated` (`severity: critical`,
  ERROR) and increments `mcp_break_glass_activations_total`. One incident worked through over
  hours is one activation however many calls it takes; notifying per request would bury the
  signal during exactly the incident it exists to announce.

  **Reactivation frequency raises a review flag and never blocks.** More than
  `gateway.break_glass_review_threshold` activations (default 3) within
  `gateway.break_glass_review_window_days` (default 30) flags the credential for review — a
  call budget that could cut off a legitimate incident response mid-session is the one
  failure a break-glass mechanism cannot afford. The tell worth watching is not call volume
  but a credential reactivating week after week, which means normal work has started reaching
  for the emergency path and needs its own credential.

  New metrics: `mcp_break_glass_uses_total`, `mcp_break_glass_activations_total`,
  `mcp_break_glass_review_flags_total`, `mcp_break_glass_expiry_timestamp_seconds` (absolute,
  not days-remaining — a countdown set at startup goes quietly wrong on a gateway that has
  not restarted in a month). `deploy/kubernetes/prometheus-rules.yaml` ships the matching
  alerts, which are the notification path
  [ADR-0017 §4](docs/adr/0017-provider-authority-is-delegated.md) requires: `MCPBreakGlassActivated`
  pages with **no `for:`** delay, where every SLO and operational alert in that file waits 2–15
  minutes to filter transients.

  All defaults are starting values to be tuned from operating history, not final ones.

- **`gateway.api_key` is now break-glass in an OIDC deployment — and only there**
  ([ADR-0023](docs/adr/0023-gateway-break-glass-attribution.md) slice 4). The rule is
  conditional on deployment shape, not on which config field the key sits in. With
  `gateway.oidc` enabled, `gateway.api_key` / `MCP_GATEWAY_API_KEY` and `MCP_ADMIN_KEY`
  authenticate *only* when the JWT path fails or is absent — break-glass in substance — so
  they get the loud treatment above. With no OIDC configured there is nothing to fall back
  *from*: the key is the deployment's ordinary everyday credential and is left exactly as it
  works today, because flagging it there would fire a high-severity event on normal traffic.
  `MCP_VIEWER_KEY` is never flagged in either shape: break-glass exists to *repair* a broken
  deployment, and a read-only credential cannot.

  **The limits are stated rather than implied.** These keys have no configured name, so the
  audit records that break-glass was used and cannot say by whom — the events carry
  `attributable: false` — and no `issued` date, so no expiry applies. Flagging makes them
  loud; only a named `break_glass: true` entry makes them attributable and expiring. A
  startup warning says exactly this, escalating once named entries exist (the bootstrap
  window is then over), and calls out the BFF password-session hazard by name.

  ⚠️ **Upgrade note for OIDC deployments whose console relays this key** — see
  [docs/upgrade.md](docs/upgrade.md). Nothing breaks and nothing is blocked, but every
  console login becomes a break-glass use until that path gets its own named, unflagged
  `console`-role entry.

## [0.3.5] - 2026-08-21

> **The provider-plane work built here was superseded before it shipped, and is not in this
> list.** [ADR-0013](docs/adr/0013-two-plane-tenancy-and-the-provider-plane.md)'s multi-issuer
> trust and elevated grants were implemented, verified against real identity providers, and
> then replaced in design by
> [ADR-0017](docs/adr/0017-provider-authority-is-delegated.md)–[0021](docs/adr/0021-separate-console-applications.md),
> which move authority over a tenant to that tenant. The code was removed rather than released,
> so no version ever offered it — the entries are omitted instead of being announced and then
> deprecated. See
> [ADR-0016](docs/adr/0016-reaching-many-tenant-gateways.md) (Rejected) for why the direction
> changed.
>
> Withdrawn on that basis, and **now removed from the code**: the *provider* second issuer with
> its per-issuer `plane` and server-side scope ceiling, elevated grant claims with their
> entitlement intersection, the single-use consumption record, and the `grant=<id>` audit field.
> The fixes and the changes below are independent of it and stand.
>
> `gateway.oidc.issuers` itself **survives** as a neutral capability — a deployment may trust
> more than one of *its own* identity providers — minus everything that made it the provider
> arrangement. `plane`, `step_up_acr`, `grant_claim` and `entitlement_claim` are no longer read;
> a config still setting them is ignored, not refused.

### Added

- **A device's upstream credential can now be held by reference instead of at rest in the
  gateway** ([ADR-0018](docs/adr/0018-device-credentials-by-reference.md), slices 1–3).
  Registration accepts `auth.credential_ref` in place of `auth.api_key`:

      secret://t-<tenant>/devices/<device>#<key>

  The registry stores the *reference*; the material is resolved at dispatch, held for that
  dispatch, and never written back, cached to disk, or included in an archive. The scheme is
  deliberately backend-neutral — naming the backend (`vault://`) would bake a deployment choice
  into every device record, so an archive could only be restored into a stack running the same
  product.

  **An exported archive of an entirely operator-provisioned fleet therefore stops being a
  credential.** It can be stored in Git, reviewed in a pull request, and diffed between
  environments.

  Three resolver backends share one interface: a Kubernetes Secret or CSI volume, an external
  manager, and a local file tree for Lite and embedded mode. Lite gains a file it already
  effectively had — not a dependency on a secrets product.

  **This does not cover gateway-minted credentials** (§1a). An OAuth2 `refresh_token` is minted
  mid-exchange and stays encrypted under `MCP_SECRET_KEY`; for a device using one, a key
  compromise carries exactly the same cost as before.

- **Registration refuses a credential it cannot honour, at registration rather than at first
  call.** Inline and by-reference together is a 400 naming the exclusivity — refused rather
  than resolved by precedence, because any precedence rule makes the losing value invisible and
  a reference that never took effect would look exactly like one that did. A malformed
  reference is also a 400 at registration, not a device that looked fine when it was added and
  fails on its first dispatch.

- **Two dispatch error codes that do not collide with `ERR_CIRCUIT_OPEN`**:
  `ERR_CREDENTIAL_UNRESOLVED` (this device's reference is bad — permanent, one device) and
  `ERR_SECRET_STORE_UNAVAILABLE` (the store is unreachable — transient, fleet-wide). Collapsing
  the two makes a sealed store look like twenty broken devices and opens every device's breaker
  on a fault none of them had.

### Changed

- **Upgrade order is now a constraint for deployments using by-reference credentials**
  ([ADR-0018 §7b](docs/adr/0018-device-credentials-by-reference.md)). **Upgrade workers first,
  then the gateway, and only then migrate any device to a reference.**

  A worker that predates the resolver wiring fails *closed* — nothing invalid is sent upstream —
  but it records no reason on the device record, because it successfully spawns the pod from a
  cached manifest and only the later health check fails. The device shows as unreachable with an
  empty `spawn_error`. A device only becomes by-reference through a gateway API call, so wiring
  the workers first means the window never opens; rolling the gateway first opens it for the
  length of the worker rollout.

- **Secret-store guidance corrected for mounted files** ([ADR-0018 §7a](docs/adr/0018-device-credentials-by-reference.md)).
  For a Kubernetes Secret or CSI volume the store is a dependency of *starting a pod*, not of
  serving a request: deleting the Secret does not reach a pod that already mounted it, modifying
  it re-projects within ~30–60s, and a pod that restarts without it fails `FailedMount` while the
  running pods keep serving. §7's fleet-wide breaker and its resolution cache describe the
  *networked* backends only.

  **Consequently, offboarding by deleting a secret is not immediate.** The credential stays
  readable in every pod that already mounted it until that pod restarts. Where revocation must
  take effect at once, rotate or empty the secret's *key* — only modification re-projects.

- **A tenant's identifier is now opaque from birth, and the namespace is a prefix rather than
  a computation** ([ADR-0019](docs/adr/0019-opaque-tenant-identity.md), superseding
  [ADR-0014](docs/adr/0014-tenant-namespace-naming-and-network-isolation.md) §1). Namespaces
  keep the `mcp-t-<16 hex>` shape they already had, so manifests, labels and Cilium selectors
  are untouched; what changes is that the suffix is **minted at random** rather than derived
  by a keyed HMAC over the customer's identifier.

  The goal is unchanged and was always right — a namespace name is not encrypted, so a
  customer name written there survives crypto-shredding and leaks into every `kubectl` output,
  metric label and dashboard in the estate. What went is the machinery for concealing a value
  we had chosen to make revealing: `MCP_TENANT_NAMESPACE_KEY` and its generation, distribution
  and rotation; the domain-separation construction; the standing rule never to reuse that key
  material for the audit pseudonym; and the collision assertion that truncating a MAC requires.
  Determinism is not lost, because nothing is computed — **the identifier is itself the durable
  record**, read from the declarative source GitOps and restore already read.

  `tools/tenant_namespace.py` is replaced by `tools/tenant_id.py` (`new`, `check`,
  `namespace`), which has tests where its predecessor had none.

  **The separator is a hyphen for a load-bearing reason.** The natural spelling of a prefixed
  identifier, `t_7f3a91c4`, is not a valid DNS-1123 label — so it could be neither a namespace
  name nor the per-tenant console hostname of ADR-0021 §5, where a public CA would also refuse
  the certificate. The ADRs were corrected before implementation.

  **Migration is a rename, which for a namespace means recreate.** No action for single-tenant
  or Lite deployments, which never used the pseudonym. An identifier is never reissued, even
  after a tenant departs (ADR-0019 §4).

- **The OIDC audit subject is now `oidc:{issuer}#{sub}`** (was `oidc:{sub}`). `sub` is
  unique *within* an issuer, not globally — `admin` at one IdP and `admin` at another are two
  different humans, and collapsing them puts both on one line of the hash-chained audit with
  no symptom, because both requests succeed. **This affects existing deployments**: audit
  records written before the upgrade carry the short form, and anything parsing the subject
  needs to accept both.

### Removed

- **The ADR-0013 provider plane, in full** — `device_mcp_gateway/grants.py` (the elevated-grant
  verifier, the entitlement intersection and the Redis consumption store), the per-issuer `plane`
  and its server-side scope ceiling in `oidc.py`, the `grant=<id>` audit field, the
  `mcp_elevated_grants_total` metric, and the `grant:used:*` Redis key. Superseded in design by
  [ADR-0017](docs/adr/0017-provider-authority-is-delegated.md), where authority over a tenant is
  **delegated by that tenant** rather than asserted by the provider.

  **No released version ever offered any of it**, so there is nothing to migrate and no
  deprecation period to serve — which is exactly why it is being removed rather than announced.
  A deployment whose config still carries `plane`, `step_up_acr`, `grant_claim` or
  `entitlement_claim` keeps working; those keys are ignored.

  Multi-issuer OIDC (`gateway.oidc.issuers`) is **kept**, with the two rules that make it safe —
  the issuer resolved from `iss` first with the decode pinned to that issuer's keys, and
  `group_roles` per issuer with no shared fallback. Trusting two of your own IdPs is a reasonable
  thing to want; trusting the provider's was the part ADR-0017 removed.

- **`MCP_TENANT_NAMESPACE_KEY`** and the derivation it keyed. Nothing reads it; a deployment
  that still sets it is not broken, only ignored. Existing namespaces keep working — they are
  valid identifiers by shape, whatever produced them.

### Fixed

- **Embedded mode no longer leaks an MCP session→owner entry per abandoned session.**
  Distributed mode was already safe: `SessionRouter` pipelines `hset` with `expire`, so an
  abandoned session costs one Redis key for at most 24 h. Embedded mode kept the same fact in
  a plain dict, written on `initialize` and removed only on an explicit `DELETE` — nothing
  reclaimed it, so every crash, timeout or restart between the two leaked an entry for the
  life of the process.

  Invisible while the only MCP clients were long-lived agents that tear their own sessions
  down. It stops being invisible for a caller that opens a session per operation, which is
  what a console tool-invocation route does. `ExpiringOwners` now backs the embedded map,
  using **the same TTL constant** as the distributed side so the two cannot drift into
  different definitions of an abandoned session. Explicit teardown is unchanged and still
  immediate.

- **A failed JWKS refresh now names the issuer and the error type.** Several httpx timeout
  exceptions stringify to the empty string, so the most common IdP misconfiguration there
  is — the JWKS host unreachable because a port is blocked by a NetworkPolicy or firewall —
  logged `OIDC JWKS refresh failed ...: ` with nothing after the colon. Meanwhile
  authentication *silently degrades* to static break-glass keys, so the blank line was the
  only signal.

  Found by pointing a real gateway at an IdP listening on a port the shipped NetworkPolicy
  does not allow. Note for operators upgrading: the egress allowlist in
  `deploy/kubernetes/networkpolicy.yaml` covers 80/443/8080/8443/9440 — **an IdP on any
  other port needs adding**, and unlike a device it will not fail a health check.

- **`gateway.oidc` and `gateway.tenant_id` no longer warn "unknown config key — ignored".**
  Neither was declared in the config-validation schema, so every deployment that actually
  enabled OIDC was told at startup that its OIDC block was a typo and was being ignored —
  while the gateway read and honoured it. An operator following the warning would have
  deleted working authentication config.

  It survived the suite because every OIDC key in the shipped `config.yaml` is commented
  out, so the "shipped config validates clean" test never exercised one. Found by pointing a
  real gateway at two real IdP realms. `oidc` is declared as an opaque mapping on purpose —
  its structure is validated by `OIDCConfig`, which fails hard at startup, and a second
  warn-only schema would be a weaker duplicate that drifts.
### Security

- The OIDC discovery-pinning fix and the plaintext-IdP refusal (TM-I-05) **shipped first in
  [0.3.4](#034---2026-08-21)**, a security-only patch cut from the `v0.3.3` tag so that a
  deployment on 0.3.3 could take the fix without the feature work in this release. They are
  included here; see the 0.3.4 notes for the detail and for who was affected.

## [0.3.4] - 2026-08-21

**A security patch on the 0.3.3 line, and nothing else.** Cut from the `v0.3.3` tag rather than
from `main`, so a deployment running 0.3.3 could take this fix without also taking the feature
work that ships in 0.3.5.

### Security

- **The gateway now pins the OIDC discovery document to the configured issuer**
  ([TM-I-05c](docs/threat-model-identity.md)). `_resolve_jwks_uri` took `jwks_uri` straight out
  of the discovery document without checking that the document declared the issuer it was
  fetched from, then cached it for the life of the process.

  Pinning `iss` and `aud` at decode time did **not** cover this. Those are claims in a token an
  attacker mints, so they can be set to whatever the gateway expects; what decides forgery is
  whose keys the signature is verified against, and that came from the document. A spoofed
  discovery response — an on-path attacker where the fetch is plaintext, or a compromised or
  misconfigured proxy in front of the real IdP — could name an attacker-controlled `jwks_uri`,
  have the attacker's signing keys installed, and mint tokens for any role. **One spoof
  poisoned every subsequent validation until restart.**

  **Who was exposed:** deployments with `gateway.oidc` enabled that did *not* set an explicit
  `gateway.oidc.jwks_uri`. Setting that key short-circuits discovery entirely, so those
  deployments never reached the affected path. Present in 0.3.3 and earlier.

  The document's declared issuer is now compared against the configured one (trailing slashes
  normalised, everything else exact), a mismatch is refused, and the cache is populated only
  after every check — so a refused document leaves nothing behind. Regression tests drive the
  real HTTP path rather than stubbing the refresh, which is why the defect survived until now:
  every existing JWKS test stubbed `_refresh`, so nothing exercised the code between the
  response and the cache.

- **A plaintext `http://` IdP is refused at startup** ([TM-I-05a/b](docs/threat-model-identity.md)),
  unless `security.allow_plaintext_idp: true` is set deliberately. The egress URL policy permits
  `http` by design — its job is SSRF, not transport encryption — so nothing had ever enforced
  TLS to the IdP, on either the gateway or the console BFF. The check covers the issuer *and* an
  explicit `jwks_uri`, and now also a `jwks_uri` discovered at runtime, which had never passed
  the startup checks a configured one gets.

  Deliberately **not** folded into `allow_private_targets`: where an IdP sits and whether it has
  TLS are independent properties, and one flag for both would leave an operator opting out of
  address checks in order to permit plaintext. There is no loopback exemption either — a silent
  "localhost is fine" rule cannot be warned about, because nothing gets set to warn on. A lab
  IdP over `http` needs the flag, which warns loudly at startup.

**Upgrading:** no configuration change and no migration. A deployment pointing at an `http://`
IdP will now **refuse to start** — set `security.allow_plaintext_idp: true` to keep the previous
behaviour, or move the IdP to `https`. A deployment whose IdP serves a discovery document
declaring a different issuer than the one configured will also refuse; that is the defect being
fixed, and the mismatch should be investigated rather than worked around.

## [0.3.3] - 2026-08-13

Two security capabilities that constrain what the gateway talks to and what it lets near it:
**endpoint fingerprinting** ([ADR-0015](docs/adr/0015-endpoint-fingerprinting.md)) and
**tenant network isolation** ([ADR-0014](docs/adr/0014-tenant-namespace-naming-and-network-isolation.md)),
plus the fix that keeps the first of them alive across a restore. Both were verified against a
live cluster before release rather than on unit tests alone — which is how three of the defects
below were found, two of them in the fixes for the other one.

### Read this before upgrading

- **⚠️ The Kubernetes NetworkPolicies now deny by default, and that is a behaviour change to
  your cluster, not just to this app.** Before this release every rule carried `ports:` with no
  `from:`/`to:`, which permits *any peer in the cluster* — the manifests described themselves as
  restricting ingress to port 8000, and they did, from anywhere (F-68). Applying the new
  manifests adds a `default-deny-all` policy plus explicit allows for intra-namespace traffic,
  DNS and monitoring scrape. **If anything outside the gateway's namespace talks to it today,
  it will stop.** Measured before the fix: a pod in an unrelated namespace got `HTTP 200` from
  `/health`. Decide deliberately whether that traffic was intended, and add an allow for it if
  so — do not skip the upgrade to keep it working by accident.
- **`deploy/kubernetes/` no longer hardcodes a namespace.** `kustomization.yaml` is now the
  single source of truth and kustomize renames the `Namespace` object as well as retargeting
  every resource. If you deploy these manifests through GitOps with your own namespace
  substitution, check it still lands where you expect before rolling.
- **The manifests now wire `MCP_ADMIN_KEY`, `MCP_VIEWER_KEY` and `MCP_ALLOW_PRIVATE_TARGETS`,
  which they never did.** They previously existed only as hand-applied additions, so a stack
  built from the manifests alone came up with no break-glass RBAC keys. If you added them by
  hand, the manifest values will now take effect — reconcile them before upgrading rather than
  discovering the difference at a restart.
- **Every device gains a fingerprint on its next probe, and the first one is trust-on-first-use.**
  The default policy is `warn`: a device whose endpoint key changes is flagged and **keeps
  serving**. Nothing stops until you opt into `security.fingerprint_policy: enforce`. TOFU
  establishes a baseline; it does not validate anything, so a device that was already pointed
  at the wrong endpoint pins the wrong value. For the devices where that matters, supply
  `expected_tls_spki_sha256` at registration and the ordinary comparison path enforces it with
  no TOFU window.
- **Restore now reports `fingerprint_warnings`, and you should read it.** An archive taken
  before this release carries no pins, so every device it restores will trust-on-first-use on
  its next probe. The restore says so per device rather than passing silently. Re-export once
  you are on 0.3.3 so your archives carry pins.
- **Why a patch number for two new capabilities.** [docs/releasing.md](docs/releasing.md) §1.5
  says a new capability is a minor bump even at `0.x`, and this release departs from it for the
  same reason `0.3.1` and `0.3.2` did. `0.4.0` is committed to **removing** the deprecated
  HTTP+SSE endpoints and to the MCP `2026-07-28` protocol shift, deliberately paired so clients
  face one transport upgrade rather than two. Numbering this release `0.4.0` would either break
  that commitment or collapse the deprecation window. Both features here are additive and
  default to non-blocking behaviour — but read the NetworkPolicy note above, which is the one
  change that can take traffic away.
- **The HTTP+SSE deprecation clock is unchanged.** `GET /v1/devices/{hostname}/sse`,
  `POST /v1/devices/{hostname}/messages`, `GET /v1/fleet/sse` and `POST /v1/fleet/messages`
  still work and are still scheduled for removal in **0.4.0**. This release does not move that
  date in either direction.

### Security

- **[ADR-0015](docs/adr/0015-endpoint-fingerprinting.md) — endpoint fingerprinting (F-69).**
  Registering a device established **where** the gateway may talk, never **what** it was
  talking to. The URL policy, per-hop redirect re-validation and `base_url` pinning all
  constrain the *address*; nothing noticed when the thing at that address became a different
  thing — DNS repointed, a host rebuilt, an appliance replaced. The gateway kept sending
  credentials to it and never mentioned the change.

  A fingerprint is now recorded per device and compared on every reachability check, across
  **three dimensions that are never collapsed into one "verified" flag**: the TLS **SPKI**
  digest (cryptographic), the upstream's *declared* `serverInfo` / OpenAPI `info`
  (self-reported, therefore spoofable — inventory that doubles as a change signal, and data
  the probe already fetched and discarded), and the existing `tools_revision`. A single badge
  over the top would lend the declared fields weight they do not have. The declared value
  comes from the `initialize` handshake for an MCP upstream and from the spec poll for an
  OpenAPI one, so a newly registered OpenAPI device shows no declared identity until its
  first poll.

  **The pin is the SPKI, not the certificate**, and that one choice decides whether the
  control is signal or noise: a routine renewal re-issues against the same key, so nothing
  fires. Pinning the certificate would alarm every 60–90 days per device across the fleet,
  and a control that fires constantly trains reflexive approval — the ADR-0013 §8 argument,
  unchanged. **The default is `warn` and the device keeps serving**; `security.fingerprint_policy:
  enforce` (per device, because the risk is not uniform) **quarantines** it instead —
  `tools/call` **and `resources/read`** are refused, while probes, diagnostics and
  `GET /v1/devices` keep working, so a quarantined device stays distinguishable from a dead
  one. Clearing the flag needs `devices:write` on `POST /v1/devices/{hostname}/fingerprint/approve`
  and is audited as the trust decision it is. Registration optionally accepts an
  `expected_tls_spki_sha256`, which converts trust-on-first-use into real verification by
  **pre-pinning** — no separate check, no TOFU window.

  ⚠️ **First use is TOFU and is labelled as such.** It establishes a baseline; it does not
  validate anything. If the endpoint was already wrong at registration, the wrong value is
  what gets pinned. A plain-`http://` upstream has **no** authenticated dimension at all.

- **ADR-0011 archives now carry the endpoint pins, and a restore never re-pins.** Without
  this every device silently re-TOFU'd on restore and the control was void from the first
  disaster recovery onward — precisely when an operator is least able to notice, and with
  nothing in any response to show for it. The archive gains a `fingerprint` block per device;
  `POST /v1/admin/restore` reports a per-device `fingerprint_warning` plus a top-level
  `fingerprint_warnings` count, which is also written to the audit record.

  Two findings from the build, neither anticipated by the ADR. **`on_conflict=overwrite`
  wiped the live pin even when the archive agreed with it** — `replace_device` rebuilds the
  record from registration inputs alone, so writing the fingerprint back is load-bearing
  rather than a no-op. And **`fingerprint_state` / `pending_tls_spki_sha256` have to travel
  too**, or a device exported mid-`pending_approval` returns `pinned` and restore becomes a
  route around the approval requirement.

  Where the archive and a live record disagree, **the live pin stays and the disagreement is
  reported** rather than applied: the archived value is historical, while the live one was
  established against the endpoint as it is now, quite possibly by an audited approval.
  Overwriting it would undo that decision silently and then, under `enforce`, quarantine a
  device nothing was wrong with. Archives written before this change still restore, and are
  **reported** as carrying no pin instead of passing as an unqualified success.

- **[ADR-0014](docs/adr/0014-tenant-namespace-naming-and-network-isolation.md) is now
  Accepted**, resolving its open question: **Tier 2 is a precondition for operating a
  provider-operated multi-tenant estate; a single-tenant deployment needs Tier 1 alone.**

  The tiers do not differ in depth — they differ in *who the boundary holds against*. Under
  Tier 1 a tenant re-opens cross-tenant reachability by **creating** a permissive
  NetworkPolicy in their own namespace, without deleting anything, because NetworkPolicy is
  additive-allow. Tier 1's guarantee is therefore RBAC-shaped, which is an acceptable
  internal control for one tenant and the wrong shape of guarantee for a customer boundary.

  New **`tools/tenant_isolation_policy.py`** generates the Tier-2 policies — one
  `CiliumClusterwideNetworkPolicy` per tenant, since a single cluster-scoped object has no
  self-reference in its selector language and cannot express "any tenant namespace other
  than the pod's own". `generate --from-cluster` discovers namespaces by label; `check`
  exits non-zero on an uncovered tenant.

  Verified on a live cluster: cross-tenant traffic blocked in both directions,
  intra-namespace and internet egress intact, and a tenant's own `podSelector: {}` allow-all
  NetworkPolicy **did not** restore cross-tenant reach — the specific claim Tier 1 cannot
  make.

  ⚠️ **A correction to the ADR as first published.** Its earlier draft proposed one
  cluster-wide object listing every tenant in `NotIn`, and stated that a tenant missing from
  that list would fail *closed*. Both were wrong. `matchExpressions` are ANDed, so "in a
  tenant-plane namespace" AND "not in any tenant namespace" matches the empty set: the
  object applies without error and enforces nothing, failing **open**. Two further shapes
  also applied cleanly and did the wrong thing — a deny-only policy flips the endpoint into
  default-deny unless `enableDefaultDeny` is false for both directions (which blocked the
  tenant's own intra-namespace traffic), and the namespace-label selector key is
  `io.cilium.k8s.namespace.labels.<label>`, not `io.kubernetes.pod.namespace.labels.<label>`,
  which matches nothing silently. All three were found by measurement, not review, and are
  recorded in the ADR's implementation notes and pinned by tests.

- **Tenant network isolation — the Kubernetes NetworkPolicies were peer-blind (F-68).**
  Every ingress rule carried `ports:` with **no `from:`**, and every egress rule `ports:`
  with no `to:` — which in NetworkPolicy semantics means *any peer in the cluster*. The
  architecture doc described the result as "Restricts ingress to port 8000", which was
  accurate and is exactly what made the gap invisible: it restricted **ports, not peers**.
  Measured on a live cluster rather than inferred — a pod in an unrelated namespace reached
  the gateway's `/health` and got `HTTP 200` with the full payload.

  Compounding it, NetworkPolicy is **pod-selected** and a pod matched by no policy is
  default-allow: the three policies named only the gateway, worker and Redis pods, so
  anything else landing in the namespace had unrestricted ingress *and* egress.

  `deploy/kubernetes/networkpolicy.yaml` is rewritten around a `default-deny-all`
  (`podSelector: {}`, both directions), with narrow allows re-opening one path at a time:
  DNS to kube-system, intra-namespace, the monitoring scrape to `:9100`, the ingress
  controller to `:8000`, and device egress now scoped by **peer as well as port**. The
  egress `ipBlock` excludes the cluster's own pod and service CIDRs — where other tenants
  live — while deliberately *not* excluding general RFC-1918 space, because devices
  legitimately live on the private LAN and blanket-blocking it would train operators to
  replace the rule with `0.0.0.0/0`.

  ⚠️ **This is a behaviour change on upgrade.** A device on a port outside the
  80/443/8080/8443 allowlist, or an ingress controller outside `ingress-nginx`, or a
  Prometheus outside `monitoring`, will stop working until the corresponding rule names it.
  The pod/service CIDR values are kind/kubeadm defaults and **must be verified** against
  your cluster. And verify enforcement itself: a CNI that does not implement NetworkPolicy
  accepts all seven objects and enforces none — see
  [testing-gaps.md TG-10](docs/testing-gaps.md).

### Added

- **[ADR-0014](docs/adr/0014-tenant-namespace-naming-and-network-isolation.md) — tenant
  namespace naming and default-deny network isolation** (Proposed). The namespace is a
  **pseudonym**, `mcp-t-<16 hex>`, derived by keyed HMAC via the new
  `tools/tenant_namespace.py`: keyed rather than hashed because a bare hash of a tenant
  identifier is reversible by dictionary attack, and pseudonymous because a namespace name
  is not encrypted and would therefore survive the crypto-shred that ADR-0013 §10 exists to
  provide. Deterministic, so GitOps, a rebuild and an ADR-0011 restore all recompute it —
  but provisioning still asserts non-existence, because a truncated-digest collision means
  two tenants sharing a namespace.

  Policy selects on **labels** (`mcp.gateway/plane`, `mcp.gateway/tenant`), since a naming
  convention alone is unenforceable. **There is no inter-tenant exception mechanism** — an
  earlier draft designed one and it was dropped for want of a single concrete use case;
  the reasoning is recorded in §6 so it is not re-litigated, and a real case gets its own ADR.

  An optional Tier-2 `CiliumClusterwideNetworkPolicy` is included for estates that need the
  deny to hold against a tenant with namespace-write, because Kubernetes NetworkPolicy is
  purely additive-allow: nothing can revoke an allow another policy grants, so under vanilla
  policy the boundary is held by RBAC rather than by the network.

- `deploy/overlays/tenant-example/` — a per-tenant kustomize overlay. The namespace is now
  parameterized from `kustomization.yaml` alone (kustomize renames the `Namespace` object
  *and* retargets every resource), and no resource file hardcodes it any more.

- **A disaster-recovery runbook, written from an actual rebuild** —
  [runbook.md § Rebuild a stack from nothing](docs/runbook.md#rebuild-a-stack-from-nothing-disaster-recovery).
  A portable archive was restored into a genuinely fresh stack on separate hardware — new
  Redis, new pods, a **different `MCP_SECRET_KEY`** — and verified by a `tools/call` on a
  restored device returning live upstream data. That closes **TG-7** and discharges
  [ADR-0011](docs/adr/0011-backup-and-restore.md)'s outstanding follow-up.

  **The archive was never the hard part.** Preflight, replay, manifest rebuild and
  `tools_revision` carry-over all behaved as designed. What stopped the rebuild three times
  was the **out-of-band dependency set** — things a backup deliberately does not carry, which
  had never been written down because nobody had rebuilt from nothing. The runbook now
  tabulates them: per-device TLS trust material (without it the gateway **fails closed at
  startup**), `MCP_ALLOW_PRIVATE_TARGETS` / `MCP_ADMIN_KEY` / `MCP_VIEWER_KEY` (which the
  `deploy/kubernetes/` manifests do not wire — they exist only as hand-applied additions), and
  DNS for any non-Kubernetes name in a `base_url`.

  The trap worth knowing: **without `MCP_ALLOW_PRIVATE_TARGETS`, restore refuses every
  private-address device and reports a correct policy refusal.** The response cannot
  distinguish a misconfigured new stack from a policy that legitimately rejects the device —
  a direct consequence of restore replaying through the real registration gates. Conversely,
  Kubernetes service DNS in `base_url`/`spec_url` needs **no** archive editing: it resolves
  unchanged in any cluster with the same service names in the same namespace.

- **TG-7, TG-8 and TG-9 in [docs/testing-gaps.md](docs/testing-gaps.md)** — the backup and
  restore shipped in 0.3.2 is implemented and unit-tested, but has not been demonstrated to do
  the job it exists for, and the register now says so. **TG-7** is disaster recovery proper:
  restoring into a genuinely fresh stack, with its own Redis and its own `MCP_SECRET_KEY`. The
  live dry run on the lab cluster does not count — every device came back
  `skipped / already registered`, which exercises the decrypt preflight and nothing else.
  **TG-8** is scale: export is a single synchronous response over the whole registry and
  restore replays devices one at a time, both only ever tested at 2–3 devices. **TG-9** is
  export and restore *mid key-rotation*, the scenario the `is_current()` double-encryption trap
  came from — a failure that raises nothing and surfaces only when a restored device
  authenticates upstream. [ADR-0011](docs/adr/0011-backup-and-restore.md) gains a pointer to
  all three; its decision text is unchanged.

### Changed

- **[ADR-0013](docs/adr/0013-two-plane-tenancy-and-the-provider-plane.md) is now Accepted**,
  with its three open questions resolved. `docs/multitenancy.md` is revised against it, and
  the "future `tenant` claim on `Principal`" seam is formally withdrawn — in-app tenancy is
  rejected **on merit**, not deferred on cost.

  **Grant lifetimes are absolute, never sliding**, because a sliding window never expires for
  an attacker who keeps working. act-on-tenant runs 60 minutes and re-authorizes, and a
  session holds it for **one tenant at a time** — concurrent grants would rebuild ambient
  estate-wide authority by accumulation. The two elevated grants **step up** (re-prove
  identity): `provider:invoke` at 15 minutes, `provider:credentials` single-use. Those are
  where a stolen provider session gets cashed out; act-on-tenant is the everyday motion, and
  step-up there would fire often enough to train reflexive approval. Renewal is a new audited
  act rather than an extension, and a grant gates *initiation*, not completion.

  **A tenant sees every act by a human provider principal in their own audit** — including
  reads, since "has someone been in my system" is exactly the question, while automated
  platform operations are not provider acts. The actor is pseudonymized **at write time**,
  which is a constraint on the audit writer rather than the UI: the record lands in the
  tenant's hash-chained audit, so anything written in the clear is readable by whoever can
  read the chain, and a hash-chained record cannot be redacted afterwards without breaking
  verification.

  **Offboarding uses per-tenant content keys.** Destroying a departed tenant's key leaves the
  provider-side chain verifiable while making the content unrecoverable, so immutability and
  erasure stop competing. ADR-0011 archives of that tenant are inside the shred — a backup on
  an independent expiry schedule would be a hole straight through the decision — and a
  per-tenant hostname is **never reissued**, so stale DNS or cached tokens cannot resolve onto
  a new tenant's stack.

  **This unblocks BFF audit logging, and constrains it:** the writer must pseudonymize at
  write time and encrypt per-tenant content under a per-tenant key from its very first record.
  Neither can be retrofitted into a hash chain.

### Fixed

- **F-70 — an invalid OpenAPI spec escaped the `ValueError` contract every caller depends
  on.** The translator's guard named `OpenAPISpecValidatorError`, which sits on a *different*
  branch of the library's hierarchy from the errors ordinary validation raises:
  `OpenAPISpecValidatorError → OpenAPIError` covers version *detection*, while the actual
  checks raise `OpenAPIValidationError → ValidationError → _Error`. Only the first was
  caught — so "cannot determine which OpenAPI version this is" converted cleanly, while a
  malformed `info`, a missing required field or a wrong type propagated uncaught.

  Every caller downstream catches `(SpecTooLargeError, ValueError)` to *report* a rejection —
  `spawn_error` at registration, a warning-and-skip in the health loop — so the escape
  bypassed all of it. A spec poll ended in an unhandled traceback instead of the warning the
  code was written to produce. Not an outage (the worker's per-device catch-all keeps the loop
  alive and the lock is released in `finally`), but the operator lost the reason.

  A second instance of the same shape sat one function earlier: `enforce_operation_count`
  runs **before** the validator and outside its guard, and called `.values()` on whatever
  `paths` held — so a `paths` that was not an object raised `AttributeError` past the same
  handlers. It now defers such a document to the validator, which is the thing that knows how
  to say *why* it is unusable.

  The guard now wraps the single validator call and converts **any** exception to
  `ValueError`, preserving the original type and message. Re-enumerating the two branches
  would have fixed today's failure and left the same trap for the next library release.
  Found while live-verifying ADR-0015 on a cluster.

- **`deploy/kubernetes/` never wired `MCP_ADMIN_KEY`, `MCP_VIEWER_KEY` or
  `MCP_ALLOW_PRIVATE_TARGETS`** — they existed only as hand-applied additions on the lab
  cluster, so a stack built from the manifests alone came up without break-glass RBAC keys
  and refused every private-addressed device with "resolves to a blocked address", which is
  correct behaviour that reads exactly like a bug. Found during the TG-7 disaster-recovery
  walk. `MCP_ALLOW_PRIVATE_TARGETS` is wired on **both** the gateway and the worker: in
  distributed mode the worker performs the fetch, so setting it on the gateway alone lets
  registration succeed while every subsequent call fails.

- `servicemonitor.yaml` pinned `namespaceSelector.matchNames` to `mcp-gateway`. That is a
  CRD field, which kustomize's `namespace:` transformer does **not** rewrite, so a
  retargeted deployment would have scraped the wrong namespace. The selector is now omitted
  — an omitted selector means "my own namespace", which is correct under any name.

## [0.3.2] - 2026-08-11

Backup and restore ([ADR-0011](docs/adr/0011-backup-and-restore.md)), two fixes that only a
live cluster could have found, and the decision that settles multitenancy. Cut deliberately
small: three changes had stacked up unverified against real infrastructure, and the longer that
queue grows the less a green verification tells you about *which* change was responsible.

### Read this before upgrading

- **`last_check` is now `null` for a device nothing has ever contacted.** The API shape is
  unchanged — `reachable` is still a bool — but `last_check` and `last_check_age_seconds` no
  longer carry the registration time for a device that was never checked. Read a null
  `last_check` as "never checked", which is a distinct state from a check that found the device
  dead. This is the only client-visible behaviour change in the release; see the F-66 entry
  under *Fixed* for what a device registered before the upgrade does.
- **`cryptography` now requires `>=44.0.0`** if you install the Python package rather than
  running the published image. The old floor (`>=41.0.7`) predated portable backup and was
  wrong the moment ADR-0011 shipped — see the third *Fixed* entry. Image users are unaffected;
  the lockfile has always pinned a version well above it.
- **Why a patch number for a new capability.** [docs/releasing.md](docs/releasing.md) §1.5 says a
  new capability is a minor bump even at `0.x`, and this release departs from it for the same
  reason `0.3.1` did. `0.4.0` is committed to **removing** the deprecated HTTP+SSE endpoints and
  to the MCP `2026-07-28` protocol shift, deliberately paired so clients face one transport
  upgrade rather than two. Numbering this release `0.4.0` would either break that commitment or
  collapse the deprecation window. Backup/restore is additive and sits behind its own scopes, so
  a patch number understates the feature without overstating the upgrade risk.
- **The HTTP+SSE deprecation clock is unchanged.** `GET /v1/devices/{hostname}/sse`,
  `POST /v1/devices/{hostname}/messages`, `GET /v1/fleet/sse` and `POST /v1/fleet/messages`
  still work and are still scheduled for removal in **0.4.0**. This release does not move that
  date in either direction.

### Added

- **[ADR-0013](docs/adr/0013-two-plane-tenancy-and-the-provider-plane.md) — two-plane
  tenancy** (Proposed). Settles what [ADR-0004](docs/adr/0004-single-tenant-per-stack.md)
  left open. Tenant stacks stay isolated and each keeps **its own IdP**; a tenant session is
  bound to one tenant at login and cannot learn that another exists (404 rather than 403,
  per-tenant hostnames so tenant selection is never an enumeration surface). Above them sits
  a separate **provider plane** — a manager-of-managers console for the party operating the
  platform, with its own IdP and its own `provider:*` scopes held in the BFF, not in any
  tenant's gateway. The plane is fixed at login from *which IdP authenticated* and is
  immutable for the session, and cross-tenant power is exercised as a discrete audited act
  on a named tenant rather than held ambiently.

  **`provider:admin` is deliberately not the gateway's `admin` role.** It is the everyday
  debugging grant — device read, configuration, governance history, lease management — with
  `tools:call` and credential access carved out into separate **elevated** grants
  (`provider:invoke`, `provider:credentials`), each time-boxed, individually justified, and
  separately audited. Tool invocation is the most consequential thing the gateway does, and
  routine debugging should not silently carry standing authority to actuate a customer's
  hardware. Credential access gets the same treatment: for the provider — who holds
  `MCP_SECRET_KEY` — a *ciphertext* [ADR-0011](docs/adr/0011-backup-and-restore.md) archive
  is a credential dump too, so no `backup:*` scope sits inside `provider:admin`.

  Two consequences to plan for. A tenant gateway will trust the **provider IdP as a second
  issuer** (`gateway.oidc.issuer`/`audience` become lists) so provider actions land in the
  tenant's own hash-chained audit as named humans instead of a shared admin key — and the
  **gateway** must bind issuer → eligible scopes server-side, because the BFF's login-time
  plane-fixing is a session-flow guarantee that a minted token can simply bypass. That makes
  `group_roles` per-issuer: kept flat across two issuers, a tenant's own IdP admin could
  create a group named whatever the provider mapping keys on and be handed provider scopes.
  Separately, cross-tenant *monitoring* aggregates from the existing Prometheus metrics
  plane, so the constant-use path holds no tenant API credentials at all.

  No code has changed yet — ADR-0013 is Proposed, with three open questions named in it.
  `docs/multitenancy.md` carries a pointer and is revised in full once it is Accepted.

- **[ADR-0011](docs/adr/0011-backup-and-restore.md) — backup and restore** (Accepted), the
  decision record behind the two entries below. Written **before** any code, which is exactly
  why it now carries an *Implementation notes* addendum: three of its claims turned out to be
  false of the code it was describing. Credentials are encrypted at rest in **distributed mode
  only** (embedded keeps them plaintext in the device record, one layer above SQLite's own
  encryption); §4's "restore replays through `register_device`, so the egress policy still
  applies" was **untrue** and became F-67; and Argon2id is not the visible cost the ADR
  predicted — roughly 0.12s, once per archive. The corrections are recorded as an addendum
  rather than quietly edited into the decision text, because an Accepted ADR is immutable and
  the gap between what it assumed and what the code did is the useful part.
- **[ADR-0012](docs/adr/0012-federation-credential-model.md) — credential model for BFF
  provider federation** (Proposed). Records that per-user OIDC relay is already shipped, so a
  per-provider service token would be a regression rather than a new trade-off; makes BFF-side
  hash-chained audit a prerequisite for federation instead of a parallel workstream.

- **Backup export ([ADR-0011](docs/adr/0011-backup-and-restore.md))** — `GET /v1/admin/backup`
  returns a **ciphertext** archive of the device registry and its tool-change governance
  record, with credentials encrypted under this stack's `MCP_SECRET_KEY`. `POST` to the same
  path takes `{"kind": "portable", "passphrase": "…"}` for a **key-independent** archive
  re-encrypted to a passphrase via Argon2id (`m=64 MiB, t=3, p=4`, random per-archive salt,
  parameters written into the envelope so raising the cost later cannot orphan older
  archives). POST rather than GET for that one so the passphrase never lands in a URL, proxy
  log, or shell history. Dead letters are opt-in via `include_deadletters`.

  Three new scopes — `backup:read`, `backup:write`, `backup:export-portable` — plus a
  `backup` role for scheduled jobs holding the first two only. `backup:export-portable` is
  never implied: a portable archive is every device credential in the stack behind one
  passphrase. Every export is audited, both kinds, and the passphrase is never a log field.
  **An archive never contains `MCP_SECRET_KEY`** — keep backing that up out-of-band.

  Two notes for operators. A ciphertext export **returns 409 when no `MCP_SECRET_KEY` is
  configured**, because the alternative is an archive labelled ciphertext that contains
  plaintext credentials. And in **embedded mode** credentials are stored as plaintext in the
  device record (encryption happens in the SQLite layer), so export encrypts them on the way
  out — the archive's security property is the same whichever mode produced it.

- **Backup restore ([ADR-0011](docs/adr/0011-backup-and-restore.md))** — `POST
  /v1/admin/restore` (`backup:write`) replays an archive into the stack. **`dry_run`
  defaults to `true`**: the destructive direction is never the one you get by omission, and
  a dry run runs the same preflight and the same per-device gates as a real run, so its
  report is a prediction rather than a parse. `on_conflict` is `skip` (default),
  `overwrite`, or `fail` — nothing silently overwrites live configuration.

  **Nothing is written until the whole archive has been decrypt-tested.** The preflight
  opens the envelope canary and then every credential; any failure aborts everything with a
  `409`, rather than leaving the registry half-applied across two key generations. The
  message names the likely cause — wrong `MCP_SECRET_KEY` for a ciphertext archive, wrong
  passphrase for a portable one.

  Each device is replayed through the **ordinary registration path, gates included**, so a
  device whose `base_url` the *current* egress policy forbids is refused. That is correct
  behaviour and is reported per device (`failed` with a `reason`) while its neighbours
  restore — a restore is not a way to reinstate what a fresh registration would reject.
  `tools_revision` and the last tool-change record are carried across, so a restored device
  does not read to a polling client as having rolled its tool set back. Restored dead
  letters (`include_deadletters`) land on the dead-letter stream, **not** the live call
  stream: they stay inert until explicitly replayed. Every restore is audited, dry runs
  included.

### Changed

- **`cryptography` 48.0.1 → 49.0.0**, lifting the deliberate `<49` cap. This clears
  PYSEC-2026-3553 and PYSEC-2026-3554 from `pip-audit`. Both remain **unreachable** in this
  codebase — they are `x509.verification` bugs, and chain building and name-constraint checking
  go through `ssl.create_default_context()`, which is OpenSSL via the stdlib — so this is
  hygiene, not a patched vulnerability. PYSEC-2026-3552 (`pkcs7_decrypt_*`) still reports
  against 49.0.0 and needs 50.0.0; it stays unreachable for the same reason as before, the
  codebase contains no PKCS#7/S-MIME/CMS at all, and 50.0.0 is 11 days old, which
  [docs/dependency-advisories.md](docs/dependency-advisories.md) rates as unseasoned. Resolved
  in a clean virtualenv as that document requires: the whole bounded set (`mcp`, `fastapi`,
  `starlette`, `pydantic`, `redis`) re-resolved unchanged, and the resulting lockfile diff is
  one line.

### Fixed

- **F-67 — the egress policy is now enforced by registration, not by one route.** The URL
  policy (F-02), hostname rules and upstream-discriminator checks lived in the
  `POST /v1/devices` handler, so they were a property of *the HTTP path to registration*
  rather than of registration itself — `Registry.register_device` never called
  `validate_target_url`. Nothing exploited it while that handler was the only caller, but it
  made [ADR-0011](docs/adr/0011-backup-and-restore.md) §4's guarantee ("restore replays
  through `register_device`, so the egress policy still applies") untrue of the code, and a
  restore built on it would have been a `backup:write` privilege-escalation primitive. The
  gates now live in `registry/validation.py` behind one `validate_device_registration(...)`
  that both callers use. No behaviour change for existing API clients.

- **F-66 / FMEA D10 — a device that was never checked no longer reports itself reachable.**
  The health loop iterates only a worker's *assigned* devices, so a device whose pod never
  spawned was never checked by anything, and `reachable` served its dataclass default (`true`)
  forever — next to a `spawn_error` saying it had failed, with a `last_check` that aged without
  bound. Fixed at both ends: the defaults now claim nothing (`reachable=false`, `last_check=0`),
  and the spawn path records the reachability verdict its spec fetch already established.
  A spec that was fetched and then rejected keeps `reachable: true` — reached-but-unusable is a
  different fact, and `spawn_error` carries it. Found during lab-cluster verification 2026-08-10.

  **What changes for a client:** the API shape is unchanged — `reachable` is still a bool — but
  `last_check` and `last_check_age_seconds` are now `null` for a device nothing has contacted
  yet, where they previously carried the registration time. Read a null `last_check` as "never
  checked", which is a distinct state from a check that found the device dead. A device
  registered *before* upgrading that was never successfully checked keeps its optimistic stored
  value until its next spawn attempt, which now records a verdict either way.

- **The declared `cryptography` floor could not run this code.** Portable backup imports
  `cryptography.hazmat.primitives.kdf.argon2.Argon2id`, which does not exist before **44.0.0**,
  while `pyproject.toml` still declared `>=41.0.7` — a floor written long before ADR-0011. `pip
  install` resolves against `pyproject.toml` and ignores the lockfile, so an environment already
  holding cryptography 43.x satisfied the dependency and then raised `ImportError` on the first
  portable export. The image and CI were never affected, which is why nothing caught it: the
  existing guards check that the *locked* version satisfies the declared range (48.0.1 did) and
  that critical dependencies exclude the next *major* — nobody compared the floor against what
  the code imports. Floor raised to `>=44.0.0`, with a new guard
  (`test_declared_floor_supports_the_apis_the_code_imports`) asserting the declared minimum
  against a table of the APIs that require it.

## [0.3.1] - 2026-08-10

Two changes, both verified against a live Kubernetes cluster rather than only in the test
suite — unusual for a patch release, and the reason to trust it.

### Read this before upgrading

- **Nothing here breaks an existing deployment.** A config with no `security.mtls.devices`
  block resolves exactly as before — same trust set, same behaviour. No action is required to
  upgrade.
- **Why a patch number for a new capability.** [docs/releasing.md](docs/releasing.md) §1.5 says
  a new capability is a minor bump even at `0.x`, and this release deliberately departs from
  that. `0.4.0` is committed in the 0.3.0 notes to **removing** the deprecated HTTP+SSE
  endpoints, and 0.3.0 shipped two days ago; numbering this release `0.4.0` would either break
  that commitment or cut the deprecation window to days. The HTTP+SSE removal and the
  `2026-07-28` protocol shift stay paired in `0.4.0` as announced. Per-device TLS is purely
  additive, so a patch number understates the feature without overstating the upgrade risk.
- **The HTTP+SSE deprecation clock is unchanged.** `GET /v1/devices/{hostname}/sse`,
  `POST /v1/devices/{hostname}/messages`, `GET /v1/fleet/sse` and `POST /v1/fleet/messages`
  still work and are still scheduled for removal in **0.4.0**. This release does not move that
  date in either direction.

### Added

- **Per-device outbound TLS trust** (`security.mtls.devices.<hostname>`), closing the last TG-4
  residual. Any of the five mTLS keys — `ca_bundle`, `client_cert`, `client_key`,
  `client_key_password`, `verify` — can now be overridden for a single device, inheriting the
  rest from the fleet block. Devices the block does not name are unaffected.

  Trust was previously **fleet-global**: setting `ca_bundle` for one self-signed device made
  that CA a trust anchor for *every* outbound call the gateway and workers made, so any
  certificate it had signed would be accepted for any device. `verify: false` was worse,
  disabling verification fleet-wide to accommodate one appliance. Heterogeneous device PKIs
  previously required separate deployments; they no longer do.

  Precedence is most-specific-first: a device block beats `MCP_MTLS_VERIFY`, which beats the
  fleet config. `MCP_TLS_CLIENT_KEY_PASSWORD` is the exception — it unlocks the *fleet* client
  key and is not applied to a device that brings its own. See
  [docs/security-mtls.md](docs/security-mtls.md#per-device-trust).

- `GET /devices/{hostname}/diagnostics` gained a **`tls`** field reporting the profile that
  device resolved to (`source: fleet|device`, `verify`, the CA basename, whether a client cert
  is presented), so the trust a device actually gets is answerable without reading config on
  the pod. Resolved live rather than stored, so it cannot drift from the running config.

### Fixed

- **A deleted device could come back as an unreadable record** (distributed mode only). Deleting a
  device removed it from the index and deleted its hash; a worker that still had the device
  assigned then wrote `pod_active`/`worker_id`, and a plain `HSET` **re-created the hash** with
  only those two fields and no `hostname`. The result was invisible to `GET /v1/devices` (which
  reads the index) while every read *by hostname* returned **500** (`KeyError: 'hostname'`) — and
  re-registering that hostname failed too, since registration reads the device first. The only
  field remedy was deleting the key by hand.

  `update_device_fields` no longer creates the record, using `WATCH`/`MULTI` rather than
  check-then-write (a delete can land between the check and the write, losing the race exactly as
  before). A partial record left by an earlier version now reads as **absent** rather than
  raising, so it stops returning 500 immediately and is overwritten the next time that hostname is
  registered — no manual cleanup. `MemoryRegistryBackend` never had the bug, which is why the
  embedded suite could not see it; both backends are now pinned to the same contract.

  Found on a cluster, not in the suite.

### Changed

- The spec fetcher and the worker health loop now keep **one HTTP client per TLS profile**
  instead of one per service. A single shared client would have put every device back on one
  trust set, which is the limitation this release removes.
- Both processes **refuse to start** on a bad per-device TLS profile — every declared profile is
  built at startup rather than on first contact with the device. An unreadable CA names the
  device in the error, and an **unknown key** inside a device block is a hard error rather than
  a warning: an ignored key there would silently leave that device on the fleet trust set.
- Setting `security.mtls.verify: false` at the fleet level now warns that a per-device opt-out is
  the narrower alternative, and per-device opt-outs are named individually in the startup
  warnings so they cannot be forgotten in a config nobody has opened in a year.

## [0.3.0] - 2026-08-08

The gateway gains a **second inbound transport**. Nothing in this release removes or changes an
existing endpoint — every 0.2.0 client keeps working untouched — but two things are worth
reading before you upgrade.

### Read this before upgrading

- **HTTP+SSE inbound is now deprecated, and this release starts its clock.** `GET /v1/devices/
  {hostname}/sse`, `POST /v1/devices/{hostname}/messages`, `GET /v1/fleet/sse` and
  `POST /v1/fleet/messages` still work and are unchanged. They are scheduled for **removal one
  minor release from now (0.4.0)**. Move clients to `POST /v1/devices/{hostname}/mcp` and
  `POST /v1/fleet/mcp` when convenient; the README now leads with those. This is not our
  deprecation to reverse — HTTP+SSE is deprecated upstream in the MCP specification.
- **An unanswered tool call now returns `504`, where it previously returned `500`.** If any
  client or alert keys on `500` to mean "the device did not answer", it needs updating. The old
  behaviour was a bug (see *Fixed* below), and `504` is the accurate answer: the call may still
  be running upstream, so it is the client's choice whether retrying is safe.

Note on the version number: this is a **minor** bump, not a patch, because it adds endpoints.
`0.2.0` remains published with a known-issues notice pointing here.

### Added

- **Streamable HTTP inbound transport (`POST /v1/devices/{hostname}/mcp`)** — the JSON-RPC
  response comes back on the request that carried it, instead of being acknowledged and
  delivered later on a stream the client holds open. Semantics are unchanged: this is still
  revision `2025-06-18`, only the transport is new.

  **Both modes.** In embedded mode the pod is in-process and answering is a call. In
  distributed mode the POST may land on any gateway replica while the device is owned by one
  worker, so the replica waits on `session:{id}:results` and correlates by JSON-RPC id. The
  stream cursor is captured *before* the call is dispatched — the reverse ordering looks more
  natural and silently loses every result from a worker fast enough to answer in the gap.
  Admission control, the reserved scope, rate-limit budgets and the F-37 session binding are
  the same as on the SSE route: a second transport must not become a way around any of them.

  This exists because our only inbound transport, HTTP+SSE, is formally deprecated with a
  removal clock and has no fallback — so it is owed regardless of adopting revision
  `2026-07-28`. It is a **separate path** rather than content negotiation on `/messages`, so
  that retiring HTTP+SSE one minor release after this completes is a deletion rather than an
  unpicking. See [ADR-0010](docs/adr/0010-tool-derived-request-headers.md) and
  [the Phase 6 scope](docs/roadmap-protocol-2026-07-28.md) for the decisions behind it.

- **Fleet sessions over Streamable HTTP (`POST /v1/fleet/mcp?devices=a,b,…`)** — one MCP
  session spanning several devices (ADR-0008) on the new transport. `devices` is read on
  `initialize` only, so a later request cannot quietly widen the session's reach; the session
  carries the fleet afterwards.

  **This removes the inline-vs-stream split.** On the SSE fleet route, distributed mode answers
  `initialize`/`ping`/`tools/list` inline but returns `{"status": "accepted"}` for `tools/call`
  and delivers the result on the stream, while embedded mode puts all four on the stream. Both
  are legal MCP, but the asymmetry cost a debugging round. Here there is no stream, so every
  method answers on the POST that asked, identically in both modes.

  `tools/list` is rebuilt per request rather than frozen at open, so a device that was down
  when the session started joins once it recovers. `tools/call` reuses the same `ResultExchange`
  as the per-device transport, so cross-replica correlation, admission control and the timeout
  contract are shared rather than reimplemented. A session opened here stays usable on the SSE
  fleet route while that transport is being retired.

  A per-device session id is refused on the fleet surface (and vice versa): the two have
  different tool namespaces and different dispatch, so sharing one session store must not let
  them be used interchangeably.

### Fixed

Both of these are **pre-existing** defects, found by running the new transport on the lab
cluster rather than by the unit suite. Neither was introduced by the transport work.

- **A blocking Redis read could outlive its own connection deadline.** `XREAD BLOCK` holds
  the connection with the server silent until the window elapses, so a socket read timeout
  less than or equal to the block window always fires first — deterministically, on every
  idle poll, not as a race. The shipped defaults were exactly equal (`block=5000ms` against
  `socket_timeout=5`), and redis-py's retry then re-issued the command, so the caller's own
  deadline was silently replaced by (retries × socket_timeout) before a `TimeoutError`
  escaped. On the Streamable HTTP path this surfaced as a **500 after 32s** where the code
  intends a 504 — telling a client the gateway is broken rather than that the device did not
  answer. On the older SSE path the same error tore down an open results stream, reaching the
  client as an unexplained disconnect on a session they still held. The block window is now
  derived from the connection's actual socket deadline, and a timeout raised anyway is
  treated as an elapsed window so the caller's timeout stays the single authority.

- **A lapsed device claim was never re-acquired.** `EXPIRE` on a missing key returns 0 and
  creates nothing, so once `claim:{hostname}` expired — a worker paused past the TTL, a
  stalled node, a failover that dropped the key — the heartbeat renewed a lease that no
  longer existed. The worker went on serving the device, pods running and upstreams polled,
  while every gateway reported it inactive and the reconciler saw it orphaned indefinitely;
  only a worker restart recovered it. The F-62 lease-flap hysteresis is written expecting
  exactly this to self-heal "on the owner's next heartbeat", so renewing nothing quietly
  defeated that design too. Claims are now re-acquired when lapsed, and — since `EXPIRE`
  succeeds on *any* live key — ownership is checked rather than mere existence, so a worker
  whose device was reassigned no longer extends the new owner's lease. If another worker has
  taken the device, this one releases it: single ownership (ADR-0003) outranks continuity.

## [0.2.0] - 2026-08-06

A feature release: the gateway can now federate a **remote MCP server** as a device, not only
an OpenAPI service. It also carries the resolution of an independent third-party review (12
findings, all closed) and the defects found by first running the stack on a real Kubernetes
cluster.

**Read before upgrading.** Two security fixes **change request handling**, two add **startup
gates a misconfigured deployment will now hit**, and one **tightens tool-argument validation**
in a way that is breaking for callers passing arguments that do not exist.

### Security

- **Dependency refresh**, clearing 7 of the 10 advisories `pip-audit` reported against the
  previous pins — notably `starlette` 1.2.1 → 1.3.1, `cryptography` 48.0.0 → 48.0.1, `mcp`
  1.27.2 → 1.29.0 and `uvicorn` 0.48.0 → 0.52.1. No constraint changes were needed.
  **None of the 10 were reachable from this codebase** — the refresh was taken because it was
  free, not because anything was exploitable. The reachability analysis, and the method for
  redoing it next time, are in the new
  [dependency-advisories.md](docs/dependency-advisories.md). The three that remain need
  `cryptography>=49`, which the deliberate major cap blocks; they target PKCS#7 decryption and
  the `x509.verification` API, neither of which this project calls.
- **`X-Forwarded-For` was attacker-controlled, so any client could choose its own rate-limit
  bucket.** The rate limiter keyed on the *left-most* XFF entry when `trust_proxy_headers`
  was on. nginx, traefik and the k8s ingresses **append** to that header rather than replace
  it, so a caller who sent their own `X-Forwarded-For` owned the left-most value — and could
  reset their counter simply by rotating it, defeating per-IP limits entirely and poisoning
  IP-based audit attribution. The client is now resolved by walking the header
  **right-to-left** from the TCP peer, popping hops while each falls inside the new
  `security.trusted_proxy_cidrs`; the first hop outside them is the client. A caller who
  skips the proxy is stopped at the first step, because their own peer address isn't trusted,
  and their header is never read. **Breaking:** enabling `gateway.trust_proxy_headers`
  without `security.trusted_proxy_cidrs` is now refused at startup — trusting the header
  without knowing which hops are yours is what created the hole.
- **DNS-rebinding race in the SSRF guard.** `validate_target_url` resolved the host, then
  httpx resolved it *again* when connecting — two lookups, so a 0-TTL alternating record
  could pass validation and connect to the blocked address. Validating more often does not
  close that window; the checked address has to *be* the dialled one. The validated address
  is now pinned through to connect, with `Host`/`sni_hostname` carrying the original name so
  virtual hosting and TLS certificate verification are unaffected.
- **Outbound port policy.** `http://host:22/` was accepted, so the guard could be aimed at a
  non-HTTP service to port-scan or smuggle a payload into another protocol. Ports carrying a
  non-HTTP protocol (22, 25, 3306, 6379, 27017, …) are now refused by default, plus
  2375/2376 — which *are* HTTP but expose the Docker daemon as a direct RCE pivot. Ordinary
  HTTP ports including non-standard ones (8000, 8080, 8443) are unaffected;
  `security.allowed_target_ports` switches to a strict allowlist for a tighter posture.
- **Weak static API keys are refused in production.** `MCP_ADMIN_KEY=admin` was accepted
  silently. A static key is a full bearer credential — and with OIDC enabled it is also the
  break-glass path that still works when the IdP is down. Keys under 16 characters or
  matching a common-value list now warn in every mode and **refuse to start in distributed
  mode** (`gateway.allow_weak_keys` overrides). The floor sits below anything this project
  generates or documents, so a real deployment is unaffected.
- **OAuth2 refresh tokens rotated at runtime were never persisted**, so a provider that
  rotates on use eventually locked the device out.

### Added

- **MCP passthrough — federate a remote MCP server as a device.** Registering with
  `upstream_kind: "mcp"` points the gateway at a server that already speaks MCP; its tools are
  discovered from `tools/list` and proxied rather than translated from an OpenAPI document.
  A remote server is a **device**, not a second entity type — it reuses `DeviceConfig`,
  `base_url` (already SSRF-validated), and the `hostname` namespace, so RBAC, rate limiting,
  admission control, the circuit breaker, dead-lettering, audit, fleet sessions and tool-change
  governance all apply with no new code. Rationale, the alternatives rejected, and the itemised
  cost register for a future tenancy retrofit are in
  [ADR-0009](docs/adr/0009-mcp-passthrough.md).

  Three things are worth knowing before pointing it at someone else's server:

  - **The upstream authors its own tool contract and can change it after review** — the
    rug-pull and tool-poisoning threats, now [threat-model.md](docs/threat-model.md) §B5.
    Detection is the change-governance machinery: every poll diffs and classifies the tool set,
    and a removed tool or a newly-required parameter is **breaking** and alertable. Structural
    sanitisation (F-26) strips control characters and bidi overrides from upstream names and
    descriptions; prose survives by design, because a proxy cannot adjudicate meaning.
  - **Protections that live in the translator had to be re-applied**, since a proxied tool
    never passes through it: text sanitisation (F-26) and the tool-count/payload caps (F-09).
    Argument validation (F-28), header-injection resistance (F-25) and the guarded client's
    SSRF policy apply unchanged.
  - **v1 proxies tools only** — no resources, no prompts, no stdio upstreams; and
    `upstream_transport: "sse"` is accepted by the schema but refused at registration.

  Implements MCP revision `2025-06-18` in both directions. Current is `2026-07-28`, which is
  **not** backwards compatible — it removes the `initialize` handshake, `Mcp-Session-Id` and
  SSE resumability in favour of a stateless per-request protocol. Interop across the two eras
  exists only where an implementation deliberately supports both. Tracked as its own piece of
  work; the passthrough seam is where it lands.
- **`mcp_oidc_validation_failures_total{reason}`.** OIDC validation failures fell through to
  static keys at `debug` log level, so an IdP or JWKS outage silently degraded the whole
  deployment to break-glass-keys-only with no operator signal, and forged-JWT probing
  produced nothing at all. Failures are now counted and warned about (rate-limited to one per
  minute, carrying the suppressed count). `reason` is a fixed six-value set rather than the
  error text, because the raw error embeds attacker-controlled JWT contents and would be an
  unbounded-cardinality vector.
- **[docs/testing-gaps.md](docs/testing-gaps.md)** — what is implemented and reasoned about
  but **not empirically validated**, and why: chaos/fault injection (F-63), the scale
  baseline, HA Redis failover, live-cluster verification, and arm64 runtime verification.

### Changed

- **Redis client now survives a failover.** `create_redis` passed only `socket_timeout` and
  `max_connections` — no retry, health check or connect timeout — so a primary failover
  reached callers as a burst of hard `ConnectionError`s and pointing the gateway at an HA
  Redis bought almost nothing. Now configured with jittered exponential-backoff retries,
  `retry_on_error`, `health_check_interval` and `socket_connect_timeout`. The jitter is
  deliberate: a failover hits every replica and worker at once, so an un-jittered curve has
  them retry in lockstep against the newly promoted primary. The budget (~2.5s by default) is
  sized to absorb the reconnect burst and a *short* election, **not** to block through a long
  Sentinel promotion — stalling a request for tens of seconds is worse than failing it.
  See [testing-gaps.md](docs/testing-gaps.md) (TG-3) for what remains unverified.
- **Kubernetes manifests are digest-pinned to a published image.** They referenced
  `device-mcp-gateway:latest`, which has no registry component and so resolved to Docker Hub —
  following the k8s docs produced `ImagePullBackOff`. Both deployments now pin by digest to
  the multi-arch GHCR image, with `imagePullPolicy` explicit and identical on both (the
  worker previously set none, defaulting to `Always` under `:latest` while the gateway used
  `IfNotPresent`, so the two could run different builds of the same tag). `kustomization.yaml`
  gains an `images:` block so retargeting is one edit rather than two.
- **CI coverage floor ratcheted 65% → 81%** (measured actual is 83%), and the CI-gating dev
  tools (`black`, `flake8`, `mypy`, `pytest`) are upper-bounded — `black --check` is a
  blocking gate, so an unbounded spec let an upstream major turn an unrelated PR red.

### Fixed

- **A device's cached manifest expired an hour after its pod spawned and never came back**, so
  a healthy device silently became undiscoverable. The manifest is stored with
  `ex=spec_cache_ttl`, but its only writers were the spawn path (which runs only when the cache
  is *already* empty) and the changed-spec branch of the health loop — and the spec poll
  returned early whenever the hash matched, which is the normal case for a stable device.
  Nothing renewed the key. The pod kept its manifest in memory throughout, so MCP clients saw
  no problem at all while `GET /devices/{h}/tools` returned 409, **`GET /v1/fleet/sse` returned
  404 "no reachable devices" for an entire healthy fleet**, and the UI showed a reachable,
  pod-active device with zero tools. The manifest is now renewed on every poll of an unchanged
  spec — a lease held up by the worker serving the device, lapsing once none does — and rebuilt
  from the current spec if it has already gone, instead of waiting for a pod respawn. Found on
  a live cluster four hours into a run; no test had ever let a TTL elapse.
- **A device's tools accepted arguments it could not send.** Generated tool schemas carried no
  `additionalProperties`, so a call naming an argument that does not exist validated cleanly,
  was dropped on the way to the device — there is nowhere to put it — and came back as a
  success. To a model, a successful call is confirmation that the argument it invented is
  real, so the failure mode is a hallucination the gateway corroborates rather than a lost
  value. Generated schemas are now closed, which states a fact: the translator lists every
  argument the dispatcher can place. **Breaking for any caller that was sending extra
  arguments** — they were already being discarded, and are now refused with the offending
  field named. Schemas published by a **proxied MCP upstream are untouched**: that contract
  belongs to the upstream, and tightening it could refuse calls its server would accept.
- **Gateway and workers exited on the first Redis connection failure at startup** instead of
  waiting for it. Start order is not guaranteed on any orchestrator, so the common cause is
  simply that Redis has not finished starting. Kubelet backoff does recover — which is why
  this was easy to miss — but it recovers by way of a stack trace and a restart count, the
  same signals an operator uses to spot a genuinely broken deployment (observed on a live
  cluster as two restarts per worker). Both now wait for Redis with jittered backoff up to
  the new `redis.startup_timeout` (default 60 s), then fail hard so a truly dead Redis still
  reaches probes and alerts. Set `redis.startup_timeout: 0` for the old fail-fast behaviour.
  This is a separate budget from `redis.retries`, which stays short on purpose: a request
  caught mid-failover should fail fast rather than block its caller.
- **An idle MCP session expired under a stream the client still had open.** The session TTL was
  refreshed inside the results-stream reader but *after* the branch handling an elapsed
  `XREAD` block, so it was unreachable on a session carrying no results — i.e. a connected
  client waiting, which is the steady state. After 24 h the session key vanished and the next
  POST got a 404 for a session that was, from the client's side, plainly alive. Found while
  auditing every TTL'd key after the manifest defect above; the rest (leader locks, device
  claims, heartbeats) renew correctly, and the short-lived ones are meant to expire.
- **Tool-change governance never ran in distributed mode.** The worker's health loop compared
  each spec poll against `spec_hash`, but the only writers of that field lived in the
  registry-side spec services, which distributed mode does not run — so the field stayed empty,
  the `if cfg.spec_hash and ...` guard was permanently false, and the branch that would have
  written the first baseline sat *inside* the branch that could never be entered. The effect
  was not a stale hash: breaking-change detection, `tools_revision`, `GET /devices/{h}/tools/diff`
  and the breaking-change alert (F-41) could not fire at all, for either upstream kind. A device
  could drop a tool, or an MCP upstream rewrite a tool description into a prompt-injection
  payload, and the gateway would keep serving the old manifest and say nothing. The spawn path
  now records the fingerprint of the spec it built the manifest from, and a poll that finds no
  baseline seeds one instead of discarding it. Found on a live cluster; every pre-existing test
  had constructed its device with `spec_hash` already set.
- **Embedded `tools/call` was recorded as an error on every success**, inverting both
  `mcp_tool_calls_total` and the audit outcome for the entire embedded-mode dispatch path.
- **`mcp` was unbounded (`>=1.0.0`)**, so a clean install resolved to 2.0.0, which removed
  `mcp.server.fastmcp` — CI had been red on every branch. Now bounded, with a test asserting
  critical dependencies reject the next major.
- **Documentation pointed at an image tag that does not exist** (`:0.1.2`, 404 on GHCR), and
  `docs/upgrade.md` referenced a `device-mcp-worker` image that has never existed — the worker
  runs the gateway image with a different command.

## [0.1.4] - 2026-07-06

### Fixed

- **Lite deployment: `MCP_API_KEY_FILE` silently never wrote a key.** The Dockerfile never
  created a `/secrets` path, so when the lite compose mounted a brand-new named volume
  there, Docker seeded it as an empty **root-owned** directory (Docker copies whatever the
  image has at a fresh volume's mount point, ownership included — a path that doesn't exist
  in the image at all gets a bare root-owned directory instead). The non-root `appuser` the
  gateway runs as couldn't write to it, so first-run bootstrap failed permission-denied and
  quietly fell back to "no key configured" — auth stayed **disabled** on a supposedly
  secured-by-default lite deployment. Fixed by pre-creating and chowning `/secrets` in the
  image, matching the existing pattern already used for `/app/data` and `/app/logs`.
- **`/health` and the FastAPI app reported a stale version.** `__version__` was a second,
  independently-maintained literal in `device_mcp_gateway/__init__.py` that drifted out of
  sync with `pyproject.toml`'s version at the 0.1.3 release. Now derived at import time from
  installed package metadata (`importlib.metadata.version`), so there is a single source of
  truth and this class of drift can't recur.

## [0.1.3] - 2026-07-05

Post-0.1.2 changes: third-party Kubernetes deployment hardening (no application code),
plus a small tool-set change-governance addition (a new read endpoint) and a translation
doc — both from a third-party review. The first slice of federated identity (ADR-0007):
inbound OIDC at the gateway, with static keys kept as break-glass. Plus a lite / home
deployment profile for low-power boxes.

### Added

- **Lite / home deployment profile.** A one-command stack for low-power hosts (Raspberry Pi,
  mini-PC, old workstation; amd64 or arm64) via `docker-compose.lite.yml` — the gateway in
  embedded mode (no Redis/worker) plus the management UI, local password login only. First
  boot self-provisions the admin API key: with `MCP_API_KEY_FILE` set (opt-in) the gateway
  generates + persists a key to a shared volume and prints it for MCP-client config, and the
  BFF reads the same file via `GATEWAY_TOKEN_FILE`. No-op unless the env var is set, so
  existing key resolution is unchanged. Multi-arch (amd64+arm64) images publish to GHCR on
  release tags. See [docs/lite-deploy.md](docs/lite-deploy.md).
- **`GET /v1/auth/me` (whoami).** Returns the authenticated caller's own `subject`, effective
  `scopes`, and `auth_method`. Requires authentication but no specific scope. It lets a UI/BFF
  gate views on the **gateway's** scopes instead of maintaining a parallel role model, so the
  two can't drift (ADR-0007) — the source of truth for the UI's scope-driven gating.
- **Inbound OIDC authentication (ADR-0007, first slice).** The gateway can now authenticate
  a request bearing an IdP-issued JWT, in addition to static API keys. A new composite
  authenticator validates the token against the issuer's JWKS — asymmetric-algorithm
  allow-list (`HS*`/`none` refused), `iss`/`aud`/`exp`/`nbf` with bounded clock skew, `kid`
  matched to a published key — then maps the token's group claim to gateway scopes via a
  `gateway.oidc.group_roles` table the gateway owns. Static keys are tried for opaque tokens
  and remain the **break-glass** path: OIDC fails *closed* (a JWT is rejected) when the
  IdP/JWKS is unreachable, while configured keys keep working. JWKS is cached with a bounded
  TTL and kid-miss refetches are rate-limited (no fetch-amplification DoS); the issuer/JWKS
  URLs go through the existing egress (SSRF) policy. Disabled by default; enable under
  `gateway.oidc`. Implements TM-I-08/09/10/12 from
  [docs/threat-model-identity.md](docs/threat-model-identity.md). The BFF OIDC login flow and
  per-user identity relay (I1/I2/I4) are the next slices.
- **Three seed RBAC roles** — `operator` (manage devices + DLQ, no tool calls), `auditor`
  (metrics only), and `caller` (machine agent: read + `tools:call`) — join `admin`/`viewer`
  in `ROLE_SCOPES`, matching [docs/rbac-roles.md](docs/rbac-roles.md). Additive; no route
  changes (routes authorize on scopes, never role strings).
- **`GET /v1/devices/{hostname}/tools/diff`** — surfaces a device's most recent tool-set
  change (added / removed / changed tool names, the `breaking` flag and reasons, and the
  `tools_revision` it produced) as `ToolsDiffResponse`. The diff was already computed and
  audited on every spec change (F-41) but discarded; it is now persisted per device (cleared
  on delete) and served, so a UI can show *what* moved, not just *that* it moved. Works in
  both modes and does not require an active pod.
- **`docs/tooling.md`** — the OpenAPI→MCP translation contract: tool naming, parameter and
  request-body mapping, `$ref`/`allOf`/`anyOf`/nullable schema resolution, argument
  validation, and the two-layer error mapping (JSON-RPC codes + result-envelope slugs).

### Changed

- **Kubernetes manifests hardened.** Gateway and worker pods now run with
  `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, all Linux capabilities
  dropped, and a `RuntimeDefault` seccomp profile (writable `emptyDir` mounts added for
  `/app/logs`, and `/tmp` on the worker for its liveness file). Preferred pod anti-affinity
  spreads gateway and worker replicas across nodes so the `minAvailable: 1` PDBs are
  meaningful and node-failure/failover can be exercised.
- **Gateway `replicas` is now `2`**, matching the HPA's `minReplicas` (was `1`, which
  contradicted the autoscaler).
- **`prometheus-rules.yaml` is no longer applied by default.** It (and the new
  `servicemonitor.yaml`) require the Prometheus Operator CRDs, so a `kubectl apply -k` on a
  cluster without the Operator would fail. Both are now opt-in; the pods still expose
  `/metrics` and carry `prometheus.io/scrape` annotations for annotation-based discovery.

### Added

- **`deploy/kubernetes/servicemonitor.yaml`** — optional Prometheus Operator scrape config
  for the gateway and worker metrics ports, so metric discovery and the alert rules assume
  the same Prometheus setup.
- **Documentation for third-party deployment**: a "Build and push the image" workflow (the
  manifests reference an unpublished image), a cluster-prerequisites table (ingress-nginx,
  metrics-server, default StorageClass, optional Prometheus Operator), a TLS-secret example,
  and an explicit note that the bundled single-replica Redis is not an HA component.
- **Redis HA guidance** ([docs/kubernetes-architecture.md](docs/kubernetes-architecture.md),
  "Redis availability & durability"): the enterprise options for a highly-available Redis —
  managed Redis (drop-in single endpoint) or self-hosted Redis/Valkey + Sentinel behind a
  primary-tracking endpoint — why the gateway needs single-primary **HA, not a sharded
  Cluster** (multi-key `MULTI`/`EXEC` + pub/sub on one keyspace) and not active-active,
  durability/AOF, the Redis 7 requirement, and that the single-URL client makes any
  failover-hiding endpoint a no-code change. `redis.yaml` now points at it.

## [0.1.2] - 2026-06-16

A second hardening patch. A follow-up third-party review confirmed every v0.1.1 fix was
genuine and test-backed, and flagged five lower-severity tails — two narrow SSRF residuals
and three reliability/correctness bugs. All five are fixed here.

### Security

- **OAuth2 token fetch is now SSRF-guarded.** `token_endpoint` was validated at register/PUT
  but the token request — which carries the `client_secret` in its body — went through an
  unguarded client, so a DNS-rebind between registration and fetch could exfiltrate the
  secret to an internal/metadata address. The fetch now re-validates the endpoint (and every
  redirect hop) against the egress policy.
- **Device tool-call dispatch re-validates the target on every call.** Dispatch already
  refused to follow redirects; it now also runs the SSRF guard per call, so a rebind of an
  already-registered device to an internal address is caught at dispatch time, not only at
  registration. (The validate→connect window remains the documented residual that full
  IP-pinning would close.)

### Fixed

- **A failed tool-call dispatch is no longer silently dropped.** In distributed mode, a call
  whose execution raised was acked without a dead-letter or a client response, so the caller
  hung until timeout. It is now dead-lettered (for inspect/replay) and the client receives a
  definitive error.
- **The shared rate-limiter can no longer leave an "immortal" counter.** A crash between the
  counter increment and its expiry could leave a key with no TTL, throttling that client
  forever. The increment and expiry now run as one atomic step, and a missing expiry
  self-heals on the next request. (Requires Redis 7, the documented deployment target.)
- **`$ref`s nested in array items or map values are now resolved.** A `$ref` inside an
  array's `items` or an object's `additionalProperties` was left dangling in the generated
  tool schema; both are now resolved like object properties.

## [0.1.1] - 2026-06-15

A security and correctness patch. A third-party re-review of v0.1.0 found six issues that
the inaugural release's verification missed — the smoke test exercised only the embedded,
no-request-body path, which was structurally blind to every one of them. All six are fixed
here. v0.1.0 remains published; this is the first release with no known correctness
regressions in either mode.

### Security

- **SSRF egress policy now covers redirects and every fetch path** (F-02 hardening). Spec
  discovery / fetch followed HTTP redirects without re-validating the target, and workers
  never consulted the policy at all — so a redirect or DNS-rebind to a private / loopback /
  cloud-metadata address bypassed the front-door check. Outbound spec fetches now go through
  an SSRF-guarded transport that validates **every hop**, and device tool-call dispatch no
  longer follows cross-origin redirects (also closing an API-key/credential-leak vector).
  `security.mtls.verify: false` now emits a startup warning. (Residual, documented: full
  DNS-rebind / TOCTOU IP-pinning is not closed — the deterministic vectors are.)
- **OAuth2 `token_endpoint` is validated against the egress policy.** A device registered
  with an attacker-chosen `token_endpoint` could exfiltrate its client secret to an internal
  or metadata address; it is now policy-checked like `base_url` / `spec_url`.

### Fixed

- **Distributed: manifest caching crashed for any device with a request body.**
  `RequestBodySpec.binary_fields` (a set) wasn't JSON-encodable, so caching the manifest
  raised and the device was unusable in distributed mode. The Redis round-trip also silently
  dropped request-body and parameter-rename metadata. Both now round-trip losslessly.
- **Distributed: a metadata-only `PUT /devices/{host}` wiped stored credentials.**
  Reconstructing auth from the encrypted-at-rest record failed and re-registered the device
  with no auth. A PUT that omits auth now preserves the stored credentials verbatim.
- **Distributed: device unassignment / config-replace could be ignored.** Unassign events
  were load-balanced to one arbitrary worker rather than the device's owner, so a pod could
  keep running after teardown and a `PUT` replace might never apply its new config. Unassign
  is now broadcast so the owning worker always tears down.
- **Embedded: `GET /devices/{host}/tools` always returned 409.** The embedded path never
  cached the manifest, so REST tool introspection failed even though MCP `tools/list` worked
  off the live pod. The manifest is now cached on pod spawn.
- **Audit chain reported false tampering under a multi-replica gateway.** Multiple replicas
  appending to one shared audit sink interleaved independent hash chains, which the verifier
  read as a break. Records are now tagged per replica and each replica's sub-chain is verified
  independently; existing single-replica logs verify unchanged.

### Added

- `MCP_INSTANCE_ID` — overrides the per-replica audit-chain identity (defaults to `HOSTNAME`,
  i.e. the pod name under Kubernetes). Only relevant when multiple gateway replicas write to a
  shared audit sink.

### Note

The v0.1.0 notes stated every review finding (F-01–F-65) was resolved; the re-review showed
that verification was incomplete. The changes above close that gap.

## [0.1.0] - 2026-06-15

First tagged release. A universal bridge that converts any OpenAPI-documented device or
service into an [MCP](https://modelcontextprotocol.io/) tool server: register a device by
URL, the gateway auto-discovers its OpenAPI spec, translates every operation into an MCP
tool, and serves it over SSE for LLM clients.

This release is the output of a comprehensive security, reliability, and operability review
(findings F-01–F-65); every finding is resolved except one deferred item (see
[Known limitations](#known-limitations)). The embedded-mode golden path
(register → auto-discover → translate → invoke over SSE) is verified end-to-end.

### Added

- **Two deployment modes from one codebase**
  ([ADR-0001](docs/adr/0001-dual-mode-embedded-distributed.md)).
  - **Embedded** (default): single process, SQLite, zero infrastructure — install and run.
  - **Distributed**: stateless gateway tier + Redis control plane + independently-scaled
    stateful workers; single-owner-per-device with lease-based failover and reassignment.
- **Security, fail-closed by default.**
  - API-key authentication with **RBAC scopes** (`admin` / `viewer`). Distributed mode
    refuses to start without auth, or against an unauthenticated Redis — explicit override
    flags exist for trusted local networks only.
  - **SSRF / egress policy**: private, loopback, and link-local targets are refused by
    default (cloud-metadata safe); opt in with `MCP_ALLOW_PRIVATE_TARGETS` for a trusted fleet.
  - **LLM-surface hardening**: header-injection defenses, schema-poisoning sanitization,
    response-size caps, server-side argument validation, and `resources/read` traversal guards.
  - **Credential protection**: Fernet encryption at rest with **zero-downtime MultiFernet
    key rotation** (`device-mcp-rotate-secrets`); credentials redacted from logs.
  - **End-to-end identity propagation** (gateway → worker → audit), optional **outbound mTLS**
    to devices, and an **adversarial test suite** (SSRF / injection / fail-open / poisoning).
- **Reliability.**
  - Bounded, jittered retries on idempotent outbound calls; an **at-most-once idempotency
    guard** for non-idempotent calls on reclaim.
  - **Admission control** with a visible `429` (no silent stream-trim), per-device and
    per-worker in-flight caps, and circuit breakers.
  - Scale-out **rebalancing**, a leader-elected reconciler with lease-flap hysteresis,
    graceful drain, and **dead-letter-queue inspect / replay / drain**.
  - Upstream `429` / `Retry-After` awareness.
- **Integration correctness.** Robust OpenAPI→tool translation (param-collision and
  path-interpolation fixes), normalized error shapes (an upstream ≥400 is no longer returned
  as a successful result), non-JSON / form / multipart request bodies, a per-device adapter
  seam, and **breaking-change governance** with a monotonic `tools_revision` signal.
- **Observability & operability.** Prometheus metrics, **SLO recording + burn-rate alerts**,
  operational alerts for silent failure modes, optional OpenTelemetry tracing, `/v1` API
  versioning, config validation (warns on typos), safe-default startup warnings, a device
  diagnostics endpoint, and an error catalog with `rid` correlation.
- **Compliance & audit.** A tamper-evident, hash-chained **audit stream** (privileged actions
  plus 401/403 with actor), per-request actor attribution, time-based retention, and a
  **SOC 2 / HIPAA / FedRAMP control map** ([docs/compliance.md](docs/compliance.md)).
- **Documentation.** Threat model, failure-mode matrix, six ADRs, an on-call
  [runbook](docs/runbook.md), an [upgrade guide](docs/upgrade.md), multitenancy and compliance
  docs, and a load-test harness.

### Known limitations

- **Resilience is designed but not yet empirically demonstrated** (F-63): the
  chaos / fault-injection plan (experiments E1–E10) is written but requires a live platform
  to execute. Analysis only so far. This and every other unvalidated claim — the scale
  baseline, HA Redis failover, live-cluster and arm64 verification — are tracked in
  [docs/testing-gaps.md](docs/testing-gaps.md).
- **Not FIPS-validated**: credential encryption uses Fernet (AES-128-CBC + HMAC), which is
  not a FIPS 140-validated module — a blocker for FedRAMP / FISMA-High as shipped. Mitigation:
  delegate credential secrecy to a FIPS-validated KMS (see [docs/compliance.md](docs/compliance.md)).
- **Single-tenant per stack** ([D-1](docs/adr/0004-single-tenant-per-stack.md)): tenant
  isolation is a deployment-boundary control, not in-application. Run one stack per tenant.
- **Pull-only**: OpenAPI `webhooks` / `callbacks` are not translated, and there is no
  long-running-operation (202 / job-poll) support — calls are synchronous.

[0.3.8]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.8
[0.3.7]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.7
[0.3.6]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.6
[0.3.5]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.5
[0.3.4]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.4
[0.3.3]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.3
[0.3.2]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.2
[0.3.1]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.1
[0.3.0]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.3.0
[0.2.0]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.2.0
[0.1.4]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.1.4
[0.1.3]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.1.3
[0.1.2]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.1.2
[0.1.1]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.1.1
[0.1.0]: https://github.com/benwold-lgtm/SyncGate/releases/tag/v0.1.0
