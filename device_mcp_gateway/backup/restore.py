# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Restore a backup archive (ADR-0011).

Restoring is not the inverse of exporting, and the difference is the whole design.

**Nothing is written until the entire archive has been decrypt-tested.** The preflight
opens the canary and then every credential in the archive; any failure aborts everything.
A partial apply is the outcome worth the most trouble to avoid — it leaves a registry
half-migrated between two key generations, with no record of where the boundary fell. The
canary is what makes the check total: an archive of devices that all use ``auth_type:
none`` has no credential ciphertext, so without it a wrong-key restore would sail through
and fail at the far end.

The preflight differs by kind on purpose. A ciphertext archive is tested against this
stack's ``MCP_SECRET_KEY`` — a key mismatch is *the* expected failure and is named as such.
A portable archive is tested against the passphrase only, because it is key-independent by
design; demanding the target's key for one would defeat the reason it exists.

**Each device is replayed through the ordinary registration path**, gates and all
(``validate_device_registration``, F-67), never a raw Redis write. That is a security
property rather than tidiness: a ``backup:write`` holder must not be able to reinstate a
device the current egress policy forbids. It follows that a restore can legitimately fail
*on a device*, and those failures are reported per device rather than swallowed or
escalated into a failed batch.

**A restore re-establishes endpoint fingerprints but never re-pins one** (ADR-0015).
Registration starts a device unpinned, and an unpinned device trusts whatever answers
first — so without the archived pin every device would silently re-TOFU on restore and the
control would be void from the first disaster recovery onward, precisely when nobody is in
a position to notice. Where the archive and a live record disagree, the live pin stays and
the disagreement is reported: the archived value is historical, while the live one was
established against the endpoint as it is now, quite possibly by an audited human approval.
See :func:`plan_fingerprint_restore`.

``dry_run=True`` is the default. The destructive direction is never the one you get by
omission — and a dry run runs the same preflight and the same per-device gates, so its
report is a real prediction rather than a parse.

**A restore can succeed and still leave a device unable to authenticate** (ADR-0018 §3). An
OAuth2 refresh token is excluded from every archive, and for ``grant_type=refresh_token`` that
token *was* the credential — so the device comes back registered, reachable, correctly
fingerprinted, and unable to get a token until a human re-authorizes it. That is reported as
its own outcome and left behind as its own persistent device state, because the failure this
design exists to prevent is a device that looks restored and fails on its first tool call.

**And the same is true of a credential reference this stack cannot resolve** (ADR-0018 §3,
which asks for "an honest and visible failure rather than a silent one"). The restore now
resolves every archived ``credential_ref`` before writing anything, so the dry run reports it
while the restore can still be stopped. It is a *warning* rather than an outcome, and
deliberately not a persistent device field: unlike *needs reconnecting* — which only a human
supplying a credential can clear — a missing secret is fixed by putting it in the store, and a
flag recording its absence would go stale the moment somebody did. See
:func:`plan_credential_refs`, which also keeps §7's two failure kinds apart.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from loguru import logger

from device_mcp_gateway.backup.envelope import (
    Argon2Params,
    BackupError,
    KIND_CIPHERTEXT,
    KIND_PORTABLE,
    fernet_for_passphrase,
    parse_archive,
    verify_canary,
)
from device_mcp_gateway.credentials.resolver import (
    CredentialRef,
    ReferenceInvalid,
    ResolverError,
    StoreUnavailable,
    build_resolver,
    require_references,
)
from device_mcp_gateway.registry.validation import validate_device_registration
from device_mcp_gateway.security.url_policy import resolve_allow_private, resolve_allowed_ports
from device_mcp_gateway.shared.crypto import CredentialCodec

ON_CONFLICT_SKIP = "skip"
ON_CONFLICT_OVERWRITE = "overwrite"
ON_CONFLICT_FAIL = "fail"
ON_CONFLICT_MODES = (ON_CONFLICT_SKIP, ON_CONFLICT_OVERWRITE, ON_CONFLICT_FAIL)

