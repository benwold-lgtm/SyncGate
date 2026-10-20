# ADR-0015: Endpoint fingerprinting — pin what a device *is*, warn when it changes

- **Status:** Accepted
- **Date:** 2026-08-12 (Proposed) · 2026-08-12 (Accepted, on resolving §9)
- **Related findings:** F-02 (SSRF / target policy), F-29 (redirect re-validation),
  F-69 (declared identity discarded — *not yet filed*)
- **Builds on:** [ADR-0009](0009-mcp-passthrough.md) (a remote MCP server is a device),
  [ADR-0011](0011-backup-and-restore.md) (archives must carry the pins — see Consequences),
  [ADR-0013](0013-two-plane-tenancy-and-the-provider-plane.md) §8 (why a noisy control is a
  broken control)

## Context

Registering a device today establishes **where** the gateway may talk, not **what** it is
talking to. `validate_device_registration` enforces the URL policy (F-02/F-67), every
request is pinned to `base_url` so a fetched spec cannot redirect the gateway elsewhere,
redirects are re-validated per hop (F-29), and tool arguments cannot override credentials
(F-25). Those are solid, and they all constrain the *address*.

Nothing constrains identity. Two consequences follow:

- **`upstream_kind` is declared by the registrant, not discovered.** It is validated only
  against the set of recognised values. The endpoint is never asked what it is, and no
  answer it gave would be checked.
- **The identity information the gateway already receives is thrown away.** An MCP upstream
  returns `serverInfo: {name, version}` from the `initialize` handshake on every
  reachability probe — and `mcp_discovery.py` logs it at debug level and discards it. The
  OpenAPI `info: {title, version}` block is likewise not retained.

Meanwhile the *functional* half of a fingerprint already exists and works:
`canonical_tools_hash()` drives the monotonic `tools_revision`, detects a changed tool
surface, and triggers pod replacement.

**The realistic threat is not a malicious registrant.** Registration requires
`devices:write`; anyone holding it who wants to exfiltrate can register anything and call
it, and no amount of endpoint verification changes that — the trust decision is *who may
register*, and that is RBAC. What nothing currently detects is the case that will actually
happen: **the thing at `base_url` becomes a different thing.** DNS repointed, a host
rebuilt, an appliance replaced, a service migrated, a certificate reissued under a new key.
The gateway keeps sending credentials to it and never mentions that anything changed.

There is also a plain operational motive, and it argues for the same change: operators need
to know what a device *is* — vendor, product, version — to organise and manage a fleet by
name. That is exactly the data being discarded.

## Decision

Record a fingerprint per device, made of three dimensions that are **never conflated**;
compare on every reachability check; **warn by default and require an audited human
approval to re-pin**, with fail-closed available as an opt-in policy.

### 1. Three dimensions, labelled by how much they can be trusted

| Dimension | Source | Trust |
|---|---|---|
| **Authenticated** | TLS **SPKI** SHA-256 (plus cert SHA-256, issuer, subject, `notAfter` as context) | Cryptographic. Proves this is the same endpoint as last time. |
| **Declared** | MCP `serverInfo{name,version}`; OpenAPI `info{title,version}` | **Self-reported and spoofable.** Good for inventory and change *detection*; worthless as proof. |
| **Behavioural** | `canonical_tools_hash` / `tools_revision` | Already exists. Detects a changed tool surface. |

**These must stay visibly separate in the model, the API and the UI.** A single "verified"
badge over the top would imply the declared fields carry weight they do not have — the same
mistake as trusting `upstream_kind` because it is present in the record. The declared
dimension is inventory that happens to be useful for detecting change, and it should read
that way.

### 2. Pin the SPKI, not the certificate

The pinned value is the SHA-256 of the **Subject Public Key Info**, not of the certificate.

This single choice decides whether the control is signal or noise. A routine renewal
re-issues the certificate against the *same key*: same SPKI, no alarm. A renewal that
rotates the key trips it — which is correct, because from the outside that is
indistinguishable from a substituted endpoint. Pinning the full certificate would fire on
every ACME renewal, i.e. every 60–90 days per device across the whole fleet.

