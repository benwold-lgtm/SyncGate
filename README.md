# Device MCP Gateway

**One governed [MCP](https://modelcontextprotocol.io/) endpoint for a whole fleet — whatever the upstream speaks.**

Point the gateway at a REST service and it auto-discovers the OpenAPI spec and translates every
operation into an MCP tool. Point it at a server that **already speaks MCP** and it federates
that server's tools instead. Either way an LLM client (Claude Desktop, Cursor, a custom agent)
connects to one endpoint over SSE, and the same control plane applies: authentication, RBAC
scopes, per-device rate limiting, health checking, circuit breaking, tool-change governance and
a tamper-evident audit trail.

### Two kinds of upstream, one device model

| `upstream_kind` | What the upstream is | What the gateway does |
|---|---|---|
| `openapi` *(default)* | a REST/OpenAPI service — a device, an appliance, an internal API | **translates**: discovers the spec, turns every operation into an MCP tool |
| `mcp` | a server that already speaks MCP | **proxies**: federates its `tools/list` and `tools/call` through |

Both register the same way, and a proxied MCP server is a **device** rather than a second
entity type — so RBAC, rate limiting, admission control, the circuit breaker, dead-lettering,
audit, fleet sessions and tool-change governance apply identically, with no parallel code path
([ADR-0009](docs/adr/0009-mcp-passthrough.md)).

That matters most for the case the gateway was built for: a **mixed fleet**. A
[fleet session](#multiple-devices-in-one-session-fleet) can span translated REST devices and
proxied MCP servers at once, presenting them to the client as a single namespaced tool set.

> ⚠️ **A proxied MCP server is an untrusted upstream.** It authors its own tool contract and
> can change it after you have reviewed it. The gateway detects that — every poll diffs the
> tool set and flags a removed tool or a newly-required parameter as breaking — but detection
> is not prevention. Read [threat-model.md §B5](docs/threat-model.md) before pointing it at a
> server you do not control.

## Architecture

The gateway supports two modes selected by `registry.mode` in `config.yaml`.

### Distributed mode (production)

```
LLM clients
    │  Streamable HTTP (POST /v1/devices/{hostname}/mcp)   ← recommended
    │  HTTP+SSE        (GET  /v1/devices/{hostname}/sse)   ← deprecated
    ▼
┌───────────────────────────────────────────────────┐
│  Gateway (stateless, scale N replicas)            │
│  FastAPI  •  rate limiter  •  result relay        │
└──────────────────┬────────────────────────────────┘
                   │ Redis Streams / pub/sub
┌──────────────────▼────────────────────────────────┐
│  Redis                                            │
│  device registry  •  assignments  •  tool calls   │
└──────────────────┬────────────────────────────────┘
                   │
┌──────────────────▼────────────────────────────────┐
│  Workers (stateful, scale M replicas)             │
│  DevicePods  •  health loop  •  call consumers    │
└──────────────────┬────────────────────────────────┘
                   │ httpx  (+ circuit breaker)
                   ▼
   OpenAPI services  ·  remote MCP servers
   (translated)         (proxied)
```

Gateway instances are stateless — they read from Redis and relay SSE events via Redis pub/sub. Workers own the DevicePods and execute tool calls. All three components scale independently.

### Embedded mode (local development)

```
LLM client → FastAPI → Registry → DevicePod → upstream
                                │             (OpenAPI service or MCP server)
                                └── SQLite (device registrations)
```

Single process, no Redis. Set `registry.mode: "embedded"` (the default).

---

## Requirements

- Python ≥ 3.10 (3.12 recommended; used in the Docker image)
- Redis ≥ 7 for distributed mode

---

## Quick Start (embedded mode)

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv && source .venv/bin/activate

# 2. Install
pip install -e ".[dev]"

# 3. Start the gateway (embedded mode; no Redis required)
device-mcp
# Override without editing config.yaml:
# device-mcp --host 0.0.0.0 --port 8000 --config /path/to/config.yaml

# 4. Register a device
curl -X POST http://localhost:8000/v1/devices \
  -H "Content-Type: application/json" \
  -d '{"hostname": "my-sensor", "base_url": "http://192.168.1.42"}'

# 5. Talk to it over Streamable HTTP — the response comes back on this request
curl -X POST http://localhost:8000/v1/devices/my-sensor/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'
#    -> 200, with an Mcp-Session-Id response header. Send that header on subsequent calls:
#    {"jsonrpc":"2.0","id":2,"method":"tools/list"}

# 6. Connect an MCP client (see MCP Client Integration below)
```

> **Leave `transport` out of registrations.** The field names the transport a device's pod
> runs internally, not how your client connects, and `"sse"` — its default — is the only
> value accepted. **From 0.4.0 the field is refused with a `400`**, `"sse"` included
> ([upgrade.md](docs/upgrade.md#breaking-api-changes)), so an example or script that sends it
> breaks on upgrade; one that omits it works on both. How a *client* reaches a device is a
> separate choice made per request: **`POST /v1/devices/{hostname}/mcp`** (Streamable HTTP,
> recommended) or the deprecated `GET /v1/devices/{hostname}/sse`.

> **Registering a device on a private/LAN address?** By default the gateway refuses
> targets that resolve to private, loopback, or link-local addresses (the Tier-0 SSRF
> guard — see [`security.allow_private_targets`](config.yaml)), so a `base_url` like the
> `192.168.1.42` above returns `400` until you opt in. For a trusted device fleet on
> private addresses, start with `MCP_ALLOW_PRIVATE_TARGETS=true` (or set
> `security.allow_private_targets: true`). Leave it off when devices are reachable over
> public DNS/addresses.
>
> Separately, target **ports** carrying a non-HTTP service (22 SSH, 25 SMTP, 3306, 6379,
> 27017, … plus 2375/2376 Docker) are refused regardless of that setting — the gateway only
> ever speaks HTTP to a device, so those can only be a port scan or protocol smuggling.
> Ordinary HTTP ports including non-standard ones (8000, 8080, 8443) work by default; see
> [`security.allowed_target_ports`](config.yaml) to switch to a strict allowlist instead.

## Quick Start (distributed mode)

```bash
# 1. Start Redis, gateway, and worker via Docker Compose
MCP_GATEWAY_API_KEY=<token> MCP_SECRET_KEY=<fernet-key> docker compose up -d

# Generate a Fernet key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Scale workers (the gateway publishes a fixed host port, so scale it via
#    Kubernetes or a load balancer — see Kubernetes Deployment below)
docker compose up -d --scale worker=2

# 3. Register a device (any gateway instance)
curl -X POST http://localhost:8000/v1/devices \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"hostname": "my-sensor", "base_url": "http://192.168.1.42"}'

# 4. Talk to it over Streamable HTTP — send this to ANY gateway replica.
#    The device is owned by one worker, but whichever replica takes the POST waits for
#    that worker's result and returns it on this response.
curl -X POST http://localhost:8000/v1/devices/my-sensor/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'
```

---

## Single-operator, one box?

That is a separate product: **[SyncGate Lite](https://github.com/benwold-lgtm/SyncGate-Lite)** —
the whole stack in embedded mode on a Raspberry Pi or mini-PC, local login only, secrets
generated on first boot. It runs these same images and has its own repo, README and release
cadence, because single-operator is a design decision rather than a smaller configuration.

---

## MCP Client Integration

> **Which endpoint should a client use?**
>
> | | Endpoint | Status |
> |---|---|---|
> | **Streamable HTTP** | `POST /v1/devices/{hostname}/mcp` | **Recommended.** The JSON-RPC response returns on the request that carried it. |
> | HTTP+SSE | `GET /v1/devices/{hostname}/sse` + `POST …/messages` | **Deprecated** upstream, with a removal clock. Kept for clients that do not speak Streamable HTTP yet; scheduled for removal one minor release after the new transport completes. |
>
> Both reach the same device with the same tools, scopes and rate limits — the choice is
> per client, not per device. Prefer `/mcp` for anything new. The examples below give the
> Streamable HTTP form first and the SSE form as a fallback.

### Claude Desktop

Claude Desktop's native server config **cannot attach an `Authorization` header**, and
every gateway deployment requires the bearer key — so pointing it
straight at the SSE URL will 401. Bridge through
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) instead: it runs locally
(Node 18+), speaks stdio to Claude Desktop, and forwards the header upstream.

Config file locations:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "my-sensor": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote@latest",
        "http://localhost:8000/v1/devices/my-sensor/mcp",
        "--allow-http",
        "--header", "Authorization:${GATEWAY_TOKEN}"
      ],
      "env": {
        "GATEWAY_TOKEN": "Bearer <gateway-api-key>"
      }
    }
  }
}
```

If your `mcp-remote` build predates Streamable HTTP support, swap the URL back to
`http://localhost:8000/v1/devices/my-sensor/sse` — everything else is identical, and the
deprecated route still works.

Two details that matter:

- The token rides in `env` and is referenced as `${GATEWAY_TOKEN}` — keep **no space**
  after `Authorization:` (mcp-remote splits `args` on spaces).
- `--allow-http` is required for a plain-HTTP gateway (a LAN deploy). Drop it once
  the gateway is behind TLS.

Restart Claude Desktop after saving. The device's tools will appear in the tool picker.

### Clients that send headers natively (Cursor, custom agents)

Clients that can attach request headers connect directly — no bridge process:

```json
{
  "mcpServers": {
    "my-sensor": {
      "type": "http",
      "url": "http://localhost:8000/v1/devices/my-sensor/mcp",
      "headers": { "Authorization": "Bearer <gateway-api-key>" }
    }
  }
}
```

The key naming the transport is the client's, not the gateway's — some call it `"http"`,
others `"streamable-http"`; check your client's docs. What the gateway requires is only the
URL and the header.

<details>
<summary>The deprecated SSE form, for clients that need it</summary>

```json
{
  "mcpServers": {
    "my-sensor": {
      "type": "sse",
      "url": "http://localhost:8000/v1/devices/my-sensor/sse",
      "headers": { "Authorization": "Bearer <gateway-api-key>" }
    }
  }
}
```
</details>

### Multiple devices in one session (fleet)

Registering more than a couple of devices? A per-device endpoint is one session per device —
fine for one or two, but an AI client needs a separate config entry (and, where header-based
auth is required, a separate bridge process) per device. The **fleet** endpoint aggregates
several devices' tools into a single MCP session instead, with tool names namespaced by
hostname (`my-sensor_get_readings`, `my-fan_get_readings`, ...) so there's no collision even
when two devices expose the same operation:

```json
{
  "mcpServers": {
    "my-fleet": {
      "type": "http",
      "url": "http://localhost:8000/v1/fleet/mcp?devices=my-sensor,my-fan,my-thermostat",
      "headers": { "Authorization": "Bearer <gateway-api-key>" }
    }
  }
}
```

`devices` is read when the session opens and ignored afterwards, so the fleet cannot be
widened by a later request.

(For Claude Desktop, use the same `mcp-remote` config as above with this fleet URL in
place of the per-device one.)

<details>
<summary>The deprecated SSE fleet form</summary>

```json
{
  "mcpServers": {
    "my-fleet": {
      "type": "sse",
      "url": "http://localhost:8000/v1/fleet/sse?devices=my-sensor,my-fan,my-thermostat",
      "headers": { "Authorization": "Bearer <gateway-api-key>" }
    }
  }
}
```
</details>

Same auth model as the per-device endpoint (`tools:call` scope, session bound to the
principal that opened it) — this doesn't grant access to anything a caller couldn't
already reach one device at a time. Capped at 25 devices per session by default
(`registry.fleet_max_devices`).

> **The rest of this section is about the deprecated SSE fleet route only.** On
> `POST /v1/fleet/mcp` there is no split to describe: every method, `tools/call` included,
> answers on the request that asked, identically in both modes. If you are using the
> recommended endpoint you can skip to the next section.

> **On the SSE fleet route, where the reply arrives differs by method, and by mode.** A
> conforming MCP client handles this already, because both shapes are legal — but anyone
> writing a client by hand against `POST /v1/fleet/messages` will hit it, so it is written
> down here:
>
> | Mode | `initialize`, `ping`, `tools/list` | `tools/call` |
> |---|---|---|
> | **distributed** | answered **inline** in the POST response body | POST returns `{"status": "accepted"}`; the result arrives on the SSE stream |
> | **embedded** | on the SSE stream (POST body is an ack) | on the SSE stream (POST body is an ack) |
>
> The gateway can answer the first three from shared state in distributed mode, so it
> does rather than making a round trip through a worker. `tools/call` must reach the
> worker holding the device, so its result comes back the only way it can — over the
> stream the session already holds open.
>
> **Read the POST body first: if it carries a `result`, that is your answer.** Otherwise
> match on the SSE stream by JSON-RPC `id`. A client that only ever waits on the stream
> will hang on `tools/list` in distributed mode; one that only ever reads the POST body
> will lose every tool result. The per-device endpoint
> (`POST /v1/devices/{hostname}/messages`) has no such split — it publishes everything to
> the worker and always answers on the stream.

### Manual invocation (Streamable HTTP)

One POST per message, and the reply is the response body. The server mints the session id on
`initialize` and returns it in the `Mcp-Session-Id` header — do not supply your own.

```bash
# initialize — note the -D- to capture the session header
curl -sD- -X POST -H "Authorization: Bearer <api-key>" -H "Content-Type: application/json" \
  http://localhost:8000/v1/devices/my-sensor/mcp \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'

# then send the returned id back on every subsequent message
curl -s -X POST -H "Authorization: Bearer <api-key>" -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: <id-from-above>" \
  http://localhost:8000/v1/devices/my-sensor/mcp \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# end the session when done
curl -s -X DELETE -H "Authorization: Bearer <api-key>" \
  -H "Mcp-Session-Id: <id-from-above>" \
  http://localhost:8000/v1/devices/my-sensor/mcp
```

### Manual invocation (SSE transport — deprecated)

The SSE transport uses a two-step protocol. The server assigns a session ID — do not supply your own.

**Step 1 — Open the event stream:**
```bash
curl -N -H "Authorization: Bearer <api-key>" \
  http://localhost:8000/v1/devices/my-sensor/sse
```

The first event is `endpoint` and its `data` is the POST URL for this session:
```
event: endpoint
data: /v1/devices/my-sensor/messages?session_id=<server-assigned-uuid>
```

**Step 2 — Send a tool invocation on that session:**
```bash
curl -X POST "http://localhost:8000/v1/devices/my-sensor/messages?session_id=<uuid-from-step-1>" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <api-key>" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
       "params": {"name": "get_readings", "arguments": {"sensor_id": 1}}}'
```

The response arrives as a `message` event on the open SSE stream, not in the HTTP response body.

---

## API Reference

All endpoints except `/health` and `/readyz` require `Authorization: Bearer <api-key>` when any API key is configured.

**Roles & scopes (RBAC).** Keys map to roles, and routes authorize on scopes:

| Role | Scopes | Can |
|------|--------|-----|
| `admin` | `devices:read`, `devices:write`, `tools:call`, `metrics:read` | everything |
| `viewer` | `devices:read`, `metrics:read` | read device state + `/v1/metrics/summary`; **no** mutations or tool calls (403) |

Configure keys via `gateway.api_key` (legacy single key = `admin`), `MCP_ADMIN_KEY` / `MCP_VIEWER_KEY`, or a `gateway.rbac` list of `{name, key, role}` (see [config.yaml](config.yaml)). If no key is set anywhere, auth is disabled (all requests permitted). The authenticated principal is recorded as `subject` in audit logs.

**OIDC is built, not planned** ([ADR-0007](docs/adr/0007-federated-identity-oidc-and-gateway-rbac.md)). Enable `gateway.oidc` and the gateway validates a JWT against the issuer's JWKS and maps its `groups` claim to a role through `group_roles`. **The gateway owns `group → role → scope`; the IdP only asserts membership** — an authenticated user with no mapped group gets zero scopes rather than a blanket 401, so the audit still shows who was denied. The principal is recorded as `oidc:{issuer}#{sub}`, issuer-qualified because `sub` is unique *within* an issuer and not globally.

With OIDC enabled the static keys become a **break-glass fallback** (OIDC JWT → static key → 401), which is what keeps the gateway reachable when the IdP is not. Two guardrails are worth knowing before you configure it:

- **A plaintext `http://` issuer is refused at startup** unless `security.allow_plaintext_idp: true` is set deliberately, which warns loudly. The egress URL policy permits `http` by design — its job is SSRF, not transport encryption — so nothing else enforces TLS to your IdP.
- **The discovery document must declare the issuer you configured**, or it is refused and nothing is cached. Pinning `iss` at decode time does not cover this: an attacker who supplies the *keys* also chooses the claims. Fixed in [0.3.4](CHANGELOG.md).

**Setting up a specific IdP?** [docs/identity-integration.md](docs/identity-integration.md) walks through Keycloak and Authentik (both verified against running instances), sketches Entra ID, Okta and Google Workspace as **untested** integration paths, and lists the failure modes that are silent — including the one above, where every SSO user gets a 401 while key holders keep working.

The seam (`authenticate()` → `Principal{subject, scopes}`) means routes are unchanged either way.

Rate limits (per source IP): `/health` and `/readyz` — 300 req/min; `POST /v1/devices` — 60 req/min; `POST /messages` — 600 req/min. Returns 429 on excess.

> **API versioning.** The management API is served under a `/v1` prefix (e.g. `POST /v1/devices`). Operational probes (`/health`, `/readyz`) and the Prometheus scrape endpoint are intentionally **unversioned** — they are infra contracts consumed by Kubernetes and Prometheus, not application clients. A backward-incompatible change to the management API will introduce `/v2` and dual-mount `/v1` for a deprecation window.

> **Tool-set change governance & webhooks.** A device's tools are generated from its upstream OpenAPI spec, so they change when the spec changes. Every change is classified (compatible vs. **breaking**), recorded to the audit stream + a `mcp_device_tools_changed_total` metric, and surfaced to clients as a monotonic `tools_revision` on `GET /v1/devices/{hostname}` — poll it to detect a change, then `GET /v1/devices/{hostname}/tools/diff` to see *what* changed (added/removed/changed tools + the breaking flag) and re-list tools. OpenAPI `webhooks`/`callbacks` are **not** translated: the gateway is pull-only (request→response), with no inbound event surface. See [docs/api-change-governance.md](docs/api-change-governance.md) and the full mapping contract in [docs/tooling.md](docs/tooling.md).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe — process status, active pod count |
| `GET` | `/readyz` | Readiness probe — backend connectivity (Redis or SQLite) |
| `POST` | `/v1/devices` | Register a device |
| `PUT` | `/v1/devices/{hostname}` | Update a device config (replaces and restarts pod) |
| `DELETE` | `/v1/devices/{hostname}` | Unregister a device |
| `GET` | `/v1/devices` | List all registered devices |
| `GET` | `/v1/devices/{hostname}` | Get a single device's status |
| `GET` | `/v1/devices/{hostname}/tools` | List a device's MCP tools |
| `GET` | `/v1/devices/{hostname}/tools/diff` | The device's most recent tool-set change — added/removed/changed tools + breaking flag (`devices:read`) |
| `GET` | `/v1/devices/{hostname}/diagnostics` | "Why is my device down?" — status, last check + age, spec/manifest state, spawn error, circuit breaker (`devices:read`) |
| `GET` | `/v1/devices/{hostname}/sse` | Open SSE stream (MCP transport — **deprecated**, see below) |
| `POST` | `/v1/devices/{hostname}/messages` | Send a JSON-RPC 2.0 message via SSE |
| `POST` | `/v1/devices/{hostname}/mcp` | Streamable HTTP transport — the JSON-RPC response returns on **this** request, in both modes (`tools:call`) |
| `GET` | `/v1/fleet/sse?devices=a,b,…` | Open one MCP session spanning several devices — tool names namespaced by hostname (`tools:call`) |
| `POST` | `/v1/fleet/messages` | Send a JSON-RPC 2.0 message on a fleet session (`tools:call`) |
| `POST` | `/v1/fleet/mcp?devices=a,b,…` | Fleet session over Streamable HTTP — `devices` on `initialize` only; every method, `tools/call` included, answers on **this** request in both modes (`tools:call`) |
| `GET` | `/v1/devices/{hostname}/deadletter` | Inspect dead-lettered tool calls (distributed mode; `devices:read`) |
| `POST` | `/v1/devices/{hostname}/deadletter/replay` | Re-publish dead-lettered calls onto the call stream; optional `{"ids":[...]}` (`devices:write`) |
| `DELETE` | `/v1/devices/{hostname}/deadletter` | Drain the dead-letter queue; optional `{"ids":[...]}` (`devices:write`) |
| `GET` | `/v1/metrics/summary` | Reachability counts and per-device rate-limit state (JSON, auth-protected) |
| `GET` | `/v1/admin/overview` | Aggregate fleet counts + device list in one call (UI/BFF enabler; `devices:read`) |

Prometheus metrics are exposed separately on a **dedicated metrics port** (`metrics.port`, default `9100`) at `GET /metrics`, not on the API port — point a `ServiceMonitor`/scrape config at that port and restrict it with a NetworkPolicy. Set `metrics.enabled: false` (or `MCP_METRICS_ENABLED=0`) to disable.

### Register / update device payload

```json
{
  "hostname": "my-device",
  "base_url": "http://device.local",
  "spec_url": "http://device.local/openapi.json",
  "auth_type": "api_key",
  "auth": {
    "api_key": "supersecret",
    "header_name": "X-API-Key"
  },
  "rate_limit_rps": 10.0
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `hostname` | Yes (POST) | Unique identifier — letters, digits, hyphens, dots; 1–253 chars |
| `base_url` | Yes (POST) | Root URL of the device API |
| `spec_url` | No | Full URL to the OpenAPI spec (JSON); auto-discovered if omitted. No spec published? See [examples/specs/](examples/specs/) |
| `transport` | No | **Omit it.** `"sse"` (the default) is the only accepted value today, and from 0.4.0 the field is refused with a `400` |
| `auth_type` | No | `"api_key"`, `"oauth2"`, or `"none"` |
| `auth` | Conditional | Required when `auth_type` is `api_key` or `oauth2` |
| `rate_limit_rps` | No | Max requests/second to the downstream device API |
| `upstream_kind` | No | `"openapi"` (default) or `"mcp"` — what the upstream **speaks** |
| `upstream_transport` | No | `"http"` (default, Streamable HTTP) — how we **talk to** an MCP upstream. `"sse"` is reserved and refused today |

`PUT` treats all fields except `hostname` as optional — omitted fields keep their existing values.

#### Registering a remote MCP server (passthrough)

Set `upstream_kind: "mcp"` and point `base_url` at the server's MCP endpoint. There is no
spec to fetch — the tool set comes from the upstream's `tools/list` — so `spec_url` must be
omitted:

```json
{
  "hostname": "vendor-mcp",
  "base_url": "https://mcp.vendor.example/mcp",
  "upstream_kind": "mcp",
  "auth_type": "api_key",
  "auth": { "api_key": "supersecret", "name": "Authorization", "value_prefix": "Bearer " }
}
```

Its tools are then served, namespaced and governed exactly like a translated device's: same
RBAC, rate limiting, argument validation, breaker, audit and fleet sessions. The gateway
holds one credential **per server** and calls it on the caller's behalf; a caller's own token
is never forwarded to an upstream.

Two things worth knowing before you register one:

- **The upstream authors its own tool contract and can change it.** Alert on
  `mcp_device_tools_changed_total{breaking="true"}` and review
  `GET /v1/devices/{hostname}/tools/diff` — see
  [api-change-governance.md](docs/api-change-governance.md) and
  [threat-model.md](docs/threat-model.md) §B5.
- **v1 proxies tools only** — not resources, prompts, or stdio servers. Rationale in
  [ADR-0009](docs/adr/0009-mcp-passthrough.md).

### Response shapes

**`GET /v1/devices/{hostname}`:**
```json
{
  "hostname": "my-sensor",
  "base_url": "http://192.168.1.42",
  "spec_url": null,
  "reachable": true,
  "pod_active": true,
  "last_check": 1717500000.0,
  "transport": "sse",
  "rate_limit_rps": null,
  "spawn_error": null
}
```

**`GET /v1/devices/{hostname}/diagnostics`:**
```json
{
  "hostname": "my-sensor",
  "mode": "embedded",
  "base_url": "http://192.168.1.42",
  "spec_url": null,
  "transport": "sse",
  "reachable": true,
  "pod_active": true,
  "worker_id": null,
  "last_check": 1717500000.0,
  "last_check_age_seconds": 12.4,
  "spec_hash": "9f3c1a2b4d5e6f70",
  "has_manifest": true,
  "tool_count": 7,
  "spawn_error": null,
  "breaker": {"available": true, "state": "closed", "fail_counter": 0, "fail_max": 5, "reset_timeout": 60, "note": null}
}
```
In distributed mode the breaker runs on the worker, so `breaker` is `{"available": false, "note": "pod runs on a worker; ..."}`.

**`POST /v1/devices` / `PUT /v1/devices/{hostname}`:**
```json
{"status": "registered", "hostname": "my-sensor", "pod_active": true, "reachable": true, "spawn_error": null}
```

**`GET /health`:**
```json
{"status": "healthy", "mode": "distributed", "active_pods": 4, "registered_devices": 5, "version": "<gateway version>"}
```

**`GET /readyz`:** Returns `200 {"status": "ready", "mode": "..."}` when the backend is reachable, `503 {"status": "not ready", "reason": "..."}` when not.

---

## Authentication

### Gateway API key

Set `MCP_GATEWAY_API_KEY` (environment variable) or `gateway.api_key` in `config.yaml`. When set, all endpoints except `/health` and `/readyz` require `Authorization: Bearer <key>`. Leave empty to disable authentication (not recommended for production).

### Device authentication

#### None
```json
{"auth_type": "none"}
```

#### API Key
```json
{"auth_type": "api_key", "auth": {"api_key": "my-key", "header_name": "X-API-Key"}}
```
The key can live in a header (default), a query param, or a cookie, with an optional scheme prefix (F-43):
```json
{"auth_type": "api_key", "auth": {"api_key": "my-key", "location": "query", "name": "apikey"}}
{"auth_type": "api_key", "auth": {"api_key": "my-key", "location": "header", "name": "Authorization", "value_prefix": "Bearer "}}
{"auth_type": "api_key", "auth": {"api_key": "my-key", "location": "cookie", "name": "session"}}
```
| Field | Default | Notes |
|-------|---------|-------|
| `location` | `header` | `header`, `query`, or `cookie` |
| `name` | per-location (`X-API-Key` / `api_key` / `api_key`) | header/param/cookie name; legacy `header_name` still accepted |
| `value_prefix` | `""` | prepended to the value, e.g. `"Bearer "` |

#### By reference — `credential_ref` (0.3.5+)

Instead of sending the secret, send a **reference** to where it lives
([ADR-0018](docs/adr/0018-device-credentials-by-reference.md)):

```json
{"auth_type": "api_key", "auth": {"credential_ref": "secret://t-3f9a1c2b7d4e8065/devices/edge-01#api_key"}}
```

The registry stores the reference. The material is resolved at dispatch, held for that one
dispatch, and **never written back, cached to disk, or included in an archive** — so an export
of an entirely by-reference fleet is configuration rather than a credential dump, and can be
committed to Git and reviewed in a pull request.

The scheme is deliberately backend-neutral. Naming the store (`vault://`) would bake a
deployment choice into every device record, so an archive could only be restored into a stack
running the same product.

| | |
|---|---|
| **Exclusive with `api_key`** | sending both is a `400` at registration, naming the exclusivity — refused rather than resolved by precedence, because any precedence rule makes the losing value invisible |
| **Malformed reference** | also a `400` at registration, not a device that looks fine until its first dispatch |
| **Backends** | today: a mounted Kubernetes Secret / CSI volume, and a local file tree for embedded mode. Networked stores (Vault, cloud managers) share the same interface and are not yet implemented |
| **Not for gateway-minted credentials** | an OAuth2 `refresh_token` is minted mid-exchange and stays encrypted under `MCP_SECRET_KEY` ([§1a](docs/adr/0018-device-credentials-by-reference.md)) |

Two dispatch errors are deliberately distinct, because collapsing them makes a sealed store
look like twenty broken devices: **`ERR_CREDENTIAL_UNRESOLVED`** (this device's reference is
bad — permanent, one device) and **`ERR_SECRET_STORE_UNAVAILABLE`** (the store is unreachable —
transient, fleet-wide).

#### OAuth2 (token endpoint)
```json
{
  "auth_type": "oauth2",
  "auth": {
    "token_endpoint": "https://auth.example.com/token",
    "client_id": "my-client",
    "client_secret": "my-secret",
    "scopes": ["read", "write"]
  }
}
```
Beyond the default `client_credentials` body flow, the following are supported (F-42):
| Field | Default | Notes |
|-------|---------|-------|
| `grant_type` | `client_credentials` | also `password` (with `username`/`password`) or `refresh_token` (with `refresh_token`) |
| `auth_style` | `request_body` | `basic` sends client id/secret as HTTP Basic to the token endpoint |
| `audience` | — | provider audience (e.g. Auth0) |
| `extra_params` | — | object merged into the token request (e.g. RFC 8707 `resource`) |

The interactive `authorization_code` grant and `jwt-bearer` assertions are intentionally **not** supported — the first needs a user redirect (impossible for an unattended gateway), the second needs per-device signing-key management.

---

## Security

The full security model — trust boundaries, adversaries, and the control addressing each
threat — is in [docs/threat-model.md](docs/threat-model.md). The sections below summarize
the key controls.

### Credential encryption

Device credentials (OAuth2 `client_secret`, API keys) are encrypted at rest with a Fernet key (`MCP_SECRET_KEY`) on **both** storage paths — the SQLite store (embedded mode) and Redis (distributed mode). The gateway and workers share the same key and the same codec, so credentials are never written in plaintext when a key is set.

**Set `MCP_SECRET_KEY` before registering any devices with credentials.**

- **Distributed mode (production):** the gateway and workers **refuse to start** without a key, because credentials would otherwise be persisted to Redis in plaintext. To override for local experiments only, set `gateway.allow_plaintext_credentials: true`.
- **Embedded mode:** without a key, credentials are stored as plaintext in SQLite and the gateway logs a startup warning.

Generate a key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Pass it as an environment variable — never store it in `config.yaml` or the Kubernetes ConfigMap:
```bash
export MCP_SECRET_KEY=<your-fernet-key>
```

**A device using `credential_ref` has nothing here to encrypt** — the gateway holds a pointer,
not a secret. `MCP_SECRET_KEY` is still required for gateway-*minted* credentials (an OAuth2
`refresh_token`), which by-reference does not cover.

### Multitenancy

The gateway is **single-tenant per stack**: it has no in-application tenant isolation. The device namespace is flat (keyed by `hostname`), RBAC scopes are global within a deployment, and co-located DevicePods share decrypted credentials in one worker process. Isolate tenants by running a **separate stack per tenant** — each with its own Redis, `MCP_SECRET_KEY`, and API keys. **Do not co-host tenants in one deployment.** See [docs/multitenancy.md](docs/multitenancy.md) for the deployment model and rules.

Where you run a stack per tenant, mint the tenant's identifier with `tools/tenant_id.py` rather
than deriving it from anything the customer chose — a namespace name is not encrypted, so a
customer name written there survives crypto-shredding and leaks into every `kubectl` output and
metric label in the estate ([ADR-0019](docs/adr/0019-opaque-tenant-identity.md)):

```bash
python3 tools/tenant_id.py new                       # -> t-3f9a1c2b7d4e8065
python3 tools/tenant_id.py namespace t-3f9a1c2b7d4e8065 --label
```

An identifier is **never reissued**, even after a tenant departs: stale DNS, a cached token and
a bookmarked console must never resolve onto a new tenant. `deploy/overlays/tenant-example/`
is the per-tenant overlay to copy.

### Rate limiting

The gateway enforces fixed-window rate limits per source IP and, on dispatch endpoints, per authenticated principal (`ratelimit.py` — fully async). In **embedded mode** the counters are in-process; in **distributed mode** they live in Redis, so limits are shared across all gateway replicas automatically. Requests over the limit receive `HTTP 429` with a `Retry-After` header.

**Behind a reverse proxy** set `gateway.trust_proxy_headers: true` **and** `security.trusted_proxy_cidrs` — the ranges your proxies connect from. Both are required together; the gateway **refuses to start** with trust enabled and no trusted ranges. The reason is that nginx, traefik and the k8s ingresses *append* to `X-Forwarded-For` rather than replace it, so the left-most entry is whatever the client typed. Keying on it would let any caller pick their own bucket — and reset it by rotating the header. Instead the client is resolved by walking `X-Forwarded-For` **right-to-left** from the TCP peer, popping hops while each one falls inside a trusted range; the first hop outside them is the client. A caller who skips the proxy is stopped at the first step, since their own peer address isn't trusted, and their header is never read.

```yaml
gateway:
  trust_proxy_headers: true
security:
  trusted_proxy_cidrs: ["10.244.0.0/16", "10.96.0.0/12"]   # k8s pod CIDR + LB range
  # docker-compose: ["172.16.0.0/12"] · host-local nginx: ["127.0.0.1/32", "::1/128"]
```

Keep the list to infrastructure you control — every range you add is one more hop an attacker doesn't have to get past, and something as broad as `0.0.0.0/0` re-opens the spoofing hole completely. If the gateway isn't behind a proxy, leave `trust_proxy_headers: false` and the socket peer is used.

### CORS

CORS headers are disabled by default. To allow browser-based clients, add your origins to `config.yaml`:
```yaml
cors:
  allowed_origins:
    - "https://my-llm-app.example.com"
```

Use `["*"]` only in development.

### Circuit breaker

Each DevicePod wraps its downstream HTTP calls in a per-device circuit breaker (opens after 5 consecutive 5xx/connection failures; resets after 60 s). A tripped breaker returns `{"error": "Device unavailable: circuit breaker open", "status_code": 503}` immediately rather than waiting 15 s for a timeout.

### Correlation IDs

Every request receives an `X-Request-Id` header (taken from the incoming request or generated as a UUID4). The ID appears in all log lines for that request chain (`rid=<id>`), is echoed in the response `X-Request-Id` header, and is **sent onward to the device** on every outbound call.

That last hop is a requirement rather than a convenience. A device authenticates the gateway, not the person behind the call — one service identity per device, permanently ([ADR-0026](docs/adr/0026-service-identity-per-device.md)) — so the only way to answer "who caused this change on the appliance?" is to join the gateway's audit record to the device's own log, and the request id is the key that join uses. See [docs/audit-logging.md](docs/audit-logging.md#attribution-across-the-device-hop-adr-0026).

### TLS

The gateway serves plain HTTP. Always run it behind a TLS-terminating proxy (nginx, Caddy, cloud load balancer) in production. The Kubernetes Ingress in `deploy/kubernetes/ingress.yaml` handles TLS termination.

---

## Configuration

All settings live in `config.yaml`. Override the file location with `MCP_CONFIG`. Most secrets should be passed as environment variables rather than stored in the file.

| Key | Default | Description |
|-----|---------|-------------|
| `gateway.api_key` | `""` | Bearer token required on protected routes. Override with `MCP_GATEWAY_API_KEY` |
| `gateway.secret_key` | `""` | **Required for credential encryption.** Fernet key. Override with `MCP_SECRET_KEY` |
| `gateway.max_body_bytes` | `1048576` | Maximum POST/PUT body size in bytes (1 MB default) |
| `server.host` | `0.0.0.0` | Bind address |
| `server.port` | `8000` | Port |
| `registry.mode` | `"embedded"` | `"embedded"` (local dev, no Redis) or `"distributed"` (production, Redis required) |
| `registry.health_check_interval` | `30` | Seconds between device reachability checks |
| `registry.spec_poll_interval` | `300` | Seconds between spec refresh checks |
| `registry.spec_cache_ttl` | `3600` | Spec cache lifetime in seconds |
| `registry.max_concurrent_pods` | `50` | Max simultaneous device pods (embedded mode only) |
| `registry.fleet_max_devices` | `25` | Max devices one fleet session (`/v1/fleet/mcp` or `/v1/fleet/sse`) may span |
| `redis.url` | `"redis://localhost:6379/0"` | Redis connection URL. Override with `MCP_REDIS_URL` |
| `redis.socket_timeout` | `5` | Redis socket timeout in seconds |
| `redis.max_connections` | `20` | Redis connection pool size per gateway instance |
| `cors.allowed_origins` | `[]` | Allowed CORS origins for browser clients. Empty = disabled |
| `auth.type` | `api_key` | Default auth type for devices (`api_key`, `oauth2`, `none`) |
| `discovery.timeout` | `10` | Spec discovery request timeout in seconds |
| `storage.db_path` | `./data/devices.db` | SQLite path (embedded mode only; use `/app/data/devices.db` in Docker) |
| `transport.default` | `sse` | Default device transport (`sse` is the only supported value). Ignored, with a startup warning, from 0.4.0 |
| `transport.sse.keep_alive_interval` | `15` | Seconds between SSE keepalive pings |
| `logging.level` | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `logging.file` | `logs/gateway.log` | Log file path |

`discovery.spec_paths` is a list of URL paths probed for auto-discovery (fetched specs are parsed as JSON). See `config.yaml` for the full default list.

---

## Docker

```bash
# Build and run all three services (gateway + worker + Redis)
docker compose up -d

# Scale workers (the gateway publishes a fixed host port, so scale it via
# Kubernetes or a load balancer rather than `--scale gateway`)
docker compose up -d --scale worker=2

# Stop everything
docker compose down
```

The compose file configures:
- A named `mcp-net` bridge network (all services)
- Per-service resource limits (gateway: 512M / 1 CPU; worker: 1G / 2 CPU; Redis: 256M / 0.5 CPU)
- Redis `healthcheck` — gateway and worker wait for Redis to be ready before starting

Pass secrets as environment variables:
```bash
MCP_GATEWAY_API_KEY=<token> MCP_SECRET_KEY=<fernet-key> docker compose up -d
```

**Embedded mode in Docker:** If running `registry.mode: "embedded"`, mount a volume for the SQLite database so registrations survive restarts:
```yaml
# In docker-compose.yml, add to the gateway service:
volumes:
  - ./data:/app/data
```

---

## Kubernetes Deployment

Pre-built manifests live in [`deploy/kubernetes/`](deploy/kubernetes/). The manifests assume distributed mode (`registry.mode: "distributed"` is set in the ConfigMap).

### Cluster prerequisites

The bundled manifests assume the following are already installed in the target cluster:

| Prerequisite | Needed for | Notes |
|--------------|-----------|-------|
| **Ingress controller (ingress-nginx)** | `ingress.yaml` | Uses `ingressClassName: nginx` and `nginx.ingress.kubernetes.io/*` annotations. Swap both if you run a different controller. |
| **metrics-server** | the CPU-based HPAs (`hpa.yaml`) | Without it the HPAs report `<unknown>` CPU and never scale. |
| **A default StorageClass** | the Redis `StatefulSet` PVC | Or set `storageClassName` explicitly in `redis.yaml`. |
| **Prometheus Operator** (optional) | `prometheus-rules.yaml`, `servicemonitor.yaml` | Only if you want the SLO/alert rules. **Not applied by default** — see [Observability](#observability) and the note in `kustomization.yaml`. |

> **Single-node test clusters (kind / minikube / k3s)** work: the pod anti-affinity is
> `preferred` (won't block scheduling) and the PDBs allow rolling updates at `replicas: 2`.
> On `kind`, skip the registry push and load the image directly (see below).

### Point the manifests at an image

**Nothing to do for a default install.** The manifests ship pinned to a published,
multi-arch (amd64/arm64) GHCR image *by digest*:

```
ghcr.io/benwold-lgtm/device-mcp-gateway:0.3.8@sha256:e752997a044b5a8ac1c926fb6c7ca921125331b836ed2bc9e94e124b540c9085
```

The digest — not the tag — is what Kubernetes resolves, so every replica on every node
runs byte-identical bits and a re-pushed tag can't silently change what's deployed.

To move to a different version, edit the `images:` block in `kustomization.yaml` **once**
rather than hand-editing both deployments (they share one image and must stay in lockstep —
they share the Redis data model, so a version skew across a schema change is a split-brain
risk). Read the digest for a tag with:

```bash
docker buildx imagetools inspect ghcr.io/benwold-lgtm/device-mcp-gateway:0.3.8
```

Prefer to build from source? The repo root holds the Dockerfile — build, push to a
registry your cluster can pull from, and retarget via the same `images:` block:

```bash
docker build -t <your-registry>/device-mcp-gateway:0.3.8 .
docker push <your-registry>/device-mcp-gateway:0.3.8
```

> **kind / minikube shortcut.** To skip a registry entirely, build locally and load the
> image into the cluster: `kind load docker-image my-gateway:dev` (or `minikube image
> load …`), then point `kustomization.yaml`'s `images:` block at `newName: my-gateway`,
> `newTag: dev`. Drop the digest when you do — a digest pin will not resolve against a
> locally built image. `imagePullPolicy: IfNotPresent` then uses the loaded image.
>
> If you use a **moving** tag (`:latest`) anywhere, switch `imagePullPolicy` to
> `Always` in both deployments — with `IfNotPresent` a node keeps running whatever it
> cached, indefinitely.

### Deploy

```bash
# Create namespace and secrets (never store secrets in the ConfigMap)
# Distributed mode requires an API key (F-23) and an authenticated Redis (F-24);
# the gateway/worker refuse to start otherwise.
kubectl create namespace mcp-gateway
REDIS_PW=$(openssl rand -hex 24)
kubectl create secret generic gateway-secrets \
  --namespace=mcp-gateway \
  --from-literal=api-key=$(openssl rand -hex 32) \
  --from-literal=secret-key=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  --from-literal=redis-password="$REDIS_PW" \
  --from-literal=redis-url="redis://:$REDIS_PW@redis:6379/0"   # rediss:// when Redis terminates TLS

# TLS for the Ingress: the Ingress references secretName: mcp-gateway-tls.
# Either let cert-manager issue it (add an Issuer + the cert-manager.io/cluster-issuer
# annotation to ingress.yaml), or create a cert manually — e.g. a self-signed cert
# for a test deployment:
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout tls.key -out tls.crt -subj "/CN=mcp-gateway.example.com"
kubectl create secret tls mcp-gateway-tls \
  --namespace=mcp-gateway --cert=tls.crt --key=tls.key

# Customise before deploying:
#   Build + push the image and set it in deployment.yaml + worker-deployment.yaml
#     (see "Build and push the image" above)
#   deploy/kubernetes/ingress.yaml   — set your hostname (replaces mcp-gateway.example.com)
#   deploy/kubernetes/worker-deployment.yaml — adjust replicas / resources

# Deploy everything (Prometheus Operator CRDs are NOT required — they are excluded
# from the default kustomization; see Observability to enable them)
kubectl apply -k deploy/kubernetes/
```

Key resources deployed:
- **Redis** StatefulSet + PVC (shared state) — **single replica; out of scope for these
  manifests as an HA component.** It is a single point of failure (see `redis.yaml`); for a
  resilience/failover test, point `MCP_REDIS_URL` at managed Redis or a Sentinel/Cluster
  endpoint instead.
- **Gateway** Deployment (stateless, 2 replicas, HPA to 10) — readiness on `/readyz`, liveness on `/livez`
- **Worker** Deployment (stateful, scale independently) — liveness via a local heartbeat file
- **PodDisruptionBudgets** for gateway and worker (`minAvailable: 1`)
- **Pod anti-affinity** (preferred) spreading gateway and worker replicas across nodes
- **Hardened pod security** — non-root, read-only root filesystem, all capabilities dropped, `RuntimeDefault` seccomp
- **NetworkPolicy** limiting ingress to the gateway/metrics ports and egress to DNS, Redis, and device-API ports (80/443/8080/8443 — a device on a non-standard port needs an added rule)
- **Ingress** for TLS termination

See [`docs/kubernetes-architecture.md`](docs/kubernetes-architecture.md) for the full architecture diagram and message-flow walkthrough.

---

## Observability

### Prometheus metrics

The gateway and each worker export Prometheus metrics on a **dedicated metrics port**
(`metrics.port`, default `9100`) at `GET /metrics` — RED-style HTTP and tool-call
metrics (route-template-labelled to stay low-cardinality), fleet gauges
(`mcp_registered_devices`, `mcp_active_pods`, `mcp_active_sse_connections`), and the
worker autoscaling signal `mcp_worker_pending_calls`. The gateway Deployments carry
`prometheus.io/scrape` annotations; a `ServiceMonitor` example, full metric reference,
scrape config, and Grafana starter queries are in
[docs/observability.md](docs/observability.md#prometheus-metrics).

```bash
# Scrape locally (one process):
curl -s localhost:9100/metrics | grep '^mcp_'
```

### Log format

The gateway writes to two sinks simultaneously:

| Sink | Format | Use |
|------|--------|-----|
| **stderr** | Human-readable colored text | `kubectl logs`, local dev |
| **File** (`logs/gateway.log`) | Newline-delimited JSON (default) | External collectors |

JSON is the default because Splunk, Grafana Loki, and Elasticsearch all ingest it without
custom extraction rules. Each record is a single JSON line; structured fields from
`logger.bind()` appear under `record.extra` and are directly indexable.

Toggle plain-text file output for local development:

```yaml
# config.yaml
logging:
  json_logs: false   # default is true
```

### Audit events

Every tool dispatch emits a structured `audit` event with these fields:

| Field | Description |
|-------|-------------|
| `record.extra.event` | Always `"audit"` — use this to filter audit records |
| `record.extra.hostname` | Registered device name |
| `record.extra.subject` | Authenticated principal — `key:<name>`, or `anonymous` when auth is disabled |
| `record.extra.method` | MCP JSON-RPC method (`"tools/call"`, `"tools/list"`, …) |
| `record.extra.status` | `"ok"`, `"error"`, or `"dispatched"` (distributed mode) |
| `record.extra.duration_ms` | Round-trip time in ms (embedded mode only) |
| `record.extra.rid` | Correlation ID — matches the `X-Request-Id` response header |

### Connecting an external collector

Full configuration snippets for **Grafana Loki (Promtail)**, **Splunk (UF and HEC)**,
and **Elasticsearch (Fluent Bit)** — including sample queries for each platform — are in
[docs/observability.md](docs/observability.md).

Quick reference:

```bash
# Grafana Loki — filter all audit events (LogQL)
{job="mcp-gateway"} | json | event="audit"

# Splunk (SPL)
index=mcp_gateway sourcetype=_json record.extra.event="audit"

# Trace a request by correlation ID across all log lines
{job="mcp-gateway"} | json | rid="<X-Request-Id value>"
```

The `X-Request-Id` header is returned on every API response; use it to correlate a
failed client call with the corresponding gateway and worker log entries.

---

## Troubleshooting

### Device registered but `pod_active: false`

The pod failed to start. Check `spawn_error` in `GET /v1/devices/{hostname}`:
```bash
curl http://localhost:8000/v1/devices/my-sensor
```

Common causes:
- **Unreachable device:** `base_url` is wrong, or the device is not accessible from the gateway network.
- **Spec not found:** No OpenAPI spec at any of the `discovery.spec_paths`. Provide `spec_url` explicitly. If the device publishes no spec at all (UniFi consoles, printers, many IoT hubs), hand-write a minimal one — walkthrough and a working UniFi example in [examples/specs/](examples/specs/).
- **Distributed mode, no worker:** Ensure `device-mcp-worker` is running and connected to the same Redis.

### Tool calls return errors

1. Check `reachable: false` — the device may have gone offline since registration. The health loop retries every `registry.health_check_interval` seconds.
2. Check the gateway logs for `circuit breaker open` — the device returned 5xx errors 5 times in a row. Wait 60 seconds for the breaker to reset, or restart the pod.
3. In distributed mode, check the worker logs for the actual httpx error.

### SSE stream connects but tool result never arrives

Ensure you are POSTing to `?session_id=<uuid>` where the UUID was taken from the `endpoint` event's `data` field — not a client-chosen value. The gateway assigns session IDs; client-supplied values are silently ignored.

### Gateway returns `503` on `/readyz`

- **Distributed mode:** The gateway cannot reach Redis. Check `MCP_REDIS_URL` and network connectivity.
- **Embedded mode:** The SQLite database is not accessible. Check `storage.db_path` and filesystem permissions.

### Credential encryption & key rotation

If `gateway.secret_key` was not set when a device was registered, its `auth_config` is stored as plaintext until its next credential write. Key **rotation does not require re-registering devices**: the codec accepts multiple keys (`MCP_SECRET_KEY="<new>,<old>"` — new key encrypts, all keys decrypt), and the `device-mcp-rotate-secrets` CLI re-encrypts stored credentials under the new key so the old one can be retired. Full zero-downtime procedure in [docs/secret-rotation.md](docs/secret-rotation.md). A device that fails to decrypt its credentials (e.g. the old key was dropped before re-encrypting) logs an error and loads without credentials — tool calls then 401 against the downstream API until the key is restored or the device re-registered.

### Rate limiting (429 responses)

In distributed mode the limits are Redis-backed and shared across replicas — a client sees the same budget no matter which replica it hits. In embedded mode (single process) the counters are in-process, which is equivalent. The window is fixed (INCR + EXPIRE), so a short burst at a window boundary can briefly exceed the nominal rate; respect the `Retry-After` header on 429s. If all clients appear as one IP, you're behind a proxy without `gateway.trust_proxy_headers: true` + `security.trusted_proxy_cidrs` (see [Rate limiting](#rate-limiting)) — and if they still do *after* setting both, your `trusted_proxy_cidrs` most likely doesn't cover the address your proxy actually connects from, so the walk stops at the peer. Confirm it with `docker network inspect` / the ingress pod IP rather than widening the range to make the symptom go away.

---

## Design, security & reliability docs

Phase-0 / governance artifacts for reviewers and operators:

| Doc | What it covers |
|-----|----------------|
| [docs/tooling.md](docs/tooling.md) | OpenAPI→MCP translation contract — tool naming, parameter/body mapping, schema resolution, argument validation, error mapping |
| [docs/rbac-roles.md](docs/rbac-roles.md) | RBAC scopes, role bundles, and IdP/OIDC group→role mapping (see [ADR-0007](docs/adr/0007-federated-identity-oidc-and-gateway-rbac.md)) |
| [docs/threat-model.md](docs/threat-model.md) | STRIDE threat model — trust boundaries, adversaries, control-per-threat, accepted risks |
| [docs/threat-model-identity.md](docs/threat-model-identity.md) | Threat-model addendum for federated identity (IdP → BFF → gateway) — new boundaries, `TM-I-nn` requirements, pre-implementation gate (see [ADR-0007](docs/adr/0007-federated-identity-oidc-and-gateway-rbac.md)) |
| [docs/failure-modes.md](docs/failure-modes.md) | FMEA matrix — per-component failure, detection (metric/alert), mitigation, operator action |
| [docs/findings-register.md](docs/findings-register.md) | Every `F-nn` cited in these docs, defined once — severity, what it was, how it was resolved. A closed record of the completed review programme |
| [docs/adr/](docs/adr/) | Architecture Decision Records — the load-bearing decisions. Start with [the index](docs/adr/README.md); it carries the register, the supersession map and the implementation order |
| [docs/load-testing.md](docs/load-testing.md) | Load-baseline methodology + the runnable harness in [tools/loadtest/](tools/loadtest/) |
| [docs/testing-gaps.md](docs/testing-gaps.md) | **What we have not empirically validated** and why — chaos/fault injection, scale baseline, HA Redis failover, arm64 verification, and disaster recovery (restore into a fresh stack, at fleet scale, across a key rotation). Read this before treating a resilience claim as proven |
| [docs/multitenancy.md](docs/multitenancy.md) | Single-tenant-per-stack deployment model (D-1) |
| [docs/runbook.md](docs/runbook.md) | On-call runbook — per-alert playbooks, symptom troubleshooting, standard procedures |
| [docs/upgrade.md](docs/upgrade.md) | Upgrade guide — versioning/compat policy, rolling procedure, breaking gates, rollback |
| [docs/releasing.md](docs/releasing.md) | Maintainer release checklist — what happens before the tag, and the digest re-pinning that can only happen after it |
| [docs/dependency-advisories.md](docs/dependency-advisories.md) | How to triage a `pip-audit` finding here — what this project actually uses from each dependency, plus the standing triage |
| [docs/operator-guides-plan.md](docs/operator-guides-plan.md) | Plan for the Provider and Tenant guides — what is writable today against shipped code, what is blocked on unbuilt architecture, and the tunables reference that gets built one entry at a time |
| [docs/compliance.md](docs/compliance.md) | Compliance mapping — SOC 2 TSC / HIPAA / FedRAMP-FIPS + shared-responsibility lines |

## Running Tests

```bash
make test          # full suite
make test-fast     # stop on first failure
make lint          # flake8
make typecheck     # mypy
make check         # lint + typecheck + test
```

All tests use a local mock target API — no real devices or Redis required.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) — you may use, modify, and share this
software for **any noncommercial purpose** (evaluation, research, personal and
nonprofit/government use). Commercial use is not granted by this license.

**Commercial licensing:** a separate commercial license is available. Contact
benwold@gmail.com to discuss commercial use.

**Contributions:** by submitting a contribution you agree it is licensed under the same
terms and that the maintainer may also license it commercially (so the project can offer
commercial licenses that include your contribution).
