# ADR-0027: Stateless requests in distributed mode — results route per request, fleets are handles

- **Status:** Accepted (merged 2026-10-16, #226). Built in 0.4.0: §1–§4 in Workstream B PR 5, §5 in PR 6 (see the implementation notes)
- **Date:** 2026-10-16
- **Related:** [ADR-0002](0002-redis-control-plane.md) (extended), [ADR-0005](0005-at-least-once-with-idempotency-guard.md)
  (unchanged — see §4), [ADR-0008](0008-fleet-sessions.md) (partly superseded — see §5),
  [Phase 6 scope](../roadmap-protocol-2026-07-28.md) Workstream B
- **Related findings:** F-06, F-08, F-37

## Context

MCP revision `2026-07-28` removes the `initialize` handshake and `Mcp-Session-Id`. A client in
that era sends each request on its own, carrying its protocol version, capabilities and client
info in `_meta`, and expects the answer on the response to that POST. The gateway will serve
both eras through `0.4.x` ([roadmap](../roadmap-protocol-2026-07-28.md), decision 2 and
Workstream B). This record is about the part of that which is hard: **distributed mode**.

In distributed mode the session id is doing five jobs at once, and only one of them is about
sessions:

| Job | Where | Keyed on |
|---|---|---|
| **Result routing** — which Redis stream a worker publishes a response to | `worker/dispatch.py` → `SessionRouter.publish_result` | `session:{id}:results` |
| **Result correlation** — which entry on that stream answers *this* POST | `DistributedResultExchange` → `await_result` | the client's JSON-RPC `id`, within the session's stream |
| **Principal binding** (F-37) — a second caller cannot graft onto a session | `api/streamable.py`, `api/streamable_fleet.py` | session hash `owner` |
| **Fleet membership** — which devices a fleet session reaches, fixed at open so a later request cannot widen it | `api/streamable_fleet.py` | session hash `fleet_hosts` |
| **Lifetime** — TTL and refresh of all of the above | `SessionRouter.refresh` | `SESSION_TTL` (24 h) |

Take the session away and each job needs a new home, or it fails in a specific way.

The dangerous one is routing. The worker builds the results key from whatever `session_id` the
stream entry carries, and does not refuse an empty one. On the success path, an entry with no
session publishes to `session::results`, **one stream shared by every sessionless caller**,
which waiters then search by client-chosen JSON-RPC `id`. Stateless clients commonly number
requests from 1. Two callers with `id: 1` in flight on that stream can each receive the other's
result — a cross-principal disclosure, not a timeout. Today this cannot happen, because both
Streamable HTTP endpoints refuse a request without a session at the edge (400,
`"an empty one would put every sessionless caller's results in a single shared bucket"`). The
new era removes the premise of that guard: every request is sessionless by design.

The error paths fail the other way. The fingerprint-quarantine, no-pod, duplicate-suppressed
and dispatch-error results are published only `if session_id`, so a sessionless call that
fails is answered with silence and surfaces as a 504 after the exchange timeout.

The fleet loses its only enforcement point. ADR-0008 reads `devices` on `initialize` and never
again. Without `initialize` there is no point at which the set is fixed, and a `devices` query
parameter accepted on every request would let any request name any fleet, up to
`fleet_max_devices`.

## Decision

### 1. A stateless request is routed by its `request_id`, never by a session

Every distributed call already carries a gateway-minted `request_id` (uuid4,
`DistributedResultExchange`), and F-08 already keys on it. For a stateless request the reply
route is derived from it:

- The gateway puts an explicit **`reply_to`** field on the stream entry: `session:{id}:results`
  for a handshake-era request (today's key, unchanged), `reply:{request_id}` for a stateless one.
- The worker publishes to `reply_to` **and nothing else**. It stops constructing a key from
  `session_id`. An entry with no `reply_to` is a gateway bug: the worker dead-letters it with
  reason `no reply route` and publishes nothing — it never falls back to a shared key.
- `reply:{request_id}` is a **single-request stream**: `XADD MAXLEN 1`, with a short TTL
  (the exchange timeout plus the result-marker TTL, so a worker answering after the waiter has
  given up writes into a key that expires instead of accumulating). The waiting replica reads
  it from `0-0`, returns the entry, and deletes the key.

That also removes the ordering hazard `DistributedResultExchange` exists to manage. Today the
cursor must be read *before* publishing, or a fast worker's result is missed on a stream other
requests also write to. A key nobody else writes to, read from the beginning, has no "before".

**Mixed-version safety.** Gateway and worker run one image (`releasing.md` §4.3), but a rolling
upgrade (`upgrade.md`) restarts workers first, so for a window a new gateway can sit beside an
old worker. An old worker ignores `reply_to` and builds its key from `session_id`. So for a
stateless request the gateway also sets `session_id` to **`req:{request_id}`**. An old worker
then publishes to `session:req:{request_id}:results` — still unique to the request, never
shared — and the new gateway's waiter watches both keys. The second key is retired one release
later, once no supported worker predates `reply_to`. Correctness never depends on which worker
picked the entry up; only the TTL does (24 h on the old path, which is bounded and acceptable
for one roll).

### 2. Correlation is by key; the client's JSON-RPC `id` is echoed, never matched across callers

On the stateless path the JSON-RPC `id` is returned to the client unchanged and used for
nothing else. Two requests are never distinguished by an identifier their callers chose. This
is the invariant §1's key exists to make true, and the one the tests in §Verification pin.

### 3. Principal binding is per request, because there is nothing else to bind

A stateless per-device request carries no state between requests, so F-37's question — "is this
the principal that opened the session?" — has no subject. Every request already authenticates
and is authorised on its own (`require_scope`, per-principal rate limits), and the `subject`
rides the stream entry to the worker's audit record as it does today. Where state does persist
— a fleet — the binding moves to the handle (§5).

### 4. The idempotency guard (ADR-0005) is unaffected

F-08 keys `exec:{request_id}` and `result:{request_id}` on the gateway-minted `request_id`,
which a stateless request has just like a session-bound one. Nothing in ADR-0005 depended on
the session, and nothing in it changes. Recorded here because the coupling is easy to assume:
the guard sits next to the session-keyed publishing in `worker/dispatch.py`, but does not
share its key.

### 5. A fleet is a server-minted handle, addressed in the URL

The fleet's device set becomes a resource the server mints and the client names:

- **Mint:** `POST /v1/fleets` with `{"devices": ["host1", "host2", …]}`, scope `tools:call`,
  the same scope and `fleet_max_devices` cap that `initialize` applies today.
  It returns an opaque handle (`flt_` + 128 random bits): unguessable, though not a credential
  — every request still authenticates.
- **Use:** `POST /v1/fleet/{handle}/mcp` is an ordinary MCP endpoint, in **both** eras. A
  stateless request names the fleet by its URL; a handshake-era `initialize` on the same URL
  opens a session over the handle's devices. A client configures it once, like any other
  server URL.
- **Bound at mint, checked every request.** The device set and the minting principal are fixed
  when the handle is created. A request from any other principal gets **404**, not 403, so the
  endpoint is not an oracle for which handles exist. A request cannot add a device; only
  minting a new handle can.
- **Membership is re-resolved, never widened.** A member device that has been deregistered or
  has no reachable pod is skipped and logged, exactly as `initialize` skips one today. The
  aggregated `tools/list` is derived data, cached per handle and keyed on the members' manifest
  revisions, so an F-41 tool change on one member invalidates it. List results carry `ttlMs` so
  a modern client re-lists rather than holding a stale set.
- **Stored as configuration** in the registry backend — Redis in distributed mode, SQLite in
  embedded — so a handle survives a restart and a client's configured URL keeps working.
  `DELETE /v1/fleets/{handle}` (owner only) removes it, and `GET /v1/fleets` lists the caller's
  own.
- **`/v1/fleet/mcp?devices=…`** (ADR-0008's open-time binding over Streamable HTTP) keeps
  working in the handshake era through `0.4.x`, is deprecated in `0.4.0`, and is removed with
  the handshake era. A stateless request to it is refused with a 400 that points at
  `POST /v1/fleets`.

A handle in the URL rather than in a tool argument is deliberate. The spec's guidance is to pass
server-minted state "as ordinary tool arguments", which suits state a *tool* needs. The fleet is
needed **before** any tool is called — `tools/list` is the method that depends on it, and it
takes no tool arguments. Putting the handle in an argument would leave `tools/list` either
listing every device's tools (what `fleet_max_devices` exists to prevent) or nothing.

## Consequences

- **Positive:** the cross-caller disclosure in Context becomes structurally impossible on the
  new path, rather than guarded at one edge. Sessionless error results reach the client instead
  of timing out. The cursor-before-publish ordering argument disappears from the stateless
  path. A fleet becomes a configurable URL, which is what the clients ADR-0008 was written for
  (Claude Desktop, Cursor, `mcp-remote`) want anyway.
- **Negative / cost:** one Redis key per in-flight stateless call (single-entry, short TTL,
  deleted on read). Two reply paths and two fleet entry points coexist through `0.4.x`. Fleet
  handles are a new persistent object, with an API, RBAC, and a place in the registry store.
  The mixed-version `req:` shim is code that exists only to be deleted.
- **Follow-ups:**
  - **Backup/restore (ADR-0011):** handles are configuration and belong in an archive, or a
    restore breaks every configured fleet URL. Not built by this record; until it is, the
    restore preview must say handles are not carried.
  - **The UI's fleet surface** (`device-mcp-gateway-ui` BFF) moves to minting handles.
  - **Retire `session:req:` and `?devices=`** in the release after the handshake era is
    dropped, together with the session router itself.
  - **Embedded mode** needs §5 (handles) but not §1: the pod is in-process and `exchange()` is a
    call, so there is no reply route to choose.

## Verification

Written so a stub that agrees with the implementation cannot pass it (roadmap, trap 1):

- **The disclosure, directly.** Two principals, each sending stateless `tools/call` with
  `id: 1` to the same device concurrently on separate replicas, through a real-Redis stack.
  Each receives its own result, checked by content the device wrote, not by status code.
- **A worker never publishes to a shared key.** A stream entry with no `reply_to` is
  dead-lettered with `no reply route`, and `session::results` never exists afterwards — checked
  with a Redis read, not by the absence of an error.
- **Error results arrive.** Quarantine, no-pod, duplicate-suppressed and dispatch-error each
  answer a stateless request before the exchange timeout.
- **Rolling-upgrade window.** A new gateway with an old-image worker serves stateless requests
  correctly via `session:req:`.
- **Fleet handles.** A non-owner gets 404. A request cannot reach a non-member device. A
  deregistered member is skipped and `tools/list` reflects it. An F-41 change on a member
  invalidates the cached list.
- **On the cluster** (`kind-ibn`, distributed mode): the roadmap's mixed fleet — an OpenAPI
  device and a proxied MCP device, through one fleet handle, reached by a modern and a legacy
  client at once.

## Alternatives considered

- **Mint a synthetic session per stateless request and reuse the session path unchanged.** It
  is the mixed-version shim in §1, and it works. It was rejected as the destination because it
  leaves "session" naming a thing that no longer exists, keeps the 24 h TTL on a key that lives
  for one request, and keeps the cursor-ordering argument alive where it no longer needs to be.
  Kept as the one-release bridge for the reason it is good at: an old worker needs no change.
- **Correlate on the client's JSON-RPC `id` within a per-principal stream.** Narrows the shared
  bucket to one principal, but one principal's concurrent stateless requests — two agent threads
  sharing a token — still collide on `id: 1`. The client's identifier is not ours to depend on.
- **Pass the fleet handle as a tool argument**, as the spec suggests for server-minted state.
  Rejected in §5: `tools/list` needs the fleet and takes no arguments.
- **Accept `devices` on every stateless request and re-authorise it each time.** No state to
  keep, but every request may name a different fleet. That is the widening ADR-0008 fixed the
  set at open to prevent, and it makes the aggregated tool list a per-request computation over
  up to `fleet_max_devices` manifests.
- **Drop fleets from the stateless era** and let clients configure one URL per device. That is
  the N-connection problem ADR-0008 exists to solve.

## Implementation note (2026-10-21, Workstream B PR 5)

Built as decided, with one correction to §1, found while checking it against the documented
roll order.

**§1 said a worker dead-letters any entry without `reply_to`.** That is right for a new
gateway's entries, but [upgrade.md](../upgrade.md#rolling-upgrade-distributed-mode) rolls
**workers first**, so the window §1 worries about runs the other way round: a *new* worker
beside *old* gateways, whose entries carry a `session_id` and no `reply_to`. Dead-lettering
those would fail every handshake-era call for the length of the roll.

So the worker's rule is:

- **`reply_to` present:** publish there and nowhere else. It must name a reply stream or a
  session results stream; anything else is dead-lettered `invalid reply route`.
- **No `reply_to`, a `session_id`:** publish to `session:{id}:results`, which is what the entry's
  sender, an old gateway, is waiting on.
- **Neither:** dead-letter `no reply route`, publish nothing. The invariant §1 exists for holds
  either way: nothing is ever published to the shared `session::results`.

The route is checked **before** the call runs, not after. An entry with nowhere to answer
is not executed, so a non-idempotent call cannot take effect with its result discarded.

The other direction (a new gateway beside an old worker) is covered as §1 says: a stateless
entry also carries `session_id = req:{request_id}`, and the gateway waits on both keys.

## Implementation note (2026-10-21, Workstream B PR 6): fleet handles

§5 is built as decided — `POST`/`GET`/`DELETE /v1/fleets`, `POST /v1/fleet/{handle}/mcp` in both
eras, 404 for anyone but the owner, Redis or SQLite storage — with three additions and one
difference:

- **Unregistered devices are refused at mint** (400 naming them), rather than skipped. A typo
  would otherwise produce a fleet that is silently one device short for its whole life.
  Membership is still re-resolved on every request, so a member deregistered *later* is
  skipped then, as §5 says.
- **A per-principal cap on handles** (`registry.fleet_handles_per_principal`, default 100).
  Handles are persistent configuration; a token minting in a loop would otherwise grow the
  store without bound.
- **`?devices=` responses carry `Deprecation: true`** and a `Link` to `/v1/fleets`, and a
  stateless request to it is a 400 (`-32600`) whose message names `POST /v1/fleets`.
- **The aggregated `tools/list` is not cached.** §5 called for a per-handle cache keyed on the
  members' manifest revisions. Each request rebuilds it from the members' cached manifests
  instead — the same work the handshake fleet already did per request — so an F-41 change or a
  deregistration is reflected on the very next request, and there is no cache to invalidate.
  A cache stays an optimisation available if a large fleet ever shows the cost.

Backup: the restore report's new `not_carried` list says fleet handles are not in an archive,
as §5's follow-up requires until they are.
