# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""ADR-0018 §3 — a restore must say when this stack cannot resolve a reference.

§3 states it as a consequence: restoring into a different stack "requires that stack to be
able to resolve the references, which is an honest and visible failure rather than a silent
one." It was neither. ``restore.py`` never touched the resolver, so a device whose
``credential_ref`` the target could not resolve was reported ``restored`` and failed at its
first tool call — the same shape as a device that looks restored and cannot authenticate,
one field over from the condition §3 spends most of its length preventing.

**The property under test is the two failure kinds staying two.** ADR-0018 §7 draws the line
and the resolver already encodes it: a ``ReferenceInvalid`` is one device's problem, a
``StoreUnavailable`` is the fleet's. An unmounted secret store reported as N independent bad
references is the exact misdiagnosis §7 is written against — it sends an operator to check N
references when the actual fault is one volume. So the store case is asserted to produce
**no** per-device results at all, not merely a different message.

**Why this is a warning and not a persistent device field.** *Needs reconnecting* is durable
because only a human supplying a credential clears it. A missing secret is different: it is
fixed by putting the secret in the store, and nothing tells the gateway that happened, so a
stored flag would go stale and start lying. The restore reports it; the device's own dispatch
path reports it thereafter.
"""

from __future__ import annotations

import itertools
import json
import stat

import pytest
from cryptography.fernet import Fernet

from device_mcp_gateway.backup.envelope import KIND_CIPHERTEXT, build_envelope, seal_canary
from device_mcp_gateway.backup.restore import (
    OUTCOME_RESTORED,
    OUTCOME_SKIPPED,
    OUTCOME_WOULD_RESTORE,
    plan_credential_refs,
    restore_archive,
)
from device_mcp_gateway.credentials.resolver import MountedFilesResolver
from device_mcp_gateway.shared.crypto import CredentialCodec

TENANT = "t-3f9a1c2b7d4e8065"
SECRET = "9c2f" * 12

_SEQ = itertools.count()


def _store(tmp_path, *names):
    """A mounted-files store holding a secret for each named device, mode 0600."""
    root = tmp_path / f"store-{next(_SEQ)}"
    for name in names:
        d = root / TENANT / "devices" / name
        d.mkdir(parents=True)
        f = d / "api-key"
        f.write_text(SECRET)
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ref(name):
    return f"secret://{TENANT}/devices/{name}#api-key"


def _archive(codec, *devices):
    """An archive carrying by-reference devices, sealed the way an export seals them.

    Built through the real ``build_envelope``/``seal_canary`` rather than as a literal, so it
    passes the same preflight a genuine archive does. A hand-written dict would have skipped
    the canary — and the preflight is precisely the step this feature runs alongside.
    """
    out = build_envelope(
        kind=KIND_CIPHERTEXT,
        gateway_version="test",
        mode="embedded",
        canary=seal_canary(codec),
        counts={"devices": len(devices)},
    )
    out["devices"] = []
    for name in devices:
        payload = {"type": "api_key", "credential_ref": _ref(name), "header_name": "X-API-Key"}
        out["devices"].append(
            {
                "hostname": name,
                "base_url": f"http://127.0.0.1:9/{name}",
                "auth_type": "api_key",
                "auth_config": codec.encrypt(json.dumps(payload)),
                "credential_excluded": [],
            }
        )
    return out


@pytest.fixture()
def codec():
    return CredentialCodec.from_secret(Fernet.generate_key().decode())


# ── The plan, directly ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_resolvable_reference_produces_no_warning(tmp_path, codec):
    root = _store(tmp_path, "prism")
    per_device, fleet = await plan_credential_refs(_archive(codec, "prism"), codec, MountedFilesResolver(str(root)))
    assert (per_device, fleet) == ({}, None)


@pytest.mark.asyncio
async def test_one_bad_reference_is_that_device_s_problem(tmp_path, codec):
    """`ReferenceInvalid`: the store is healthy and answering "no" about one path."""
    root = _store(tmp_path, "prism")  # 'ghost' deliberately absent
    per_device, fleet = await plan_credential_refs(
        _archive(codec, "prism", "ghost"), codec, MountedFilesResolver(str(root))
    )

    assert fleet is None, "a healthy store must not be reported as a fleet failure"
    assert set(per_device) == {"ghost"}
    assert _ref("ghost") in per_device["ghost"]
    assert "separate operation" in per_device["ghost"], "say that provisioning is not this restore's job"


@pytest.mark.asyncio
async def test_an_unmounted_store_is_the_FLEET_s_problem_and_names_no_devices(tmp_path, codec):
    """The misdiagnosis ADR-0018 §7 exists to prevent.

    A root that is not present means the volume failed, and every by-reference device in the
    archive is affected for a reason that is none of their faults. Reporting it as three bad
    references would send an operator to check three references when one mount is wrong — so
    the per-device map must be **empty**, not merely differently worded.
    """
    missing = tmp_path / "never-mounted"
    per_device, fleet = await plan_credential_refs(
        _archive(codec, "a", "b", "c"), codec, MountedFilesResolver(str(missing))
    )

    assert per_device == {}, "a store outage must not be smeared across devices"
    assert fleet is not None
    assert "secret store" in fleet
    assert "per-device results are not reported" in fleet


@pytest.mark.asyncio
async def test_no_resolver_configured_is_its_own_message(tmp_path, codec):
    """Distinct from a store outage: nothing is broken, this stack simply has no
    credential-by-reference set up. An operator told "store unavailable" would go looking for
    a mount that was never meant to exist."""
    per_device, fleet = await plan_credential_refs(_archive(codec, "prism"), codec, None)

    assert per_device == {}
    assert fleet is not None
    assert "no credential resolver configured" in fleet
    assert "MCP_CREDENTIAL_ROOT" in fleet, "name the knob, not just the concept"


@pytest.mark.asyncio
async def test_an_archive_with_no_references_asks_the_resolver_nothing(tmp_path, codec):
    """An all-inline fleet must not be made to depend on a secret store it never used."""

    class _Exploding:
        async def resolve(self, ref):
            raise AssertionError("the resolver must not be consulted for an inline credential")

    archive = {
        "kind": "ciphertext",
        "devices": [
            {
                "hostname": "inline",
                "base_url": "http://127.0.0.1:9",
                "auth_type": "api_key",
                "auth_config": codec.encrypt(json.dumps({"type": "api_key", "api_key": "k" * 20})),
            }
        ],
    }
    assert await plan_credential_refs(archive, codec, _Exploding()) == ({}, None)


@pytest.mark.asyncio
async def test_the_resolved_material_is_not_returned_anywhere(tmp_path, codec):
    """This resolves to find out whether resolution works, and keeps nothing."""
    root = _store(tmp_path, "prism")
    per_device, fleet = await plan_credential_refs(_archive(codec, "prism"), codec, MountedFilesResolver(str(root)))
    assert SECRET not in json.dumps([per_device, fleet])


# ── Through a real restore ───────────────────────────────────────────────────────────────


class _Backend:
    def __init__(self):
        self.devices = {}

    async def get_last_tool_change(self, hostname):
        return None

    async def update_device_fields(self, hostname, **fields):
        return True

    async def set_last_tool_change(self, hostname, change):
        return None


class _Registry:
    def __init__(self, existing=()):
        self._backend = _Backend()
        self._existing = set(existing)
        self.registered = []

    async def get_device(self, hostname):
        return object() if hostname in self._existing else None

    async def register_device(self, **kwargs):
        self.registered.append(kwargs["hostname"])

    async def replace_device(self, **kwargs):
        self.registered.append(kwargs["hostname"])

    async def update_persisted_fields(self, hostname, **fields):
        await self._backend.update_device_fields(hostname, **fields)


def _config(root):
    return {"gateway": {"credentials": {"root": str(root)}}, "security": {"allow_private_targets": True}}


@pytest.mark.asyncio
async def test_the_DRY_RUN_reports_it_while_the_restore_can_still_be_stopped(tmp_path, codec):
    """The whole point of deciding this before anything is written. After the fact, a device
    with an unresolvable reference is indistinguishable from one nobody has used yet."""
    root = _store(tmp_path, "prism")
    report = await restore_archive(
        raw_archive=_archive(codec, "prism", "ghost"),
        registry=_Registry(),
        codec=codec,
        config=_config(root),
        dry_run=True,
    )

    assert report["dry_run"] is True
    assert report["credential_warnings"] == 1
    by_host = {d["hostname"]: d for d in report["devices"]}
    assert by_host["ghost"]["outcome"] == OUTCOME_WOULD_RESTORE
    assert "cannot resolve" in by_host["ghost"]["credential_warning"]
    assert "credential_warning" not in by_host["prism"]


@pytest.mark.asyncio
async def test_the_device_still_restores_rather_than_failing(tmp_path, codec):
    """Refusing would couple the restore to the order the secret store came back in — a DR
    restore of a large fleet would fail wholesale because the registry was rebuilt first. The
    archive carries configuration and the configuration is valid; what is missing is a secret
    somebody provisions separately (ADR-0018 §2a)."""
    root = _store(tmp_path, "prism")
    registry = _Registry()
    report = await restore_archive(
        raw_archive=_archive(codec, "prism", "ghost"),
        registry=registry,
        codec=codec,
        config=_config(root),
        dry_run=False,
    )

    assert sorted(registry.registered) == ["ghost", "prism"]
    assert {d["outcome"] for d in report["devices"]} == {OUTCOME_RESTORED}
    assert report["credential_warnings"] == 1


@pytest.mark.asyncio
async def test_a_store_outage_reports_once_at_the_top_and_not_per_device(tmp_path, codec):
    report = await restore_archive(
        raw_archive=_archive(codec, "a", "b", "c"),
        registry=_Registry(),
        codec=codec,
        config=_config(tmp_path / "never-mounted"),
        dry_run=True,
    )

    assert report["credential_store_error"] is not None
    assert report["credential_warnings"] == 0
    assert all("credential_warning" not in d for d in report["devices"])


@pytest.mark.asyncio
async def test_a_skipped_device_is_not_warned_about(tmp_path, codec):
    """It is the live record's business, not this restore's — an errand nothing is waiting on."""
    root = _store(tmp_path, "prism")
    report = await restore_archive(
        raw_archive=_archive(codec, "ghost"),
        registry=_Registry(existing={"ghost"}),
        codec=codec,
        config=_config(root),
        dry_run=False,
    )

    assert report["devices"][0]["outcome"] == OUTCOME_SKIPPED
    assert "credential_warning" not in report["devices"][0]
    assert report["credential_warnings"] == 0


@pytest.mark.asyncio
async def test_a_healthy_by_reference_restore_stays_quiet(tmp_path, codec):
    root = _store(tmp_path, "prism")
    report = await restore_archive(
        raw_archive=_archive(codec, "prism"),
        registry=_Registry(),
        codec=codec,
        config=_config(root),
        dry_run=False,
    )
    assert report["credential_warnings"] == 0
    assert report["credential_store_error"] is None
