# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Load-test harness for the Device MCP Gateway (F-22 — Phase-0 load baseline).

A self-contained async load generator with no dependencies beyond ``httpx`` (already a
runtime dependency), so it runs anywhere the gateway runs. It drives one of two
workloads against a *running* gateway and reports a latency/throughput baseline:

  * ``toolcall`` — the real end-to-end MCP path over Streamable HTTP
    (``/v1/devices/{device}/mcp``): each virtual client POSTs ``tools/call`` messages and
    times the full round-trip to the answer on that same response (gateway → Redis →
    worker → device → back). ``--era stateless`` (the default) sends each call on its own,
    in the ``2026-07-28`` envelope; ``--era handshake`` performs ``initialize`` once per
    client and replays its session, as a ``2025-11-25`` client does.
  * ``register`` — device-registration throughput against ``POST /v1/devices`` (control
    plane), useful for sizing the spec-translation pool and onboarding bursts.

Usage (see ``--help`` for all flags):

    python -m tools.loadtest.loadgen toolcall \\
        --base-url http://localhost:8000 --device my-sensor \\
        --tool get_readings --arguments '{"sensor_id": 1}' \\
        --api-key "$MCP_GATEWAY_API_KEY" \\
        --concurrency 20 --duration 60 --rps 200 --out baseline.json

This is a *baseline* tool, not a benchmark of record: it measures the gateway under a
synthetic device. Point it at a representative device (or a stub that mimics your real
upstream latency) and record the result with the methodology in
``docs/load-testing.md``. Nothing here writes to the gateway except the chosen workload.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field

try:  # httpx is a runtime dependency; guard only so --help works without an install
    import httpx
except ImportError:  # pragma: no cover - exercised only on a broken env
    httpx = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a live gateway)
# ---------------------------------------------------------------------------


def percentile(samples: list[float], p: float) -> float:
    """Nearest-rank percentile of ``samples`` (p in [0, 100]). Empty → ``nan``.

    Nearest-rank (not interpolated) so the reported number is an observation that
    actually happened — the honest choice for a latency baseline.
    """
    if not samples:
        return math.nan
    if p <= 0:
        return min(samples)
    if p >= 100:
        return max(samples)
    ordered = sorted(samples)
    rank = math.ceil(p / 100.0 * len(ordered))
    return ordered[min(rank, len(ordered)) - 1]


#: The stateless revision ``--era stateless`` speaks.
MODERN_VERSION = "2026-07-28"
#: The handshake revision ``--era handshake`` speaks.
HANDSHAKE_VERSION = "2025-11-25"


def build_call(era: str, msg_id: str, tool: str, arguments: dict, session_id: str | None = None) -> tuple[dict, dict]:
    """The ``(body, headers)`` of one ``tools/call`` in ``era``.

    Stateless: the ``_meta`` envelope and the routing headers a ``2026-07-28`` server checks.
    Handshake: the plain request, carrying the session ``initialize`` issued.
    """
    params: dict = {"name": tool, "arguments": arguments}
    headers = {"Accept": "application/json, text/event-stream"}
    if era == "stateless":
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "loadgen", "version": "1"},
        }
        headers.update({"MCP-Protocol-Version": MODERN_VERSION, "Mcp-Method": "tools/call", "Mcp-Name": tool})
    else:
        headers["MCP-Protocol-Version"] = HANDSHAKE_VERSION
        if session_id:
            headers["Mcp-Session-Id"] = session_id
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}, headers


def classify_reply(status: int, body: dict | None) -> str:
    """``ok``, or the error bucket a reply belongs in."""
    if status == 429:
        return "throttled_429"
    if status >= 400:
        return f"post_{status}"
    if not isinstance(body, dict):
        return "bad_body"
    if body.get("error"):
        return "rpc_error"
    return "ok"


class RateLimiter:
    """Token-bucket pacer: caps the *aggregate* offered load to ``rps`` across all
    virtual clients. ``rps <= 0`` disables pacing (open-loop, as-fast-as-possible)."""

    def __init__(self, rps: float) -> None:
        self.rps = rps
        self._tokens = rps
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rps <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.rps, self._tokens + (now - self._last) * self.rps)
            self._last = now
            if self._tokens < 1:
                deficit = (1 - self._tokens) / self.rps
                await asyncio.sleep(deficit)
                self._tokens = 0
            else:
                self._tokens -= 1


@dataclass
class Stats:
    """Accumulates per-request outcomes into a baseline report."""

    latencies_ms: list[float] = field(default_factory=list)
    ok: int = 0
    errors: int = 0
    timeouts: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def record_ok(self, latency_ms: float) -> None:
        self.ok += 1
        self.latencies_ms.append(latency_ms)

    def record_error(self, kind: str) -> None:
        self.errors += 1
        if kind == "timeout":
            self.timeouts += 1
        self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1

    def report(self) -> dict:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        total = self.ok + self.errors
        lat = self.latencies_ms
        return {
            "requests": total,
            "ok": self.ok,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "error_rate": (self.errors / total) if total else 0.0,
            "error_kinds": dict(sorted(self.error_kinds.items())),
            "throughput_rps": round(self.ok / elapsed, 2),
            "elapsed_s": round(elapsed, 2),
            "latency_ms": {
                "count": len(lat),
                "mean": round(statistics.fmean(lat), 2) if lat else None,
                "p50": round(percentile(lat, 50), 2) if lat else None,
                "p90": round(percentile(lat, 90), 2) if lat else None,
                "p99": round(percentile(lat, 99), 2) if lat else None,
                "max": round(max(lat), 2) if lat else None,
            },
        }


