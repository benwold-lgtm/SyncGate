# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Compare a just-made observation with a device's stored fingerprint, and persist any change.

The one implementation both modes run (ADR-0015). It used to live inside the distributed
worker's health loop, which is why embedded mode never pinned, compared or recorded a
fingerprint at all: the same device registered on an embedded gateway kept
``fingerprint_state: unpinned`` forever, and its declared identity was never stored.

The comparison itself is ``fingerprint.plan_update`` (pure). This module adds the two
effects around it: writing the fields through the caller's writer, and the operator-facing
log line, so the message a human acts on reads the same whichever mode noticed.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from device_mcp_gateway.security import fingerprint as fp

Writer = Callable[..., Awaitable[Any]]


async def record_observation(write: Writer, hostname: str, cfg: Any, seen: fp.Observation) -> str | None:
    """Apply ``seen`` to the device's stored fingerprint via ``write(**fields)``.

    Returns the verdict, or None when nothing was observed. Never raises: fingerprinting is
    a diagnostic layered onto the health loop, and a failure here must not stop reachability
    or spec polling for the device.
    """
    try:
        if seen.is_empty():
            return None
        stored = fp.Observation(
            tls_spki_sha256=cfg.tls_spki_sha256,
            tls_cert_sha256=cfg.tls_cert_sha256,
            tls_issuer=cfg.tls_issuer,
            tls_not_after=cfg.tls_not_after,
            declared_name=cfg.declared_name,
            declared_version=cfg.declared_version,
        )
        verdict, fields = fp.plan_update(
            stored,
            cfg.fingerprint_state,
            cfg.pending_tls_spki_sha256,
            seen,
            now=time.time(),
        )
        if not fields:
            return verdict
        await write(**fields)
        if verdict in fp.NEEDS_APPROVAL:
            # WARNING, not an error: under the default policy the device keeps working and a
            # human decides. The message names both keys because an operator approving this
            # needs to see what it is changing FROM, and the audit record carries the
            # accountability (ADR-0015 §6).
            logger.warning(
                f"Endpoint fingerprint CHANGED for {hostname} ({verdict}): "
                f"pinned key {(cfg.tls_spki_sha256 or '')[:16]}... now presenting "
                f"{(seen.tls_spki_sha256 or '')[:16]}... — device is flagged "
                "pending_approval; approve or remove it"
            )
        else:
            logger.debug(f"Fingerprint for {hostname}: {verdict}")
        return verdict
    except Exception:
        logger.exception(f"Fingerprint comparison failed for {hostname}")
        return None


def declared_from_spec(spec: Any) -> tuple[str | None, str | None] | None:
    """An OpenAPI document's ``info`` identity (title, version), or None.

    Self-reported and spoofable, exactly like MCP's ``serverInfo``: the DECLARED dimension
    only, never evidence of identity. Never raises on a malformed document.
    """
    info = spec.get("info") if isinstance(spec, dict) else None
    if not isinstance(info, dict):
        return None
    title, version = info.get("title"), info.get("version")
    if not (title or version):
        return None
    return (str(title) if title else None, str(version) if version else None)
