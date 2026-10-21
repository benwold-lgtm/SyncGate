# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Build a backup archive from a live registry (ADR-0011).

What goes in, and why the list is short
---------------------------------------
**Included** — the device registry and the governance record of how each device's tool
surface last changed (``device:{h}:tools_change``, deliberately un-TTL'd because it is the
one thing here with no other source of truth).

**Opt-in** — dead letters. Valuable mid-incident, unbounded noise otherwise.

**Excluded on purpose** — everything reconstructible or ephemeral: claims and leases,
worker membership, assignment and call streams, sessions, idempotency markers, rate-limit
counters, the TTL'd manifest cache. Restoring a stale claim or a half-consumed stream would
actively harm a fresh stack, so the omission is a feature, not a gap.

**And, since ADR-0018 §3, the OAuth2 refresh token** — for exactly that reason rather than a
new one. It is gateway-accumulated runtime state that had been slipping through only because
it is stored inside ``auth_config`` next to genuine registration inputs. See
``backup/rotating.py`` for the boundary and why it falls where it does.

Per device the archive carries **registration inputs**, not runtime state. ``pod_active``,
``reachable``, ``last_check``, ``worker_id`` and ``spec_hash`` are all measurements of a
particular running stack and mean nothing in another one — restoring them would assert
facts about a fleet that has not been contacted yet, which is exactly the mistake F-66 was.
The restoring stack establishes them itself.

The one apparent exception is the endpoint fingerprint (ADR-0015), and it proves the rule.
A pin looks like a measurement — something a probe observed — but it is a **baseline
somebody is trusting**, in the same family as ``tools_revision``: TOFU established it, or a
human approved it, and either way it encodes a decision rather than a reading. So it
travels. Dropping it would not lose a fact the new stack can re-derive; it would silently
re-run trust-on-first-use against whatever now answers at ``base_url`` — the exact
substitution the control exists to catch, at the exact moment nobody is watching.

The credential field
--------------------
The archive's ``auth_config`` is always **ciphertext under whatever seals this archive** —
the stack's ``MCP_SECRET_KEY`` for a ciphertext archive, the passphrase-derived key for a
portable one. That invariant needs stating because the two modes do not store credentials
the same way:

- **Distributed** encrypts before writing to Redis (``Registry._register_distributed``).
- **Embedded** stores ``DeviceConfig.auth_config`` as **plaintext JSON** — encryption
  happens a layer lower, in the SQLite store.

So exporting the stored value verbatim would produce a genuine ciphertext archive on one
mode and a plaintext credential dump on the other, from the same "safe by default" call.
Normalising here is what makes the archive's security property independent of the mode it
came from, and a ciphertext export with no key configured is **refused** rather than
quietly downgraded.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Any

from loguru import logger

from device_mcp_gateway.backup.envelope import (
    Argon2Params,
    BackupError,
    KIND_CIPHERTEXT,
    KIND_PORTABLE,
    build_envelope,
    PASSPHRASE_GENERATED,
    PASSPHRASE_SUPPLIED,
    check_passphrase,
    fernet_for_passphrase,
    generate_passphrase,
    seal_canary,
)
from device_mcp_gateway.backup.rotating import needs_reconnect, strip_rotating
from device_mcp_gateway.shared.crypto import CredentialCodec

# Registration inputs — the fields a restore feeds back through register_device. Runtime
# state is deliberately absent; see the module docstring.
_DEVICE_FIELDS = (
    "hostname",
    "base_url",
    "spec_url",
    "auth_type",
    "rate_limit_rps",
    "upstream_kind",
    "upstream_transport",
)

# The endpoint fingerprint (ADR-0015), carried as its own block rather than folded in
# above, because it restores under different rules: these are not inputs to a
# registration, and one of them is a decision a human made.
#
# The pin is a *governance* record, not a runtime measurement, which is why it travels
# while `reachable` and `last_check` do not. Leaving it out would mean every device
# silently re-TOFUs on the first restore — the comparison would find no stored SPKI, read
# the endpoint as a first pin, and record whatever answered. The control would be void
# from the first disaster recovery onward, which is precisely when nobody is in a position
# to notice (ADR-0015, Consequences).
#
# `fingerprint_state` and `pending_tls_spki_sha256` are here for the same reason: a device
# exported mid-`pending_approval` must come back still pending. Restoring it as `pinned`
# would let a restore launder an unapproved endpoint change into an approved baseline.
#
# `fingerprint_policy` is a per-device *configuration* rather than an observation, and
# dropping it would silently downgrade an `enforce` device to the deployment default.
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


class CiphertextExportUnavailable(BackupError):
    """A ciphertext export was requested on a stack with no ``MCP_SECRET_KEY``.

    Refused rather than served: the archive would be labelled ciphertext and contain
    plaintext credentials, which is the one outcome the default kind exists to prevent.
    """


