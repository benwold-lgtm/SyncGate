# Phase 6 scope — Streamable HTTP inbound, then MCP `2026-07-28`

**Status:** decisions 1–4 taken 2026-08-07, 5–6 on 2026-10-16. Written 2026-08-06 against spec
revision `2026-07-28`.

| Piece | State |
|---|---|
| Workstream A — Streamable HTTP inbound | ✅ shipped in `0.3.0` (`POST /v1/devices/{hostname}/mcp`, `POST /v1/fleet/mcp`) |
| SDK `mcp` 1.x → 2.x | ✅ `0.3.7` — `FastMCP` deleted, `mcp` moved to the `dev` extra (see below) |
| Workstream B — dual-era | ⬜ not started; distributed-mode design is [ADR-0027](adr/0027-stateless-requests-in-distributed-mode.md) (Accepted) |
| HTTP+SSE removal | ⬜ `0.4.0`, paired with Workstream B |

## Why this is not optional

Two independent forces point the same way, and only one of them is about being current.

1. **Our only inbound transport is deprecated.** The gateway serves clients over
   **HTTP+SSE** — `GET /sse` returning an `endpoint` event, then `POST /messages?session_id=`.
   That transport has been deprecated since revision `2025-03-26` and is now formally
   *Deprecated* under the feature-lifecycle policy, with a removal clock. `transport != "sse"`
   is a hard reject in `api/devices.py` and `api/sse.py`: there is no alternative to fall back
   to. **We owe a Streamable HTTP inbound regardless of whether we adopt `2026-07-28`.**
2. **We speak `2025-06-18`, current is `2026-07-28`,** and the two eras do not interoperate.
   Not "degrade gracefully" — *fail*, in both directions, unless an implementation
   deliberately supports both. As upstreams move, a passthrough device pointed at a modern-only
   server stops working.

Doing these separately means paying for the transport work twice. Do the transport, then the
era, in that order — the second lands inside the first.

## What `2026-07-28` actually changes

It is the largest revision since launch, and its direction is **statelessness**: the protocol
stops owning session state so it can scale on ordinary HTTP infrastructure.

| Removed | Replaced by |
|---------|-------------|
| `initialize` / `notifications/initialized` handshake | Per-request `_meta`: `io.modelcontextprotocol/protocolVersion`, `clientCapabilities`, `clientInfo` |
| `Mcp-Session-Id` | Server-minted handles passed as ordinary tool arguments, where state is genuinely needed |
| SSE resumability (`Last-Event-ID`, event IDs) | Nothing — a broken stream loses the in-flight request; the client re-issues it with a new id |
| The HTTP `GET` endpoint, `resources/subscribe`/`unsubscribe` | `subscriptions/listen` — one long-lived POST-response stream, opted into per notification type |
| `ping`, `logging/setLevel`, `notifications/roots/list_changed` | Log level per request via `_meta`; the rest deleted |

New obligations: `server/discover` is **mandatory** for servers; every result carries
`resultType`; list results carry `ttlMs` and `cacheScope`; `Mcp-Method` / `Mcp-Name` headers
are required on POST; error codes are renumbered (`-32020`–`-32099` reserved for the spec) and
resource-not-found moves `-32002` → `-32602`.

Roots, Sampling and Logging are **Deprecated** (twelve-month minimum window). We implement
none of them, so this costs us nothing — and MRTR, which replaces server-initiated requests,
is mostly not our problem for the same reason.

## The shape of the work

### Workstream A — Streamable HTTP inbound (legacy semantics)

Serve `2025-06-18` over Streamable HTTP alongside the existing HTTP+SSE endpoints. One new
endpoint per surface (device and fleet) accepting a JSON-RPC POST and answering inline or as
an SSE-framed response body. Session state still exists here — this workstream does not change
semantics, only the transport carrying them.

This is where the distributed-mode integration work lives: a POST arriving at any gateway
replica has to wait for a worker's result and return it on *that* response, rather than
publishing it to a stream a different replica is holding open. The result plumbing already
exists (`session:{id}:results`); what changes is who is waiting on it.

### Workstream B — the modern era (dual-era)

**Inbound (we are a server).** Branch on how the client opens: modern `_meta` → stateless;
`initialize` → legacy, exactly as today. Add `server/discover`, `resultType` on every result,
cache hints on lists, the required request headers, and the renumbered errors. Advertise both
eras. Legacy clients keep working — that is the whole point of dual-era, and it is why the
existing SSE surface can stay through a deprecation window rather than being cut over.