# Per-device outcomes, reported back to the caller.
OUTCOME_RESTORED = "restored"
OUTCOME_WOULD_RESTORE = "would_restore"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
# ADR-0018 §3. Distinct outcomes rather than a flag on `restored`, because `counts` is what
# an operator actually reads at the end of a restore: `{"restored": 47,
# "restored_needs_reconnect": 3}` states the cost in the headline, where a count of 50 with
# an advisory beside it invites the reading that the restore simply worked.
#
# The dry-run variant exists for the same reason the dry run exists at all — a prediction
# that omitted the one outcome requiring a human would be predicting the easy half.
OUTCOME_RESTORED_NEEDS_RECONNECT = "restored_needs_reconnect"
OUTCOME_WOULD_RESTORE_NEEDS_RECONNECT = "would_restore_needs_reconnect"

#: The persistent per-device state a `needs_reconnect` restore leaves behind, mirrored on
#: ``DeviceConfig.credential_state``.
CREDENTIAL_STATE_NEEDS_RECONNECT = "needs_reconnect"

#: Said in the per-device `reason` and again in the device's own status. Deliberately names
#: what the operator must DO and why nothing else can do it — "credential missing" would send
#: them to the archive looking for a value that was never in it.
RECONNECT_REASON = (
    "restored without its OAuth2 refresh token, which is excluded from every archive "
    "(ADR-0018 §3). The token was this device's credential and nothing in an archive can "
    "re-mint one that required consent — re-authorize the device to restore it to service."
)


# The fingerprint fields an archive carries, in the order they are written back. Kept in
# lockstep with ``backup.export._FINGERPRINT_FIELDS`` — the two halves of one format.
#
# A *superset* of ``security.fingerprint.TRUST_FIELDS``, which is the subset that actually
# constitutes trust; the extra two describe what the upstream said it was. The relationship
# is asserted by a test rather than left to this comment, because a field added to the trust
# record and forgotten here would silently stop being archived.
_FINGERPRINT_FIELDS = (
    "tls_spki_sha256",
    "tls_cert_sha256",
    "tls_issuer",
    "tls_not_after",
    "declared_name",
    "declared_version",
    "fingerprint_state",
    "fingerprint_pinned_at",
    "pending_tls_spki_sha256",
    "fingerprint_policy",
)

# How much of a digest to show an operator comparing two of them. The full 64 hex
# characters twice in one sentence is unreadable, and the API has the whole value.
_DIGEST_PREVIEW = 16


class RestorePreflightError(BackupError):
    """The archive could not be opened. Raised before anything is written, always."""


def _opener(archive: dict[str, Any], codec: CredentialCodec, passphrase: str | None) -> Any:
    """The thing that decrypts this archive's credentials — codec or passphrase Fernet."""
    kind = archive.get("kind")
    if kind == KIND_PORTABLE:
        if not passphrase:
            raise RestorePreflightError("this is a portable archive; it needs the passphrase it was exported with")
        kdf = archive.get("kdf")
        if not isinstance(kdf, dict):
            raise RestorePreflightError("portable archive is missing its key-derivation parameters")
        # Derived from what the ARCHIVE declares, never from today's config: an archive
        # made under a different cost would otherwise derive a different key and look
        # exactly like a wrong passphrase.
        return fernet_for_passphrase(passphrase, Argon2Params.from_envelope(kdf))
    if kind == KIND_CIPHERTEXT:
        if not codec.enabled:
            raise RestorePreflightError(
                "this is a ciphertext archive and this stack has no MCP_SECRET_KEY to open it "
                "with. Restore it into a stack sharing that key, or use a portable archive."
            )
        return codec
    raise RestorePreflightError(f"unknown archive kind {kind!r}")


def _open_credential(blob: str, opener: Any) -> str:
    plaintext = opener.decrypt(blob if isinstance(opener, CredentialCodec) else blob.encode())
    return plaintext if isinstance(plaintext, str) else plaintext.decode()


