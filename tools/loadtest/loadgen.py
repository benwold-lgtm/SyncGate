# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Load-test harness for the Device MCP Gateway (F-22 — Phase-0 load baseline).

A self-contained async load generator with no dependencies beyond ``httpx`` (already a
runtime dependency), so it runs anywhere the gateway runs. It drives one of two
workloads against a *running* gateway and reports a latency/throughput baseline:

  * ``toolcall`` — the real end-to-end MCP path: each virtual client opens an SSE
    stream, reads its session ``endpoint``, then POSTs ``tools/call`` messages and
    matches the corresponding ``message`` event back off the stream, timing the full
    round-trip (gateway → Redis → worker → device → back).
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


def parse_sse_endpoint(chunk: str) -> str | None:
    """Extract the POST endpoint from an SSE ``endpoint`` event block.

    The gateway's first SSE event is ``event: endpoint`` whose ``data:`` line is the
    session-scoped messages URL. Returns the URL, or ``None`` if this block isn't an
    endpoint event.
    """
    is_endpoint = False
    data: str | None = None
    for line in chunk.splitlines():
        if line.startswith("event:") and line.split(":", 1)[1].strip() == "endpoint":
            is_endpoint = True
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
    return data if is_endpoint else None


def parse_sse_message(chunk: str) -> dict | None:
    """Parse a ``message`` event block's JSON ``data`` payload, or ``None``."""
    is_message = False
    data: str | None = None
    for line in chunk.splitlines():
        if line.startswith("event:") and line.split(":", 1)[1].strip() == "message":
            is_message = True
        elif line.startswith("data:"):
            data = line.split(":", 1)[1].strip()
    if is_message and data:
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None
    return None


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


async def _read_sse_until(response, predicate, timeout: float):
    """Read SSE event blocks (separated by a blank line) until ``predicate(block)``
    returns a non-None value or ``timeout`` elapses. Returns the value or None."""
    buffer = ""
    deadline = time.monotonic() + timeout
    async for raw in response.aiter_lines():
        if time.monotonic() > deadline:
            return None
        if raw == "":  # event delimiter
            if buffer.strip():
                value = predicate(buffer)
                if value is not None:
                    return value
            buffer = ""
        else:
            buffer += raw + "\n"
    return None


async def _toolcall_client(idx: int, args, stats: Stats, limiter: RateLimiter, deadline: float) -> None:
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    sse_url = f"{args.base_url}/v1/devices/{args.device}/sse"
    payload_args = json.loads(args.arguments) if args.arguments else {}
    timeout = httpx.Timeout(args.timeout, read=None)  # stream read has no fixed timeout
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", sse_url, headers=headers) as stream:
                if stream.status_code != 200:
                    stats.record_error(f"sse_{stream.status_code}")
                    return
                endpoint = await _read_sse_until(stream, parse_sse_endpoint, args.timeout)
                if not endpoint:
                    stats.record_error("no_endpoint")
                    return
                messages_url = f"{args.base_url}{endpoint}"
                msg_id = 0
                while time.monotonic() < deadline:
                    await limiter.acquire()
                    if time.monotonic() >= deadline:
                        break
                    msg_id += 1
                    body = {
                        "jsonrpc": "2.0",
                        "id": f"{idx}-{msg_id}",
                        "method": "tools/call",
                        "params": {"name": args.tool, "arguments": payload_args},
                    }
                    t0 = time.monotonic()
                    try:
                        post = await client.post(messages_url, headers=headers, json=body)
                        if post.status_code == 429:
                            stats.record_error("throttled_429")
                            continue
                        if post.status_code >= 400:
                            stats.record_error(f"post_{post.status_code}")
                            continue
                        # The result returns asynchronously on the SSE stream.
                        result = await _read_sse_until(stream, lambda b: parse_sse_message(b), args.timeout)
                        latency_ms = (time.monotonic() - t0) * 1000
                        if result is None:
                            stats.record_error("timeout")
                        elif isinstance(result, dict) and result.get("error"):
                            stats.record_error("rpc_error")
                        else:
                            stats.record_ok(latency_ms)
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
