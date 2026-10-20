# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Abstract interface for device registry persistence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from loguru import logger


class AbstractDeviceStore(ABC):
    """Persist and retrieve registered device records."""

    @abstractmethod
    async def initialize(self) -> None:
        """Set up storage backend (create tables, open files, etc.)."""
        ...

    @abstractmethod
    async def save(self, hostname: str, record: dict[str, Any]) -> None:
        """Upsert a device record keyed by hostname."""
        ...

    @abstractmethod
    async def delete(self, hostname: str) -> None:
        """Remove a device record."""
        ...

    @abstractmethod
    async def load_all(self) -> list[dict[str, Any]]:
        """Return all persisted device records."""
        ...

    async def update_fingerprint(self, hostname: str, fields: dict[str, Any]) -> None:
        """Persist just a device's endpoint-fingerprint fields (ADR-0015).

        A pin, an approval or a declared identity changes at runtime, long after
        registration's ``save()``, and ``save()`` replaces the whole row, so it cannot be
        reused for this without the full record. Concrete and loud for the same reason as
        ``update_credentials``: a store that drops it keeps working until a restart, then
        silently re-pins every device on first contact, and a re-pin is exactly the event a
        pin exists to catch.
        """
        logger.warning(
            f"{type(self).__name__} does not implement update_fingerprint; the fingerprint "
            f"record for {hostname} was not persisted and will be lost on restart"
        )

    async def update_credentials(self, hostname: str, auth_config: dict[str, Any]) -> None:
        """Re-persist only a device's credential blob, leaving the rest of the record alone.

        Called when an auth handler rotates its own material at runtime — an OAuth2
        provider handing back a new refresh token — where a full ``save()`` would need
        fields the caller no longer has. Concrete (not abstract) so existing stores and
        test doubles keep satisfying the interface; the default is a loud no-op rather
        than a silent one, because a store that drops the rotation will authenticate
        fine until it restarts and then fail permanently.
        """
        logger.warning(
            f"{type(self).__name__} does not implement update_credentials; rotated credentials "
            f"for {hostname} were not persisted and will be stale after a restart"
        )