def preflight(archive: dict[str, Any], codec: CredentialCodec, passphrase: str | None) -> Any:
    """Decrypt-test the whole archive. Returns the opener, or raises.

    Every credential, not a sample: the point is that a restore never discovers halfway
    through that it cannot read the rest.
    """
    opener = _opener(archive, codec, passphrase)
    try:
        verify_canary(archive, opener)
    except BackupError as exc:
        raise RestorePreflightError(str(exc)) from exc

    for record in archive.get("devices", []):
        blob = record.get("auth_config")
        if not blob:
            continue
        try:
            _open_credential(blob, opener)
        except Exception as exc:
            raise RestorePreflightError(
                f"credential for device '{record.get('hostname')}' could not be decrypted "
                f"({type(exc).__name__}). Nothing has been written — the whole restore is "
                "aborted rather than applied in part."
            ) from exc
    return opener


def _short(digest: str | None) -> str:
    return f"{digest[:_DIGEST_PREVIEW]}…" if digest and len(digest) > _DIGEST_PREVIEW else str(digest)


def plan_fingerprint_restore(record: dict[str, Any], existing: Any) -> tuple[dict[str, Any], str | None]:
    """What to write back for a device's endpoint fingerprint. ``(fields, warning)``.

    Pure, so the policy is testable without a registry — the same reason
    ``security.fingerprint.plan_update`` is pure.

    **The load-bearing rule is that a restore never re-pins a device that is already
    pinned** (ADR-0015, Consequences). Where the archive and the live record disagree, the
    live pin wins and the disagreement is *reported*. The archived value is historical; the
    live one was established by observing the endpoint as it is now, quite possibly through
    an audited human approval (ADR-0015 §6). Writing the old value over it would undo that
    decision silently and then, under ``enforce``, quarantine a device that nothing was
    wrong with — a restore that takes a healthy fleet offline is a restore nobody runs.

    ⚠️ Writing the live values back is **not** a no-op, which is the non-obvious part.
    ``on_conflict=overwrite`` goes through ``replace_device``, and that builds a *fresh*
    ``DeviceConfig`` from registration inputs alone — so by the time this lands, the live
    pin has already been wiped and re-establishing it is the only thing keeping the device
    from re-TOFU-ing.

    When a live pin exists the whole live block is kept, not a merge of the two. A pin, its
    context fields, its state and its policy are one coherent trust record; half from each
    era would describe neither.
    """
    block = record.get("fingerprint")
    live_spki = getattr(existing, "tls_spki_sha256", None) if existing is not None else None

    if live_spki:
        fields = {field: getattr(existing, field, None) for field in _FINGERPRINT_FIELDS}
        archived_spki = (block or {}).get("tls_spki_sha256")
        warning = None
        if archived_spki and archived_spki != live_spki:
            warning = (
                f"archive pins this device to SPKI {_short(archived_spki)} but it is currently "
                f"pinned to {_short(live_spki)}; the live pin was kept and the archived one "
                "discarded — a restore warns rather than re-pinning (ADR-0015). If the archived "
                "value is the correct one, delete the device and restore it again."
            )
        return fields, warning

    # No live pin to protect: whatever the archive carries beats trust-on-first-use.
    if block is None:
        return {}, _no_pin_warning(
            record,
            "this archive predates endpoint fingerprinting (ADR-0015) and carries no pin for "
            "this device, so it will trust-on-first-use on its next probe and record whatever "
            "answers. Re-export from a gateway running this version to capture pins.",
        )

    fields = {field: block.get(field) for field in _FINGERPRINT_FIELDS}
    if not block.get("tls_spki_sha256"):
        return fields, _no_pin_warning(
            record,
            "no fingerprint pin had been recorded for this device when the archive was made, "
            "so it will trust-on-first-use on its next probe.",
        )
    return fields, None


def _no_pin_warning(record: dict[str, Any], message: str) -> str | None:
    """Suppress the missing-pin warning for an upstream that could never have had one.

    A plain-``http://`` device has no authenticated dimension at all (ADR-0015 §7), so
    "no pin" is its permanent and correct state rather than a gap in the archive. Warning
    on it every restore would be noise of exactly the kind ADR-0015 §2 argues destroys a
    control — and it would be attached to the devices where the warning means least.
    """
    base_url = (record.get("base_url") or "").strip().lower()
    return message if base_url.startswith("https://") else None


