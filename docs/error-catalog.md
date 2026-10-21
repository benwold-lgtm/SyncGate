# Error Catalog

Every error the gateway surfaces falls into one of two layers. Both carry a stable
machine-readable identifier so a client can branch on the cause instead of parsing
prose, and correlate a failure with the server logs.

The catalog is defined in code at [`device_mcp_gateway/core/errors.py`](../device_mcp_gateway/core/errors.py)
and this document is kept in sync with it by a test (`tests/test_errors.py`). If you add
or change an entry there, update this file.

---

## 1. Protocol layer — JSON-RPC error codes

Returned on the MCP channel when a call fails *before or around* dispatch (bad request,
no worker, internal fault). Shape:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "error": {
    "code": -32001,
    "message": "Tool call timed out after 30s — no worker responded",
    "data": { "reason": "no_worker", "rid": "a1b2c3", "request_id": "…" }
  }
}
```

- `error.data.reason` — stable slug (branch on this, not the message).
- `error.data.rid` — the gateway request id, also printed in the access log
  (`… rid=<rid>`), so you can find the matching server-side log line. Omitted when unknown.
- `error.data.request_id` — internal call-correlation id (distributed mode), present on
  the timeout/no-worker errors so you can trace the call across gateway and worker.

| Code | reason | Meaning | Likely cause |
|------|--------|---------|--------------|
| `-32601` | `method_not_found` | Unknown MCP method or tool name. | The tool isn't in the device manifest, or an unsupported MCP method was called. |
| `-32602` | `invalid_params` | Request arguments failed validation. | Missing/extra/wrong-typed tool arguments, or a malformed resource URI/path. |
| `-32000` | `internal_error` | The tool handler raised an unexpected error. | A gateway/pod bug or an unhandled device response — correlate with `rid` in the logs. |
| `-32001` | `no_worker` | The call was accepted but no worker served it in time. | No worker owns the device, the owning worker died, or it is saturated/slow (distributed mode). |
| `-32002` | `duplicate_suppressed` | A duplicate delivery of a non-idempotent call was suppressed (F-08). | The call was redelivered (a worker died/shed the device mid-flight) after a prior attempt had begun; re-running a non-idempotent operation could double-apply, so it was not retried. Retry explicitly if the operation is safe to repeat. |

`-32601` and `-32602` are standard JSON-RPC 2.0 codes; `-32000`/`-32001`/`-32002` are in the
JSON-RPC server-defined range. MCP `2026-07-28` reserves `-32020…-32099` of that range for the
spec itself, and the gateway's own codes sit below it.

### Stateless-era refusals (`2026-07-28`)

A request in the stateless era (an `MCP-Protocol-Version` header naming a modern revision, or a
`_meta` envelope) is checked before it reaches a device, in this order. These come back as the
**response to the POST**, with the HTTP status shown, and carry no `error.data.reason`: they are
the protocol's own codes, and the SDK's table decides the status.

| Code | HTTP | Meaning | Likely cause |
|------|------|---------|--------------|
| `-32602` | 400 | The `_meta` envelope is missing or incomplete. | The request has no `io.modelcontextprotocol/protocolVersion` or `…/clientCapabilities` in `params._meta`. |
| `-32020` | 400 | **Header mismatch.** A routing header disagrees with the body. | `MCP-Protocol-Version` missing or different from the envelope's, `Mcp-Method` ≠ `method`, `Mcp-Name` ≠ the tool, prompt or URI, or one of them sent twice. |
| `-32022` | 400 | **Unsupported protocol version.** `error.data` is `{"supported": [...], "requested": "..."}`. | The client asked for a revision the gateway does not serve. An auto-negotiating client uses `supported` to retry or fall back. |
| `-32601` | 404 | The method does not exist in this era. | `initialize`, `ping`, `logging/setLevel` and `resources/subscribe` were removed in `2026-07-28`. |
| `-32602` | 400 | An unknown tool name. | In this era `-32601` is a 404, which a client may read as "this endpoint has no `tools/call`", so an unknown *tool* is answered as invalid params. |

---

## 2. Application layer — tool-result envelope `error.type`

When a tool call *reaches the device path* but the device (or the call to it) fails, the
result is a normalized envelope (see the [adapter](../device_mcp_gateway/core/adapter.py)),
not a JSON-RPC error — so the model gets a structured, inspectable result:

```json
{ "ok": false, "status": 503, "error": { "type": "circuit_open", "message": "…" } }
```

Branch on `error.type`:

| `error.type` | Meaning | Likely cause |
|--------------|---------|--------------|
| `http_error` | Upstream device returned an HTTP error (status >= 400). | A 4xx is usually a bad request/auth; a 5xx is a device-side fault. |
| `response_too_large` | Upstream response exceeded the size cap and was not buffered. | The device returned a body larger than the gateway's response limit. |
| `circuit_open` | The device's circuit breaker is open; the call was short-circuited. | Repeated 5xx/connection failures tripped the breaker; it resets after a cooldown. |
| `timeout` | The request to the device timed out. | The device was slow or unresponsive within the request timeout. |
| `connection_error` | Could not connect to the device. | DNS failure, connection refused/reset, or TLS error reaching the `base_url`. |
| `internal` | Unexpected gateway-side error while handling the call. | A gateway/pod bug or an unhandled device response — check server logs via `rid`. |

A successful call returns `{ "ok": true, "status": <2xx>, "body": <parsed> }`.

### Pagination (F-48)

When the device's response carries header-based pagination — an RFC 5988 `Link`
header or a known cursor/total header — the success envelope gains a `pagination`
object so the next page is reachable instead of invisible (only the body would
otherwise be returned):

```json
{
  "ok": true,
  "status": 200,
  "body": [ ... ],
  "pagination": {
    "next_url": "https://api.example.com/items?page=2",
    "links": { "next": "...", "last": "..." },
    "next_cursor": "eyJpZCI6MTB9",
    "total": "500",
    "has_more": true
  }
}
```

Fields are present only when the corresponding signal exists. `next_url`/`links`
come from the `Link` header; `next_cursor` from the first of `X-Next-Cursor`,
`X-Next-Page`, `Next-Cursor`, `X-Cursor`, `X-Page-Token`; `total` from
`X-Total-Count`/`X-Total`/`X-Total-Pages`. `has_more` is true when a next page is
reachable. Body-embedded cursors are **not** parsed (too vendor-specific) — they
already ride in `body` for the model to read.

### Long-running operations (F-45)

The gateway serves a tool call synchronously, so it can't wait out a slow upstream
job. When the device returns an **accepted-but-incomplete** async operation, the
success envelope gains an `operation` handle so the model can poll for completion
rather than treating the call as done:

```json
{
  "ok": true,
  "status": 202,
  "body": { "...": "..." },
  "operation": {
    "status": "pending",
    "poll_url": "https://api.example.com/operations/abc123",
    "retry_after": "5"
  }
}
```

Triggers on `202 Accepted` or an `Operation-Location` header (the Azure async
pattern). `poll_url` is where to check status — `Operation-Location`, or `Location`
on a 202 (a 201's `Location` is the created resource, not a job, so it is ignored).
`retry_after` echoes the server's `Retry-After` hint. The model continues by
calling the device's status/poll endpoint (or `resources/read` on the URL). The
gateway does **not** poll server-side — that would hold a worker for the operation's
full duration; a bounded server-side poll is a possible future enhancement.

---

## Telling the failure modes apart

| Symptom | Where it shows | What it means |
|---------|----------------|---------------|
| `no_worker` (JSON-RPC `-32001`) | error event on the SSE stream | The gateway accepted the call but no worker served it before the timeout. |
| `duplicate_suppressed` (JSON-RPC `-32002`) | error event on the SSE stream | A non-idempotent call was redelivered after a prior attempt began; the guard refused to re-run it to avoid a double side effect (F-08). |
| `circuit_open` (envelope) | tool result | The worker reached the device path, but the breaker is open from prior failures. |
| `http_error` (envelope) | tool result | The device answered with a >= 400 status (the body is included). |
| `connection_error` / `timeout` (envelope) | tool result | The device couldn't be reached / didn't answer in time. |
| `invalid_params` (JSON-RPC `-32602`) | tool result | The arguments were rejected before any device call was made. |
