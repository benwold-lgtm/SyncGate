# ADR-0008: Fleet MCP sessions — aggregate multiple devices into one session

- **Status:** Accepted; partly superseded by [ADR-0027](0027-stateless-requests-in-distributed-mode.md) §5 (fleet handles). Its SSE fleet surface was removed in 0.4.0; `/v1/fleet/mcp?devices=` remains for the handshake era, deprecated
- **Date:** 2026-07-06
- **Related findings:** —

## Context

Every device gets its own MCP session (`GET /v1/devices/{hostname}/sse` +
`POST /v1/devices/{hostname}/messages`) — one SSE connection, one tool namespace, one
session per device. An MCP client (Claude Desktop via the `mcp-remote` stdio bridge,
Cursor, a custom agent) that wants to reach N devices needs N separate connections: N
config entries and, for clients that need a header-injecting bridge, N bridge processes.
That's a minor inconvenience at two or three devices and an unworkable one at a real
fleet — which is precisely the scenario distributed mode (ADR-0001/0002) exists to serve.
Distributed mode scales the gateway's *backend* (more devices registered, more replicas,
Redis-backed shared state); it does nothing for the *client-facing* fan-in problem. Nobody
had built that half yet.

## Decision

Add a **fleet** endpoint — `GET /v1/fleet/sse?devices=host1,host2,...` and
`POST /v1/fleet/messages?session_id=...` — that aggregates several devices' tools into one
MCP session. Tool names are namespaced by hostname (`{hostname}_{tool}`, with the same
numeric-suffix collision handling `core.translator` already uses within a single device's
spec, extended one level across devices) so two devices exposing the same operation don't
collide in the aggregated `tools/list`.

Routing a `tools/call` back to the right device requires a lookup table (display name →
`{hostname, real_name}`), built once when the fleet session opens:

- **Embedded mode:** the table lives in-process (`app.state.fleet_transports`), scoped to
  the SSE connection's lifetime — no Redis involved, matching embedded mode's no-shared-state
  design.
- **Distributed mode:** the table is persisted in Redis (`SessionRouter.set_fleet_tools`/
  `get_fleet_tools`, TTL'd and refreshed alongside the existing per-session hash) because the
  `POST` carrying a `tools/call` may land on a different gateway replica than the `GET` that
  opened the session. `tools/call` dispatch then reuses the *existing* per-device distributed
  path unchanged — admission control (F-06), `publish_tool_call` onto the resolved device's
  own call-stream, and the F6 timeout watcher — parameterized by whichever hostname the fleet
  call resolves to.

No worker changes were needed. `SessionRouter.publish_result`/`subscribe` already route
purely by `session_id`, blind to whether that session belongs to one device or a fleet of
them — the same result-delivery mechanism that makes cross-replica delivery work for a
single device (ADR-0002) already generalizes to a fleet session for free.

## Consequences

- **Positive:** an AI client can address an entire fleet through one connection instead of
  one per device — the missing half of what "enterprise/distributed mode" was meant to
  provide. No changes to the worker, the RBAC model (still one global `tools:call` scope —
  there was no per-device ACL layer to preserve or weaken), or the per-device routes, which
  are untouched and remain the simpler choice for a single device.
- **Negative / cost:** two lookup-table storage paths (in-process vs. Redis) to keep in
  sync conceptually, though not code — they share the same aggregation logic
  (`fleet_service.build_fleet_manifest`) and differ only in where the resulting table is
  persisted. A fleet session is capped at `registry.fleet_max_devices` (default 25) so one
  session can't silently balloon into hundreds of tools dumped into an LLM's context.
- **Follow-ups:** `resources/*` and `prompts/*` aggregation is out of scope for now (fleet
  sessions serve `initialize`/`ping`/`tools/list`/`tools/call` only) — add if a real need
  shows up. Distributed-mode GET streaming is intentionally not covered by an automated
  live-HTTP test (see the comment in `tests/test_fleet_distributed.py`); a fakeredis-backed
  single-threaded event loop and an infinite SSE generator don't combine reliably under
  either `TestClient` or a background-thread `uvicorn` server. This matches the project's
  existing convention (`test_admission_control.py` never drives the per-device `GET /sse`
  route either) — coverage instead comes from `SessionRouter` unit tests, a cross-replica
  read test, and a real-Redis roundtrip test in `test_integration_redis.py`.

## Alternatives considered

- **Do nothing gateway-side; document the `mcp-remote` bridge as the answer:** doesn't
  solve the underlying problem — a client would still need N bridge processes and N config
  entries, one per device. Reasonable as a *stopgap* for a single stubborn client
  (Claude Desktop's OAuth-only Custom Connector UI), orthogonal to this decision, and still
  valid advice for a single device.
- **Full OAuth 2.1 authorization server so the gateway is a native Claude connector:**
  solves a different problem (client-side auth-flow compatibility), not the fan-in problem,
  and is a materially larger undertaking (the gateway would need to *issue* tokens, not just
  validate externally-issued ones as ADR-0007 already does) — out of scope here.
  Distinguished explicitly from the OAuth question so the two aren't conflated when
  reviewing this decision (they were initially conflated in the original discussion that
  motivated this ADR).
- **Encode the routing hostname into the tool-name string and parse it back at call time:**
  rejected — hostnames may contain dots/hyphens while tool names are sanitized to
  `[a-zA-Z0-9_]`, so a combined string can't be losslessly split back apart. An explicit
  lookup table built at aggregation time is the only safe option.