**Outbound (we are a client).** `StreamableHttpClient` gains era detection: attempt a modern
request, inspect the body of a `400` before falling back to `initialize`, and **cache the
verdict per origin** (the spec is explicit that era is a property of the server, not a
request). Everything handshake-shaped then becomes dead code on the modern path:
`ensure_initialized`, the init lock, session tracking, the 404 re-handshake, `close_session`.

### What gets deleted, and what does not

The honest accounting, because "simpler" should be checkable:

| Deleted once legacy support is dropped | Survives untouched |
|---|---|
| Session registry, ownership, TTL, replica routing (`shared/session_router.py`, 228 lines, reaching into 6 modules) | Pods, breaker, token bucket, admission control |
| The `endpoint`-event dance in `api/sse.py` (236 lines) | Manifest, F-28 validation, F-26 sanitisation |
| Per-session fleet tool storage in `api/fleet.py` | Tool-change governance (F-41) |
| Handshake + session handling in `upstream/mcp_client.py` | Worker dispatch and result correlation — **per request instead of per session** |
| Inbound `initialize` / `notifications` / `ping` handling in `pods/pod_base.py` | The idempotency guard (F-08), which becomes *more* load-bearing |

**Net: fewer moving parts at the destination, more during the overlap** — dual-era means both
paths exist until legacy is dropped. Two bugs this codebase actually shipped (a missing
handshake; a session leaked per health cycle, ~5,760/device/day) are bugs that cannot exist in
a stateless protocol.

The SDK was not an obstacle, and is now out of the way. This section originally said `FastMCP`
appeared once, "for a server name and an instructions string". That understated it:
`DevicePod` also registered every tool on the instance — but nothing ever dispatched through
it (the router calls `_tool_dispatch`), and `instructions` never reached a client, because
`initialize` is built by hand. Its only consumers were three tests reaching into
`_tool_manager`. So `0.3.7` **deleted** it rather than porting it to 2.x's `MCPServer`, and
`mcp` moved to the `dev` extra: the gateway has no runtime dependency on the SDK at all, and
keeps it only as the reference implementation trap 1 below calls for.

## Decisions — 1–4 taken 2026-08-07, 5–6 on 2026-10-16

1. **`x-mcp-header` (SEP-2243): adopt for OpenAPI, exclude passthrough permanently.**
   Recorded as [ADR-0010](adr/0010-tool-derived-request-headers.md).

   The framing in the first draft of this document was wrong and is corrected there. F-25 does
   not forbid deriving headers from tool parameters — the OpenAPI path already does it, via a
   reserved-header denylist and CRLF rejection. What has no header derivation at all is the
   **proxy** path, and `test_tool_arguments_cannot_reach_the_wire_as_headers` pins that
   specifically.

   So the question was never "do we cross a line we drew", but "do we give the *less* trusted
   upstream kind a header channel it does not have". The answer is no, **permanently** rather
   than pending review: on a proxied server the upstream chooses the header *name* as well as
   the model choosing the value, on a request that carries the device's credentials. ADR-0010
   records what superseding it would require.

2. **HTTP+SSE inbound stays for one minor release** after Streamable HTTP inbound ships,
   announced in the CHANGELOG when it lands and removed in the following minor. This is what
   makes the deletion column above real rather than hypothetical.

3. **Advertise three revisions: `2026-07-28`, `2025-11-25`, `2025-06-18`.** Drop `2025-03-26`
   and `2024-11-05`, which are currently listed but have never been tested against —
   advertising an untested version is a claim we cannot back. `2025-11-25` is included because
   it is what an SDK-default client offers on the handshake path, so it negotiates without a
   fallback round trip. It costs a row in the verification matrix.

   *Corrected 2026-10-16.* This was first justified by 1.x reporting `2025-11-25` as
   `LATEST_PROTOCOL_VERSION`. In 2.x (`mcp-types` 2.3.0) that constant is `2026-07-28`; the
   handshake default moved to `LATEST_HANDSHAKE_VERSION`, still `2025-11-25`. A 2.x client
   probes `server/discover` at `2026-07-28` first and falls back to `initialize` offering
   `2025-11-25`, so the decision stands and the reason now names the right constant.
   Advertising it is a conformance claim, though — echo-if-supported means a client asking for
   `2025-11-25` is told we speak it — so the delta from `2025-06-18` has to be checked, not
   assumed, when the version list changes.

