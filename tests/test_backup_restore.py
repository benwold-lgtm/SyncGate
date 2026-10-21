# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Backup restore — ADR-0011 PR 2.

Three properties carry the design, and each is tested by the thing it would let through:

**Fail-closed means nothing was written, not "it raised".** A wrong-key restore that threw
an exception *after* applying half the archive would satisfy a naive test and leave a
registry split across two key generations, with no record of where the boundary fell. So
the tests assert the registry is untouched, not merely that an error came back.

**The canary is what makes the preflight total.** An archive whose devices all use
``auth_type: none`` has no credential ciphertext to test, so without it a wrong-key restore
would pass preflight and fail at the far end.

**Restore must not be a way to register what registration would refuse.** ADR-0011 §4 said
the egress policy still applies because restore replays through ``register_device`` — which
was not true of the code (F-67: the policy lived in the route handler). The test for that
restores an archive containing a device whose ``base_url`` the current policy forbids and
requires that it fails *while its neighbours succeed* — per-device, not per-batch.
"""

from __future__ import annotations

import itertools

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from device_mcp_gateway.backup.export import build_archive
from device_mcp_gateway.backup.restore import (
    ON_CONFLICT_FAIL,
    ON_CONFLICT_OVERWRITE,
    OUTCOME_FAILED,
    OUTCOME_RESTORED,
    OUTCOME_SKIPPED,
    OUTCOME_WOULD_RESTORE,
    RestorePreflightError,
    plan_fingerprint_restore,
    restore_archive,
)
from device_mcp_gateway.shared.crypto import CredentialCodec

#: One working directory per app built in this module — see `_client`.
_STACK_SEQ = itertools.count()

ADMIN_KEY = "a" * 40
DEVICE_SECRET = "SUPER-SECRET-DEVICE-KEY-9f3a2b"
CHEAP_KDF = {"argon2_memory_cost_kib": 8, "argon2_iterations": 1, "argon2_lanes": 1, "passphrase_min_length": 16}


def _client(monkeypatch, tmp_path, *, secret_key: str, allow_private: bool = True):
    # Embedded mode persists to ``storage.db_path``, which defaults to the RELATIVE
    # "./data/devices.db" — so without this every test here writes its devices into the
    # repository's own working directory and leaves them there. They then load on the next
    # run of ANY embedded test, whose lifespan probes each one; a fleet of unreachable
    # `127.0.0.1:9` devices turns a 0.7s startup into 18s and makes `test_main.py`'s
    # shared-app livez test fail with "Semaphore is bound to a different event loop".
    #
    # Verified rather than assumed: `origin/main` fails that test identically once the
    # leaked database is put in place, and passes without it. Chdir-ing into `tmp_path`
    # keeps the relative default pointing somewhere pytest cleans up.
    #
    # A fresh directory PER CLIENT, not per test: several tests here build two stacks to
    # export from one and restore into the other, and a shared store would couple them.
    # (They get away with it today only because a `TestClient` used without `with` never
    # runs the lifespan, so nothing ever reads the file back — these tests leak writes and
    # perform no loads. That is not a property worth depending on.)
    stack_dir = tmp_path / f"stack-{next(_STACK_SEQ)}"
    stack_dir.mkdir()
    monkeypatch.chdir(stack_dir)
    monkeypatch.setenv("MCP_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("MCP_SECRET_KEY", secret_key)
    monkeypatch.setenv("MCP_ALLOW_PRIVATE_TARGETS", "true" if allow_private else "false")
    from device_mcp_gateway.main import create_app

    return TestClient(create_app())


def _auth():
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def _register(client, hostname, *, base_url="http://127.0.0.1:9", secret=None):
    body = {"hostname": hostname, "base_url": base_url}
    if secret:
        body.update({"auth_type": "api_key", "auth": {"api_key": secret}})
    resp = client.post("/v1/devices", headers=_auth(), json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp


def _restore(client, archive, *, dry_run=True, **over):
    """Preview, or preview-then-apply with the plan_token the preview minted.

    ADR-0018 §6: an apply must reference the plan it was previewed from. When the
    preview itself fails (a wrong key, a bad on_conflict, ...), the apply would fail
    identically — nothing else to bind a token to — so that response is returned as-is.
    """
    body = {"archive": archive}
    body.update(over)
    if dry_run:
        return client.post("/v1/admin/restore/preview", headers=_auth(), json=body)

    preview = client.post("/v1/admin/restore/preview", headers=_auth(), json=body)
    if preview.status_code != 200:
        return preview
    token = preview.json()["plan_token"]
    return client.post("/v1/admin/restore/apply", headers=_auth(), json={**body, "plan_token": token})


def _hostnames(client):
    return {d["hostname"] for d in client.get("/v1/devices", headers=_auth()).json()["devices"]}


# --- The round trip ---------------------------------------------------------


def test_export_wipe_restore_returns_the_fleet_and_its_credentials(monkeypatch, tmp_path):
    """The whole point of the feature, end to end, including the credential.

    A restore that reinstates hostnames but loses credentials is the failure that looks
    like success until the first tool call — so this decrypts the restored blob and
    compares plaintext, rather than checking the device exists.
    """
    key = Fernet.generate_key().decode()
    client = _client(monkeypatch, tmp_path, secret_key=key)
    _register(client, "alpha", secret=DEVICE_SECRET)
    _register(client, "beta")

    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    assert archive["counts"]["devices"] == 2

    for host in ("alpha", "beta"):
        assert client.delete(f"/v1/devices/{host}", headers=_auth()).status_code in (200, 204)
    assert _hostnames(client) == set()

    report = _restore(client, archive, dry_run=False)
    assert report.status_code == 200, report.text
    assert report.json()["counts"] == {OUTCOME_RESTORED: 2}
    assert _hostnames(client) == {"alpha", "beta"}

    restored = client.app.state.registry.get_profile("alpha").config
    codec = CredentialCodec.from_secret(key)
    stored = restored.auth_config
    # Embedded mode stores plaintext; either way it must decrypt/parse to the original.
    try:
        recovered = json.loads(codec.decrypt(stored))
    except Exception:
        recovered = json.loads(stored)
    assert recovered["api_key"] == DEVICE_SECRET, "the credential did not survive the round trip"


def test_a_dry_run_predicts_the_real_run_and_writes_nothing(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(client, "alpha")
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    client.delete("/v1/devices/alpha", headers=_auth())

    dry = _restore(client, archive).json()  # dry_run defaults to true
    assert dry["dry_run"] is True
    assert dry["counts"] == {OUTCOME_WOULD_RESTORE: 1}
    assert _hostnames(client) == set(), "a dry run must not write"

    wet = _restore(client, archive, dry_run=False).json()
    assert wet["counts"] == {OUTCOME_RESTORED: 1}
    assert [d["hostname"] for d in dry["devices"]] == [d["hostname"] for d in wet["devices"]]


def test_a_preview_says_fleet_handles_are_not_carried(monkeypatch, tmp_path):
    """ADR-0027 §5 follow-up: until an archive carries fleet handles, a restore must say so —
    otherwise every configured fleet URL answering 404 afterwards reads as a broken restore."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(client, "alpha")
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    preview = _restore(client, archive).json()
    assert any("fleet handles" in note for note in preview["not_carried"])


