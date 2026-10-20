# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""SQLite-backed device store using aiosqlite."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import aiosqlite
from loguru import logger

from .base import AbstractDeviceStore
from device_mcp_gateway.security.fingerprint import TRUST_FIELDS
from device_mcp_gateway.shared.crypto import CredentialCodec

# What a device's endpoint fingerprint is made of, as persisted (ADR-0015): the trust record
# plus the self-reported declared identity, which is observation rather than trust but is
# compared across restarts all the same.
PERSISTED_FINGERPRINT_FIELDS = (*TRUST_FIELDS, "declared_name", "declared_version")

# Every field that changes after registration's ``save()`` and must survive a restart: the
# fingerprint record above, the credential condition (ADR-0018 §3) and a curated spec
# snapshot (ADR-0020 §4b). ``update_fields`` writes only these, and ``load_persisted_devices``
# restores all of them — a column the store keeps but the registry neither writes nor reads
# back is a setting that silently resets on every restart.
PERSISTED_RUNTIME_FIELDS = (*PERSISTED_FINGERPRINT_FIELDS, "credential_state", "curated_spec")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS devices (
    hostname           TEXT PRIMARY KEY,
    base_url           TEXT NOT NULL,
    spec_url           TEXT,
    transport          TEXT NOT NULL DEFAULT 'sse',
    auth_type          TEXT,
    auth_config        TEXT,
    rate_limit_rps     REAL,
    upstream_kind      TEXT NOT NULL DEFAULT 'openapi',
    upstream_transport TEXT NOT NULL DEFAULT 'http',
    -- Endpoint fingerprint (ADR-0015). The pin is the SPKI digest, not the cert digest,
    -- so a routine renewal against the same key does not fire. See registry_backend.py.
    tls_spki_sha256    TEXT,
    tls_cert_sha256    TEXT,
    tls_issuer         TEXT,
    tls_not_after      TEXT,
    declared_name      TEXT,
    declared_version   TEXT,
    fingerprint_state  TEXT NOT NULL DEFAULT 'unpinned',
    fingerprint_pinned_at REAL NOT NULL DEFAULT 0,
    pending_tls_spki_sha256 TEXT,
    fingerprint_policy TEXT,
    -- Credential condition (ADR-0018 §3): 'ok' | 'needs_reconnect'. Orthogonal to
    -- reachability — see registry_backend.py.
    credential_state   TEXT NOT NULL DEFAULT 'ok',
    -- ADR-0020 §4a: the provider's snapshotted spec, carried instead of a spec_url to
    -- fetch. Text, because §4b hashes exactly these bytes.
    curated_spec       TEXT
)
"""

# Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` is a no-op against a
# database that already exists, so extending the DDL above does nothing for one already on
# disk — and the next INSERT naming a new column fails with "no such column". Every addition
# needs a matching ALTER here or an upgraded deployment comes back up unable to register.
# ADD COLUMN with a DEFAULT backfills existing rows, which is what makes this safe to
# re-run: the second attempt raises "duplicate column name" and is swallowed.
_MIGRATIONS = (
    "ALTER TABLE devices ADD COLUMN rate_limit_rps REAL",
    "ALTER TABLE devices ADD COLUMN upstream_kind TEXT NOT NULL DEFAULT 'openapi'",
    "ALTER TABLE devices ADD COLUMN upstream_transport TEXT NOT NULL DEFAULT 'http'",
    # ADR-0015 endpoint fingerprint.
    "ALTER TABLE devices ADD COLUMN tls_spki_sha256 TEXT",
    "ALTER TABLE devices ADD COLUMN tls_cert_sha256 TEXT",
    "ALTER TABLE devices ADD COLUMN tls_issuer TEXT",
    "ALTER TABLE devices ADD COLUMN tls_not_after TEXT",
    "ALTER TABLE devices ADD COLUMN declared_name TEXT",
    "ALTER TABLE devices ADD COLUMN declared_version TEXT",
    "ALTER TABLE devices ADD COLUMN fingerprint_state TEXT NOT NULL DEFAULT 'unpinned'",
    "ALTER TABLE devices ADD COLUMN fingerprint_pinned_at REAL NOT NULL DEFAULT 0",
    "ALTER TABLE devices ADD COLUMN pending_tls_spki_sha256 TEXT",
    "ALTER TABLE devices ADD COLUMN fingerprint_policy TEXT",
    # ADR-0018 §3 credential condition.
    "ALTER TABLE devices ADD COLUMN credential_state TEXT NOT NULL DEFAULT 'ok'",
    # ADR-0020 §4a curated spec snapshot.
    "ALTER TABLE devices ADD COLUMN curated_spec TEXT",
)


class SqliteDeviceStore(AbstractDeviceStore):
    """Persists device registrations in a local SQLite database."""

    def __init__(
        self,
        db_path: str = "./data/devices.db",
        fernet: Optional[Any] = None,
        codec: Optional[CredentialCodec] = None,
    ) -> None:
        self._db_path = db_path
        # Prefer an injected codec; fall back to wrapping a bare Fernet for
        # back-compat with callers/tests that pass fernet= directly.
        self._codec = codec or CredentialCodec(fernet)
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        # Bootstrap schema synchronously so the table exists before the async
        # lifespan runs (required for bare TestClient usage and cold starts).
        import sqlite3

        with sqlite3.connect(db_path) as conn:
            conn.execute(_CREATE_TABLE)
            for stmt in _MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception:
                    pass  # column already exists

    def _encrypt(self, plaintext: str) -> str:
        return self._codec.encrypt(plaintext)

    def _decrypt(self, stored: str) -> str:
        return self._codec.decrypt(stored)

    async def initialize(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_CREATE_TABLE)
            for stmt in _MIGRATIONS:
                try:
                    await db.execute(stmt)
                except Exception:
                    pass  # column already exists
            await db.commit()
        logger.info(f"SQLite device store initialised at {self._db_path}")

    async def save(self, hostname: str, record: dict[str, Any]) -> None:
        auth_config = record.get("auth_config")
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO devices
                    (hostname, base_url, spec_url, transport, auth_type, auth_config,
                     rate_limit_rps, upstream_kind, upstream_transport,
                     tls_spki_sha256, tls_cert_sha256, tls_issuer, tls_not_after,
                     declared_name, declared_version, fingerprint_state,
                     fingerprint_pinned_at, pending_tls_spki_sha256, fingerprint_policy,
                     credential_state, curated_spec)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hostname,
                    record["base_url"],
                    record.get("spec_url"),
                    record.get("transport", "sse"),
                    record.get("auth_type"),
                    self._encrypt(json.dumps(auth_config)) if auth_config else None,
                    record.get("rate_limit_rps"),
                    record.get("upstream_kind") or "openapi",
                    record.get("upstream_transport") or "http",
                    record.get("tls_spki_sha256"),
                    record.get("tls_cert_sha256"),
                    record.get("tls_issuer"),
                    record.get("tls_not_after"),
                    record.get("declared_name"),
                    record.get("declared_version"),
                    record.get("fingerprint_state") or "unpinned",
                    record.get("fingerprint_pinned_at") or 0.0,
                    record.get("pending_tls_spki_sha256"),
                    record.get("fingerprint_policy"),
                    record.get("credential_state") or "ok",
                    record.get("curated_spec"),
                ),
            )
            await db.commit()

    async def delete(self, hostname: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM devices WHERE hostname = ?", (hostname,))
            await db.commit()

    async def health_check(self) -> None:
        """Verify the SQLite database is accessible. Raises on failure."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("SELECT 1")

    async def iter_raw_credentials(self) -> list[tuple[str, str]]:
        """Return (hostname, raw encrypted auth_config) for every credentialled device.

        Reads the stored ciphertext WITHOUT decrypting it, for the key-rotation
        pass (F-34): rotation re-encrypts the token under the new primary key, so
        a missing/old key surfaces as an error on that one record instead of the
        decrypt-and-drop behaviour of load_all().
        """
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT hostname, auth_config FROM devices WHERE auth_config IS NOT NULL AND auth_config != ''"
            ) as cursor:
                rows = await cursor.fetchall()
        return [(row["hostname"], row["auth_config"]) for row in rows]

    async def update_fields(self, hostname: str, fields: dict[str, Any]) -> None:
        # Column names come from this allowlist only, never from the caller, so building the
        # SET clause from them cannot inject SQL.
        cols = [c for c in PERSISTED_RUNTIME_FIELDS if c in fields]
        if not cols:
            return
        assignments = ", ".join(f"{c} = ?" for c in cols)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                f"UPDATE devices SET {assignments} WHERE hostname = ?",  # nosec B608
                (*[fields[c] for c in cols], hostname),
            )
            await db.commit()

    async def update_credentials(self, hostname: str, auth_config: dict[str, Any]) -> None:
        """Re-encrypt and persist just a device's credential blob, leaving the rest of the
        record untouched. Used when an auth handler rotates its own material at runtime
        (OAuth2 refresh-token rotation) — a full ``save()`` would need the whole record."""
        await self.set_raw_credential(hostname, self._encrypt(json.dumps(auth_config)))

    async def set_raw_credential(self, hostname: str, raw_auth_config: str) -> None:
        """Overwrite a device's stored ciphertext in place (key-rotation pass, F-34)."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE devices SET auth_config = ? WHERE hostname = ?",
                (raw_auth_config, hostname),
            )
            await db.commit()

    async def load_all(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT hostname, base_url, spec_url, transport, auth_type, auth_config, "
                "rate_limit_rps, upstream_kind, upstream_transport, "
                "tls_spki_sha256, tls_cert_sha256, tls_issuer, tls_not_after, "
                "declared_name, declared_version, fingerprint_state, "
                "fingerprint_pinned_at, pending_tls_spki_sha256, fingerprint_policy, "
                "credential_state, curated_spec "
                "FROM devices"
            ) as cursor:
                rows = await cursor.fetchall()
        result = []
        for row in rows:
            auth_config = None
            if row["auth_config"]:
                try:
                    auth_config = json.loads(self._decrypt(row["auth_config"]))
                except Exception:
                    logger.error(
                        f"Failed to decrypt auth_config for {row['hostname']} — "
                        "key may have rotated; device will load without credentials"
                    )
            result.append(
                {
                    "hostname": row["hostname"],
                    "base_url": row["base_url"],
                    "spec_url": row["spec_url"],
                    "transport": row["transport"],
                    "auth_type": row["auth_type"],
                    "auth_config": auth_config,
                    "rate_limit_rps": row["rate_limit_rps"],
                    # A row backfilled by the ALTER carries the DEFAULT, but a row written
                    # by an older binary through a migrated table can still hold NULL.
                    "upstream_kind": row["upstream_kind"] or "openapi",
                    "upstream_transport": row["upstream_transport"] or "http",
                    # Same NULL caveat as above for every ADR-0015 column.
                    "tls_spki_sha256": row["tls_spki_sha256"],
                    "tls_cert_sha256": row["tls_cert_sha256"],
                    "tls_issuer": row["tls_issuer"],
                    "tls_not_after": row["tls_not_after"],
                    "declared_name": row["declared_name"],
                    "declared_version": row["declared_version"],
                    "fingerprint_state": row["fingerprint_state"] or "unpinned",
                    "fingerprint_pinned_at": row["fingerprint_pinned_at"] or 0.0,
                    "pending_tls_spki_sha256": row["pending_tls_spki_sha256"],
                    "fingerprint_policy": row["fingerprint_policy"],
                    "credential_state": row["credential_state"] or "ok",
                    "curated_spec": row["curated_spec"],
                }
            )
        return result
