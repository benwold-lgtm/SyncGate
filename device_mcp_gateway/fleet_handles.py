# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Fleet handles — a fleet as a server-minted resource, addressed in the URL (ADR-0027 §5).

ADR-0008 fixed a fleet's device set on ``initialize`` and never read it again, which is what
stopped a later request from widening it. The stateless era has no ``initialize``, so there is
no moment to fix the set on; a ``devices`` list accepted on every request would let any request
name any fleet. So the set is fixed when a handle is **minted** instead, bound to the principal
that minted it, and the client names the handle in the URL it is configured with:
``POST /v1/fleet/{handle}/mcp``. That URL serves both eras.

A handle is opaque and unguessable (``flt_`` + 128 random bits) but not a credential: every
request still authenticates, and a principal other than the owner is told the handle does not
exist (404, not 403, so the endpoint is not an oracle for which handles exist).

Handles are configuration, so they persist — Redis in distributed mode, SQLite in embedded —
and a client's configured URL survives a restart.
"""

from __future__ import annotations

import json
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

HANDLE_PREFIX = "flt_"


def mint_handle() -> str:
    """A new handle: unguessable, and recognisably a fleet handle in a log line."""
    return HANDLE_PREFIX + secrets.token_hex(16)


def looks_like_handle(value: str) -> bool:
    """Shape check only — cheap enough to run before touching storage on a bad URL."""
    body = value[len(HANDLE_PREFIX) :]
    return value.startswith(HANDLE_PREFIX) and len(body) == 32 and all(c in "0123456789abcdef" for c in body)


@dataclass
class FleetHandle:
    handle: str
    owner: str
    devices: list[str]
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        """What the API returns. The owner is the caller, so it is not repeated back."""
        return {
            "handle": self.handle,
            "devices": list(self.devices),
            "created_at": self.created_at,
            "url": f"/v1/fleet/{self.handle}/mcp",
        }


class FleetHandleStore(ABC):
    """Where handles live. Every method is owner-agnostic; ownership is the caller's check."""

    @abstractmethod
    async def create(self, fleet: FleetHandle) -> None: ...

    @abstractmethod
    async def get(self, handle: str) -> FleetHandle | None: ...

    @abstractmethod
    async def delete(self, handle: str) -> None: ...

    @abstractmethod
    async def list_for(self, owner: str) -> list[FleetHandle]: ...


class MemoryFleetHandles(FleetHandleStore):
    """In-process, for an embedded gateway with no store configured (and for tests)."""

    def __init__(self) -> None:
        self._by_handle: dict[str, FleetHandle] = {}

    async def create(self, fleet: FleetHandle) -> None:
        self._by_handle[fleet.handle] = fleet

    async def get(self, handle: str) -> FleetHandle | None:
        return self._by_handle.get(handle)

    async def delete(self, handle: str) -> None:
        self._by_handle.pop(handle, None)

    async def list_for(self, owner: str) -> list[FleetHandle]:
        return sorted((f for f in self._by_handle.values() if f.owner == owner), key=lambda f: f.created_at)


class RedisFleetHandles(FleetHandleStore):
    """Distributed mode: one hash per handle, plus a per-owner index so listing is not a scan."""

    def __init__(self, redis: Any) -> None:
        from device_mcp_gateway.shared.keys import KEYS

        self._r = redis
        self._keys = KEYS

    async def create(self, fleet: FleetHandle) -> None:
        pipe = self._r.pipeline()
        pipe.hset(
            self._keys.fleet_handle(fleet.handle),
            mapping={"owner": fleet.owner, "devices": json.dumps(fleet.devices), "created_at": str(fleet.created_at)},
        )
        pipe.sadd(self._keys.fleet_handles_of(fleet.owner), fleet.handle)
        await pipe.execute()

    async def get(self, handle: str) -> FleetHandle | None:
        raw = await self._r.hgetall(self._keys.fleet_handle(handle))
        if not raw:
            return None
        try:
            devices = json.loads(raw.get("devices") or "[]")
        except ValueError:
            return None
        return FleetHandle(
            handle=handle,
            owner=raw.get("owner") or "",
            devices=[d for d in devices if isinstance(d, str)],
            created_at=float(raw.get("created_at") or 0),
        )

    async def delete(self, handle: str) -> None:
        fleet = await self.get(handle)
        pipe = self._r.pipeline()
        pipe.delete(self._keys.fleet_handle(handle))
        if fleet is not None:
            pipe.srem(self._keys.fleet_handles_of(fleet.owner), handle)
        await pipe.execute()

    async def list_for(self, owner: str) -> list[FleetHandle]:
        handles = await self._r.smembers(self._keys.fleet_handles_of(owner))
        found = [f for f in [await self.get(h) for h in sorted(handles)] if f is not None and f.owner == owner]
        return sorted(found, key=lambda f: f.created_at)


class SqliteFleetHandles(FleetHandleStore):
    """Embedded mode: a table beside the devices table, in the same database file."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS fleet_handles (
        handle     TEXT PRIMARY KEY,
        owner      TEXT NOT NULL,
        devices    TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ready = False

    async def _db(self) -> Any:
        import aiosqlite

        db = await aiosqlite.connect(self._db_path)
        if not self._ready:
            await db.execute(self._SCHEMA)
            await db.execute("CREATE INDEX IF NOT EXISTS fleet_handles_owner ON fleet_handles (owner)")
            await db.commit()
            self._ready = True
        return db

    async def create(self, fleet: FleetHandle) -> None:
        db = await self._db()
        try:
            await db.execute(
                "INSERT INTO fleet_handles (handle, owner, devices, created_at) VALUES (?, ?, ?, ?)",
                (fleet.handle, fleet.owner, json.dumps(fleet.devices), fleet.created_at),
            )
            await db.commit()
        finally:
            await db.close()

    async def get(self, handle: str) -> FleetHandle | None:
        db = await self._db()
        try:
            async with db.execute(
                "SELECT owner, devices, created_at FROM fleet_handles WHERE handle = ?", (handle,)
            ) as cur:
                row = await cur.fetchone()
        finally:
            await db.close()
        if row is None:
            return None
        return FleetHandle(handle=handle, owner=row[0], devices=json.loads(row[1]), created_at=row[2])

    async def delete(self, handle: str) -> None:
        db = await self._db()
        try:
            await db.execute("DELETE FROM fleet_handles WHERE handle = ?", (handle,))
            await db.commit()
        finally:
            await db.close()

    async def list_for(self, owner: str) -> list[FleetHandle]:
        db = await self._db()
        try:
            async with db.execute(
                "SELECT handle, devices, created_at FROM fleet_handles WHERE owner = ? ORDER BY created_at",
                (owner,),
            ) as cur:
                rows = await cur.fetchall()
        finally:
            await db.close()
        return [FleetHandle(handle=r[0], owner=owner, devices=json.loads(r[1]), created_at=r[2]) for r in rows]


__all__ = [
    "FleetHandle",
    "FleetHandleStore",
    "MemoryFleetHandles",
    "RedisFleetHandles",
    "SqliteFleetHandles",
    "looks_like_handle",
    "mint_handle",
]