def test_the_preview_route_never_writes(monkeypatch, tmp_path):
    """The destructive direction is not reachable through the preview route at all.

    ADR-0018 §6 made this structural rather than a body default: there is no `dry_run`
    field left to omit or flip. `POST /admin/restore/preview` always simulates.
    """
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(client, "alpha")
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    client.delete("/v1/devices/alpha", headers=_auth())

    resp = client.post("/v1/admin/restore/preview", headers=_auth(), json={"archive": archive})
    assert resp.json()["dry_run"] is True
    assert _hostnames(client) == set()


# --- Fail-closed ------------------------------------------------------------


def test_a_wrong_key_restore_writes_absolutely_nothing(monkeypatch, tmp_path):
    """Not "it raised" — *nothing was applied*.

    A preflight that ran per device would abort partway and leave the registry split
    between two key generations, which is the outcome the whole fail-closed design exists
    to prevent.
    """
    exporter = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(exporter, "alpha", secret=DEVICE_SECRET)
    _register(exporter, "beta", secret=DEVICE_SECRET)
    _register(exporter, "gamma", secret=DEVICE_SECRET)
    archive = exporter.get("/v1/admin/backup", headers=_auth()).json()

    # A different stack, different key.
    target = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    assert _hostnames(target) == set()

    resp = _restore(target, archive, dry_run=False)
    assert resp.status_code == 409
    assert "MCP_SECRET_KEY" in resp.json()["detail"]
    assert _hostnames(target) == set(), "a failed preflight must leave the registry untouched"