def credential_plaintext(blob: str | None, codec: CredentialCodec) -> str | None:
    """The plaintext JSON behind a stored ``auth_config``, whichever way it was stored.

    Tries a decrypt and falls back to treating the value as plaintext. Deliberately not
    ``codec.is_current`` — that answers "encrypted under the *primary* key", so during a
    key rotation a value under the older key would look like plaintext and be encrypted a
    second time, producing an archive of double-wrapped credentials that decrypt to
    ciphertext and fail nothing loudly.
    """
    if not blob:
        return None
    if not codec.enabled:
        return blob
    from cryptography.fernet import InvalidToken

    try:
        return codec.decrypt(blob)
    except InvalidToken:
        # Not encrypted under any configured key: embedded mode's plaintext JSON.
        return blob


@dataclass(frozen=True)
class ExportResult:
    """An archive, and the passphrase this call minted for it — if it minted one.

    Two fields rather than one dict because they must never travel together into storage:
    writing the passphrase into the archive would put the secret inside the thing it
    protects. Keeping them separate at the type level makes that mistake awkward to make.
    """

    archive: dict[str, Any]
    #: Present only for a portable export with no supplied passphrase. The caller's single
    #: opportunity to capture it; it is stored nowhere and cannot be re-read.
    passphrase: str | None = None


async def build_archive(
    *,
    registry: Any,
    codec: CredentialCodec,
    config: dict,
    kind: str = KIND_CIPHERTEXT,
    passphrase: str | None = None,
    include_deadletters: bool = False,
    gateway_version: str,
    mode: str,
) -> "ExportResult":
    """Collect the registry into a sealed archive.

    Returns an :class:`ExportResult` rather than the archive alone, because a portable export
    with no supplied passphrase **mints one**, and the caller gets exactly one chance to
    receive it (ADR-0011). A function that generated a secret and returned only the thing it
    protects would be handing back an archive nobody can ever open.

    Raises :class:`CiphertextExportUnavailable` when a ciphertext export has no key to
    encrypt with, and :class:`~device_mcp_gateway.backup.envelope.PassphraseTooWeak` when a
    *supplied* portable passphrase is below the configured floor.
    """
    backup_cfg = config.get("backup", {})
    # Set only when this call minted a passphrase. A supplied one is never echoed back: the
    # caller already has it, and repeating a secret multiplies the places it can be captured.
    revealed: str | None = None

    if kind == KIND_CIPHERTEXT:
        if not codec.enabled:
            raise CiphertextExportUnavailable(
                "a ciphertext archive needs MCP_SECRET_KEY to encrypt credentials with, and "
                "none is configured — this stack would emit them in plaintext. Set a Fernet "
                "key, or request a portable archive (kind=portable), which carries its own "
                "passphrase-derived encryption."
            )
        sealer: Any = codec
        kdf_envelope = None
    elif kind == KIND_PORTABLE:
        floor = int(backup_cfg.get("passphrase_min_length", 16))
        if passphrase:
            passphrase = check_passphrase(passphrase, minimum=floor)
            passphrase_source = PASSPHRASE_SUPPLIED
        else:
            # ADR-0011: absence is now a request to mint one, not an error. A scheduled caller
            # that has always supplied a passphrase is unaffected; one that never could now
            # gets a stronger secret than the floor would have accepted.
            passphrase = generate_passphrase()
            passphrase_source = PASSPHRASE_GENERATED
            revealed = passphrase
        params = Argon2Params.generate(
            memory_cost_kib=int(backup_cfg.get("argon2_memory_cost_kib", 65536)),
            iterations=int(backup_cfg.get("argon2_iterations", 3)),
            lanes=int(backup_cfg.get("argon2_lanes", 4)),
        )
        # Derived once for the whole archive, not per credential.
        sealer = fernet_for_passphrase(passphrase, params)
        kdf_envelope = params.to_envelope(
            passphrase_source=passphrase_source,
            passphrase_min_length=floor,
        )
    else:
        raise BackupError(f"unknown archive kind {kind!r}")

    backend = registry._backend
    devices_raw = await registry.list_devices()

    devices: list[dict[str, Any]] = []
    # Devices whose credential WAS the excluded token (ADR-0018 §3): they restore as
    # configuration and then need a human. Counted at the top of the archive so an operator
    # planning a restore learns the cost before running one, not from a per-device scan.
    excluded_devices: list[str] = []
    tool_changes: dict[str, Any] = {}
    dead_letters: dict[str, list[dict]] = {}
    deadletter_limit = int(backup_cfg.get("deadletter_limit", 1000))

    for cfg_obj in devices_raw:
        record = {field: getattr(cfg_obj, field, None) for field in _DEVICE_FIELDS}
        # Governance counter travels with the device: resetting it on restore would tell
        # every polling client the tool set had rolled back (F-41).
        record["tools_revision"] = getattr(cfg_obj, "tools_revision", 0)
        # Always written, even when nothing is pinned: the block's *presence* is how a
        # reader tells a fingerprint-aware archive from one exported before ADR-0015, and
        # those two call for different advice — re-export the stack versus accept that
        # this device has no baseline yet.
        record["fingerprint"] = {field: getattr(cfg_obj, field, None) for field in _FINGERPRINT_FIELDS}
        plaintext = credential_plaintext(getattr(cfg_obj, "auth_config", None), codec)
        plaintext, excluded, reconnect = _exclude_rotating(cfg_obj.hostname, record["auth_type"], plaintext)
        record["auth_config"] = _seal(plaintext, sealer) if plaintext else None
        # Written even when empty, and that is the point: the *presence* of the key is how a
        # reader tells an archive that applied ADR-0018 §3 from one made before it existed.
        # Without it, "no refresh_token here" is unfalsifiable — indistinguishable from a
        # device that never had one — and the exclusion becomes a claim rather than a record.
        record["credential_excluded"] = excluded
        if reconnect:
            # Decided at export because this is where the credential is still readable. The
            # restore must not have to decrypt a blob to work out that the blob is missing
            # the one thing that mattered.
            record["needs_reconnect"] = True
            excluded_devices.append(cfg_obj.hostname)
        devices.append(record)

        change = await backend.get_last_tool_change(cfg_obj.hostname)
        if change:
            tool_changes[cfg_obj.hostname] = change

        if include_deadletters:
            entries = await backend.dead_letter_export(cfg_obj.hostname, count=deadletter_limit)
            if entries:
                dead_letters[cfg_obj.hostname] = entries

    counts = {
        "devices": len(devices),
        "tool_changes": len(tool_changes),
        "dead_letters": sum(len(v) for v in dead_letters.values()),
        "needs_reconnect": len(excluded_devices),
    }
    archive = build_envelope(
        kind=kind,
        gateway_version=gateway_version,
        mode=mode,
        canary=seal_canary(sealer),
        counts=counts,
        kdf=kdf_envelope,
    )
    archive["devices"] = devices
    archive["tool_changes"] = tool_changes
    archive["dead_letters"] = dead_letters

    logger.info(
        f"Backup archive built: kind={kind} devices={counts['devices']} "
        f"tool_changes={counts['tool_changes']} dead_letters={counts['dead_letters']}"
    )
    if excluded_devices:
        logger.warning(
            f"{len(excluded_devices)} device(s) hold a gateway-minted refresh token, which is "
            f"excluded from every archive (ADR-0018 §3): {', '.join(sorted(excluded_devices))}. "
            "They will restore as configuration and need re-authorizing by a human — nothing "
            "an archive can carry re-mints a token that required consent."
        )
    return ExportResult(archive=archive, passphrase=revealed)


