# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""``device-mcp devices`` — list registered devices and inspect what each one exposes.

Usage:
    device-mcp devices list                      every API and MCP device, with health
    device-mcp devices show HOSTNAME [--schemas] one device: tools, resources and prompts

The CLI reads the **gateway's HTTP API**, never Redis or SQLite directly: the same
endpoints the web console reads (``/v1/admin/overview`` for the list,
``/v1/devices/{hostname}`` and ``/v1/devices/{hostname}/features`` for a device), under the
same token-based RBAC (``devices:read``). That is what keeps the two consistent — they
cannot disagree about a device, because there is one backend and one read path, and the
CLI needs no registry credentials an operator's laptop should not hold.

Connection settings, flags overriding environment:
    --url / MCP_GATEWAY_URL           gateway base URL (default http://localhost:8000)
    --token-file / MCP_GATEWAY_TOKEN  bearer token; a file keeps it out of shell history
    --ca-file / MCP_GATEWAY_CA        CA bundle for a gateway behind private TLS

Exit status: 0 success, 1 the gateway refused or could not be reached, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, TextIO

import httpx

DEFAULT_URL = "http://localhost:8000"


class CliError(Exception):
    """A failure to report on stderr with exit status 1."""


# --- presentation helpers -----------------------------------------------------------------


def _kind(upstream_kind: str | None) -> str:
    # Same labels as the console's Kind column, so the two read alike.
    return "mcp" if upstream_kind == "mcp" else "openapi"


def _health(device: dict, stale_after: float | None, now: float) -> str:
    """The console's rule (web/src/tokens.ts ``deviceHealth``), so CLI and UI agree."""
    if stale_after is None:
        return "online" if device.get("reachable") else "offline"
    last = device.get("last_check")
    if last is None or now - last > stale_after:
        return "stale"
    return "online" if device.get("reachable") else "offline"


def _age(last_check: float | None, now: float) -> str:
    if last_check is None:
        return "never checked"
    age = max(0, round(now - last_check))
    if age < 90:
        return f"{age}s ago"
    if age < 5400:
        return f"{round(age / 60)}m ago"
    return f"{round(age / 3600)}h ago"


def _table(rows: list[list[str]], out: TextIO) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        out.write("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() + "\n")


def _one_line(text: str, limit: int = 72) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- the gateway client -------------------------------------------------------------------


def _token(args: argparse.Namespace) -> str | None:
    if args.token_file:
        try:
            with open(args.token_file, encoding="utf-8") as fh:
                # Strip: a trailing newline from `echo … > file` or a mounted Secret would
                # otherwise be sent as part of the token and read as a wrong one.
                return fh.read().strip() or None
        except OSError as exc:
            raise CliError(f"cannot read token file {args.token_file}: {exc.strerror}") from exc
    return (os.getenv("MCP_GATEWAY_TOKEN") or "").strip() or None


class _Gateway:
    def __init__(self, args: argparse.Namespace, client: httpx.Client | None = None) -> None:
        self.base = args.url.rstrip("/")
        self._headers = {"Accept": "application/json"}
        token = _token(args)
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        # Headers go on each request rather than the client, so an injected client (tests
        # pass the gateway's own TestClient) is used exactly as given.
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=args.timeout, verify=args.ca_file or True)

    def get(self, path: str, *, not_found: str | None = None) -> Any:
        try:
            resp = self._client.get(self.base + path, headers=self._headers)
        except httpx.HTTPError as exc:
            raise CliError(f"cannot reach the gateway at {self.base}: {exc}") from exc
        if resp.status_code == 401:
            raise CliError("unauthorized — set MCP_GATEWAY_TOKEN or pass --token-file")
        if resp.status_code == 403:
            raise CliError("forbidden — this token lacks the devices:read scope")
        if resp.status_code == 404 and not_found:
            raise CliError(not_found)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise CliError(f"gateway answered {resp.status_code} for {path}: {_one_line(str(detail), 200)}")
        return resp.json()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


# --- commands -----------------------------------------------------------------------------


def _cmd_list(gw: _Gateway, args: argparse.Namespace, out: TextIO) -> None:
    overview = gw.get("/v1/admin/overview")
    devices = sorted(overview.get("devices", []), key=lambda d: d["hostname"])
    if args.json:
        json.dump({"devices": devices, "stale_after_seconds": overview.get("stale_after_seconds")}, out, indent=2)
        out.write("\n")
        return
    if not devices:
        out.write("No devices registered.\n")
        return
    now = time.time()
    stale_after = overview.get("stale_after_seconds")
    rows = [["HOSTNAME", "KIND", "HEALTH", "POD", "LAST CHECK", "CREDENTIALS", "BASE URL"]]
    for d in devices:
        rows.append(
            [
                d["hostname"],
                _kind(d.get("upstream_kind")),
                _health(d, stale_after, now),
                "active" if d.get("pod_active") else "inactive",
                _age(d.get("last_check"), now),
                d.get("credential_state") or "ok",
                d.get("base_url", ""),
            ]
        )
    _table(rows, out)
    kinds = [_kind(d.get("upstream_kind")) for d in devices]
    out.write(
        f"\n{len(devices)} device(s): {kinds.count('openapi')} openapi, {kinds.count('mcp')} mcp · "
        f"{sum(1 for d in devices if d.get('pod_active'))} with an active pod\n"
    )


def _cmd_show(gw: _Gateway, args: argparse.Namespace, out: TextIO) -> None:
    missing = f"device '{args.hostname}' is not registered"
    device = gw.get(f"/v1/devices/{args.hostname}", not_found=missing)
    features = gw.get(f"/v1/devices/{args.hostname}/features", not_found=missing)
    if args.json:
        json.dump({"device": device, "features": features}, out, indent=2)
        out.write("\n")
        return

    now = time.time()
    w = out.write
    w(f"{device['hostname']}  ({_kind(device.get('upstream_kind'))} · {device.get('base_url', '')})\n")
    server = features.get("server")
    if server:
        w(f"  Server          {server['name']} {server['version']}".rstrip() + "\n")
    w(
        f"  Status          {'reachable' if device.get('reachable') else 'unreachable'} · "
        f"pod {'active' if device.get('pod_active') else 'inactive'} · "
        + ("never checked" if device.get("last_check") is None else f"last check {_age(device['last_check'], now)}")
        + "\n"
    )
    if device.get("credential_state", "ok") != "ok":
        w(f"  Credentials     {device['credential_state']} — re-authorize before it can work\n")
    if device.get("spawn_error"):
        w(f"  Spawn error     {_one_line(device['spawn_error'], 120)}\n")
    w(f"  Tools revision  {features.get('tools_revision', 0)}\n")

    if not features.get("manifest_cached"):
        w(
            "\nNo manifest cached for this device — it has not been discovered yet, or has been\n"
            "down longer than the manifest cache lives. Its tools, resources and prompts are\n"
            "unknown, not empty.\n"
        )
        return

    discovered = features.get("discovered", {})

    tools = features.get("tools", [])
    w(f"\nTools ({len(tools)})\n")
    if tools:
        rows = []
        for t in tools:
            http = f"{t['method']} {t['path']}" if t.get("method") else "(proxied)"
            rows.append(["  " + t["name"], http, _one_line(t.get("description", ""), 60)])
        _table(rows, out)
        if args.schemas:
            for t in tools:
                w(f"\n  {t['name']} input schema:\n")
                for line in json.dumps(t.get("schema") or {}, indent=2).splitlines():
                    w(f"    {line}\n")
    else:
        w("  none\n")

    resources = features.get("resources", [])
    if not discovered.get("resources", True):
        w("\nResources  not discovered for a proxied MCP server\n")
    else:
        w(f"\nResources ({len(resources)})\n")
        if resources:
            _table(
                [
                    ["  " + r["name"], r["uri"], r.get("mime_type", ""), _one_line(r.get("description", ""), 50)]
                    for r in resources
                ],
                out,
            )
        else:
            w("  none\n")

    prompts = features.get("prompts", [])
    if not discovered.get("prompts", True):
        w("\nPrompts    not discovered for a proxied MCP server\n")
    else:
        w(f"\nPrompts ({len(prompts)})\n")
        if prompts:
            _table(
                [
                    [
                        "  " + p["name"],
                        "args: " + (", ".join(p.get("arguments") or []) or "none"),
                        _one_line(p.get("description", ""), 60),
                    ]
                    for p in prompts
                ],
                out,
            )
        else:
            w("  none\n")


# --- entry point --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="device-mcp devices",
        description="List registered devices and inspect their tools, resources and prompts. "
        "Reads the gateway API — the same source as the web console.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("MCP_GATEWAY_URL", DEFAULT_URL),
        help=f"gateway base URL (default: $MCP_GATEWAY_URL or {DEFAULT_URL})",
    )
    parser.add_argument(
        "--token-file",
        metavar="PATH",
        help="file holding the bearer token (default: $MCP_GATEWAY_TOKEN); needs devices:read",
    )
    parser.add_argument(
        "--ca-file",
        metavar="PATH",
        default=os.getenv("MCP_GATEWAY_CA") or None,
        help="CA bundle for verifying the gateway's TLS certificate (default: $MCP_GATEWAY_CA)",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="request timeout in seconds (default 15)")
    parser.add_argument("--json", action="store_true", help="print the gateway's JSON instead of a table")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    sub.add_parser("list", help="every registered device — openapi and mcp — with its health")
    show = sub.add_parser("show", help="one device's tools, resources and prompts")
    show.add_argument("hostname")
    show.add_argument("--schemas", action="store_true", help="also print each tool's input schema")
    return parser


def devices_main(
    argv: list[str],
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    client: httpx.Client | None = None,
) -> int:
    """Run ``device-mcp devices``; returns the exit status. ``client`` is for tests."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = _parser().parse_args(argv)  # exits 2 on a usage error, as argparse does
    gw: _Gateway | None = None
    try:
        gw = _Gateway(args, client=client)
        if args.command == "list":
            _cmd_list(gw, args, out)
        else:
            _cmd_show(gw, args, out)
        return 0
    except CliError as exc:
        err.write(f"device-mcp devices: {exc}\n")
        return 1
    finally:
        if gw is not None:
            gw.close()