def test_the_canary_catches_a_wrong_key_when_no_device_has_a_credential(monkeypatch, tmp_path):
    """The archive with nothing to decrypt — the case a credential-only preflight misses."""
    exporter = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(exporter, "alpha")  # auth_type none: no ciphertext anywhere
    _register(exporter, "beta")
    archive = exporter.get("/v1/admin/backup", headers=_auth()).json()
    assert all(d["auth_config"] is None for d in archive["devices"]), "premise: nothing else can be tested"

    target = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    resp = _restore(target, archive, dry_run=False)
    assert resp.status_code == 409
    assert _hostnames(target) == set()


@pytest.mark.asyncio
async def test_preflight_opens_every_credential_not_just_the_first():
    """One unreadable credential anywhere aborts the whole archive."""

    class _Reg:
        _backend = None

        async def list_devices(self):
            return []

    codec = CredentialCodec.from_secret(Fernet.generate_key().decode())
    other = CredentialCodec.from_secret(Fernet.generate_key().decode())
    archive = (
        await build_archive(
            registry=_Reg(),
            codec=codec,
            config={"backup": CHEAP_KDF},
            gateway_version="test",
            mode="embedded",
        )
    ).archive
    archive["devices"] = [
        {"hostname": "good", "base_url": "http://good.example.com", "auth_config": codec.encrypt("{}")},
        {"hostname": "bad", "base_url": "http://bad.example.com", "auth_config": other.encrypt("{}")},
    ]

    with pytest.raises(RestorePreflightError) as exc:
        await restore_archive(
            raw_archive=archive,
            registry=_Reg(),
            codec=codec,
            config={},
            dry_run=False,
        )
    assert "bad" in str(exc.value), "the failing device should be named"
    assert "aborted" in str(exc.value)


# --- The F-67 property: restore is not a policy bypass ----------------------


def test_a_device_the_egress_policy_now_forbids_fails_while_its_neighbours_restore(monkeypatch, tmp_path):
    """ADR-0011 §4, made real by F-67.

    The archive is taken on a stack that allows private targets and restored into one that
    does not — the ordinary "policy tightened since the backup" case, and the one a
    `backup:write` holder would otherwise use to reinstate a forbidden target. The
    forbidden device must fail *and the others must still land*: per-device, as the ADR
    says, not a failed batch and not a silent success.
    """
    key = Fernet.generate_key().decode()
    permissive = _client(monkeypatch, tmp_path, secret_key=key, allow_private=True)
    _register(permissive, "private-one", base_url="http://127.0.0.1:9")
    _register(permissive, "public-one", base_url="http://example.com")
    archive = permissive.get("/v1/admin/backup", headers=_auth()).json()

    strict = _client(monkeypatch, tmp_path, secret_key=key, allow_private=False)
    report = _restore(strict, archive, dry_run=False).json()

    outcomes = {d["hostname"]: d["outcome"] for d in report["devices"]}
    assert outcomes["private-one"] == OUTCOME_FAILED, "restore must not reinstate a now-forbidden target"
    reason = next(d["reason"] for d in report["devices"] if d["hostname"] == "private-one")
    assert "base_url" in reason
    assert outcomes["public-one"] == OUTCOME_RESTORED, "one refused device must not fail the batch"
    assert _hostnames(strict) == {"public-one"}


def test_the_dry_run_reports_the_policy_refusal_too(monkeypatch, tmp_path):
    """So an operator learns before the real run, which is the point of previewing."""
    key = Fernet.generate_key().decode()
    permissive = _client(monkeypatch, tmp_path, secret_key=key, allow_private=True)
    _register(permissive, "private-one", base_url="http://127.0.0.1:9")
    archive = permissive.get("/v1/admin/backup", headers=_auth()).json()

    strict = _client(monkeypatch, tmp_path, secret_key=key, allow_private=False)
    report = _restore(strict, archive).json()
    assert report["devices"][0]["outcome"] == OUTCOME_FAILED