def _exclude_rotating(
    hostname: str, auth_type: str | None, plaintext: str | None
) -> tuple[str | None, list[str], bool]:
    """Drop gateway-minted material from one device's credential before it is sealed.

    Returns ``(plaintext, excluded_fields, needs_reconnect)``.

    **An unparseable payload is dropped whole rather than passed through**, and that is the
    non-obvious branch. Exclusion can only be *proved* on a payload this function could read;
    re-sealing an opaque blob would put a value in the archive that nobody has shown is free
    of a live token, while the archive claims otherwise. A claim that holds except where it
    silently does not is worth less than no claim.

    Dropping it costs the device nothing it still had: a payload this stack cannot decode is
    one the worker cannot decode either, so that device is already dispatching without
    credentials. The archive records the fact instead of the blob.
    """
    if not plaintext:
        return None, [], False
    try:
        payload = json.loads(plaintext)
        if not isinstance(payload, dict):
            raise ValueError(f"expected an object, got {type(payload).__name__}")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            f"Device '{hostname}' has an unreadable stored credential ({exc}); it is omitted "
            "from the archive rather than copied through unexamined, because ADR-0018 §3's "
            "exclusion cannot be verified on a payload that cannot be parsed. This device is "
            "already dispatching without credentials on this stack."
        )
        return None, [], False

    excluded = strip_rotating(auth_type, payload)
    reconnect = needs_reconnect(auth_type, payload)
    return json.dumps(payload), excluded, reconnect


def _seal(plaintext: str, sealer: Any) -> str:
    """Encrypt a credential blob under whatever seals this archive (codec or Fernet)."""
    out = sealer.encrypt(plaintext if isinstance(sealer, CredentialCodec) else plaintext.encode())
    return out if isinstance(out, str) else out.decode()