async def plan_credential_refs(
    archive: dict[str, Any], opener: Any, resolver: Any
) -> tuple[dict[str, str], str | None]:
    """Can this stack actually resolve the references the archive carries?

    Returns ``(per_device_reason, fleet_reason)``.

    ADR-0018 §3 says restoring into a different stack "requires that stack to be able to
    resolve the references, which is an honest and visible failure rather than a silent one."
    It was neither: restore never touched the resolver, so a device whose ``credential_ref``
    the target cannot resolve came back reported as ``restored`` and failed at its first tool
    call. That is the same shape as a device that looks restored and cannot authenticate —
    one field over from the condition §3 spends most of its length preventing.

    **The two failure kinds stay two, which is the whole design.** §7 draws the line and the
    resolver already encodes it: a ``ReferenceInvalid`` is one device's problem, a
    ``StoreUnavailable`` is the fleet's. Reporting an unmounted secret store as N independent
    bad references is precisely the misdiagnosis §7 is written against — so the first store
    failure ends the pass and is reported once, at the top, instead of being smeared across
    every device in the archive.

    Read-only and side-effect-free: this resolves to find out whether resolution works, and
    keeps nothing. The material never leaves this function.
    """
    from device_mcp_gateway.worker.runner import _auth_from_config

    refs: list[tuple[str, str]] = []
    for record in archive.get("devices", []):
        blob = record.get("auth_config")
        if not blob:
            continue
        try:
            # Through the real handler, not by reading a field name out of the JSON. A handler
            # can hold more than one reference — `OAuth2Auth` holds `client_secret_ref` and
            # `password_ref` — and a second copy of that list here is exactly how one gets
            # added and silently skipped by this check. `credential_refs()` is the one answer.
            auth = _auth_from_config(record.get("auth_type"), _open_credential(blob, opener))
        except Exception:  # noqa: BLE001 — preflight already proved these open; a shape
            continue  # surprise here is not worth failing a restore over.
        if auth is None:
            continue
        for raw in auth.credential_refs().values():
            refs.append((record.get("hostname") or "<unnamed>", raw))

    if not refs:
        return {}, None

    if resolver is None:
        # Distinct from "the store is down": nothing is misconfigured at the store, this stack
        # simply has no credential-by-reference set up at all. An operator told "store
        # unavailable" would go looking for a mount that was never meant to exist.
        return {}, (
            f"{len(refs)} device(s) in this archive hold their credential by reference, but this "
            "stack has no credential resolver configured (gateway.credentials.root / "
            "MCP_CREDENTIAL_ROOT). They will restore as configuration and fail to authenticate "
            "until the secret store is mounted."
        )

    per_device: dict[str, str] = {}
    for hostname, raw in refs:
        try:
            await resolver.resolve(CredentialRef.parse(raw))
        except StoreUnavailable as exc:
            return {}, (
                f"the secret store is not usable on this stack ({exc}). Every by-reference device "
                "in this archive is affected, so per-device results are not reported — fix the "
                "store and re-run."
            )
        except ReferenceInvalid as exc:
            per_device[hostname] = (
                f"this stack cannot resolve {raw!r}: {exc}. The device restores as configuration "
                "and cannot authenticate until the secret exists in this stack's store — "
                "provisioning it is a separate operation (ADR-0018 §2a)."
            )
        except ResolverError as exc:
            per_device[hostname] = f"this stack cannot resolve {raw!r}: {type(exc).__name__}: {exc}"
    return per_device, None