[ADR-0013 §8](0013-two-plane-tenancy-and-the-provider-plane.md) made this argument for
step-up and it applies unchanged: **a control that fires constantly trains people to
approve reflexively, destroying the signal exactly where it needs to mean something.** A
fleet-wide quarterly false alarm would not merely be ignored, it would get the feature
switched off.

### 3. Classify the change; do not raise one undifferentiated alarm

| Observed | Verdict |
|---|---|
| Tool hash changed only | **Not a fingerprint event.** Routine — a firmware or feature upgrade. Already handled by `tools_revision`. |
| Cert reissued, SPKI unchanged | Informational. Auto-accept, record the new cert and expiry. |
| Declared identity changed, SPKI unchanged | Informational — usually a version upgrade. Record; surface in inventory. |
| **SPKI changed** | **Warn. Requires approval.** |
| **SPKI *and* declared identity changed** | **Warn, prominently.** The strongest available signal that this is a different thing. |

Folding these into one alarm would mean the common, boring cases drown the one that matters.

### 4. Warn by default, and the device keeps working

On an SPKI change the device is flagged `fingerprint_pending_approval`, surfaced in the API
and UI, and **continues to serve**. Clearing the flag requires an explicit approval that
re-pins the new value.

Warning rather than stopping is deliberate. A fleet that halts on certificate rotation is a
fleet whose operators disable the check within a quarter — and a disabled control detects
nothing at all, which is strictly worse than a warning someone might read. The default is
chosen for what survives contact with operations.

### 5. Fail-closed is opt-in

`security.fingerprint_policy: warn | enforce`, deployment-wide, overridable per device (the
same shape as the existing per-device `rate_limit_rps`).

Under `enforce`, a device with an unapproved fingerprint change **stops serving tool calls**
— refused with an error naming the fingerprint change — while staying registered and
visible. It is quarantined, not deleted.

Per-device override matters because the risk is not uniform: a storage array holding
production data warrants `enforce`; a lab sensor does not. A deployment-wide-only setting
would be tuned to the least critical device.

### 6. Approval is an audited act, not a dismissal

Approving a changed fingerprint is a **trust decision**, and is recorded as one: the
principal, the device, the old and new values, and the time. It follows the ADR-0013 pattern
— cross-cutting authority is exercised and recorded, never held ambiently. A UI that lets
someone clear the flag without it appearing in the audit chain would convert the control
into a nuisance dialog.

### 7. Plain HTTP devices have **no** authenticated dimension

An `http://` upstream has no certificate, so it gets declared and behavioural dimensions
only. This must be stated in the record and shown in the UI rather than left to inference —
a device whose fingerprint "matches" on two spoofable dimensions is not verified, and
displaying it beside a TLS-pinned device without distinction would be misleading.

It is a reasonable follow-on to warn at registration that an `http://` device cannot be
fingerprinted meaningfully.

### 8. First registration is trust-on-first-use, and says so

The first fingerprint is recorded without verification. TOFU **establishes a baseline; it
does not validate anything** — if the endpoint was already wrong at registration, the wrong
value is what gets pinned. The record and the UI should use that language rather than
"verified".

For deployments that need better, registration may optionally accept an **expected SPKI**,
verified out of band, and refuse the registration on mismatch. That converts TOFU into real
verification for the devices where it is worth the effort.

## Consequences

- **Positive:** the realistic failure — an endpoint quietly becoming a different endpoint —
  becomes visible; the inventory metadata operators need to manage a fleet by name arrives
  as a by-product of the same change; the behavioural dimension already exists, so a third
  of this is done; the declared dimension is data already fetched on every probe and
  currently discarded, so capturing it costs almost nothing.
