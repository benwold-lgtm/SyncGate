# OpenAPI → MCP Translation Contract

How the gateway turns an upstream device's OpenAPI 3.0/3.1 spec into the MCP tools,
resources, and prompts an LLM client sees — the naming, parameter, request-body,
validation, and error-mapping rules. This is the contract a device author can rely on
and an operator can reason about. The implementation lives in
[`core/translator.py`](../device_mcp_gateway/core/translator.py),
[`core/adapter.py`](../device_mcp_gateway/core/adapter.py), and
[`core/errors.py`](../device_mcp_gateway/core/errors.py).

> **Trust note.** Device-supplied spec text (summaries, descriptions, titles) becomes
> LLM-facing tool metadata and is **untrusted** (Tier-0 F-26). The translator strips
> control/bidi/zero-width characters and length-caps it, but cannot neutralize plain
> semantic prompt injection — MCP clients should still treat tool descriptions as
> device-provided content.

## What gets generated

| OpenAPI element | Becomes | Notes |
|-----------------|---------|-------|
| `GET/POST/PUT/DELETE/PATCH` operation | one **MCP tool** | other methods (HEAD, OPTIONS, TRACE) are skipped |
| `GET` operation | also an **MCP resource** | read-only, `device://{hostname}{path}` |
| `info.title`/`info.description` | an **MCP prompt** | a "what does this device do" template listing tool names |

The spec is validated (`openapi-spec-validator`) before translation; an invalid spec is
rejected with `ValueError`. A spec with an absurd operation count is refused up front
(F-09) before the expensive validation/translation runs.

## Tool naming

1. Base name = `operationId` if present, else `{method}_{path}`.
2. **Sanitized**: non-`[A-Za-z0-9_]` → `_`, runs of `_` collapsed, lowercased, leading/trailing `_` trimmed. Example: `getSensor-Reading` → `getsensor_reading`; `GET /sensors/{id}/temp` → `get_sensors_id_temp`.
3. **Collisions** (two operations sanitizing to the same name) get a numeric suffix — `name`, `name_2`, `name_3`, … — and a warning is logged. The first one keeps the bare name.

Because `operationId` drives the name, **a stable, unique `operationId` per operation gives stable, readable tool names**; relying on the path fallback makes names sensitive to path edits.

## Fleet sessions: naming and dispatch

A fleet (`POST /v1/fleet/{handle}/mcp` after `POST /v1/fleets`, or the deprecated
handshake-only `POST /v1/fleet/mcp?devices=a,b,…`) aggregates several devices' tools behind
one MCP endpoint. The contract:

- **Display name** = sanitize(`{hostname}_{tool_name}`) using the same sanitizer as
  above — `my-sensor.local`'s `get_readings` appears as `my_sensor_local_get_readings`.
- **Determinism**: hostnames are processed in sorted order, so the same device set always
  yields the same names. A cross-device collision (possible after sanitization) gets the
  numeric-suffix treatment (`name`, `name_2`, …) with a warning logged.
- **Dispatch**: each display name maps back to `(hostname, real tool name)`; a
  `tools/call` is rewritten to the device's real tool name and routed to that device —
  same admission control, timeout watcher, and audit as a per-device call.
- **Availability**: a hostname that is unregistered or whose pod is down at session open
  is *skipped* (logged) rather than failing the whole session. In embedded mode the
  manifest is rebuilt against the originally requested hostnames on every `tools/list`,
  so a skipped device joins the session once it comes up, and tool-set changes on pod
  replace are picked up mid-session.
- **Cap**: `registry.fleet_max_devices` (default 25) bounds one session's device count.

## Parameter mapping