def format_report(report: dict, workload: str) -> str:
    lat = report["latency_ms"]
    lines = [
        "",
        f"=== Device MCP Gateway load baseline — workload: {workload} ===",
        f"  duration         : {report['elapsed_s']} s",
        f"  requests         : {report['requests']} (ok {report['ok']}, errors {report['errors']})",
        f"  throughput (ok)  : {report['throughput_rps']} req/s",
        f"  error rate       : {report['error_rate'] * 100:.2f}%",
        f"  latency p50/p90  : {lat['p50']} / {lat['p90']} ms",
        f"  latency p99/max  : {lat['p99']} / {lat['max']} ms",
        f"  latency mean     : {lat['mean']} ms",
    ]
    if report["error_kinds"]:
        kinds = ", ".join(f"{k}={v}" for k, v in report["error_kinds"].items())
        lines.append(f"  error breakdown  : {kinds}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workloads (require a live gateway + httpx)
# ---------------------------------------------------------------------------


async def _toolcall_client(idx: int, args, stats: Stats, limiter: RateLimiter, deadline: float) -> None:
    auth = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    url = f"{args.base_url}/v1/devices/{args.device}/mcp"
    payload_args = json.loads(args.arguments) if args.arguments else {}
    session_id: str | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
            if args.era == "handshake":
                init = await client.post(
                    url,
                    headers={**auth, "Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": f"{idx}-init",
                        "method": "initialize",
                        "params": {"protocolVersion": HANDSHAKE_VERSION, "capabilities": {}},
                    },
                )
                session_id = init.headers.get("Mcp-Session-Id")
                if init.status_code != 200 or not session_id:
                    stats.record_error(f"initialize_{init.status_code}")
                    return
            msg_id = 0
            while time.monotonic() < deadline:
                await limiter.acquire()
                if time.monotonic() >= deadline:
                    break
                msg_id += 1
                body, headers = build_call(args.era, f"{idx}-{msg_id}", args.tool, payload_args, session_id)
                t0 = time.monotonic()
                try:
                    post = await client.post(url, headers={**auth, **headers}, json=body)
                    latency_ms = (time.monotonic() - t0) * 1000
                    try:
                        reply = post.json()
                    except ValueError:
                        reply = None
                    verdict = classify_reply(post.status_code, reply)
                    if verdict == "ok":
                        stats.record_ok(latency_ms)
                    else:
                        stats.record_error(verdict)
                except httpx.TimeoutException:
                    stats.record_error("timeout")
                except httpx.HTTPError:
                    stats.record_error("transport")
    except httpx.HTTPError as exc:
        stats.record_error(f"connect_{type(exc).__name__}")


async def _register_client(idx: int, args, stats: Stats, limiter: RateLimiter, deadline: float) -> None:
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    url = f"{args.base_url}/v1/devices"
    seq = 0
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        while time.monotonic() < deadline:
            await limiter.acquire()
            if time.monotonic() >= deadline:
                break
            seq += 1
            body = {
                "hostname": f"{args.hostname_prefix}-{idx}-{seq}",
                "base_url": args.target_url,
            }
            t0 = time.monotonic()
            try:
                resp = await client.post(url, headers=headers, json=body)
                latency_ms = (time.monotonic() - t0) * 1000
                if resp.status_code == 429:
                    stats.record_error("throttled_429")
                elif resp.status_code >= 400:
                    stats.record_error(f"http_{resp.status_code}")
                else:
                    stats.record_ok(latency_ms)
            except httpx.TimeoutException:
                stats.record_error("timeout")
            except httpx.HTTPError:
                stats.record_error("transport")


async def run(args) -> dict:
    if httpx is None:
        raise SystemExit("httpx is required to run the load harness (pip install -e .)")
    stats = Stats()
    limiter = RateLimiter(args.rps)
    deadline = time.monotonic() + args.duration
    client_fn = _toolcall_client if args.workload == "toolcall" else _register_client
    stats.started = time.monotonic()
    await asyncio.gather(*(client_fn(i, args, stats, limiter, deadline) for i in range(args.concurrency)))
    return stats.report()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loadgen",
        description="Load-test harness for the Device MCP Gateway (F-22).",
    )
    p.add_argument("workload", choices=["toolcall", "register"], help="which path to exercise")
    p.add_argument("--base-url", default="http://localhost:8000", help="gateway base URL")
    p.add_argument("--api-key", default="", help="bearer token (omit if auth disabled)")
    p.add_argument("--concurrency", type=int, default=10, help="virtual clients")
    p.add_argument("--duration", type=float, default=30.0, help="run length in seconds")
    p.add_argument("--rps", type=float, default=0.0, help="aggregate offered RPS cap (0 = open loop)")
    p.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    p.add_argument("--out", default="", help="write the JSON report to this path")
    # toolcall
    p.add_argument("--device", default="", help="[toolcall] registered device hostname")
    p.add_argument("--tool", default="", help="[toolcall] MCP tool name to call")
    p.add_argument("--arguments", default="{}", help="[toolcall] JSON tool arguments")
    p.add_argument(
        "--era",
        choices=["stateless", "handshake"],
        default="stateless",
        help="[toolcall] MCP era: stateless 2026-07-28 requests, or a 2025-11-25 session",
    )
    # register
    p.add_argument("--target-url", default="http://127.0.0.1:9", help="[register] device base_url to submit")
    p.add_argument("--hostname-prefix", default="loadtest", help="[register] generated hostname prefix")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workload == "toolcall" and (not args.device or not args.tool):
        print("toolcall workload requires --device and --tool", file=sys.stderr)
        return 2
    report = asyncio.run(run(args))
    print(format_report(report, args.workload))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
