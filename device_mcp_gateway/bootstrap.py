# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""First-run bootstrap for a file-provisioned deployment.

A home user shouldn't have to invent an API key before the gateway will require one.
When ``MCP_API_KEY_FILE`` is set (a compose deployment points it at a shared volume), this
self-provisions a single **admin** key on first boot: it generates the key, persists it
to that path, uses it, and prints it once so the operator can point an MCP/LLM client at
the gateway. The BFF reads the same file (``GATEWAY_TOKEN_FILE``) so the two agree without
anyone copying a secret between containers.

Opt-in by design. With ``MCP_API_KEY_FILE`` unset — the default, and every enterprise
deploy — this is a no-op: nothing is generated or written, and key resolution in
``rbac.build_static_authenticator`` behaves exactly as before. An operator-supplied key
(via any of the env vars / config that builder already honors) always wins; the file is
only consulted when no key is configured anywhere.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from pathlib import Path

from loguru import logger


def apply_gateway_bootstrap(cfg: dict) -> None:
    """Fill in ``gateway.api_key`` from ``MCP_API_KEY_FILE`` (reading or generating it) when
    no key is otherwise configured. Mutates ``cfg`` in place. No-op unless the env var is set."""
    key_file = os.getenv("MCP_API_KEY_FILE", "").strip()
    if not key_file:
        return

    gateway = cfg.setdefault("gateway", {})
    # Respect any operator-provided credential — mirror the sources build_static_authenticator
    # resolves, so we never override or shadow an explicitly configured key.
    if (
        os.getenv("MCP_GATEWAY_API_KEY")
        or os.getenv("MCP_ADMIN_KEY")
        or os.getenv("MCP_VIEWER_KEY")
        or gateway.get("api_key")
        or gateway.get("rbac")
    ):
        return

    path = Path(key_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = path.read_text().strip() if path.exists() else ""
    except OSError as exc:
        logger.warning(f"MCP_API_KEY_FILE {key_file!r} unavailable ({exc}); leaving auth unconfigured")
        return

    generated = False
    if not key:
        key = secrets.token_urlsafe(24)
        try:
            _write_private(path, key)
        except OSError as exc:
            logger.warning(f"Could not write MCP_API_KEY_FILE {key_file!r} ({exc}); leaving auth unconfigured")
            return
        generated = True

    gateway["api_key"] = key
    if generated:
        logger.info(f"Generated a gateway admin API key (first run); saved to {path}")
        _announce_key(key, path)


def _write_private(path: Path, key: str) -> None:
    # Create 0600 before writing so the key is never briefly world-readable on a shared volume.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, key.encode())
    finally:
        os.close(fd)


def _announce_key(key: str, path: Path) -> None:
    banner = (
        "\n"
        "============================================================\n"
        " Device MCP Gateway — admin API key generated\n"
        "------------------------------------------------------------\n"
        " MCP / LLM clients must send it as a bearer token:\n"
        "   Authorization: Bearer <key>\n"
        " MCP endpoint (Streamable HTTP):\n"
        "   http://<this-host>:8000/v1/devices/<name>/mcp\n"
        f"   Key: {key}\n"
        "------------------------------------------------------------\n"
        f" Saved to {path} — delete that file to rotate the key.\n"
        " Set MCP_API_KEY to choose your own instead.\n"
        "============================================================\n"
    )
    print(banner, file=sys.stderr, flush=True)