All `path`, `query`, and `header` parameters are flattened into a **single JSON-Schema
`properties` object** (the tool's `inputSchema`). Each argument's source is tracked in
`param_locations` so the adapter routes it to the right place on the upstream call.

| OpenAPI `in:` | Exposed as a tool argument? | Routed to |
|---------------|------------------------------|-----------|
| `path` | yes | interpolated into the URL path template |
| `query` | yes | URL query string |
| `header` | yes | request header (subject to the header-injection denylist, F-25) |
| `cookie` | **no** (skipped) | — |
| request body | yes — flattened (see below) | request body |

### Name collisions across locations (F-04)

OpenAPI allows the same name in two places (e.g. `id` in `path` **and** in the body).
Flattening naively would let one silently overwrite the other, so:

- **Path params claim their bare name** (the `{placeholder}` must match literally).
- A colliding `query`/`header`/body param is exposed under a suffixed key — `id__query`, `id__body`, etc. — and `param_wire_names` maps that MCP arg back to the real upstream wire name, so the call still routes correctly. The renaming is logged.

A `required` param becomes a `required` entry in the schema — but only if its property
actually survived (a dropped/renamed property is never left dangling in `required`, F-47).

## Request body encoding

A tool is a single callable, so the translator selects **one** request-body content type,
in priority order (F-40):

1. `application/json`
2. `application/x-www-form-urlencoded`
3. `multipart/form-data`
4. otherwise the first declared type (treated as a **raw** body)

- **Object body** → each top-level property is flattened into a body argument (merged into the same flat `properties`). Properties typed `string` with `format: binary`/`byte` are tracked as `binary_fields` so a multipart body sends them as file parts.
- **Composed body** (`oneOf`/`anyOf` at the top level) → the union of branch properties is exposed as flat args; `required` is not carried across alternatives.
- **Non-object/scalar body** (e.g. `application/octet-stream`, `text/plain`) → a single `body` argument is exposed and sent raw, rather than being dropped.

## Schema resolution

Each parameter/body schema is resolved recursively before it's emitted:

- **`$ref`** — any internal pointer (`#/components/schemas/...`, `.../parameters/...`, `.../requestBodies/...`) is resolved against the spec root. **External** (file/URL) refs are unsupported → replaced with `{}` and a warning. A `$ref` cycle is broken (returns `{"type": "object"}`).
- **`allOf`** — merged (union of properties and `required`), per its JSON-Schema meaning.
- **`anyOf`/`oneOf`** — **preserved** as composition (not flattened to a union), so "exactly one of"/alternatives semantics survive into the emitted schema.
- **`nullable: true`** (OpenAPI 3.0) — normalized to a JSON-Schema nullable type (`"type": ["string", "null"]`, or a `{"type": "null"}` branch added to a combiner).
- **Nested** `properties`, array `items` (including tuple/positional item lists), and object `additionalProperties` (the schema form, not the bool) are all descended, so a `$ref` nested inside them is resolved too (fixed in 0.1.2).
- `enum`, `format`, `description`, `default`, etc. pass through unchanged — the emitted schema is meaningful to the client, not `unknown`.

## Argument validation

Before dispatch, the pod validates the call's arguments against the tool's generated
JSON Schema with a **Draft 2020-12** validator (Tier-0 F-28,
[`device_pod.py`](../device_mcp_gateway/pods/device_pod.py)):

- Valid → the call proceeds upstream.
- Invalid → a JSON-RPC `-32602 invalid_params` error (see below); the upstream device is **not** called.
- **Fail-open on a bad schema**: if the *generated schema itself* isn't valid JSON Schema (some heavily-flattened specs aren't), validation is skipped with a warning rather than blocking every call — the upstream device remains the backstop.

## Error mapping

Failures are surfaced in two layers, both catalogued in
[`core/errors.py`](../device_mcp_gateway/core/errors.py) (and mirrored in
[`docs/error-catalog.md`](error-catalog.md), kept in sync by a test). A correlating
request id (`rid`, also in the access log) rides along so a client failure can be traced
to server logs.

**Protocol layer — JSON-RPC error codes** (failures before/around dispatch):

| Code | `reason` | When |
|------|----------|------|
| `-32601` | `method_not_found` | unknown MCP method or tool name |
| `-32602` | `invalid_params` | arguments failed schema validation, or a malformed resource URI |
| `-32000` | `internal_error` | the tool handler raised unexpectedly |
| `-32001` | `no_worker` | call accepted but no worker served it in time (distributed) |
| `-32002` | `duplicate_suppressed` | a redelivered non-idempotent call was not re-run (F-08) |

