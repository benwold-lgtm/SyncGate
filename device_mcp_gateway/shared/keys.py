# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""The single place Redis key names are built.

Every key used to be an inline f-string at its call site — around seventy of them across a
dozen modules, with no helper and no shared vocabulary. Nothing was wrong with that while
one deployment owns one Redis. It becomes expensive the moment that stops being true: a
tenant or cell prefix would have to be threaded through every one, and each edit is a chance
to rename a key and silently orphan whatever is already stored under the old name — live
device config, claim leases, dead letters.

Routing them through one builder makes that a single change. It also makes a cheaper
intermediate deployment possible without any application change: several small stacks
sharing one Redis under different prefixes.

**The prefix is an explicit constructor argument, deliberately.** There is no ambient or
request-scoped prefixing — no contextvar, no global mutation. Implicit scoping is the part
that is hard to audit (you cannot tell from a call site which keyspace you are in) and hard
to remove if the tenancy model goes a different way. An explicit argument is legible and
costs nothing to delete.

Two things that live here but are deliberately *not* prefixed, because they are not keys:

- **Consumer group names.** A group is scoped to its stream, so prefixing one is meaningless
  at best. At worst it creates a *second* group on the same stream, and every message gets
  delivered to both — silent duplicate execution.
- **``device://`` resource URIs.** These are handed to MCP clients and may be cached by
  them. They are part of the external contract, not the keyspace.