- **Negative / cost:**
  - **⚠️ [ADR-0011](0011-backup-and-restore.md) archives must carry the pins.** If they do
    not, every device silently re-TOFUs on restore and the control is void from the first
    disaster recovery onward — precisely when an operator is least able to notice. Restoring
    a pin that no longer matches must warn rather than re-pin.
    **Closed 2026-08-13** — the archive carries a `fingerprint` block per device and the
    restore never re-pins. Two things the build found that this ADR had not accounted for:
    (a) `on_conflict=overwrite` goes through `replace_device`, which rebuilds the record
    from registration inputs alone, so an overwrite restore **wiped the live pin** even
    when the archive agreed with it — writing the fingerprint back is load-bearing, not a
    no-op; (b) `fingerprint_state` and `pending_tls_spki_sha256` have to travel too, or a
    device exported mid-`pending_approval` comes back `pinned` and the restore becomes a
    way to launder an unapproved change past §6. Archives written before this change are
    still readable and are **reported** as carrying no pin rather than restored silently.
  - TOFU baselines are unverified by construction. This buys change detection, not identity
    validation, and must not be described as the latter.
  - The declared fields will appear in the UI, where users will over-trust them regardless
    of labelling. The mitigation is presentation, and it is imperfect.
  - `enforce` can take a fleet offline on a genuine key rotation. That is the point of the
    mode, and it is why it is not the default.
  - A schema change reaching `DeviceConfig`, both storage backends, the API models and the
    UI — plus the archive format above.
  - Capturing the peer certificate requires reading it from the live connection during the
    request, which couples fingerprinting to the transport layer rather than leaving it a
    property of the registry.

## Implementation feasibility (verified 2026-08-12)

Confirmed workable on the current stack (httpx 0.28.1, `cryptography` 49.0.0) before
proposing it:

- The peer certificate is reachable via `response.extensions["network_stream"]` →
  `get_extra_info("ssl_object")` → `getpeercert(True)`. `SsrfGuardTransport` already wraps
  the transport and is the natural capture point.
- ⚠️ **`getpeercert` must be called positionally.** The object returned is a raw
  `_SSLSocket`, not the `SSLSocket` wrapper, so the documented `binary_form=True` keyword
  raises `TypeError: _SSLSocket.getpeercert() takes no keyword arguments`.
- SPKI is derived with `cryptography`'s `x509.load_der_x509_certificate(...)` →
  `public_key().public_bytes(DER, SubjectPublicKeyInfo)`; subject, issuer and `not_valid_after`
  come from the same object for the contextual fields.

## Implementation notes (verified on a live cluster, 2026-08-13)

The whole decision was exercised against a real fleet before release: a Nutanix appliance
on its own certificate, an in-cluster MCP upstream over plain HTTP, and a purpose-built
TLS endpoint whose **server key was rotated under an unchanged CA** — the §2 scenario that
must alarm, isolated from any trust change. Every pinned and pending value was checked
against an independently computed digest rather than against the gateway's own report.

