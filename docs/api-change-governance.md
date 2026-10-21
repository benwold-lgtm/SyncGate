# API change governance

How the gateway handles two facts about the upstream APIs it fronts: their
**OpenAPI specs change over time** (F-41), and OpenAPI describes more than the
request/response `paths` the gateway translates (F-46).

---

## Tool-set change governance (F-41)

A device's MCP tool set comes from its upstream: **generated from an OpenAPI spec**, or
**discovered from a remote MCP server's `tools/list`** for a passthrough device
([ADR-0009](adr/0009-mcp-passthrough.md)). The gateway re-fetches on a schedule (and on
registration); when the fingerprint changes it re-pods the device and regenerates the tool
set. That means the tools a live MCP client sees can change underneath it — a tool can
disappear, a parameter can become required, an HTTP method can change. Without governance
that mutation is silent: a client keeps calling the old shape and starts
failing with no signal as to why.

**For a proxied MCP upstream this is a security control, not just an operability one.** An
OpenAPI document is a static file; a remote MCP server *authors its own contract* and can
answer the next poll differently — the rug-pull and tool-poisoning threats in
[threat-model.md](threat-model.md) §B5. The machinery below is the detection for both.

The gateway turns every spec-driven tool-set mutation into a recorded, classified
signal.

### What is recorded

On each tool-set change the gateway computes a diff between the old and new tool
sets and classifies it:

| Class | Meaning | Examples |
|-------|---------|----------|
| **compatible** | A client calling the old shape still works | tool added; optional parameter added; a parameter that was required is now optional; description changed |
| **breaking** | A previously-valid call can now be rejected | tool removed; parameter removed; parameter newly required; HTTP method changed |

The change is recorded three ways:

1. **Audit event** — `event="audit"`, `action="device.tools_changed"`,
   `outcome="breaking"` or `"compatible"`, with `added` / `removed` / `changed`
   tool names, the `breaking` flag, and human-readable `reasons`. It rides the
   tamper-evident audit stream (see [audit-logging.md](audit-logging.md)), so a
   breaking change is attributable and non-repudiable.
2. **Metric** — `mcp_device_tools_changed_total{hostname, breaking}` (Prometheus
   counter). A sustained `breaking="true"` rate means upstream APIs are changing
   under live clients — alertable (see [observability.md](observability.md)).
3. **Log** — a `WARNING` for a breaking change (naming the reasons), `INFO`
   otherwise.

### What "changed" means, per upstream kind

The comparison is against a stored **fingerprint** of the last-seen upstream, and how that
fingerprint is computed differs by kind — deliberately.

| Kind | Fingerprint | Why |
|------|-------------|-----|
| `openapi` | Hash of the parsed document | A static document parses in a fixed order, so this is stable |
| `mcp` | Hash of a **canonical projection** of `tools/list` — sorted by tool name, sorted keys, over `name` / `description` / `inputSchema` / `annotations` only | `tools/list` ordering is server-controlled. Hashing the raw response would make every poll of every proxied device look like a change: pod replaced each cycle, revision inflated, a breaking-change alert each time |

Both are written by one function so the baseline and the comparison cannot diverge. A
baseline computed under a different rule than the comparison is worse than no baseline —
it reports a change on the next poll, and every poll after it.

