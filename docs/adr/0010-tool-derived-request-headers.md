# ADR-0010: Tool-derived request headers — adopt SEP-2243 for OpenAPI, exclude passthrough permanently

- **Status:** Accepted
- **Date:** 2026-08-07
- **Related findings:** F-25 (tool-call header injection), F-02 (SSRF), F-26 (untrusted text)
- **Relates to:** [ADR-0009](0009-mcp-passthrough.md) (the trust asymmetry this decision rests
  on), and the Phase 6 scope in [roadmap-protocol-2026-07-28.md](../roadmap-protocol-2026-07-28.md)

## Context

MCP revision `2026-07-28` introduces **SEP-2243 `x-mcp-header`**: a tool may declare that one
of its parameters is carried as an HTTP header on the outbound request, rather than in the
body. The gateway has two upstream kinds, and they start from opposite positions.

**The OpenAPI path already does this.** An operation can declare an `in: header` parameter,
and `DevicePod` maps it onto the wire. F-25 does not forbid that — it constrains it, in
`_sanitize_header_params`: a **denylist** of 20 reserved headers (`authorization`,
`proxy-authorization`, `cookie`, `host`, `content-length`, the `x-forwarded-*` family, and the
hop-by-hop set) is dropped, and any value containing CR or LF is dropped. Auth headers are
applied *after* tool-derived ones, so a tool argument cannot override the device's credentials.

**The passthrough path deliberately does not.** Outbound headers on a proxied call are built
solely from `auth.apply()` plus fixed protocol headers, and
`test_tool_arguments_cannot_reach_the_wire_as_headers` pins that. No tool argument reaches the
wire as a header at all.

That asymmetry is not an oversight, and it is the whole of this decision. The two upstream
kinds differ in **who authors the tool contract**:

| | OpenAPI device | Proxied MCP server |
|---|---|---|
| Who writes the tool schema | the operator's own API, described by a spec they fetched and can review | **the upstream itself**, at `tools/list`, changeable after review |
| What the gateway can check | the spec is a document, fetched once and hashed; a change is a governed diff | the same governance detects a change — *after* it has happened |
| Trust position | a service the operator chose to expose | ADR-0009: "a *less* trustworthy source than an OpenAPI document, not more" |

## Decision

**Adopt SEP-2243 on the OpenAPI path. Exclude it from the passthrough path permanently.**

The exclusion is a **standing design constraint, not a deferral.** It is recorded here so that
a future contributor reading "the spec supports this and we don't" finds a reason rather than a
gap, and so that closing the gap requires superseding this ADR rather than filing a chore.

### Why this is not "revisit later"

A deferral would be the wrong shape, because nothing about it improves with time. The reason
to refuse is not that the implementation is hard or that the spec is young — it is structural:

- **The header value would be LLM-controlled and the header *name* upstream-controlled.** On
  the OpenAPI path the operator's own spec fixes which header a parameter maps to, and the
  model supplies only the value. Under SEP-2243 on a proxied server, the upstream's tool schema
  chooses the header slot too. That is a different mechanism wearing the same name.
- **It targets an authenticated request.** The gateway attaches the device's credentials to
  proxied calls. Handing an untrusted party influence over headers on that request is a
  credential-adjacent decision, not a compatibility one.
- **Detection is not prevention** — the recurring theme of ADR-0009 and threat-model §B5. Tool
  change governance would flag a newly added `x-mcp-header` on the *next* poll. The window
  between an upstream adding it and the diff firing is exactly the window that matters.
- **A denylist cannot be sound here.** F-25's reserved set is adequate for a header slot the
  operator chose. It is not adequate against an adversary choosing the slot: the interesting
  headers are the ones nobody thought to list, and they vary by whatever sits in front of the
  upstream (a proxy, a WAF, a service mesh, a cloud LB).

### What we give up

Spec-completeness on one feature for one upstream kind, and it should be stated plainly rather
than glossed:

- A proxied upstream that *requires* `x-mcp-header` for some tool will have that tool fail, or
  work only if the same value can travel in the body. The gateway does not fake it.
- `server/discover` must not advertise support we do not provide on that path.
- The two upstream kinds now differ on a visible protocol feature. That divergence is the point
  — the kinds differ in trust, so they should differ in capability — but it is a real cost and
  belongs in the docs, not only here.

### If it is ever revisited

Superseding this ADR should require all of: a concrete upstream that needs it, a **per-device
operator opt-in that is off by default**, an **allowlist of header names supplied by the
operator** (never by the upstream), the F-25 reserved set unreachable regardless of the
allowlist, and the opt-in surfaced in the UI and the audit trail. Anything less reintroduces
the mechanism this ADR refuses.

## Consequences

- `_sanitize_header_params` stays the single chokepoint on the OpenAPI path and gains the
  SEP-2243 mapping. The reserved denylist and CRLF rejection continue to apply to it.
- `test_tool_arguments_cannot_reach_the_wire_as_headers` is promoted from an incidental
  regression test to the **executable statement of this ADR**, and should say so in its
  docstring. It must not be weakened without superseding this record.
- The Phase 6 verification matrix gains a case: a proxied upstream advertising `x-mcp-header`
  is accepted, its tool is callable, and the header does not appear on the wire.
- Documentation must state the divergence where a user meets it — the passthrough section of
  the README and the tool-contract description in `tooling.md`.

## Implementation note (2026-10-21, 0.4.0): the proxy path in the stateless era

`2026-07-28` makes `x-mcp-header` a two-way contract: a client mirrors an annotated argument into
an `Mcp-Param-*` header, and a server **rejects** a call whose header is missing or disagrees
(`-32020`). This record keeps the proxy path without a header channel, so in 0.4.0:

- **Inbound**, a proxied tool's schema is advertised with its `x-mcp-header` annotations
  stripped, so a conforming client sends no `Mcp-Param-*` headers to the gateway for it.
- **Outbound**, a tool on a stateless-era upstream whose schema is annotated is **not offered**
  at all, and discovery logs a warning naming it. That upstream would refuse every call the
  gateway can make to it, and offering the model a tool that always fails is worse than
  leaving it out. On a handshake-era upstream the annotation means nothing, so the tool is
  kept, with the annotation stripped.
- The `Mcp-Name` and `Mcp-Method` headers the gateway *does* send are routing headers, not
  this channel: they repeat the method and the tool, prompt or URI already in the body, never
  an argument. `test_the_stateless_routing_headers_are_sent_and_no_argument_ever_is` pins that.

Superseding this record to forward `Mcp-Param-*` headers is now concretely what it would take
to use such a tool through the proxy. The case is narrower than the one this record weighed:
the header name is always prefixed `Mcp-Param-`, so it cannot collide with `Authorization` or a
custom credential header.