4. **Passthrough keeps polling; `subscriptions/listen` is not adopted.** Governance is a diff,
   not a notification — classifying a breaking change needs the before and the after
   regardless, so a notification would trigger work polling already does. Polling also cannot
   be silenced by an upstream that simply stops sending notifications, which matters for a
   source we do not trust. `tools.listChanged: false` stays honest.

5. **A fleet is a server-minted handle, addressed in the URL.** Recorded as
   [ADR-0027](adr/0027-stateless-requests-in-distributed-mode.md) §5. `POST /v1/fleets` mints a
   principal-bound handle over a fixed device set; `POST /v1/fleet/{handle}/mcp` serves it in
   both eras. Without `initialize` there is no moment to fix a fleet's membership, so the
   server fixes it when minting the handle instead. It lives in the URL rather than a tool
   argument because `tools/list` needs it and takes none. `/v1/fleet/mcp?devices=` stays for the
   handshake era only, deprecated in `0.4.0`.

6. **`transport: "sse"` is rejected in `0.4.0`, as a breaking change — not accepted as an
   alias.** Once HTTP+SSE is gone, the per-device `transport` field selects nothing: every
   device is served on `/v1/devices/{hostname}/mcp`. Accepting `"sse"` and ignoring it would
   keep a field that claims a choice which no longer exists, and would hide from a client that
   the transport it names is gone. So from `0.4.0`:

   - `POST`/`PUT /v1/devices` carrying `transport` is refused with a **400** that names the
     removal and points at `docs/upgrade.md`. This follows the `0.3.6` precedent for
     `expected_tls_spki_sha256`: a field the server would silently discard is refused instead.
   - **Stored records are migrated, not refused.** The field is dropped when a record is read,
     so an upgrade does not take every registered device offline.
   - **Restoring a pre-`0.4.0` archive drops the field and says so** in the restore preview, as
     a warning rather than a refusal. An archive is configuration, and refusing it would make
     every earlier backup unrestorable.
   - **Config:** `transport.default` and `transport.sse.*` are ignored, with a startup warning
     naming the keys. This is a warning, not a gate: stale config that selects nothing is not
     worth refusing to start over.
   - **Callers to update first:** the UI's BFF sends the current record's `transport` back on
     every device edit (`device-mcp-gateway-ui` `bff/app/routers/api.py`), so it must stop
     forwarding the field **before** the gateway reaches `0.4.0`, or every console edit will
     get the 400. `tools/loadtest/loadgen.py` registers with `"transport": "sse"` and drives
     `/sse`; it moves to `/mcp` in the same release.

## Traps, from what this codebase has already taught us

1. **Test against an implementation nobody here wrote.** The stateless-upstream bug in Phase 3
   survived a full suite because every stub was stateless too: the tests agreed with the
   implementation instead of checking it. The official SDK's server *and* client are the
   reference for both directions.
2. **Test the cold path.** Two of this feature's three real defects were on paths the tests
   skipped because a fixture pre-seeded state (a cached manifest; an already-set `spec_hash`).
   For Phase 6 that means: a first request from a client whose era is unknown, and an upstream
   whose era has never been probed.
3. **Era detection is per origin and must be cached** — and the cache must be invalidated when
   the assumption fails, or a server upgrading mid-flight wedges every device pointed at it.
4. **`resultType` is required on *every* result.** Missing it on one rarely-taken branch is a
   protocol violation a modern client may reject; grep the router for every `return` that
   builds a result, not just the common ones.
5. **The renumbered error codes are covered by `error-catalog.md`,** which a test keeps in sync
   — the doc must move with the code or the suite goes red.

## Verification

- The official MCP SDK's server and client at both eras, exercised in all four
  client/server-era combinations, with the two "Fails" cells asserted to fail *cleanly* and
  with an actionable message — not to hang.
- On the cluster, distributed mode, mixed fleet: an OpenAPI device and a proxied MCP device
  reached by a modern client and a legacy client at the same time.
- The existing suite must stay green throughout: dual-era means today's clients keep working,
  and any regression there is the feature failing at its stated purpose.

## Sizing

Larger than MCP passthrough was. Workstream A is a contained transport addition. Workstream B
touches the inbound router, the upstream client, the session layer and the error catalogue at
once, and its cost is dominated by supporting **two eras concurrently** rather than by either
era alone. Deletion of the legacy path is a later, separate, satisfying commit.