# --- Conflicts --------------------------------------------------------------


def test_on_conflict_modes(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    client = _client(monkeypatch, tmp_path, secret_key=key)
    _register(client, "alpha", base_url="http://original.example.com")
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    # The live device now differs from the archived one.
    client.put(
        "/v1/devices/alpha",
        headers=_auth(),
        json={"hostname": "alpha", "base_url": "http://changed.example.com"},
    )

    skipped = _restore(client, archive, dry_run=False).json()  # skip is the default
    assert skipped["counts"] == {OUTCOME_SKIPPED: 1}
    live = client.get("/v1/devices/alpha", headers=_auth()).json()
    assert live["base_url"] == "http://changed.example.com", "skip must not touch live configuration"

    failed = _restore(client, archive, dry_run=False, on_conflict=ON_CONFLICT_FAIL).json()
    assert failed["counts"] == {OUTCOME_FAILED: 1}

    overwritten = _restore(client, archive, dry_run=False, on_conflict=ON_CONFLICT_OVERWRITE).json()
    assert overwritten["counts"] == {OUTCOME_RESTORED: 1}
    live = client.get("/v1/devices/alpha", headers=_auth()).json()
    assert live["base_url"] == "http://original.example.com"


def test_an_unknown_on_conflict_is_refused_rather_than_defaulted(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    resp = _restore(client, archive, dry_run=False, on_conflict="clobber")
    assert resp.status_code == 409
    assert "on_conflict" in resp.json()["detail"]


# --- Governance continuity --------------------------------------------------


def test_the_tools_revision_survives_so_clients_do_not_read_a_rollback(monkeypatch, tmp_path):
    """``register_device`` starts a device at revision 0. For a *restored* device that is
    wrong: a client polling ``tools_revision`` (F-41) would read the reset as the tool set
    having rolled back."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(client, "alpha")
    client.app.state.registry.get_profile("alpha").config.tools_revision = 5

    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    assert archive["devices"][0]["tools_revision"] == 5
    client.delete("/v1/devices/alpha", headers=_auth())

    _restore(client, archive, dry_run=False)
    assert client.app.state.registry.get_profile("alpha").config.tools_revision == 5


# --- Portable archives ------------------------------------------------------


def test_a_portable_archive_restores_into_a_stack_with_a_different_key(monkeypatch, tmp_path):
    """The migration path: the target's MCP_SECRET_KEY is unrelated to the exporter's."""
    passphrase = "correct horse battery staple"
    exporter = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(exporter, "alpha", secret=DEVICE_SECRET)
    archive = exporter.post(
        "/v1/admin/backup", headers=_auth(), json={"kind": "portable", "passphrase": passphrase}
    ).json()

    target = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    report = _restore(target, archive, dry_run=False, passphrase=passphrase).json()
    assert report["counts"] == {OUTCOME_RESTORED: 1}

    stored = target.app.state.registry.get_profile("alpha").config.auth_config
    codec = CredentialCodec.from_secret(None)
    try:
        recovered = json.loads(codec.decrypt(stored))
    except Exception:
        recovered = json.loads(stored)
    assert recovered["api_key"] == DEVICE_SECRET


def test_a_portable_archive_without_the_passphrase_is_refused(monkeypatch, tmp_path):
    exporter = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(exporter, "alpha")
    archive = exporter.post(
        "/v1/admin/backup", headers=_auth(), json={"kind": "portable", "passphrase": "z" * 20}
    ).json()

    target = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    no_pass = _restore(target, archive, dry_run=False)
    assert no_pass.status_code == 409
    assert "passphrase" in no_pass.json()["detail"]

    wrong = _restore(target, archive, dry_run=False, passphrase="q" * 20)
    assert wrong.status_code == 409
    assert "passphrase" in wrong.json()["detail"]
    assert _hostnames(target) == set()


# --- Authorization ----------------------------------------------------------


def test_preview_needs_only_backup_read(monkeypatch, tmp_path):
    """ADR-0018 §6: a preview is read-level work. A `backup:read`-only holder must not be
    turned away from the very loop (preview, adjust, preview again) the split exists to
    let run with no elevation held at all."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    from device_mcp_gateway.rbac import Principal, SCOPE_BACKUP_READ

    reader = Principal(subject="key:ro", scopes=frozenset({SCOPE_BACKUP_READ}), auth_method="api_key")
    client.app.state.authenticator._keys["r" * 40] = reader
    resp = client.post(
        "/v1/admin/restore/preview", headers={"Authorization": f"Bearer {'r' * 40}"}, json={"archive": archive}
    )
    assert resp.status_code == 200, resp.text


def test_apply_requires_backup_write_and_read_alone_is_not_enough(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    from device_mcp_gateway.rbac import Principal, SCOPE_BACKUP_READ

    reader = Principal(subject="key:ro", scopes=frozenset({SCOPE_BACKUP_READ}), auth_method="api_key")
    client.app.state.authenticator._keys["r" * 40] = reader
    resp = client.post(
        "/v1/admin/restore/apply", headers={"Authorization": f"Bearer {'r' * 40}"}, json={"archive": archive}
    )
    assert resp.status_code == 403


def test_a_body_without_an_archive_is_a_400_not_a_500(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    # A missing archive is caught before plan_token is even considered, on both routes.
    assert client.post("/v1/admin/restore/preview", headers=_auth(), json={}).status_code == 400
    assert client.post("/v1/admin/restore/apply", headers=_auth(), json={}).status_code == 400

    # Preview has no plan_token concept — a nonsense archive fails parsing directly.
    assert client.post("/v1/admin/restore/preview", headers=_auth(), json={"archive": "nonsense"}).status_code == 409

    # Apply without plan_token is refused before an archive — sensible or not — is ever
    # touched: there is no legitimate way to hold a token for a plan that never previewed
    # successfully, since preview would have failed on the same nonsense archive too.
    resp = client.post("/v1/admin/restore/apply", headers=_auth(), json={"archive": "nonsense"})
    assert resp.status_code == 400
    assert "plan_token" in resp.json()["detail"]


# --- ADR-0018 §6: plan_digest / plan_token ------------------------------------


def test_a_successful_preview_returns_plan_digest_and_plan_token(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    preview = client.post("/v1/admin/restore/preview", headers=_auth(), json={"archive": archive}).json()
    assert len(preview["plan_digest"]) == 64
    int(preview["plan_digest"], 16)  # hex
    assert isinstance(preview["plan_token"], str) and preview["plan_token"]


def test_a_successful_apply_also_carries_plan_digest(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(client, "alpha")
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    client.delete("/v1/devices/alpha", headers=_auth())

    applied = _restore(client, archive, dry_run=False).json()
    assert applied["plan_digest"]
    assert "plan_token" not in applied, "apply does not need to mint a fresh one"


def test_apply_without_a_plan_token_is_refused(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    resp = client.post("/v1/admin/restore/apply", headers=_auth(), json={"archive": archive})
    assert resp.status_code == 400
    assert "plan_token" in resp.json()["detail"]


def test_apply_with_a_forged_plan_token_is_refused_as_stale(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    resp = client.post(
        "/v1/admin/restore/apply", headers=_auth(), json={"archive": archive, "plan_token": "not-a-real-token"}
    )
    assert resp.status_code == 409
    body = resp.json()["detail"]
    assert body["error_code"] == "ERR_PLAN_STALE"
    # Names the field space the digest covers, never a diff or a previous value — there
    # is nothing to diff against, since a forged token never decodes to real inputs.
    assert set(body["fields"]) == {"archive", "passphrase", "on_conflict", "include_deadletters"}


def test_apply_with_a_plan_token_from_a_different_request_is_refused_as_stale(monkeypatch, tmp_path):
    """The legitimate-drift case: the token verifies (it is real, unexpired) but was
    minted over a different on_conflict than the one now being submitted."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(client, "alpha")
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    skip_preview = client.post(
        "/v1/admin/restore/preview", headers=_auth(), json={"archive": archive, "on_conflict": "skip"}
    ).json()

    resp = client.post(
        "/v1/admin/restore/apply",
        headers=_auth(),
        json={"archive": archive, "on_conflict": "overwrite", "plan_token": skip_preview["plan_token"]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "ERR_PLAN_STALE"
    # And nothing was written: the check runs before restore_archive is ever called.
    live = client.get("/v1/devices/alpha", headers=_auth()).json()
    assert live["base_url"] == archive["devices"][0]["base_url"]


def test_an_expired_plan_token_is_refused_as_stale(monkeypatch, tmp_path):
    import time

    import device_mcp_gateway.api.backup as backup_module

    monkeypatch.setattr(backup_module, "plan_digest_validity_seconds", lambda cfg: 0)
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    preview = client.post("/v1/admin/restore/preview", headers=_auth(), json={"archive": archive}).json()
    time.sleep(1.2)  # past the 0s window
    resp = client.post(
        "/v1/admin/restore/apply",
        headers=_auth(),
        json={"archive": archive, "plan_token": preview["plan_token"]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "ERR_PLAN_STALE"


# --- Dead letters -----------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restored_dead_letters_are_inert_until_replayed(real_redis):
    """They land on the dead-letter stream, never the live call stream.

    Restoring onto the call stream would re-execute every failed tool call in the archive
    the moment a worker picked the device up — for an archive taken mid-incident, that is
    the incident again.
    """
    from device_mcp_gateway.shared.keys import KEYS
    from device_mcp_gateway.shared.registry_backend import RedisRegistryBackend

    backend = RedisRegistryBackend(real_redis)
    call = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "reboot"}})
    entries = [{"id": "1526919030474-0", "fields": {"request_id": "r1", "message": call, "reason": "timeout"}}]

    written = await backend.dead_letter_import("dev", entries)
    assert written == 1

    assert await real_redis.exists(KEYS.device_calls("dev")) == 0, "nothing may reach the live call stream"
    round_tripped = await backend.dead_letter_export("dev")
    assert round_tripped[0]["fields"]["message"] == call
    assert round_tripped[0]["id"] == "1526919030474-0", "original ids preserve incident correlation"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dead_letter_import_keeps_the_call_when_an_id_cannot_be_reused(real_redis):
    """A colliding/older id must not cost us the call — the payload matters more than the
    timestamp, so the entry is appended rather than dropped."""
    from device_mcp_gateway.shared.registry_backend import RedisRegistryBackend

    backend = RedisRegistryBackend(real_redis)
    await backend.dead_letter_import("dev", [{"id": "9999999999999-0", "fields": {"message": "newest"}}])
    # An archived entry older than what the stream already holds: Redis refuses the id.
    written = await backend.dead_letter_import("dev", [{"id": "1-0", "fields": {"message": "older"}}])

    assert written == 1
    messages = [e["fields"]["message"] for e in await backend.dead_letter_export("dev")]
    assert "older" in messages, "the call must survive even when its id cannot"


# --- Endpoint fingerprints survive a restore (ADR-0015) ---------------------
#
# The failure these defend against is silent and one-directional: a device that comes back
# *unpinned* looks completely healthy, works normally, and trusts whatever answers at
# base_url on its next probe. Nothing raises, nothing is logged as wrong, and the control
# is void from the first disaster recovery onward — precisely when nobody is in a position
# to notice. So these assert on the restored fingerprint STATE, never on the restore
# report's success.

SPKI_A = "a1" * 32
SPKI_B = "b2" * 32


def _register_pinned(client, hostname, spki, *, base_url="https://127.0.0.1:9", policy=None):
    body = {"hostname": hostname, "base_url": base_url, "expected_tls_spki_sha256": spki}
    if policy:
        body["fingerprint_policy"] = policy
    resp = client.post("/v1/devices", headers=_auth(), json=body)
    assert resp.status_code in (200, 201), resp.text
    return resp


def _detail(client, hostname):
    resp = client.get(f"/v1/devices/{hostname}", headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_restored_device_is_still_pinned_and_does_not_re_tofu(monkeypatch, tmp_path):
    """The headline property of this change.

    Before the archive carried pins, this restore produced a device with
    ``fingerprint_state="unpinned"`` — indistinguishable from a healthy one in every
    response, and guaranteed to trust the next thing that answered.
    """
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_pinned(client, "alpha", SPKI_A, policy="enforce")

    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    assert client.delete("/v1/devices/alpha", headers=_auth()).status_code in (200, 204)

    report = _restore(client, archive, dry_run=False).json()
    assert report["counts"] == {OUTCOME_RESTORED: 1}

    detail = _detail(client, "alpha")
    assert detail["tls_spki_sha256"] == SPKI_A, "the pin did not survive — the device will re-TOFU"
    assert detail["fingerprint_state"] == "pinned"
    assert detail["fingerprint_policy"] == "enforce", "a per-device enforce must not silently downgrade"
    assert report["fingerprint_warnings"] == 0


def test_overwrite_keeps_the_live_pin_and_warns_rather_than_re_pinning(monkeypatch, tmp_path):
    """ADR-0015: restoring a pin that no longer matches must warn, not re-pin.

    The live pin was established against the endpoint as it is now, quite possibly by an
    audited approval. Writing the archived value over it would undo that decision silently
    and then, under ``enforce``, quarantine a device nothing was wrong with.
    """
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_pinned(client, "alpha", SPKI_A)
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    # The endpoint's key rotated and an operator re-pinned it since the backup was taken.
    client.delete("/v1/devices/alpha", headers=_auth())
    _register_pinned(client, "alpha", SPKI_B)

    report = _restore(client, archive, dry_run=False, on_conflict=ON_CONFLICT_OVERWRITE).json()

    assert _detail(client, "alpha")["tls_spki_sha256"] == SPKI_B, "the live pin was overwritten"
    assert report["fingerprint_warnings"] == 1
    warning = report["devices"][0]["fingerprint_warning"]
    assert SPKI_A[:16] in warning and SPKI_B[:16] in warning, "the operator must see both sides"


def test_overwrite_does_not_lose_a_pin_that_both_sides_agree_on(monkeypatch, tmp_path):
    """``replace_device`` rebuilds the record from registration inputs alone, so writing
    the fingerprint back is doing real work even when nothing disagrees. Without it an
    ordinary overwrite restore would quietly unpin the whole fleet."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_pinned(client, "alpha", SPKI_A)
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    report = _restore(client, archive, dry_run=False, on_conflict=ON_CONFLICT_OVERWRITE).json()

    assert report["counts"] == {OUTCOME_RESTORED: 1}
    assert _detail(client, "alpha")["tls_spki_sha256"] == SPKI_A
    assert _detail(client, "alpha")["fingerprint_state"] == "pinned"
    assert report["fingerprint_warnings"] == 0, "agreement is not a warning"


def test_a_quarantined_device_comes_back_quarantined(monkeypatch, tmp_path):
    """A restore must not launder an unapproved endpoint change into an approved baseline.

    Restoring a ``pending_approval`` device as ``pinned`` would clear a quarantine that a
    human never signed off — turning backup/restore into a way around ADR-0015 §6.
    """
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_pinned(client, "alpha", SPKI_A)
    archive = client.get("/v1/admin/backup", headers=_auth()).json()

    # As the health loop would have left it: still pinned to A, new key B seen and pending.
    archive["devices"][0]["fingerprint"].update(
        {"fingerprint_state": "pending_approval", "pending_tls_spki_sha256": SPKI_B}
    )
    client.delete("/v1/devices/alpha", headers=_auth())

    assert _restore(client, archive, dry_run=False).json()["counts"] == {OUTCOME_RESTORED: 1}

    detail = _detail(client, "alpha")
    assert detail["fingerprint_state"] == "pending_approval", "the quarantine was cleared by the restore"
    assert detail["pending_tls_spki_sha256"] == SPKI_B
    assert detail["tls_spki_sha256"] == SPKI_A, "the approved pin must still be the one on record"


def test_an_archive_from_before_fingerprinting_says_so_instead_of_failing_quietly(monkeypatch, tmp_path):
    """A v0.3.2 archive has no pins and cannot be given any. The restore still works — but
    it must not present a fleet of re-TOFU-ing devices as an unqualified success."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_pinned(client, "alpha", SPKI_A)
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    archive["devices"][0].pop("fingerprint")  # as an older gateway would have written it
    client.delete("/v1/devices/alpha", headers=_auth())

    report = _restore(client, archive, dry_run=False).json()

    assert report["counts"] == {OUTCOME_RESTORED: 1}
    assert report["fingerprint_warnings"] == 1
    assert "trust-on-first-use" in report["devices"][0]["fingerprint_warning"]
    assert _detail(client, "alpha")["fingerprint_state"] == "unpinned"


def test_a_plain_http_device_does_not_warn_about_a_pin_it_could_never_have(monkeypatch, tmp_path):
    """ADR-0015 §7: an http:// upstream has no authenticated dimension at all, so "no pin"
    is its permanent correct state. Warning on it every restore is the noise ADR-0015 §2
    argues destroys a control."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register(client, "alpha", base_url="http://127.0.0.1:9")
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    archive["devices"][0].pop("fingerprint")
    client.delete("/v1/devices/alpha", headers=_auth())

    report = _restore(client, archive, dry_run=False).json()
    assert report["fingerprint_warnings"] == 0
    assert "fingerprint_warning" not in report["devices"][0]


def test_a_dry_run_reports_the_fingerprint_warning_before_anything_is_written(monkeypatch, tmp_path):
    """A dry run is a real prediction. A restore about to discard an archived pin must say
    so while it can still be stopped."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_pinned(client, "alpha", SPKI_A)
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    client.delete("/v1/devices/alpha", headers=_auth())
    _register_pinned(client, "alpha", SPKI_B)

    dry = _restore(client, archive, on_conflict=ON_CONFLICT_OVERWRITE).json()

    assert dry["dry_run"] is True
    assert dry["counts"] == {OUTCOME_WOULD_RESTORE: 1}
    assert dry["fingerprint_warnings"] == 1
    assert _detail(client, "alpha")["tls_spki_sha256"] == SPKI_B, "a dry run must not write"


def test_a_skipped_device_carries_no_fingerprint_warning(monkeypatch, tmp_path):
    """on_conflict=skip writes nothing, so nothing can be lost and there is nothing to
    warn about. A warning here would train operators to ignore the real ones."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_pinned(client, "alpha", SPKI_A)
    archive = client.get("/v1/admin/backup", headers=_auth()).json()
    client.delete("/v1/devices/alpha", headers=_auth())
    _register_pinned(client, "alpha", SPKI_B)

    report = _restore(client, archive, dry_run=False).json()  # skip is the default
    assert report["counts"] == {OUTCOME_SKIPPED: 1}
    assert report["fingerprint_warnings"] == 0


# --- plan_fingerprint_restore, directly -------------------------------------


class _Live:
    """Just enough of a DeviceConfig for the planner, which only reads attributes."""

    def __init__(self, **fields):
        for name in (
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
        ):
            setattr(self, name, fields.get(name))


def test_planner_restores_the_archived_pin_for_a_new_device():
    record = {"base_url": "https://d", "fingerprint": {"tls_spki_sha256": SPKI_A, "fingerprint_state": "pinned"}}
    fields, warning = plan_fingerprint_restore(record, None)
    assert fields["tls_spki_sha256"] == SPKI_A
    assert fields["fingerprint_state"] == "pinned"
    assert warning is None


def test_planner_keeps_the_whole_live_block_not_a_merge_of_two_eras():
    """A pin, its context, its state and its policy are one coherent trust record. Halves
    from two eras would describe neither."""
    live = _Live(tls_spki_sha256=SPKI_B, fingerprint_state="pinned", fingerprint_policy="enforce")
    record = {
        "base_url": "https://d",
        "fingerprint": {"tls_spki_sha256": SPKI_A, "fingerprint_state": "pinned", "fingerprint_policy": "warn"},
    }
    fields, warning = plan_fingerprint_restore(record, live)
    assert fields["tls_spki_sha256"] == SPKI_B
    assert fields["fingerprint_policy"] == "enforce"
    assert warning is not None


def test_planner_takes_the_archive_when_the_live_device_has_no_pin():
    live = _Live(fingerprint_state="unpinned")
    record = {"base_url": "https://d", "fingerprint": {"tls_spki_sha256": SPKI_A, "fingerprint_state": "pinned"}}
    fields, warning = plan_fingerprint_restore(record, live)
    assert fields["tls_spki_sha256"] == SPKI_A, "there is no trust decision here to protect"
    assert warning is None
