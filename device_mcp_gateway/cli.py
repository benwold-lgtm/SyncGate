# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""
CLI entry point for the Device MCP Gateway.

Usage:
    device-mcp [--config PATH] [--host HOST] [--port PORT] [--log-level LEVEL]
    device-mcp devices {list,show} ...    inspect devices via the gateway API (cli_devices)
"""

import argparse
import os
import sys


def main() -> None:
    # `device-mcp devices …` is a client of a running gateway, not the server: route it
    # before the server's parser, so the server's flags and behaviour stay exactly as they
    # were (a bare `device-mcp` still starts the gateway).
    if sys.argv[1:2] == ["devices"]:
        from device_mcp_gateway.cli_devices import devices_main

        sys.exit(devices_main(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        prog="device-mcp",
        description="Device MCP Gateway — serve OpenAPI devices as MCP tool servers",
        epilog="To inspect devices on a running gateway: device-mcp devices --help",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=os.getenv("MCP_CONFIG", "config.yaml"),
        help="Path to config.yaml (default: $MCP_CONFIG or ./config.yaml)",
    )
    parser.add_argument("--host", metavar="HOST", help="Bind address (overrides config)")
    parser.add_argument("--port", metavar="PORT", type=int, help="Port (overrides config)")
    parser.add_argument(
        "--log-level",
        metavar="LEVEL",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity (overrides config)",
    )
    args = parser.parse_args()

    # Set before importing the app so load_config() picks it up.
    os.environ["MCP_CONFIG"] = args.config

    if not os.path.exists(args.config):
        print(f"Warning: config file '{args.config}' not found — using built-in defaults", file=sys.stderr)

    try:
        import uvicorn
        from device_mcp_gateway.cfg import load_config

        cfg = load_config(args.config)
        host = args.host or cfg.get("server", {}).get("host", "0.0.0.0")  # nosec B104 — bind-all intended in containers
        port = args.port or cfg.get("server", {}).get("port", 8000)
        log_level = (args.log_level or cfg.get("logging", {}).get("level", "INFO")).lower()

        # Export the resolved bind address so the import-time app (create_app →
        # warn_unsafe_settings) sees the effective host, not just config — otherwise a
        # `--host 127.0.0.1` override still triggers the "binding 0.0.0.0" warning.
        os.environ["MCP_BIND_HOST"] = host

        uvicorn.run(
            "device_mcp_gateway.main:app",
            host=host,
            port=port,
            log_level=log_level,
        )
    except ImportError as exc:
        print(f"Error: missing dependency — {exc}", file=sys.stderr)
        print("Run: pip install -e .", file=sys.stderr)
        sys.exit(1)


def worker_main() -> None:
    """Entry point for the device-mcp-worker process (distributed mode)."""
    import asyncio
    import os
    import socket

    parser = argparse.ArgumentParser(
        prog="device-mcp-worker",
        description="Device MCP Worker — runs DevicePods for distributed mode",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=os.getenv("MCP_CONFIG", "config.yaml"),
        help="Path to config.yaml (default: $MCP_CONFIG or ./config.yaml)",
    )
    parser.add_argument(
        "--worker-id",
        metavar="ID",
        default=os.getenv("WORKER_ID", socket.gethostname()),
        help="Unique worker identifier (default: $WORKER_ID or hostname)",
    )
    args = parser.parse_args()
    os.environ["MCP_CONFIG"] = args.config

    try:
        from device_mcp_gateway import metrics
        from device_mcp_gateway.cfg import load_config, resolve_mode
        from device_mcp_gateway.shared.crypto import CredentialCodec
        from device_mcp_gateway.shared.redis_client import create_redis, wait_for_redis
        from device_mcp_gateway.shared.registry_backend import RedisRegistryBackend
        from device_mcp_gateway.worker.runner import DeviceWorker
        from device_mcp_gateway.logging_setup import setup_logging

        cfg = load_config(args.config)

        # The worker only has a role in distributed mode. Refuse to run against an
        # embedded config so it can't silently diverge from an embedded gateway.
        mode = resolve_mode(cfg)
        if mode != "distributed":
            print(
                f"Error: device-mcp-worker requires distributed mode, but registry.mode "
                f"resolves to '{mode}'. Set registry.mode: distributed (or MCP_REGISTRY_MODE=distributed).",
                file=sys.stderr,
            )
            sys.exit(1)
        log_cfg = cfg.get("logging", {})
        setup_logging(
            level=log_cfg.get("level", "INFO"),
            log_file=log_cfg.get("file", "logs/worker.log"),
            max_size_mb=log_cfg.get("max_size", 50),
            backup_count=log_cfg.get("backup_count", 5),
            # Each process owns its own audit file so the per-process hash chains don't
            # interleave (F-57); the worker's stream is separate from the gateway's.
            audit_file=log_cfg.get("worker_audit_file", "logs/worker-audit.log"),
            audit_retention=log_cfg.get("audit_retention", "90 days"),
            audit_enabled=log_cfg.get("audit_enabled", True),
        )

        try:
            codec = CredentialCodec.from_config(cfg)
        except ValueError as exc:
            print(f"Error: invalid MCP_SECRET_KEY / gateway.secret_key: {exc}", file=sys.stderr)
            sys.exit(1)

        # The worker only runs in distributed mode and reads credentials from
        # Redis; refuse to start without a key unless explicitly allowed.
        allow_plaintext = bool(cfg.get("gateway", {}).get("allow_plaintext_credentials", False))
        if not codec.enabled and not allow_plaintext:
            print(
                "Error: refusing to start worker without MCP_SECRET_KEY — device credentials in "
                "Redis cannot be decrypted. Set a Fernet key, or set "
                "gateway.allow_plaintext_credentials: true to override.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Redis control-plane authn gate (Tier-0 F-24): the worker consumes tool calls from
        # Redis; refuse an unauthenticated Redis unless redis.allow_insecure is set.
        from device_mcp_gateway.shared.redis_client import assert_redis_secure

        try:
            assert_redis_secure(cfg)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        # Workers have no API server, so expose Prometheus on the dedicated metrics
        # port here (same pattern as the gateway). This also unlocks the
        # redis-stream-lag signal the worker HPA wants.
        if metrics.metrics_enabled(cfg):
            metrics.start_metrics_server(metrics.metrics_port(cfg), auth_token=metrics.metrics_token(cfg))

        async def _run() -> None:
            redis_client = await create_redis(cfg)
            # A worker is usually the first thing scheduled alongside Redis, so it is the
            # process most likely to find it not yet listening. Wait instead of exiting —
            # kubelet backoff recovers from the crash, but only by way of a stack trace and
            # a restart count that reads as a broken deployment (see wait_for_redis).
            await wait_for_redis(redis_client, cfg, component=f"worker {args.worker_id}")
            backend = RedisRegistryBackend(redis_client)
            worker = DeviceWorker(
                worker_id=args.worker_id,
                config=cfg,
                redis_client=redis_client,
                codec=codec,
            )
            await worker.run(backend)

        try:
            asyncio.run(_run())
        except RuntimeError as exc:
            # Redis never came up within redis.startup_timeout. Still a failure and still
            # a non-zero exit, but reported the way the other startup gates above report
            # theirs — a stack trace here says "bug", and this is configuration.
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    except ImportError as exc:
        import sys

        print(f"Error: missing dependency — {exc}", file=sys.stderr)
        sys.exit(1)


def rotate_secrets_main() -> None:
    """Entry point for `device-mcp-rotate-secrets` — re-encrypt stored credentials (F-34).

    Run during a key rotation, after deploying with the new key primary and the
    old key still present (`secret_keys: [<new>, <old>]`). Re-encrypts every
    stored device credential under the new primary key so the old key can be
    retired. Idempotent and safe to re-run.
    """
    import asyncio

    parser = argparse.ArgumentParser(
        prog="device-mcp-rotate-secrets",
        description="Re-encrypt stored device credentials under the current primary Fernet key (F-34)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=os.getenv("MCP_CONFIG", "config.yaml"),
        help="Path to config.yaml (default: $MCP_CONFIG or ./config.yaml)",
    )
    args = parser.parse_args()
    os.environ["MCP_CONFIG"] = args.config

    try:
        from device_mcp_gateway.cfg import load_config, resolve_mode
        from device_mcp_gateway.shared.crypto import CredentialCodec
        from device_mcp_gateway.shared.rotate import rotate_redis_credentials, rotate_sqlite_credentials
    except ImportError as exc:
        print(f"Error: missing dependency — {exc}", file=sys.stderr)
        sys.exit(1)

    cfg = load_config(args.config)

    try:
        codec = CredentialCodec.from_config(cfg)
    except ValueError as exc:
        print(f"Error: invalid MCP_SECRET_KEY / gateway.secret_key(s): {exc}", file=sys.stderr)
        sys.exit(1)

    if not codec.enabled:
        print(
            "Error: no Fernet key configured — nothing to rotate. Set MCP_SECRET_KEY or "
            "gateway.secret_keys (new key first, old key second).",
            file=sys.stderr,
        )
        sys.exit(1)
    if not codec.multi_key:
        print(
            "Warning: only one key is configured, so rotation is a no-op. To rotate, set "
            "gateway.secret_keys: [<new>, <old>] (or MCP_SECRET_KEY='<new>,<old>') and re-run.",
            file=sys.stderr,
        )

    mode = resolve_mode(cfg)

    async def _run() -> int:
        if mode == "distributed":
            from device_mcp_gateway.shared.redis_client import assert_redis_secure, create_redis
            from device_mcp_gateway.shared.registry_backend import RedisRegistryBackend

            try:
                assert_redis_secure(cfg)
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            redis_client = await create_redis(cfg)
            backend = RedisRegistryBackend(redis_client)
            await backend.initialize()
            result = await rotate_redis_credentials(backend, codec)
        else:
            from device_mcp_gateway.storage.sqlite_store import SqliteDeviceStore

            db_path = cfg.get("storage", {}).get("db_path", "./data/devices.db")
            store = SqliteDeviceStore(db_path=db_path, codec=codec)
            await store.initialize()
            result = await rotate_sqlite_credentials(store, codec)

        print(f"Rotation complete ({mode} mode): {result.summary()}")
        return 1 if result.failed else 0

    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
