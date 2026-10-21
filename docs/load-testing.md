# Load Testing & Baseline Methodology

Phase-0 artifact (F-22). The SLO targets in `prometheus-rules.yaml` and
[failure-modes.md](failure-modes.md) are **starting points** — they need a measured load
baseline to be tuned honestly. This doc is the method; the runnable harness lives in
[`tools/loadtest/`](../tools/loadtest/) (`python -m tools.loadtest.loadgen`).

> There is no standing performance lab in this repo. This produces a *baseline on the
> environment you run it against* — record it with the environment, don't treat one number
> as universal.

## What to measure

| Workload | Path exercised | Question it answers |
|----------|----------------|---------------------|
| `toolcall` | `POST /v1/devices/{device}/mcp` `tools/call` → result on that response; `--era stateless` (default, `2026-07-28`) or `--era handshake` (one `initialize`, then the session) | End-to-end tool-call latency + throughput (the user-facing SLI) |
| `register` | `POST /v1/devices` (discovery + translate + spawn) | Onboarding/burst capacity; sizes the spec-translation pool (F-09/F-11) |

Primary metrics: **p50/p90/p99 latency**, **throughput (ok req/s)**, **error rate** and
its breakdown (429 shedding, timeouts, transport, rpc_error).

## The upstream matters most

Tool-call latency is dominated by the **upstream device**. To measure the *gateway's*
overhead, point devices at a **stub upstream with a known, fixed latency** (e.g. a tiny
FastAPI app that sleeps a configured N ms and returns a small body). Then the gateway's
added latency = measured p50 − stub latency. To capacity-plan for a *real* device, point
at that device (or a faithful mock of its latency distribution and rate limits).

## Procedure

1. **Isolate the environment.** Dedicated gateway + worker(s) + Redis; record CPU/mem
   limits, replica counts, `registry.max_concurrent_calls_per_device`,
   `max_concurrent_calls_per_worker`, and the upstream stub's latency. A baseline without
   its environment recorded is noise.
2. **Warm up.** Run 30–60s and discard — let pods spawn, breakers settle, JIT/connection
   pools warm.
3. **Find the knee (open loop).** Run `--rps 0` (as fast as concurrency allows) at
   increasing `--concurrency`; plot throughput vs latency. The knee (throughput flat,
   latency climbing) is the saturation point.
4. **Steady-state baseline (closed loop).** Pick an RPS below the knee, run ≥5 min with
   `--rps <target>`, record the report. This is the number you tune SLOs against.
5. **Overload behavior.** Push past the knee and confirm the system **sheds visibly**
   (`429`/`throttled_429` rising, `mcp_calls_rejected_overload_total` →
   `MCPAdmissionShedding`) rather than silently timing out — i.e. F-06 working.
6. **Record** the JSON (`--out`) alongside the environment in your perf log / the table
   below.

## Running the harness

```bash
pip install -e .          # httpx is the only dependency the harness needs

# End-to-end tool-call baseline (20 clients, capped at 200 rps, 5 min)
python -m tools.loadtest.loadgen toolcall \
  --base-url http://localhost:8000 --device my-sensor \
  --tool get_readings --arguments '{"sensor_id": 1}' \
  --api-key "$MCP_GATEWAY_API_KEY" \
  --concurrency 20 --rps 200 --duration 300 --out baseline-toolcall.json

# Registration throughput (open loop, 10 clients, 60 s)
python -m tools.loadtest.loadgen register \
  --base-url http://localhost:8000 --api-key "$MCP_GATEWAY_API_KEY" \
  --target-url http://stub-device:8080 \
  --concurrency 10 --duration 60 --out baseline-register.json
```

`--rps` caps the **aggregate** offered load across all clients (token bucket); `0` =
open loop. See `--help` for all flags.

## Interpreting results against SLOs

- **p99 latency** → seeds `slo:tool_call_latency:p99_5m`; set an alerting target once you
  have a steady-state number plus headroom.
- **error rate** → must stay under the 0.5% tool-call success budget at your target RPS;
  if errors are mostly `throttled_429` you're past the device's capacity (expected,
  visible shedding), not a gateway fault.
- **throughput knee** → informs HPA targets and `max_concurrent_calls_per_*` settings.

## Recording a baseline (template)

Keep results with their environment — a number without context can't be compared.

| Date | Env (replicas / limits) | Upstream | Workload | Conc / RPS | p50 | p99 | Throughput | Err% |
|------|-------------------------|----------|----------|------------|-----|-----|------------|------|
| _e.g._ 2026-06-15 | gw×2 (1cpu) / wk×3 (2cpu) / redis×1 | stub@20ms | toolcall | 20 / 200 | — | — | — | — |

## Limitations & next step

This is a *load* harness, not a *chaos* harness. It measures performance under healthy
conditions; it does not inject faults (kill workers mid-run, partition Redis, pause for GC).
Fault-injection / game-day is tracked separately as **F-63** — the experiment plan
(`E1–E10`) is ready to run when a live platform exists. Combine the two: baseline first,
then break things and confirm the failure-mode mitigations in
[failure-modes.md](failure-modes.md) behave as documented.

Both are recorded in [testing-gaps.md](testing-gaps.md) — the baseline table below is
**TG-2** and is still empty, which is exactly the gap. Chaos is **TG-1**, and depends on
this baseline existing first: chaos results are uninterpretable without a healthy
comparison.