async def restore_archive(
    *,
    raw_archive: Any,
    registry: Any,
    codec: CredentialCodec,
    config: dict,
    passphrase: str | None = None,
    dry_run: bool = True,
    on_conflict: str = ON_CONFLICT_SKIP,
    include_deadletters: bool = False,
) -> dict[str, Any]:
    """Replay an archive into this stack. Returns a per-device report."""
    if on_conflict not in ON_CONFLICT_MODES:
        raise BackupError(f"on_conflict must be one of: {', '.join(ON_CONFLICT_MODES)}")

    archive = parse_archive(raw_archive)
    opener = preflight(archive, codec, passphrase)  # raises before any write

    allow_private = resolve_allow_private(config)
    allowed_ports = resolve_allowed_ports(config)
    backend = registry._backend

    # Decided before anything is written, so the DRY RUN carries it too. A reference that this
    # stack cannot resolve is exactly the thing worth learning while the restore can still be
    # stopped — after the fact it is indistinguishable from a device nobody has used yet.
    cred_warnings, cred_fleet_warning = await plan_credential_refs(archive, opener, build_resolver(config))
    # F-67's rule applied to ADR-0018 §1: a restore must not be a way to reinstate a device
    # that a fresh registration would refuse.
    require_refs = require_references(config)

    results: list[dict[str, Any]] = []
    for record in archive.get("devices", []):
        results.append(
            await _restore_one(
                record,
                registry=registry,
                backend=backend,
                archive=archive,
                opener=opener,
                dry_run=dry_run,
                on_conflict=on_conflict,
                allow_private=allow_private,
                allowed_ports=allowed_ports,
                include_deadletters=include_deadletters,
                credential_warning=cred_warnings.get(record.get("hostname") or "<unnamed>"),
                require_references_setting=require_refs,
            )
        )

    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1

    # Surfaced at the top level, not left for the caller to find by scanning the device
    # list. A fingerprint warning on 3 of 500 devices is exactly the thing that gets
    # missed in a per-device report during an incident, and it is the thing worth reading.
    warned = sum(1 for r in results if r.get("fingerprint_warning"))
    # Lifted out of `counts` as its own name for the same reason `fingerprint_warnings` is:
    # this is the number a caller should branch on, and reading it out of a counts dict means
    # knowing both outcome spellings and remembering to add the dry-run one.
    # Counted from the RESULTS, not from the plan: a device the conflict rules skipped is not
    # one this restore is putting into service, and warning about its reference would send an
    # operator to fix something nothing is waiting on.
    cred_warned = sum(1 for r in results if r.get("credential_warning"))
    reconnect = counts.get(OUTCOME_RESTORED_NEEDS_RECONNECT, 0) + counts.get(OUTCOME_WOULD_RESTORE_NEEDS_RECONNECT, 0)

    logger.info(
        f"Restore {'(dry run) ' if dry_run else ''}complete: {counts}"
        + (f" ({warned} fingerprint warning{'s' if warned != 1 else ''})" if warned else "")
    )
    if cred_fleet_warning:
        logger.error(f"Restore: {cred_fleet_warning}")
    elif cred_warned:
        logger.warning(
            f"{cred_warned} restored device(s) hold a credential reference this stack cannot "
            "resolve; they are registered and will fail to authenticate until the secret exists "
            "in this stack's secret store."
        )
    if reconnect:
        # WARNING, not INFO. A restore that ends with devices nobody can bring back without a
        # human is the one line from this operation that must survive a log level set during
        # an incident to keep the noise down.
        logger.warning(
            f"{reconnect} device(s) {'would need' if dry_run else 'need'} re-authorizing: their "
            "OAuth2 refresh token is excluded from every archive (ADR-0018 §3) and nothing in an "
            "archive can re-mint one. They are registered and reachable but cannot authenticate "
            "until a human reconnects them."
        )
    return {
        "dry_run": dry_run,
        "kind": archive.get("kind"),
        "on_conflict": on_conflict,
        "created_at": archive.get("created_at"),
        "counts": counts,
        "fingerprint_warnings": warned,
        "credential_warnings": cred_warned,
        #: Set when the failure is the STORE rather than any one device (ADR-0018 §7). Its
        #: presence means per-device credential results were deliberately not produced.
        "credential_store_error": cred_fleet_warning,
        "needs_reconnect": reconnect,
        #: ADR-0027 §5 follow-up, said up front because a restore that silently left every
        #: configured fleet URL answering 404 would read as a broken restore.
        "not_carried": [
            "fleet handles: an archive does not include them yet, so a restored stack has none; "
            "re-mint them with POST /v1/fleets and update the URLs clients are configured with"
        ],
        "devices": results,
    }


