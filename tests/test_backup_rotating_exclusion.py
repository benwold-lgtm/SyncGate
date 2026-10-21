# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""ADR-0018 §3 — the gateway-minted refresh token is excluded from every archive.

Three properties, and each is tested by the thing that would let it through.

**The token is gone from the archive, not merely from the field it lived in.** The headline
test registers an OAuth2 device through the **real embedded registration path** and greps
the whole serialised archive for the token string — the same standard
``test_backup_export`` set for the mode-independence property, and for the same reason. A
test that asserted ``payload["refresh_token"] is None`` would pass on an implementation that
left a copy in ``extra_params``, in a nested block, or in a second device's record.

**Excluding it must cost the devices it does not apply to nothing.** ``client_credentials``
and ``password`` devices also receive rotated refresh tokens from providers, and for them
the token is disposable — the operator-provisioned ``client_secret`` or ``password`` is
still what authenticates. Reporting those as needing a human would be a false alarm on a
device that restores seamlessly, so the discrimination is tested directly rather than
assumed from the strip.

**The one real cost must be visible, not discovered.** A ``grant_type=refresh_token`` device
arrives from a restore registered, reachable, correctly fingerprinted and unable to
authenticate. The ADR's standard is that it must not look restored and fail on its first
tool call — so the tests assert the restore *report* says so, and that the device's own
status still says so afterwards, on the **list** projection and not only the detail view.
That last part is a defect ``fingerprint_state`` already has and this field must not inherit.
"""

from __future__ import annotations

import itertools

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from device_mcp_gateway.backup.restore import (
    CREDENTIAL_STATE_NEEDS_RECONNECT,
    OUTCOME_RESTORED,
    OUTCOME_RESTORED_NEEDS_RECONNECT,
    OUTCOME_WOULD_RESTORE,
    OUTCOME_WOULD_RESTORE_NEEDS_RECONNECT,
)
from device_mcp_gateway.backup.rotating import needs_reconnect, strip_rotating

#: One working directory per app built in this module — see `_client`.
_STACK_SEQ = itertools.count()

ADMIN_KEY = "a" * 40
REFRESH_TOKEN = "REFRESH-TOKEN-SENTINEL-4c81ba97"
CLIENT_SECRET = "CLIENT-SECRET-SENTINEL-70de2f13"
DEVICE_PASSWORD = "DEVICE-PASSWORD-SENTINEL-1a9fe4"
TOKEN_ENDPOINT = "http://127.0.0.1:9/token"


def _client(monkeypatch, tmp_path, *, secret_key: str):
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
    monkeypatch.setenv("MCP_ALLOW_PRIVATE_TARGETS", "true")
    from device_mcp_gateway.main import create_app

    return TestClient(create_app())


def _auth():
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


def _register_oauth2(client, hostname, **auth_over):
    auth = {
        "token_endpoint": TOKEN_ENDPOINT,
        "client_id": "gateway",
        "client_secret": CLIENT_SECRET,
    }
    auth.update(auth_over)
    resp = client.post(
        "/v1/devices",
        headers=_auth(),
        json={
            "hostname": hostname,
            "base_url": f"http://127.0.0.1:9/{hostname}",
            "auth_type": "oauth2",
            "auth": auth,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp


def _backup(client):
    resp = client.get("/v1/admin/backup", headers=_auth())
    assert resp.status_code == 200, resp.text
    return resp.json()


def _restore(client, archive, *, dry_run=True, **over):
    """Preview, or preview-then-apply with the plan_token the preview minted (ADR-0018 §6)."""
    body = {"archive": archive}
    body.update(over)
    if dry_run:
        resp = client.post("/v1/admin/restore/preview", headers=_auth(), json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()

    preview = client.post("/v1/admin/restore/preview", headers=_auth(), json=body)
    assert preview.status_code == 200, preview.text
    token = preview.json()["plan_token"]
    resp = client.post("/v1/admin/restore/apply", headers=_auth(), json={**body, "plan_token": token})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _opened(archive, key):
    """Every device's credential payload, decrypted. What the archive actually carries.

    Greping the serialised archive alone would be **decorative** here and it is worth saying
    why, because the first version of these tests made exactly that mistake: the archive's
    ``auth_config`` is ciphertext, so a token that was never excluded does not appear in the
    JSON either. That test passed with the exclusion disabled. The grep still earns its
    place — it catches a leak into an unencrypted field, which is a real way to reintroduce
    this — but the load-bearing assertion has to run against the opened payloads.
    """
    from device_mcp_gateway.shared.crypto import CredentialCodec

    codec = CredentialCodec.from_secret(key)
    return {d["hostname"]: json.loads(codec.decrypt(d["auth_config"])) for d in archive["devices"] if d["auth_config"]}


def _summary(client, hostname):
    """The device as the FLEET LIST renders it — deliberately not the detail view."""
    devices = client.get("/v1/devices", headers=_auth()).json()["devices"]
    return next(d for d in devices if d["hostname"] == hostname)


# ── The boundary, in isolation ───────────────────────────────────────────────────────────


def test_a_refresh_grant_loses_its_token_and_is_flagged():
    payload = {"type": "oauth2", "grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN}
    assert needs_reconnect("oauth2", dict(payload)) is True
    assert strip_rotating("oauth2", payload) == ["refresh_token"]
    assert "refresh_token" not in payload


def test_a_client_credentials_device_loses_its_token_and_is_NOT_flagged():
    """The discrimination that keeps this from being a false alarm.

    Providers hand rotated refresh tokens to ``client_credentials`` devices too, and for
    them it is disposable: ``client_secret`` survived the archive, so the restored gateway
    re-runs the token exchange on first use and no human is involved. Flagging it would
    attach *needs reconnecting* to a device that restores seamlessly — and a control that
    fires on healthy devices is one operators learn to dismiss (ADR-0015 §2).
    """
    payload = {
        "type": "oauth2",
        "grant_type": "client_credentials",
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }
    assert needs_reconnect("oauth2", dict(payload)) is False
    assert strip_rotating("oauth2", payload) == ["refresh_token"], "still excluded — it is still gateway-minted"
    assert payload["client_secret"] == CLIENT_SECRET, "the operator-provisioned half must survive"


def test_a_password_grant_keeps_what_authenticates_it():
    payload = {
        "type": "oauth2",
        "grant_type": "password",
        "username": "svc",
        "password": DEVICE_PASSWORD,
        "refresh_token": REFRESH_TOKEN,
    }
    assert needs_reconnect("oauth2", dict(payload)) is False
    strip_rotating("oauth2", payload)
    assert payload["password"] == DEVICE_PASSWORD
    assert payload["username"] == "svc"


def test_an_absent_grant_type_reads_as_client_credentials():
    """The handler's own default. Reading a missing key as "unknown, so flag it" would
    attach the condition to every OAuth2 device written before ``grant_type`` was stored."""
    assert needs_reconnect("oauth2", {"type": "oauth2", "client_secret": CLIENT_SECRET}) is False


def test_an_api_key_device_is_untouched():
    """§1a's first row: operator-provisioned, nothing here to exclude."""
    payload = {"type": "api_key", "api_key": "k" * 20}
    assert strip_rotating("api_key", payload) == []
    assert needs_reconnect("api_key", payload) is False
    assert payload["api_key"] == "k" * 20