"""

from __future__ import annotations


class KeyBuilder:
    """Builds every Redis key the gateway and workers use.

    ``prefix`` is empty by default, which reproduces the historical key names exactly.
    """

    __slots__ = ("_prefix",)

    def __init__(self, prefix: str = "") -> None:
        self._prefix = f"{prefix}:" if prefix else ""

    @property
    def prefix(self) -> str:
        """The configured prefix without its separator ("" when unprefixed)."""
        return self._prefix[:-1] if self._prefix else ""

    def _k(self, suffix: str) -> str:
        return f"{self._prefix}{suffix}"

    # --- registry state ------------------------------------------------------

    @property
    def devices_set(self) -> str:
        """Set of every registered hostname."""
        return self._k("devices:all")

    def device_config(self, hostname: str) -> str:
        """Hash holding a device's DeviceConfig."""
        return self._k(f"device:{hostname}:config")

    def device_manifest(self, hostname: str) -> str:
        """Cached translated manifest (TTL'd)."""
        return self._k(f"device:{hostname}:manifest")

    def device_tools_change(self, hostname: str) -> str:
        """Last recorded tool-set change (no TTL — governance history)."""
        return self._k(f"device:{hostname}:tools_change")

    # --- streams -------------------------------------------------------------

    @property
    def assignments_stream(self) -> str:
        """Competing-consumers stream: which worker owns which device."""
        return self._k("device:assignments")

    @property
    def unassign_stream(self) -> str:
        """Broadcast stream — every worker tails it independently."""
        return self._k("device:unassignments")

    def device_calls(self, hostname: str) -> str:
        """Per-device tool-call stream."""
        return self._k(f"device:{hostname}:calls")

    def device_calls_dead(self, hostname: str) -> str:
        """Per-device dead-letter stream."""
        return self._k(f"device:{hostname}:calls:dead")

    # --- consumer groups (NOT keys — never prefixed; see module docstring) ----

    @property
    def worker_group(self) -> str:
        """Consumer group on the assignments stream."""
        return "workers"

    def device_calls_group(self, hostname: str) -> str:
        """Consumer group on a device's call stream."""
        return f"workers-{hostname}"

    # --- sessions ------------------------------------------------------------

    def session(self, hostname_or_session_id: str) -> str:
        """Hash of {hostname, gateway_id, owner} for an open MCP session."""
        return self._k(f"session:{hostname_or_session_id}")

    def session_results(self, session_id: str) -> str:
        """Durable per-session results stream, so a reconnecting client loses nothing."""
        return self._k(f"session:{session_id}:results")

    def reply(self, request_id: str) -> str:
        """Single-request results stream for a stateless call (ADR-0027 §1).

        One entry, one waiter, a short TTL: nothing else ever writes to it, so a result can
        never be delivered to a request it does not answer.
        """
        return self._k(f"reply:{request_id}")

    def fleet_tools(self, session_id: str) -> str:
        """Display-name -> tool entry map for a fleet session."""
        return self._k(f"fleet:{session_id}:tools")

    # --- worker membership and leases ---------------------------------------

    @property
    def workers_active(self) -> str:
        return self._k("workers:active")

    def worker_heartbeat(self, worker_id: str) -> str:
        return self._k(f"worker:{worker_id}:heartbeat")

    def worker_devices(self, worker_id: str) -> str:
        return self._k(f"worker:{worker_id}:devices")

    def claim(self, hostname: str) -> str:
        """Single-owner device lease (ADR-0003)."""
        return self._k(f"claim:{hostname}")

    def rebalance_cooldown(self, hostname: str) -> str:
        return self._k(f"rebalance:cooldown:{hostname}")

    def health_lock(self, hostname: str) -> str:
        return self._k(f"health_lock:{hostname}")

    # --- leader locks --------------------------------------------------------

    @property
    def reconciler_leader(self) -> str:
        return self._k("reconciler:leader")

    @property
    def gauge_leader(self) -> str:
        return self._k("gateway:gauge-leader")

    # --- idempotency markers -------------------------------------------------

    def exec_marker(self, request_id: str) -> str:
        """ "Started" marker for a non-idempotent call (F-08)."""
        return self._k(f"exec:{request_id}")

    def result_marker(self, request_id: str) -> str:
        """ "Already completed" marker, so a redelivery is not re-executed (F-08)."""
        return self._k(f"result:{request_id}")

    # --- break-glass activity (ADR-0023 §3) ----------------------------------

    def break_glass_session(self, subject: str) -> str:
        """Marker that ``subject`` is inside an *active* break-glass session.

        **This key's expiry is the signal, not a cleanup detail.** Its absence is what
        makes the next use a new *activation*: the key is (re)written with a TTL of the
        session gap on every use, so continuous use during one incident stays one
        activation however many calls it takes, and a use after a quiet gap is a new one.
        """
        return self._k(f"breakglass:{subject}:session")

    def break_glass_window(self, subject: str) -> str:
        """Hash of {activations, last_use} over the trailing review window.

        TTL is refreshed on each *activation*, so the window slides from the last
        activation rather than resetting on a fixed boundary. That is deliberate: a
        credential reactivating every week is exactly the signal §3 asks to watch, and a
        fixed window would zero the count underneath it. A credential quiet for a full
        window starts clean, which is the reason this expires at all.
        """
        return self._k(f"breakglass:{subject}:window")

    # --- device write-planned proposals and grants (ADR-0022) ----------------

    def write_planned_proposal(self, proposal_id: str) -> str:
        """Hash holding a pending device-write proposal awaiting human review."""
        return self._k(f"write_planned:proposal:{proposal_id}")

    def write_planned_grant(self, digest: str) -> str:
        """Hash holding a `devices:write-planned` grant, scoped to one plan digest."""
        return self._k(f"write_planned:grant:{digest}")

    # --- support requests and grants (ADR-0017) -------------------------------

    def support_request(self, request_id: str) -> str:
        """Hash holding a pending support request awaiting a tenant admin's decision."""
        return self._k(f"support:request:{request_id}")

    def support_grant(self, grant_id: str) -> str:
        """Hash holding a live support grant — checked on every request under its bearer,
        not consumed once like a `write_planned` grant (ADR-0017 §2: the credential is
        valid for its whole window, not a single redemption)."""
        return self._k(f"support:grant:{grant_id}")

    @property
    def support_pending_index(self) -> str:
        """Set of request ids still awaiting a tenant admin's decision — what the tenant
        console's inbox reads. Membership is best-effort: a hash's own TTL reaps it
        independently of this index, so a reader must treat a member with no surviving hash
        as already gone, not as an error."""
        return self._k("support:pending")

    @property
    def support_active_grants_index(self) -> str:
        """Set of grant ids currently live — what "who can reach my stack right now" (the
        control ADR-0017 gives the tenant) reads before offering a revoke button. Same
        best-effort membership caveat as `support_pending_index`."""
        return self._k("support:active_grants")

    @property
    def support_standing_consent(self) -> str:
        """Hash holding the tenant's standing-consent setting (ADR-0017 §3) — global to this
        gateway, since the setting is a tenant-wide toggle, not per-operator."""
        return self._k("support:standing_consent")

    # --- enrolment (ADR-0024 §10) ---------------------------------------------

    def enrolment_invitation(self, code_hash: str) -> str:
        """Hash holding an unredeemed invitation, keyed by a HASH of the code rather than the
        code itself (ADR-0024 §10).

        The code is a bearer credential for exactly one call, so storing it verbatim would put
        a live credential in Redis for anyone with keyspace access — the thing ADR-0018's
        by-reference discipline exists to avoid. Redemption hashes what it was given and looks
        that up, so this key names the invitation without being able to present it. TTL'd:
        unlike the enrolment it produces, an invitation is the part that expires.
        """
        return self._k(f"enrolment:invitation:{code_hash}")

    @property
    def enrolment_invitation_index(self) -> str:
        """Set of live invitation code-hashes, so the tenant console can show what it has
        outstanding. Same best-effort membership caveat as `support_pending_index`: the hash's
        own TTL reaps it independently of this set."""
        return self._k("enrolment:invitations")

    def enrolment(self, enrolment_id: str) -> str:
        """Hash holding a live enrolment — the standing relationship between this tenant and
        its provider.

        **Deliberately not TTL'd**, unlike every other credential-bearing key in this file.
        ADR-0024 §10: an enrolment carries no capability (the provider's side permits one verb,
        *ask*), so an expiry would not be a security control but "a scheduled outage with a
        security-shaped name". The control is revocation, and the replacement for expiry's one
        virtue is that enrolments are listed and carry last-used.

        Note this sits at the same level as `enrolment:all` and `enrolment:invitations`. That
        is safe because an id is `secrets.token_urlsafe(16)` — 22 URL-safe characters, never a
        bare word — so no id can ever name an index. Written down because it is an invariant
        of the id generator rather than of this key, and a future change to shorter or
        caller-supplied ids would break it silently.
        """
        return self._k(f"enrolment:{enrolment_id}")

    @property
    def enrolment_index(self) -> str:
        """Set of live enrolment ids — what the tenant console lists. Unlike the support
        indexes nothing reaps members behind this set's back, so a member with no surviving
        hash means a revoke removed one and not the other; readers still tolerate it.

        Named `enrolment:all` rather than `enrolments` to keep every key in this namespace
        under one prefix — the convention `support:pending` and `devices:all` already follow.
        A bare plural also collides with the natural JSON field name in the API response,
        which made `test_no_inline_redis_key_strings_outside_the_keys_module` flag a response
        body as a stray key literal. The guard was right that the namespace was ambiguous."""
        return self._k("enrolment:all")

    def enrolment_credential(self, credential_hash: str) -> str:
        """Maps a presented enrolment credential to its enrolment id, keyed by the credential's
        hash for the same reason invitations are. This is the lookup `rbac.py` performs on every
        request the provider makes, so it is a direct key rather than a scan."""
        return self._k(f"enrolment:credential:{credential_hash}")

    def support_self_issue_window(self, subject: str) -> str:
        """Hash of {self_issues, last_self_issue} over a trailing review window — the same
        shape as `break_glass_window`, for the same reason: a standing-consent self-issued
        grant has no per-instance human approval, so a subject self-issuing very often is the
        signal worth flagging (ADR-0017 slice 5). TTL slides from the last self-issue."""
        return self._k(f"support:selfissue:{subject}:window")

    @property
    def tenant_notifications(self) -> str:
        """Capped list of durable, tenant-facing security notifications (ADR-0017 slice 5 /
        ADR-0023's confirmed gap) — the surface a tenant console will eventually poll so a
        break-glass activation or a frequently self-issued support grant is something the
        tenant is shown, not only something logged. Newest first; capped by LTRIM, not TTL'd,
        since a fixed-size recent list is the semantics wanted here, not an expiring one."""
        return self._k("tenant:notifications")

    # --- rate limiting -------------------------------------------------------

    def ratelimit(self, key: str) -> str:
        """Fixed-window counter. ``key`` is the caller-derived bucket ("scope:client")."""
        return self._k(f"rl:{key}")


def device_resource_uri(hostname: str, path: str = "") -> str:
    """An MCP resource URI for a device path.

    Client-facing and part of the external contract — a module-level function rather than a
    ``KeyBuilder`` method precisely so it cannot pick up a keyspace prefix by accident.
    """
    return f"device://{hostname}{path}"


#: Default unprefixed builder. Call sites use this; a future tenant-aware caller would
#: construct its own ``KeyBuilder(prefix=...)`` and pass it down explicitly.
KEYS = KeyBuilder()
