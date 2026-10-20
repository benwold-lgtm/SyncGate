# ADR-0028: A proxied MCP server's resources and prompts — listed from the manifest, fetched upstream

- **Status:** Accepted
- **Date:** 2026-10-20
- **Supersedes:** [ADR-0009](0009-mcp-passthrough.md)'s "v1 proxies tools only" (resources and
  prompts only; stdio upstreams and SSE upstream transport stay unbuilt)
- **Related findings:** F-09 (count cap), F-26 (text sanitisation), F-08 (idempotency), ADR-0015 §9
  (quarantine)

## Context

ADR-0009 proxied a remote MCP server's tools and nothing else. Its reason for leaving resources
and prompts out was specific: "minting a second client-visible URI shape would create another
thing to migrate under a future tenancy model, for no capability anyone has asked for yet."

The capability has now been asked for. Device inspection (`GET /v1/devices/{h}/features`, the
console's device detail, `device-mcp devices show`) is a baseline requirement covering tools,
resources **and** prompts for every device, and for a proxied server two of the three were
reported as "not discovered".

The objection was to the gateway **minting** URIs, not to resources as such. And the trust
question is the same one ADR-0009 already answered for tools: the upstream is less trustworthy
than an OpenAPI document. Its descriptions are instructions the model reads, and it can rewrite
them at any time.

## Decision

Resources and prompts follow the tools model exactly.

1. **Discovered the way tools are.** One routine, `snapshot_upstream`, serves embedded discovery,
   the worker's health loop and its spawn path. It lists resources and prompts **only when the
   upstream advertises the capability**; a server that does not claim one has none, and that is
   a discovered fact. A failure to list one kind costs only that kind, never the tools, and is
   recorded per device in the manifest as `discovered`.
2. **Sanitised and capped the way tools are.**
   - Names are sanitised and deduped, and the upstream's own prompt name is kept for the wire.
   - Every description goes through F-26.
   - Over the count cap, a kind is *not listed and reported as not discovered*, instead of
     rejecting the device: a device with working tools is not made unusable by its resource
     count.
3. **URIs are the upstream's own, verbatim.** The gateway mints nothing, which removes ADR-0009's
   objection rather than overriding it. A URI that sanitisation would change is **dropped**, not
   rewritten, since a "cleaned" URI is one the upstream does not know.
4. **Lists from the manifest; content forwarded.** `resources/list` and `prompts/list` are served
   from the sanitised manifest, as `tools/list` is. `resources/read` and `prompts/get` are
   forwarded and their results returned untouched, as a tool result is. **Only a URI or prompt
   the manifest lists can be fetched**, so a client cannot use the gateway to reach anything
   discovery did not see.
5. **Same guards as a tool call.**
   - The same breaker and token bucket apply.
   - `prompts/get` joins `tools/call` and `resources/read` in the ADR-0015 quarantine set, since it
     too carries the device's credentials upstream.
   - Both are reads, so F-08's idempotency guard re-runs them freely.
   - The upstream's JSON-RPC error codes are untrusted and their numbering is not ours. They are
     reported in the message and mapped onto `-32602` or `-32000`.
6. **Change detection stays upgrade-stable.** The device's hash covers the whole snapshot, but is
   **byte-identical to the old tools-only hash when an upstream lists no resources and no
   prompts**. Upgrading therefore changes nothing for most MCP devices, and replaces the pod once
   only for those that really serve resources or prompts. A resource- or prompt-only change
   replaces the pod but records no tool change, because `tools_revision` and the F-41 diff are
   still about tools.

## Consequences

- **Positive:** inspection is complete for every device kind. Proxied resources and prompts
  become usable by MCP clients through the gateway with the protections tools already have. The
  gateway still mints no URI shape.
- **Negative / cost:** two more upstream round trips per discovery cycle, and only for a server
  that advertises the capability. A proxied prompt's text is the upstream's, fetched live, so it
  reaches the model unsanitised, exactly as a tool *result* always has. The sanitised listing
  bounds what the model is offered, not what an offered prompt returns.
- **Follow-ups:**
  - `resources/templates/list` is not proxied.
  - List pagination (`nextCursor`) is not followed, matching `tools/list` today.
  - `listChanged` notifications are not adopted (roadmap decision 4: governance polls).
  - Fleets still serve tools only (ADR-0008).

## Alternatives considered

- **Forward `resources/list` and `prompts/list` live.** This was rejected for the reason ADR-0009
  rejected it for tools: it hands the upstream a direct, unsanitised channel to the model on every
  list, and makes each list a round trip.
- **Mint gateway URIs (`device://{host}/…`) for proxied resources.** This is the shape ADR-0009
  objected to: a second client-visible scheme to migrate later, and a translation table that can
  drift from the upstream.
- **Reject a device whose upstream lists too many resources or prompts.** That is consistent with
  the tool cap, but it would take away working tools over an optional feature.