**The baseline is written when the manifest is built.** A device whose pod spawns from a
freshly fetched spec records the fingerprint at that moment; a poll that finds no baseline
seeds one and reports nothing, because a first sighting is not a change. (Recording one
would report a device's entire tool set as `added` on every worker restart.) This was a real
defect in distributed mode — the baseline was never written at all, so **no** change could
ever be detected; see the CHANGELOG.

### The client-facing signal: `tools_revision`

Each device carries a monotonic **`tools_revision`** counter, bumped once per
tool-set change (compatible or breaking). It is exposed on:

- `GET /v1/devices/{hostname}` — `DeviceDetail.tools_revision`
- `GET /v1/devices/{hostname}/diagnostics` — `DeviceDiagnostics.tools_revision`

A client (or a UI) detects "the tools moved under me" by polling one of those
and comparing `tools_revision` to the value it last saw; on a bump it re-issues
`tools/list`. A no-op spec edit (hash changed but the generated tool set is
identical) does **not** bump the revision — only a real tool-set change does.

### Seeing *what* changed: `GET /v1/devices/{hostname}/tools/diff`

The revision tells a client *that* the tools moved; this endpoint tells it *what*
moved. It returns the most recent classified change — `added` / `removed` /
`changed` tool names, the `breaking` flag, the `breaking_reasons`, and the
`tools_revision` that change produced (`ToolsDiffResponse`). `last_change` is
`null` when no change has been observed since registration (the tool set is as
first generated). Unlike `/tools` it does not require an active pod, so a UI can
show "the tools changed (and how)" even for a device that is currently down. The
record is the same diff written to the audit stream when the change was recorded;
it is persisted per device (cleared when the device is deleted).

### Why not a real-time `notifications/tools/list_changed` push?

The MCP layer advertises `tools.listChanged: false` in its `initialize`
response — honestly. There is no connection to push on: every request is answered
on its own response, and the stateless era (`2026-07-28`) has no session at all.
Its list results carry a `ttlMs` hint instead, so a modern client re-lists on a
known cadence. Rather than fake a push capability we
cannot reliably deliver across the distributed boundary, the governance signal
is the **recorded, classified change** plus the **pollable revision**. A future
inbound-notification bridge (see F-46 below) is the natural place a true push
would land.

### Operator guidance

- Alert on `rate(mcp_device_tools_changed_total{breaking="true"}[1h]) > 0` to
  catch upstreams breaking their contract under live clients.
- Forward the `device.tools_changed` audit records to your SIEM for a change log
  of every device's tool surface over time.
- Pin a flaky upstream by registering it against a **versioned spec URL**
  (e.g. `/v3/openapi.json`) so a vendor's `/openapi.json` rev doesn't silently
  re-pod it.
- **For a proxied MCP upstream, treat a breaking change as a security signal.** There is no
  spec-URL equivalent to pin against — the server *is* the contract, and it can change it.
  Review `GET /v1/devices/{hostname}/tools/diff` and the post-sanitisation descriptions in
  `GET /v1/devices/{hostname}/tools` before letting clients resume against it.
- Detection latency is one `registry.spec_poll_interval` (default 300s). Shortening it
  narrows the window in which a changed upstream is served unreviewed, at the cost of one
  discovery round trip per device per interval.

---

## Webhooks & callbacks are out of scope — the gateway is pull-only (F-46)

The translator generates MCP tools from a spec's request/response **`paths`**
only. Two OpenAPI constructs that describe **server-initiated** delivery are
deliberately **not** translated:

- **`webhooks`** (OpenAPI 3.1) — operations the upstream *calls on a registered
  receiver* when an event occurs.
- per-operation **`callbacks`** (OpenAPI 3.0/3.1) — out-of-band requests the
  upstream makes back to the caller after an operation (async completion,
  subscriptions).

### Why

The gateway is a **pull-only** request/response bridge: an MCP client invokes a
tool → the gateway calls the upstream → the response is returned. There is no
inbound HTTP surface on which an upstream could deliver an event, and MCP's
`tools/call` is request/response, not a subscription. Translating `webhooks` /
`callbacks` into tools would produce tools that can never be invoked
meaningfully, so they are omitted rather than surfaced as dead entries.

### Implications for integrators

- An API whose primary integration model is "register a webhook and receive
  push events" is **only partially usable** through the gateway: its synchronous
  `paths` work as tools; its event-push half does not.
- If an upstream offers **both** a webhook subscription *and* a polling endpoint
  (e.g. `GET /events?since=…`), use the polling endpoint — it translates to a
  normal tool and fits the pull model. Pagination/cursor signals on such
  endpoints are surfaced on the result envelope (see
  [error-catalog.md](error-catalog.md)).
- Long-running operations that complete asynchronously are surfaced as a poll
  handle on the result envelope (F-45), not via callbacks — the client polls the
  device's status endpoint rather than the device calling back.

### Future direction

The natural extension is an **inbound webhook → MCP notification bridge**: a
gateway ingress endpoint that receives an upstream's webhook delivery,
authenticates it, and fans it out to subscribed MCP clients as a server
notification. That requires the server→client push channel the current
transport intentionally does not provide (see the F-41 note above), so it is
tracked as future work, not a current capability.
