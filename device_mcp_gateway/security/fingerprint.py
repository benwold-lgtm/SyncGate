# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Endpoint fingerprinting — pin what a device *is* (ADR-0015, F-69).

Three dimensions, deliberately never collapsed into one "verified" flag:

* **Authenticated** — the TLS **SPKI** digest. Cryptographic; proves this is the same
  endpoint as last time.
* **Declared** — MCP ``serverInfo`` / OpenAPI ``info``. Self-reported, therefore spoofable.
  Inventory that doubles as a change signal, never evidence of identity.
* **Behavioural** — ``canonical_tools_hash`` / ``tools_revision``, which already exist.

Why SPKI and not the certificate
--------------------------------
A routine renewal re-issues the certificate against the **same key**, so the SPKI is
unchanged and nothing fires. Pinning the certificate digest would alarm on every ACME
renewal — every 60-90 days, for every device — and
[ADR-0013 §8]'s argument applies unchanged: a control that fires constantly trains people
to approve reflexively, which destroys the signal exactly where it needs to mean something.
A *changed key* is indistinguishable from a substituted endpoint, and that is worth an alarm.

What this does not do
---------------------
The first observation is **trust on first use**: it establishes a baseline, it does not
validate anything. If the endpoint was already the wrong one at registration, the wrong
value is what gets pinned. Nothing here should be described as verifying identity.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from loguru import logger

# Fingerprint states, mirrored on DeviceConfig.fingerprint_state.
STATE_UNPINNED = "unpinned"
STATE_PINNED = "pinned"
STATE_PENDING = "pending_approval"

# Verdicts from compare().
VERDICT_UNCHANGED = "unchanged"
VERDICT_FIRST_PIN = "first_pin"
VERDICT_CERT_ROTATED = "cert_rotated"  # same key, new cert — informational
VERDICT_DECLARED_CHANGED = "declared_changed"  # same key, new name/version — informational
VERDICT_KEY_CHANGED = "key_changed"  # SPKI moved — WARN, needs approval
VERDICT_KEY_AND_DECLARED_CHANGED = "key_and_declared_changed"  # strongest signal

# Verdicts that require a human decision before the device is trusted again.
NEEDS_APPROVAL = frozenset({VERDICT_KEY_CHANGED, VERDICT_KEY_AND_DECLARED_CHANGED})

POLICY_WARN = "warn"
POLICY_ENFORCE = "enforce"

# The device fields that together constitute one endpoint's TRUST RECORD: what key was
# seen, what state that leaves the device in, and which policy governs a change.
#
# Named here rather than at either call site because it is the fingerprint module's
# business what a pin consists of, and because two callers now have to agree about it —
# a restore writing an archived record back, and an update carrying the live one across a
# rebuild. A second hand-maintained list of field names is how one of them silently stops
# covering a field that was added to the other.
#
# ⚠️ Deliberately NOT the same list as the archive's fingerprint block, which also carries
# `declared_name`/`declared_version`. Those describe what the upstream *said it was*, are
# re-derived from the spec on every provision, and are not part of what is trusted. See
# ``backup.restore._FINGERPRINT_FIELDS``, which is a *format* list and a superset of this.
TRUST_FIELDS = (
    "tls_spki_sha256",
    "tls_cert_sha256",
    "tls_issuer",
    "tls_not_after",
    "fingerprint_state",
    "fingerprint_pinned_at",
    "pending_tls_spki_sha256",
    "fingerprint_policy",
)


def has_trust_record(cfg: Any) -> bool:
    """Whether this device carries anything worth preserving across a rebuild.

    A pin is the obvious case, but not the only one: a device can be *unpinned* and still
    carry a per-device ``fingerprint_policy`` of ``enforce``, and dropping that on an
    unrelated edit downgrades the device to whatever the fleet default happens to be.
    A pending approval is likewise a decision in progress that an edit must not discard.
    """
    if cfg is None:
        return False
    return bool(
        getattr(cfg, "tls_spki_sha256", None)
        or getattr(cfg, "pending_tls_spki_sha256", None)
        or getattr(cfg, "fingerprint_policy", None)
        or getattr(cfg, "fingerprint_state", STATE_UNPINNED) != STATE_UNPINNED
    )


