# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Unit tests for SqliteDeviceStore."""

import pytest
import pytest_asyncio

from device_mcp_gateway.storage.sqlite_store import SqliteDeviceStore


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_devices.db")


@pytest_asyncio.fixture
async def store(db_path):
    s = SqliteDeviceStore(db_path=db_path)
    await s.initialize()
    return s


@pytest.mark.asyncio
async def test_save_and_load(store):
    await store.save(
        "my-device",
        {
            "base_url": "http://device.local",
            "spec_url": None,
            "auth_type": "api_key",
            "auth_config": {"type": "api_key", "api_key": "secret", "header_name": "X-API-Key"},
        },
    )
    records = await store.load_all()
    assert len(records) == 1
    r = records[0]
    assert r["hostname"] == "my-device"
    assert r["base_url"] == "http://device.local"
    assert r["auth_config"]["api_key"] == "secret"


@pytest.mark.asyncio
async def test_upsert_replaces_existing(store):
    await store.save("dev", {"base_url": "http://a.local", "auth_type": None, "auth_config": None})
    await store.save("dev", {"base_url": "http://b.local", "auth_type": None, "auth_config": None})
    records = await store.load_all()
    assert len(records) == 1
    assert records[0]["base_url"] == "http://b.local"
    assert "transport" not in records[0]  # vestigial column since 0.4.0: never read back


@pytest.mark.asyncio
async def test_delete_removes_record(store):
    await store.save("dev", {"base_url": "http://x.local", "auth_type": None, "auth_config": None})
    await store.delete("dev")
    records = await store.load_all()
    assert records == []


@pytest.mark.asyncio
async def test_load_empty_store(store):
    records = await store.load_all()
    assert records == []


@pytest.mark.asyncio
async def test_multiple_devices(store):
    for i in range(3):
        await store.save(
            f"dev-{i}",
            {"base_url": f"http://dev-{i}.local", "auth_type": None, "auth_config": None},
        )
    records = await store.load_all()
    assert len(records) == 3
    hostnames = {r["hostname"] for r in records}
    assert hostnames == {"dev-0", "dev-1", "dev-2"}


@pytest.mark.asyncio
async def test_encrypted_auth_config_roundtrip(db_path):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    fernet = Fernet(key)
    store = SqliteDeviceStore(db_path=db_path, fernet=fernet)
    await store.initialize()

    await store.save(
        "enc-device",
        {
            "base_url": "http://enc.local",
            "auth_type": "api_key",
            "auth_config": {"type": "api_key", "api_key": "s3cr3t", "header_name": "X-API-Key"},
        },
    )
    records = await store.load_all()
    assert records[0]["auth_config"]["api_key"] == "s3cr3t"


@pytest.mark.asyncio
async def test_key_rotation_loads_device_without_credentials(db_path):
    """Encrypted record + different key at load time: device loads with auth_config=None, not corrupt data."""
    from cryptography.fernet import Fernet

    key_old = Fernet.generate_key()
    store_old = SqliteDeviceStore(db_path=db_path, fernet=Fernet(key_old))
    await store_old.initialize()
    await store_old.save(
        "rotated-device",
        {
            "base_url": "http://rotated.local",
            "auth_type": "api_key",
            "auth_config": {"type": "api_key", "api_key": "secret"},
        },
    )

    key_new = Fernet.generate_key()
    store_new = SqliteDeviceStore(db_path=db_path, fernet=Fernet(key_new))
    records = await store_new.load_all()
    assert len(records) == 1
    assert records[0]["hostname"] == "rotated-device"
    assert records[0]["auth_config"] is None  # credential lost, not corrupt


@pytest.mark.asyncio
async def test_decrypt_raises_on_invalid_token(db_path):
    """_decrypt raises rather than silently returning ciphertext as plaintext."""
    from cryptography.fernet import Fernet

    store = SqliteDeviceStore(db_path=db_path, fernet=Fernet(Fernet.generate_key()))
    import pytest

    with pytest.raises(Exception):
        store._decrypt("this-is-not-valid-fernet-ciphertext")