**Application layer — tool-result envelope `error.type`** (the call reached the device path):

| `error.type` | When |
|--------------|------|
| `http_error` | upstream returned **status ≥ 400** |
| `response_too_large` | upstream body exceeded the response-size cap (F-27) |
| `circuit_open` | the device's breaker is open; the call was short-circuited |
| `timeout` | the request to the device timed out |
| `connection_error` | DNS/connection/TLS failure reaching the `base_url` |
| `internal` | unexpected gateway-side error handling the call |

Key behaviors:

- **An upstream `≥ 400` is returned as an error envelope, never as a successful tool result** (F-39) — the LLM sees a failure, not a 404 body masquerading as success.
- **`4xx` vs `5xx`**: a `5xx` (or connection failure) trips the per-device circuit breaker; a `4xx` is treated as a client/LLM error and does **not** affect breaker state.
- Response bodies are size-capped before buffering back to the client (F-27); tool arguments can never set denylisted upstream headers (F-25).

## Resources and prompts

- Every `GET` operation also becomes a read-only **resource** at `device://{hostname}{path}`. `resources/read` is traversal-guarded.
- One **prompt** per device is generated from `info.title`/`info.description`, listing the available tool names — a "what can this device do" primer for the client. It is served by `prompts/list` and `prompts/get` (advertised in `initialize` as `prompts: {listChanged: false}`), rendered by the gateway with no device call. A spec with no `info.description` yields no prompt, so `prompts/list` answers an empty list.

## What is *not* translated

- **`webhooks` / `callbacks`** — the gateway is **pull-only** (request→response); there is no inbound event surface. If you need push, run a separate event bridge that turns device events into tool calls or notifications out-of-band; don't expect the gateway to receive callbacks.
- **Long-running operations** (`202 Accepted` + job-poll) — calls are synchronous; model a poll as a separate tool.
- **The interactive `authorization_code` OAuth2 grant and `jwt-bearer`** device auth — see the README Authentication section for the supported grants.

## Tool-set change governance

The generated tool set changes when the upstream spec changes. Every change is classified
**compatible** vs **breaking**, recorded to the audit stream and the
`mcp_device_tools_changed_total` metric, and exposed to clients as a monotonic
`tools_revision` on `GET /v1/devices/{hostname}`. To see *what* changed, read
`GET /v1/devices/{hostname}/tools/diff` (added/removed/changed tool names, the breaking
flag, and reasons). A removal, a parameter becoming required, or a method change is
**breaking**; adding a tool or an optional parameter is **compatible**. See
[`docs/api-change-governance.md`](api-change-governance.md).

## Worked example

Given this operation:

```yaml
paths:
  /sensors/{sensor_id}/readings:
    get:
      operationId: getSensorReadings
      summary: Read recent samples for a sensor
      parameters:
        - { name: sensor_id, in: path, required: true, schema: { type: integer } }
        - { name: limit, in: query, required: false, schema: { type: integer, default: 10 } }
        - { name: unit, in: query, schema: { type: string, enum: [c, f], nullable: true } }
```

The generated MCP tool is:

```json
{
  "name": "getsensorreadings",
  "description": "Read recent samples for a sensor",
  "method": "GET",
  "path": "/sensors/{sensor_id}/readings",
  "inputSchema": {
    "type": "object",
    "properties": {
      "sensor_id": { "type": "integer", "description": "" },
      "limit": { "type": "integer", "default": 10, "description": "" },
      "unit": { "type": ["string", "null"], "enum": ["c", "f"], "description": "" }
    },
    "required": ["sensor_id"]
  }
}
```

Calling it with `{"sensor_id": 1, "limit": 5}` → `GET /sensors/1/readings?limit=5`. Calling
it with `{"limit": 5}` → `-32602 invalid_params` (missing required `sensor_id`); the device
is not called.