@dataclass(frozen=True)
class Observation:
    """What a single probe saw. Every field optional — a plain-http upstream has no TLS
    dimension at all (ADR-0015 §7), and a probe that failed before the handshake has none
    of them."""

    tls_spki_sha256: str | None = None
    tls_cert_sha256: str | None = None
    tls_issuer: str | None = None
    tls_not_after: str | None = None
    declared_name: str | None = None
    declared_version: str | None = None

    def has_tls(self) -> bool:
        return bool(self.tls_spki_sha256)

    def is_empty(self) -> bool:
        """Nothing was learned. Distinct from "learned that nothing is there"."""
        return not any(
            (
                self.tls_spki_sha256,
                self.tls_cert_sha256,
                self.declared_name,
                self.declared_version,
            )
        )


def spki_sha256(der_cert: bytes) -> str:
    """SHA-256 of the certificate's SubjectPublicKeyInfo, hex encoded."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    cert = x509.load_der_x509_certificate(der_cert)
    spki = cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


def observe_tls(ssl_object: Any) -> Observation:
    """Build an :class:`Observation` from a live TLS connection.

    ⚠️ ``getpeercert`` must be called **positionally**. The object handed back by
    httpcore's ``get_extra_info("ssl_object")`` is a raw ``_SSLSocket``, not the
    ``ssl.SSLSocket`` wrapper, so the documented ``binary_form=True`` keyword raises
    ``TypeError: _SSLSocket.getpeercert() takes no keyword arguments``.

    Never raises: a fingerprint that cannot be read must not break the request it rode in
    on. A failure here yields an empty observation, which compare() treats as "learned
    nothing" rather than as "the endpoint changed".
    """
    try:
        der = ssl_object.getpeercert(True)
        if not der:
            return Observation()
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        return Observation(
            tls_spki_sha256=spki_sha256(der),
            tls_cert_sha256=hashlib.sha256(der).hexdigest(),
            tls_issuer=cert.issuer.rfc4514_string(),
            tls_not_after=cert.not_valid_after_utc.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 — see docstring: must never break the request
        logger.debug(f"TLS fingerprint capture failed: {type(exc).__name__}: {exc}")
        return Observation()


def compare(stored: Observation, seen: Observation) -> str:
    """Classify ``seen`` against the pinned ``stored`` values.

    Returns one of the ``VERDICT_*`` constants. The classification exists so the common,
    boring cases do not drown the one that matters (ADR-0015 §3) — a single
    undifferentiated "fingerprint changed" alarm would fire mostly on certificate renewals
    and version bumps.

    Note the tool-surface hash is deliberately **not** an input: it changes on ordinary
    firmware and feature upgrades, and ``tools_revision`` already tracks it.
    """
    if seen.is_empty():
        # Learned nothing this round — an unreachable device, or a probe that failed before
        # the handshake. Absence of evidence is not evidence of change.
        return VERDICT_UNCHANGED

    if not stored.tls_spki_sha256 and not stored.declared_name:
        return VERDICT_FIRST_PIN

    declared_changed = _declared_changed(stored, seen)

    # Only compare the key when both sides have one. An https device that becomes
    # unreachable, or a probe that could not read the cert, must not read as "key removed".
    if stored.tls_spki_sha256 and seen.tls_spki_sha256:
        if stored.tls_spki_sha256 != seen.tls_spki_sha256:
            return VERDICT_KEY_AND_DECLARED_CHANGED if declared_changed else VERDICT_KEY_CHANGED
        if stored.tls_cert_sha256 and seen.tls_cert_sha256 and stored.tls_cert_sha256 != seen.tls_cert_sha256:
            # Same key, new certificate: the renewal case that must NOT alarm.
            return VERDICT_CERT_ROTATED

    return VERDICT_DECLARED_CHANGED if declared_changed else VERDICT_UNCHANGED


def _declared_backfill(stored: Observation, seen: Observation) -> dict[str, Any]:
    """Declared values the stored record lacks, so a first sighting is recorded.

    Deliberately **not** routed through ``compare`` as a "declared changed" verdict.
    Learning a name for the first time is not evidence that the endpoint became something
    else, and treating it as one would let ``key_and_declared_changed`` — the strongest
    signal this module can raise (ADR-0015 §3) — fire on a device that merely acquired an
    inventory label in the same cycle its key rotated. The verdict stays honest; only the
    record is completed.

    Self-limiting, so it costs one write and not a per-cycle churn: once the value is
    stored, ``stored.declared_*`` is truthy and this returns nothing.
    """
    fields: dict[str, Any] = {}
    if seen.declared_name and not stored.declared_name:
        fields["declared_name"] = seen.declared_name
    if seen.declared_version and not stored.declared_version:
        fields["declared_version"] = seen.declared_version
    return fields


def _declared_changed(stored: Observation, seen: Observation) -> bool:
    """True only when the upstream reported something *different*, not when it reported
    nothing. A probe that skipped the handshake has no declared fields, and treating that
    as a change would alarm on every transient failure."""
    for old, new in (
        (stored.declared_name, seen.declared_name),
        (stored.declared_version, seen.declared_version),
    ):
        if new and old and new != old:
            return True
    return False


def plan_update(
    stored: Observation,
    state: str,
    pending_spki: str | None,
    seen: Observation,
    now: float,
) -> tuple[str, dict[str, Any]]:
    """Decide what to persist after a probe. Returns ``(verdict, fields_to_write)``.

    Pure so the policy can be tested without a registry. An **empty dict means write
    nothing** — which is the common case, and deliberately so: the health loop runs every
    ~30s per device, and re-writing an unchanged fingerprint each cycle would be pure write
    amplification against Redis for no new information.

    The load-bearing rule is that a changed key **does not re-pin**. The approved
    ``tls_spki_sha256`` stays put and the new value lands in ``pending_tls_spki_sha256``, so
    an operator can see both sides of the decision and so a silent substitution cannot
    launder itself into the baseline by being observed twice.
    """
    verdict = compare(stored, seen)

    if verdict == VERDICT_UNCHANGED:
        # Nothing moved — but the record may still be missing a declared value the probe
        # can now supply, so fill it rather than writing nothing. This is the ordinary
        # state of any device pinned before its declared source existed: an OpenAPI device
        # upgraded onto a gateway that reads `info`, or one whose first pin came from
        # registration pre-pinning. Without the fill it stays blank forever, because
        # `_declared_changed` compares only when BOTH sides have a value, so None → "Acme"
        # is not a change and the informational branch below is never reached.
        return verdict, _declared_backfill(stored, seen)

    if verdict == VERDICT_FIRST_PIN:
        # Trust on first use: this establishes a baseline, it does not validate anything.
        return verdict, {
            "tls_spki_sha256": seen.tls_spki_sha256,
            "tls_cert_sha256": seen.tls_cert_sha256,
            "tls_issuer": seen.tls_issuer,
            "tls_not_after": seen.tls_not_after,
            "declared_name": seen.declared_name,
            "declared_version": seen.declared_version,
            "fingerprint_state": STATE_PINNED,
            "fingerprint_pinned_at": now,
        }

    if verdict in NEEDS_APPROVAL:
        if state == STATE_PENDING and pending_spki == seen.tls_spki_sha256:
            # Already flagged, and the endpoint is still showing the same new key. Nothing
            # new to record; writing every cycle would just churn.
            return verdict, {}
        return verdict, {
            "fingerprint_state": STATE_PENDING,
            "pending_tls_spki_sha256": seen.tls_spki_sha256,
        }

    # Informational: same key, so the endpoint is still the same endpoint. Refresh the
    # contextual fields in place and stay pinned — this is the renewal / version-bump path
    # that must NOT interrupt anyone.
    fields: dict[str, Any] = {}
    if verdict == VERDICT_CERT_ROTATED:
        fields.update(
            {
                "tls_cert_sha256": seen.tls_cert_sha256,
                "tls_issuer": seen.tls_issuer,
                "tls_not_after": seen.tls_not_after,
            }
        )
    if seen.declared_name:
        fields["declared_name"] = seen.declared_name
    if seen.declared_version:
        fields["declared_version"] = seen.declared_version
    return verdict, fields


def approve(seen_spki: str | None, now: float) -> dict[str, Any]:
    """Fields that re-pin a device to its pending key (ADR-0015 §6).

    Clears the pending value in the same write. Leaving it set would make an approved
    device still look mid-decision to the UI and to any later comparison.
    """
    return {
        "tls_spki_sha256": seen_spki,
        "pending_tls_spki_sha256": None,
        "fingerprint_state": STATE_PINNED,
        "fingerprint_pinned_at": now,
    }


def resolve_policy(device_policy: str | None, default_policy: str | None) -> str:
    """Effective policy for a device: its own override, else the deployment default,
    else ``warn`` (ADR-0015 §5).

    Defaults to warn rather than enforce on purpose. A fleet that stops on certificate
    rotation is a fleet whose operators switch the check off within a quarter, and a
    disabled control detects nothing — strictly worse than a warning someone might read.
    """
    for candidate in (device_policy, default_policy):
        if candidate and candidate.lower() in (POLICY_WARN, POLICY_ENFORCE):
            return candidate.lower()
    return POLICY_WARN


# JSON-RPC methods that *use* the device: they send its credentials upstream and return its
# data. Everything else — initialize, tools/list, resources/list, ping — only *observes*,
# and stays available so a quarantined device remains distinguishable from a dead one
# (ADR-0015 §9). resources/read is here deliberately: it carries the same exposure as a
# GET-shaped tool call and differs only in code path, so stopping one and not the other
# would leave a quiet route to exactly what the quarantine prevents.
# prompts/get joined with ADR-0028: for a proxied device it is forwarded to the upstream with
# the device's credentials, exactly like a resource read.
QUARANTINED_METHODS = frozenset({"tools/call", "resources/read", "prompts/get"})


def quarantine_reason(
    fingerprint_state: str,
    device_policy: str | None,
    default_policy: str | None,
    method: str | None = None,
) -> str | None:
    """Why this call must be refused, or None to let it through.

    Returns a message rather than a bool so the caller can tell the client *why* — a
    generic failure would send an operator hunting the device instead of the fingerprint.
    """
    if method is not None and method not in QUARANTINED_METHODS:
        return None
    if not is_quarantined(fingerprint_state, resolve_policy(device_policy, default_policy)):
        return None
    return (
        "device is quarantined: its endpoint fingerprint changed and has not been approved "
        "(security.fingerprint_policy=enforce). Approve the new fingerprint or remove the device."
    )


def is_quarantined(fingerprint_state: str, effective_policy: str) -> bool:
    """Whether the device's *use* paths must refuse (ADR-0015 §9).

    Quarantine covers tool calls, resource reads **and** prompt fetches: all send the device's credentials
    and return its data, differing only in code path, so stopping one and not the other
    would leave a quiet route to exactly what the quarantine prevents. Observation paths —
    reachability probes, diagnostics, GET /v1/devices — deliberately keep working, so a
    quarantined device stays distinguishable from a dead one.
    """
    return fingerprint_state == STATE_PENDING and effective_policy == POLICY_ENFORCE