async def _restore_one(
    record: dict[str, Any],
    *,
    registry: Any,
    backend: Any,
    archive: dict[str, Any],
    opener: Any,
    dry_run: bool,
    on_conflict: str,
    allow_private: bool,
    allowed_ports: set[int] | None,
    include_deadletters: bool,
    credential_warning: str | None = None,
    require_references_setting: bool = False,
) -> dict[str, Any]:
    hostname = record.get("hostname") or "<unnamed>"
    # Assigned once the device is known to be one this restore will write; read by
    # _result at call time, so the outcomes decided before that carry no warning.
    fp_warning: str | None = None

    def _result(outcome: str, reason: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"hostname": hostname, "outcome": outcome}
        if reason:
            out["reason"] = reason
        if fp_warning:
            out["fingerprint_warning"] = fp_warning
        # Attached only to outcomes this restore is actually putting into service. A `skipped`
        # device is one the conflict rules left alone; its reference is the live record's
        # business, not this restore's, and warning about it is an errand nothing is waiting on.
        if credential_warning and outcome in (
            OUTCOME_RESTORED,
            OUTCOME_WOULD_RESTORE,
            OUTCOME_RESTORED_NEEDS_RECONNECT,
            OUTCOME_WOULD_RESTORE_NEEDS_RECONNECT,
        ):
            out["credential_warning"] = credential_warning
        return out

    upstream_kind = record.get("upstream_kind") or "openapi"
    upstream_transport = record.get("upstream_transport") or "http"
    spec_url = record.get("spec_url")
    # Built before the gates rather than just before the write, so `validate_device_registration`
    # can see the credential form (ADR-0018 §1) and the DRY RUN predicts a refusal. Rebuilding
    # it later would mean the preview passed and the apply failed — the one thing a dry run
    # exists to rule out.
    try:
        auth = _auth_for(record, opener)
    except Exception as exc:  # noqa: BLE001 — reported per device, never fatal to the batch
        return _result(OUTCOME_FAILED, f"credential could not be rebuilt: {exc}")
    try:
        # The same gates POST /v1/devices runs (F-67). `declared` names only the upstream
        # keys an OpenAPI device may legitimately carry, so a restored record does not trip
        # the "upstream_transport on an OpenAPI device" rule on a value it merely stored.
        validate_device_registration(
            hostname=hostname,
            base_url=record.get("base_url") or "",
            spec_url=spec_url,
            transport=record.get("transport") or "sse",
            upstream_kind=upstream_kind,
            upstream_transport=upstream_transport,
            declared={"upstream_kind"} if upstream_kind == "mcp" else set(),
            allow_private=allow_private,
            allowed_ports=allowed_ports,
            auth=auth,
            require_references=require_references_setting,
        )
    except HTTPException as exc:
        # A device the current policy no longer permits. Correct behaviour, reported —
        # the restore does not become a way to reinstate what registration would refuse.
        return _result(OUTCOME_FAILED, str(exc.detail))

    existing = await registry.get_device(hostname)
    if existing is not None:
        if on_conflict == ON_CONFLICT_SKIP:
            return _result(OUTCOME_SKIPPED, "already registered")
        if on_conflict == ON_CONFLICT_FAIL:
            return _result(OUTCOME_FAILED, "already registered (on_conflict=fail)")

    # Decided before the dry-run return on purpose: a dry run is a real prediction, and a
    # restore that would discard an archived pin must say so while it can still be stopped.
    fp_fields, fp_warning = plan_fingerprint_restore(record, existing)

    # ADR-0018 §3: read from the archive rather than re-derived from the credential here.
    # Export is where the payload was last readable, so it is where the question "was the
    # excluded token this device's whole credential" has an answer; asking again after the
    # value is gone could only guess.
    reconnect = bool(record.get("needs_reconnect"))

    if dry_run:
        return _result(OUTCOME_WOULD_RESTORE_NEEDS_RECONNECT if reconnect else OUTCOME_WOULD_RESTORE)

    kwargs = dict(
        hostname=hostname,
        base_url=record.get("base_url"),
        spec_url=spec_url,
        auth=auth,
        transport=record.get("transport") or "sse",
        rate_limit_rps=record.get("rate_limit_rps"),
        upstream_kind=upstream_kind,
        upstream_transport=upstream_transport,
    )
    try:
        if existing is not None:  # on_conflict == overwrite
            await registry.replace_device(**kwargs)
        else:
            await registry.register_device(**kwargs)
    except HTTPException as exc:
        return _result(OUTCOME_FAILED, str(exc.detail))
    except Exception as exc:  # noqa: BLE001
        return _result(OUTCOME_FAILED, f"{type(exc).__name__}: {exc}")

    await _restore_governance(
        hostname,
        record,
        archive,
        backend,
        registry=registry,
        include_deadletters=include_deadletters,
        fingerprint_fields=fp_fields,
        needs_reconnect=reconnect,
    )
    if reconnect:
        return _result(OUTCOME_RESTORED_NEEDS_RECONNECT, RECONNECT_REASON)
    return _result(OUTCOME_RESTORED)