def test_a_by_reference_device_is_untouched():
    payload = {"type": "api_key", "credential_ref": "secret://t-abc/devices/prism#api-key"}
    assert strip_rotating("api_key", payload) == []
    assert payload["credential_ref"].startswith("secret://")


# ── The archive, end to end through the real registration path ───────────────────────────


def test_the_token_appears_nowhere_in_the_archive_opened_or_closed(monkeypatch, tmp_path):
    """The headline property, tested the way it can actually fail.

    Two assertions because they fail to different bugs. The **opened** one is what proves the
    exclusion — asserting ``payload["refresh_token"] is None`` instead would pass on an
    implementation that left the value in ``extra_params`` or a nested block, so this
    serialises the whole decrypted payload and greps it. The **closed** one catches the other
    direction: a leak into an unencrypted archive field, which is exactly what
    ``credential_excluded`` and ``needs_reconnect`` would become if either ever carried a
    value instead of a name.
    """
    key = Fernet.generate_key().decode()
    client = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(client, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    _register_oauth2(client, "cc", refresh_token=REFRESH_TOKEN)

    archive = _backup(client)
    assert REFRESH_TOKEN not in json.dumps(_opened(archive, key)), "the token survived inside the ciphertext"
    assert REFRESH_TOKEN not in json.dumps(archive), "the token leaked into an unencrypted field"


def test_the_operator_provisioned_secret_still_travels(monkeypatch, tmp_path):
    """The bound on the cost. If the client secret went too this would be a data-loss bug
    wearing a security argument — a ``client_credentials`` device must restore seamlessly."""
    key = Fernet.generate_key().decode()
    client = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(client, "cc", refresh_token=REFRESH_TOKEN)

    archive = _backup(client)
    assert REFRESH_TOKEN not in json.dumps(_opened(archive, key)), "gateway-minted: excluded even here"

    payload = _opened(archive, key)["cc"]
    assert payload["client_secret"] == CLIENT_SECRET
    assert "refresh_token" not in payload


def test_the_archive_records_what_it_dropped(monkeypatch, tmp_path):
    """An absence is unfalsifiable: a record with no ``refresh_token`` may be one this rule
    stripped, or a device that never had one. The marker is what makes the exclusion a
    record rather than a claim — and it is written even when empty, so its presence
    distinguishes a §3-aware archive from one made before §3 existed."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_oauth2(client, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    _register_oauth2(client, "plain")

    by_host = {d["hostname"]: d for d in _backup(client)["devices"]}
    assert by_host["rotator"]["credential_excluded"] == ["refresh_token"]
    assert by_host["rotator"]["needs_reconnect"] is True
    assert by_host["plain"]["credential_excluded"] == []
    assert "needs_reconnect" not in by_host["plain"]


def test_the_export_counts_the_devices_that_will_need_a_human(monkeypatch, tmp_path):
    """At the top of the archive, so the cost of a restore is knowable before running one."""
    client = _client(monkeypatch, tmp_path, secret_key=Fernet.generate_key().decode())
    _register_oauth2(client, "rotator-a", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    _register_oauth2(client, "rotator-b", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    _register_oauth2(client, "cc")

    counts = _backup(client)["counts"]
    assert counts["devices"] == 3
    assert counts["needs_reconnect"] == 2


@pytest.mark.asyncio
async def test_an_unreadable_credential_is_dropped_rather_than_copied_through():
    """Exclusion can only be *proved* on a payload the exporter could read.

    Re-sealing an opaque blob would put a value in the archive that nothing has shown is
    free of a live token, while the archive claims otherwise — and a claim that holds except
    where it silently does not is worth less than no claim. The device loses nothing it
    still had: a payload this stack cannot decode is one the worker cannot decode either.
    """
    from device_mcp_gateway.backup.export import build_archive
    from device_mcp_gateway.shared.crypto import CredentialCodec
    from device_mcp_gateway.shared.registry_backend import DeviceConfig

    class _Backend:
        async def get_last_tool_change(self, hostname):
            return None

        async def dead_letter_export(self, hostname, count=1000):
            return []

    class _Registry:
        _backend = _Backend()

        async def list_devices(self):
            return [
                DeviceConfig(
                    hostname="broken",
                    base_url="http://127.0.0.1:9",
                    auth_type="oauth2",
                    auth_config="not-json-and-not-decryptable-" + REFRESH_TOKEN,
                )
            ]

    result = await build_archive(
        registry=_Registry(),
        codec=CredentialCodec.from_secret(Fernet.generate_key().decode()),
        config={},
        gateway_version="test",
        mode="embedded",
    )
    assert result.archive["devices"][0]["auth_config"] is None
    assert REFRESH_TOKEN not in json.dumps(result.archive)


# ── The restore says so, and the device keeps saying so ──────────────────────────────────


def test_a_restored_rotator_reports_its_own_outcome(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    source = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(source, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    _register_oauth2(source, "cc")
    archive = _backup(source)

    target = _client(monkeypatch, tmp_path, secret_key=key)
    report = _restore(target, archive, dry_run=False)

    outcomes = {d["hostname"]: d["outcome"] for d in report["devices"]}
    assert outcomes == {"rotator": OUTCOME_RESTORED_NEEDS_RECONNECT, "cc": OUTCOME_RESTORED}
    assert report["counts"] == {OUTCOME_RESTORED_NEEDS_RECONNECT: 1, OUTCOME_RESTORED: 1}
    assert report["needs_reconnect"] == 1

    reason = next(d["reason"] for d in report["devices"] if d["hostname"] == "rotator")
    assert "re-authorize" in reason, "the reason must name the action, not just the symptom"


def test_the_dry_run_predicts_it_too(monkeypatch, tmp_path):
    """A prediction that omitted the one outcome requiring a human would be predicting the
    easy half — and the dry run exists so the cost is known while it can still be stopped."""
    key = Fernet.generate_key().decode()
    source = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(source, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    _register_oauth2(source, "cc")
    archive = _backup(source)

    target = _client(monkeypatch, tmp_path, secret_key=key)
    dry = _restore(target, archive)
    assert dry["counts"] == {OUTCOME_WOULD_RESTORE_NEEDS_RECONNECT: 1, OUTCOME_WOULD_RESTORE: 1}
    assert dry["needs_reconnect"] == 1


def test_the_restored_device_still_says_so_on_the_FLEET_LIST(monkeypatch, tmp_path):
    """The precedent's defect, not repeated.

    ``fingerprint_state`` is absent from ``DeviceSummary``, so a device awaiting approval is
    invisible in the fleet list and discoverable only by opening it. A device needing
    reconnection after a restore is precisely the case an operator scans a list for, so it
    would arrive with the same gap on day one. This reads the LIST endpoint deliberately.
    """
    key = Fernet.generate_key().decode()
    source = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(source, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    _register_oauth2(source, "cc")
    archive = _backup(source)

    target = _client(monkeypatch, tmp_path, secret_key=key)
    _restore(target, archive, dry_run=False)

    assert _summary(target, "rotator")["credential_state"] == CREDENTIAL_STATE_NEEDS_RECONNECT
    assert _summary(target, "cc")["credential_state"] == "ok"


def test_the_condition_is_not_a_health_reading(monkeypatch, tmp_path):
    """It is an AUTHORIZATION condition requiring a human, carried as its own orthogonal
    field. A device that needs reconnecting may be perfectly reachable, and collapsing the
    two would make a health signal answer a question health cannot answer."""
    key = Fernet.generate_key().decode()
    source = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(source, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    archive = _backup(source)

    target = _client(monkeypatch, tmp_path, secret_key=key)
    _restore(target, archive, dry_run=False)

    summary = _summary(target, "rotator")
    assert summary["credential_state"] == CREDENTIAL_STATE_NEEDS_RECONNECT
    assert "credential_state" in summary and "reachable" in summary
    detail = target.get("/v1/devices/rotator", headers=_auth()).json()
    assert detail["credential_state"] == CREDENTIAL_STATE_NEEDS_RECONNECT, "detail view too"


def test_a_dry_run_leaves_no_state_behind(monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    source = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(source, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    archive = _backup(source)

    target = _client(monkeypatch, tmp_path, secret_key=key)
    _restore(target, archive)
    assert target.get("/v1/devices/rotator", headers=_auth()).status_code == 404


# ── Clearing it is the reconnection, and only the reconnection ───────────────────────────


def test_a_put_that_supplies_a_new_credential_clears_it(monkeypatch, tmp_path):
    """The reconnection itself. No separate "mark reconnected" endpoint is needed or wanted:
    the condition is "this device has no credential a human has supplied", so supplying one
    is what ends it."""
    key = Fernet.generate_key().decode()
    source = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(source, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    archive = _backup(source)

    target = _client(monkeypatch, tmp_path, secret_key=key)
    _restore(target, archive, dry_run=False)
    assert _summary(target, "rotator")["credential_state"] == CREDENTIAL_STATE_NEEDS_RECONNECT

    resp = target.put(
        "/v1/devices/rotator",
        headers=_auth(),
        json={
            "base_url": "http://127.0.0.1:9/rotator",
            "auth_type": "oauth2",
            "auth": {
                "token_endpoint": TOKEN_ENDPOINT,
                "client_id": "gateway",
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": "A-NEW-TOKEN-THE-HUMAN-JUST-CONSENTED-TO",
            },
        },
    )
    assert resp.status_code in (200, 201), resp.text
    assert _summary(target, "rotator")["credential_state"] == "ok"


def test_a_put_that_touches_no_credential_does_NOT_clear_it(monkeypatch, tmp_path):
    """The trap, and the reason ``_carry_credential_state`` exists.

    ``replace_device`` rebuilds a device from registration inputs alone, so every field not
    carried through resets to its default — and the default is ``"ok"``. A PUT that changed a
    rate limit would therefore have cleared *needs reconnecting* from a device nobody had
    reconnected: the operator's list goes quiet and the device still cannot authenticate.
    """
    key = Fernet.generate_key().decode()
    source = _client(monkeypatch, tmp_path, secret_key=key)
    _register_oauth2(source, "rotator", grant_type="refresh_token", refresh_token=REFRESH_TOKEN)
    archive = _backup(source)

    target = _client(monkeypatch, tmp_path, secret_key=key)
    _restore(target, archive, dry_run=False)

    resp = target.put(
        "/v1/devices/rotator",
        headers=_auth(),
        json={"base_url": "http://127.0.0.1:9/rotator", "rate_limit_rps": 4.0},
    )
    assert resp.status_code in (200, 201), resp.text
    assert _summary(target, "rotator")["credential_state"] == CREDENTIAL_STATE_NEEDS_RECONNECT


# ── The field survives the two places a device record actually lives ─────────────────────


def test_a_redis_hash_written_before_this_field_existed_reads_as_ok():
    """``to_redis_hash`` writes ``""`` for an unset value and a pre-upgrade hash has no key
    at all. Both must land on the default — an empty ``credential_state`` matches neither
    branch of the comparison, so the device would read as neither healthy nor needing a
    human, which is the silent-failure shape this field exists to remove."""
    from device_mcp_gateway.shared.registry_backend import DeviceConfig

    h = DeviceConfig(hostname="dev1", base_url="http://dev1").to_redis_hash()
    assert DeviceConfig.from_redis_hash({**h, "credential_state": ""}).credential_state == "ok"
    del h["credential_state"]
    assert DeviceConfig.from_redis_hash(h).credential_state == "ok"


def test_a_needs_reconnect_device_survives_a_redis_round_trip():
    from device_mcp_gateway.shared.registry_backend import DeviceConfig

    cfg = DeviceConfig(hostname="dev1", base_url="http://dev1", credential_state=CREDENTIAL_STATE_NEEDS_RECONNECT)
    assert DeviceConfig.from_redis_hash(cfg.to_redis_hash()).credential_state == CREDENTIAL_STATE_NEEDS_RECONNECT


@pytest.mark.asyncio
async def test_an_embedded_database_from_before_this_change_still_accepts_writes(tmp_path):
    """The migration trap, same as ``upstream_kind``'s. ``CREATE TABLE IF NOT EXISTS`` is a
    no-op against an existing table, so adding a column to the DDL does nothing for a
    database already on disk — and the next INSERT naming it fails with "no such column".
    An embedded deployment would upgrade cleanly and then be unable to register anything.
    """
    import sqlite3

    from device_mcp_gateway.storage.sqlite_store import SqliteDeviceStore

    db = str(tmp_path / "pre_upgrade.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE devices (hostname TEXT PRIMARY KEY, base_url TEXT NOT NULL, "
            "spec_url TEXT, transport TEXT NOT NULL DEFAULT 'sse', auth_type TEXT, "
            "auth_config TEXT, rate_limit_rps REAL)"
        )
        conn.execute("INSERT INTO devices (hostname, base_url) VALUES ('legacy', 'http://legacy')")

    store = SqliteDeviceStore(db_path=db)
    await store.initialize()

    rows = {r["hostname"]: r for r in await store.load_all()}
    assert rows["legacy"]["credential_state"] == "ok", "a device nobody restored is not one needing a human"

    await store.save(
        "restored",
        {
            "base_url": "http://restored",
            "credential_state": CREDENTIAL_STATE_NEEDS_RECONNECT,
        },
    )
    rows = {r["hostname"]: r for r in await store.load_all()}
    assert rows["restored"]["credential_state"] == CREDENTIAL_STATE_NEEDS_RECONNECT


# ── The same rule in distributed mode, which is a different code path ────────────────────


@pytest.mark.asyncio
async def test_distributed_mode_carries_the_state_across_a_keep_auth_put():
    """The cold path. Every test above runs the embedded registry through the HTTP API, and
    ``replace_device`` branches on mode long before it reaches the credential — the
    distributed half writes through ``_write_distributed`` and never touches a profile
    object. A rule verified only on the branch that happens to be the test default is a rule
    holding on one of the two shapes this gateway ships.
    """
    from cryptography.fernet import Fernet

    from device_mcp_gateway.auth.api_key import ApiKeyAuth
    from device_mcp_gateway.registry.server import Registry
    from device_mcp_gateway.shared.crypto import CredentialCodec
    from device_mcp_gateway.shared.registry_backend import MemoryRegistryBackend

    backend = MemoryRegistryBackend()
    reg = Registry(
        config={"mode": "distributed"},
        backend=backend,
        codec=CredentialCodec.from_secret(Fernet.generate_key().decode()),
    )
    await reg.register_device("dev1", "http://dev1", auth=ApiKeyAuth(api_key="s3cret-key"))
    await backend.update_device_fields("dev1", credential_state=CREDENTIAL_STATE_NEEDS_RECONNECT)

    # A PUT that changes only base_url — the credential was kept, so the condition on it is
    # still true and must still be visible.
    await reg.replace_device("dev1", base_url="http://dev1-new", keep_auth=True)
    after = await backend.get_device("dev1")
    assert after.base_url == "http://dev1-new"
    assert after.credential_state == CREDENTIAL_STATE_NEEDS_RECONNECT

    # A PUT that supplies a new credential IS the reconnection.
    await reg.replace_device("dev1", base_url="http://dev1-new", auth=ApiKeyAuth(api_key="fresh-key"))
    assert (await backend.get_device("dev1")).credential_state == "ok"