**The declared dimension was half-implemented, and only a live probe showed it.** The MCP
half worked from the start: `serverInfo` is read off the `initialize` handshake on every
reachability check. The **OpenAPI half did not exist** — nothing read the document's
`info` block, so an OpenAPI device (the *default* `upstream_kind`) had no declared
identity at all. The unit suite could not see it: every test of the declared dimension
supplied an `Observation` directly, so the comparison logic was correct and thoroughly
covered while nothing ever populated it for that kind of device. The consequences were
quiet ones — `key_and_declared_changed` (§3's strongest signal) could never fire for an
OpenAPI device, degrading silently to `key_changed`, and the inventory motive in the
Context above went unmet for most of a fleet.

It is now captured during the spec poll, which is the only point in the health loop
holding a parsed document. Two consequences worth stating rather than discovering: the
value is compared on the *following* cycle rather than the same one, and a newly
registered device therefore shows no declared identity until its first spec poll. Neither
can raise a false alarm — a cycle with nothing observed compares as "learned nothing", and
a change needs both a stored and a seen value.

**The fix for that gap had the same shape as the gap, and the cluster caught it too.**
Capturing the value was not enough to record it. `_declared_changed` compares only when
*both* the stored and seen values are present, so on an already-pinned device
`None → "Acme Array"` was not a change, the verdict stayed `unchanged`, and `plan_update`
wrote nothing — the field stayed blank through every probe. The new unit tests passed
throughout because they all began from a device with **no pin at all**, which takes the
`first_pin` path and writes every field. The state that actually exists on a running
fleet — pinned, with no declared value yet — was never constructed. `plan_update` now
backfills a declared value the record lacks, deliberately as a *fill* rather than a
`declared_changed` verdict: learning a label for the first time is not evidence the
endpoint became something else, and routing it through the verdict would let
`key_and_declared_changed` (§3's strongest signal) fire on a device that merely acquired
an inventory name in the same cycle its key rotated. The fill is self-limiting, so it
costs one write per device and not a per-cycle churn.

A device sitting at `pending_approval` does not backfill, because that branch returns
early to avoid churn while a human decides; it fills on the cycle after approval. Verified
on the cluster rather than assumed.

**§7 held in practice, not just on paper.** The plain-HTTP upstream recorded a declared
identity and no authenticated dimension, and stayed visibly distinct from the TLS-pinned
devices in the API response.

## Alternatives considered

- **Pin the full certificate:** rejected — §2. Fires on every routine renewal, which trains
  reflexive approval and gets the feature disabled.
- **Pin the issuer / CA only:** rejected. Any certificate from the same public CA would
  pass, which for a publicly-issued cert is close to no control at all.
- **Fail closed by default:** rejected — §4. Correct on paper; in practice it gets switched
  off after the first rotation outage, and a disabled control detects nothing.
- **Rely on ordinary TLS validation:** rejected. TLS proves the certificate matches the
  *name* requested. Repoint the DNS at a host with a valid certificate for that name and TLS
  is perfectly happy — which is the exact scenario this ADR exists to catch.
- **Verify device type by matching the spec against known vendor profiles:** rejected. It was
  considered because it sounds like the strongest option; it is brittle, needs continuous
  maintenance per vendor, and is trivially spoofed by an endpoint that serves a matching
  spec. Worst of all it would *look* like identity verification while delivering pattern
  matching, which is a worse position than having no such check.
- **Treat a changed tool hash as a fingerprint alarm:** rejected — §3. It changes on ordinary
  upgrades and would supply most of the noise while carrying the least signal.

## Resolved questions

The two questions this ADR was Proposed pending are settled below (2026-08-12), which is
what moves it to Accepted.

### 9. `devices:write` approves, and `enforce` stops the *use* paths only

**Approval uses the existing `devices:write` scope.** No new scope is introduced. The
gateway's RBAC is deliberately small ([ADR-0007](0007-federated-identity-oidc-and-gateway-rbac.md)),
and in practice the operator who registers a device is the operator who knows whether a
changed endpoint is still the right one — splitting them would add a scope without adding a
decision-maker.

**State the trade-off rather than implying separation of duty:** anyone who can register a
device can also approve a fingerprint change. The accountability therefore rests entirely on
the **audit record** (§6) — principal, device, old and new values, timestamp — not on scope
separation. If a deployment later needs the split, a narrower `devices:approve` is an
additive change that does not disturb this decision.

**Under `enforce`, the device is quarantined from use but stays observable:**

| Path | Under `enforce` with an unapproved change |
|---|---|
| Tool calls | **Refused**, with an error naming the fingerprint change |
| Resource reads | **Refused** — same risk as a GET tool: credentials go out, device data comes back |
| Reachability probe / diagnostics | **Continue** |
| `GET /v1/devices/...`, UI visibility | **Continue** |
| Delete / re-approve | **Always available** |

The dividing line is *using* the device versus *observing* it. Refusing everything would
make a quarantined device indistinguishable from a dead one, exactly when an operator is
trying to work out which it is; continuing to probe is what lets them see the new
fingerprint, and see the device recover on its own if the change was a rotation.

⚠️ **Resource reads are included deliberately**, which extends "stops tool calls" slightly.
A resource read sends the device's credentials and returns its data — the same exposure as a
GET-shaped tool call, differing only in the code path. Stopping one and not the other would
leave a quiet route to precisely what the quarantine exists to prevent. Called out here
rather than buried, because it is a broader stop than the phrase "tool calls" implies.

**Recovery is manual and has two forms**, both ordinary existing operations: re-approve
(re-pins the new fingerprint, audited) or delete the device. Quarantine never blocks
deletion — a device an operator no longer trusts must always be removable.

### The operator half (UI, 2026-08-13)

The presentation mitigation this ADR calls "imperfect" is now built (MCP-Gateway-UI #19,
closing F-69). What it does, so the claim is checkable rather than aspirational:

- The three dimensions render as **three separately-labelled groups** — Authenticated
  (`tls_spki_sha256`), Self-reported (`declared_*`, captioned as spoofable and never
  evidence of identity), Behavioural (`tools_revision`). **A test asserts that no combined
  "verified" badge exists**, because one over the first two would lend the self-reported
  half a weight it has not earned.
- The state chip describes **the pin, not trust**. "pinned" is worded as *the same key as
  last time*, which is all a TOFU baseline can support.
- The **full** SPKI digest is shown, not a preview — an operator's real check is comparing
  it against an independently computed digest, which a truncation cannot support.
- An `http://` device's absent pin is reported as a **fact** ("no key to pin"), distinct
  from "not observed yet". Conflating them would manufacture a permanent false alarm, which
  is the §2 argument against certificate pinning applied to the display layer.
- An unset `fingerprint_policy` is reported as **inherited**. The effective value is resolved
  at enforcement time and not returned by the API, so naming one would be a guess printed as
  a fact.
- Approval is admin-only and recorded in the BFF's audit chain under the *same* action name
  the gateway uses, `device.fingerprint.approve`, so the two chains describe one event. The
  gateway's 409 for a device that is not pending passes through to the operator: approving
  from a stale screen must fail visibly rather than appear to succeed.

**Known residual.** `DeviceSummary` carries no `fingerprint_state`, so the device *list*
cannot flag a device awaiting approval — it is visible only on the detail view. Under
`enforce` that device's calls are actively being refused, so this is a discovery gap rather
than a cosmetic one. Tracked for the next gateway release; it is one field on the list
projection plus a `pending_approval` count on the overview.

## Implementation note: embedded mode (2026-10-20)

Nothing in this record scopes it to distributed mode, but until now only the distributed
worker implemented it. An **embedded** gateway never observed a TLS key or declared identity,
so it never pinned, compared or flagged a device. Beneath that it had two more gaps of the
same kind:
- SQLite was written only at registration, so a registration-time pin
  (`expected_tls_spki_sha256`), a per-device `fingerprint_policy` and any approval were
  **lost on restart**;
- the startup reload restored none of them.

All three are now closed, and the behaviour is the same in both modes:

- **One comparison.** `security/fingerprint_record.record_observation` is shared by the worker
  and the embedded health loop, so the verdicts and the operator-facing log line cannot drift.
- **One probe client per device in embedded mode.** The pooled outbound client is shared per TLS
  profile and records only "the last connection it saw". The embedded loop probes devices
  concurrently, so reading that after device A's probe could return device B's certificate,
  which would be a false `key_changed` and, under `enforce`, a wrongful quarantine.
  `SpecService.probe_client(hostname)` makes each observation attributable by construction.
  The distributed worker probes sequentially, which is why it never hit this.
- **One write path.** Every fingerprint write goes through `Registry.update_persisted_fields`:
  an observation, a registration pin or policy, an approval, a trust record carried across a
  PUT, and what a backup restore puts back. In embedded mode it writes the live record **and**
  SQLite (`update_fields`, an allowlisted-column update), and the startup reload restores the
  record. The same path carries `credential_state` and `curated_spec`, which had the same gap.

Consequence for an existing embedded deployment: the first probe after upgrading is
trust-on-first-use for every device (§8), exactly as registration is in distributed mode.