def _auth_for(record: dict[str, Any], opener: Any) -> Any:
    """Rebuild the device's auth handler from the archived credential.

    Uses the existing ``_auth_from_config``, which is already the exact inverse of the
    ``_auth_to_record`` that wrote it — reimplementing the mapping here is how the two
    drift.
    """
    from device_mcp_gateway.worker.runner import _auth_from_config

    blob = record.get("auth_config")
    if not blob:
        return None
    return _auth_from_config(record.get("auth_type"), _open_credential(blob, opener))


async def _restore_governance(
    hostname: str,
    record: dict[str, Any],
    archive: dict[str, Any],
    backend: Any,
    *,
    registry: Any,
    include_deadletters: bool,
    fingerprint_fields: dict[str, Any] | None = None,
    needs_reconnect: bool = False,
) -> None:
    """Put back what registration does not reconstruct.

    ``register_device`` starts a device at ``tools_revision=0`` with no change history,
    which is right for a new device and wrong for a restored one: a client polling the
    revision would read the reset as the tool set having rolled back (F-41). The endpoint
    fingerprint is here for the same reason and with higher stakes: registration starts a
    device unpinned, and an unpinned device trusts the next thing that answers (ADR-0015).
    These are written after the replay rather than through it, because they are governance
    metadata *about* a registration rather than inputs *to* one — the egress policy has
    nothing to say about them.
    """
    change = (archive.get("tool_changes") or {}).get(hostname)
    revision = record.get("tools_revision") or 0
    fields: dict[str, Any] = {}
    if revision:
        fields["tools_revision"] = revision
    fields.update(fingerprint_fields or {})
    if needs_reconnect:
        # Written here, with the fingerprint block, because it is the same kind of thing:
        # governance metadata ABOUT a registration rather than an input TO one. Registration
        # has nothing to say about it, and `register_device` would have just reset it to the
        # default — this device is not one whose credential is merely absent, it is one whose
        # credential requires a human.
        fields["credential_state"] = CREDENTIAL_STATE_NEEDS_RECONNECT
    if fields:
        # Through the registry, not the backend: in embedded mode the backend is memory only,
        # so a restored pin or `needs_reconnect` written there was gone at the next restart.
        await registry.update_persisted_fields(hostname, **fields)
    if change:
        await backend.set_last_tool_change(hostname, change)

    if include_deadletters:
        entries = (archive.get("dead_letters") or {}).get(hostname) or []
        if entries:
            written = await backend.dead_letter_import(hostname, entries)
            logger.info(f"Restored {written} dead-letter entr{'y' if written == 1 else 'ies'} for {hostname}")
