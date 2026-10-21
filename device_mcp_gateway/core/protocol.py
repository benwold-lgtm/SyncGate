# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""The MCP protocol facts the gateway serves, in one place.

Until 0.4.0 the ``initialize`` result was built by hand in four places — the device pod, the
SSE fleet, the Streamable HTTP fleet and the fleet service — each with its own copy of the
capability set and server info. Four copies of a wire shape is four places for one of them to
miss a field the next revision requires, and the modern era (``2026-07-28``) adds several. So
the versions, the negotiation and the result shapes live here, and every surface asks.

**Which versions, and why** (roadmap decision 3). Advertised:

- ``2025-11-25`` and ``2025-06-18`` on the ``initialize`` handshake. ``2025-11-25`` is what an
  SDK-default client offers, so it negotiates without a fallback round trip. Echoing it is a
  conformance claim, and ``tests/test_protocol.py`` checks every result shape the gateway
  produces against the official SDK's wire models for it rather than assuming the delta from
  ``2025-06-18`` is harmless.
- ``2026-07-28``, the stateless era, carried per request in ``_meta`` rather than negotiated.

``2025-03-26`` and ``2024-11-05`` were listed until 0.4.0 and never tested against; advertising
an untested version is a claim the gateway cannot back, so they are dropped. A client asking
for one is answered with the preferred version, which is what the handshake specifies.
"""

from __future__ import annotations

from typing import Any

#: Revisions reachable through the ``initialize`` handshake, newest first.
HANDSHAKE_PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18")
#: Revisions that use the stateless per-request envelope (no handshake, no session).
MODERN_PROTOCOL_VERSIONS: tuple[str, ...] = ("2026-07-28",)
#: What an ``initialize`` that asks for something unsupported (or nothing) is answered with.
PREFERRED_HANDSHAKE_VERSION: str = HANDSHAKE_PROTOCOL_VERSIONS[0]


def negotiate_protocol_version(requested: Any) -> str:
    """The ``protocolVersion`` to answer an ``initialize`` with.

    Echoes the client's request when it is a handshake revision the gateway serves, and
    otherwise answers the preferred one, which per the spec tells the client to continue on
    that version or disconnect. A modern revision is not negotiable here: it is never reached
    through ``initialize``, so asking for it on the handshake gets the preferred version too.
    """
    if isinstance(requested, str) and requested in HANDSHAKE_PROTOCOL_VERSIONS:
        return requested
    return PREFERRED_HANDSHAKE_VERSION


def server_capabilities(*, resources: bool, prompts: bool) -> dict[str, Any]:
    """The capability object the gateway advertises.

    ``listChanged`` is ``False`` everywhere, honestly: the gateway polls upstreams for tool
    changes and records them (F-41) rather than pushing notifications (roadmap decision 4).
    A device pod advertises resources and prompts even when it has none, because an empty
    list is a truthful "none" where "method not found" would not be. A fleet serves tools only
    (ADR-0008), so it does not advertise them.
    """
    caps: dict[str, Any] = {"tools": {"listChanged": False}}
    if resources:
        caps["resources"] = {"listChanged": False, "subscribe": False}
    if prompts:
        caps["prompts"] = {"listChanged": False}
    return caps


#: A device pod's capabilities: tools, resources and prompts.
DEVICE_CAPABILITIES: dict[str, Any] = server_capabilities(resources=True, prompts=True)
#: A fleet's capabilities: tools only (ADR-0008).
FLEET_CAPABILITIES: dict[str, Any] = server_capabilities(resources=False, prompts=False)


def initialize_response(
    msg_id: Any,
    params: Any,
    *,
    server_name: str,
    server_version: str,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    """The JSON-RPC response to an ``initialize`` request, for any surface."""
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "protocolVersion": negotiate_protocol_version(requested),
            # A copy, so a caller that edits its result cannot change what the next one sends.
            "capabilities": {k: dict(v) for k, v in capabilities.items()},
            "serverInfo": {"name": server_name, "version": server_version},
        },
    }


# --- the stateless era (2026-07-28) ----------------------------------------------------------
#
# A modern request carries everything a handshake used to establish in its own `_meta`, and
# expects its answer on the response to that POST. What follows is the edge of that: deciding a
# request is modern, checking its envelope, and shaping what goes back. The pods are untouched —
# they still produce handshake-era results, and `modern_response` turns one into the other —
# so a distributed worker needs no change to answer a modern request, which is what keeps a
# rolling upgrade safe in both directions.
#
# Modelled on the official SDK (`mcp.shared.inbound`, `mcp.server.runner`), which is the
# reference both directions are tested against; the gateway has no runtime dependency on it.

#: The ``_meta`` keys of the per-request envelope.
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"

#: Routing headers (lowercase, as ASGI delivers them).
PROTOCOL_VERSION_HEADER = "mcp-protocol-version"
METHOD_HEADER = "mcp-method"
NAME_HEADER = "mcp-name"
#: Method → the params key a modern client mirrors into ``Mcp-Name``.
NAME_BEARING_METHODS: dict[str, str] = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}

#: JSON-RPC codes, including those 2026-07-28 introduces (the -32020..-32099 band is reserved).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: The HTTP status a modern error answers with (the SDK's table). Unlisted codes answer 200.
ERROR_HTTP_STATUS: dict[int, int] = {
    PARSE_ERROR: 400,
    INVALID_REQUEST: 400,
    INVALID_PARAMS: 400,
    HEADER_MISMATCH: 400,
    UNSUPPORTED_PROTOCOL_VERSION: 400,
    METHOD_NOT_FOUND: 404,
}

#: Methods a device serves in the stateless era. ``initialize``, ``ping``, ``logging/setLevel``
#: and ``resources/subscribe`` do not exist there; asking for one is "method not found".
MODERN_DEVICE_METHODS: frozenset[str] = frozenset(
    {"server/discover", "tools/list", "tools/call", "resources/list", "resources/read", "prompts/list", "prompts/get"}
)
#: Results that carry a cache hint in this era, and for how long (ms). The set is the SDK's:
#: every result model with a required ``ttlMs``, which includes ``resources/read`` — leaving the
#: hint off one does not merely omit a hint, it makes the result fail its model.
#:
#: - Lists and discovery: one minute. F-41 can change a tool set at any spec poll, so a client
#:   that re-lists every minute sees a change within one.
#: - ``resources/read``: zero, "immediately stale". A read is the device's live state — a
#:   sensor's status — and a client caching it would be caching a measurement.
#:
#: ``private`` throughout: none of this is user-specific today, but it sits behind
#: authentication, and a shared cache serving it across tokens is not something to invite.
CACHE_TTL_MS: dict[str, int] = {
    "server/discover": 60_000,
    "tools/list": 60_000,
    "resources/list": 60_000,
    "prompts/list": 60_000,
    "resources/read": 0,
}
CACHE_SCOPE = "private"


def is_modern_request(headers: Any, payload: Any) -> bool:
    """Whether a POST belongs to the stateless era.

    The ``MCP-Protocol-Version`` header decides when present: anything other than a handshake
    revision, including a value nobody recognises, is modern, so the ladder below can answer
    it with a structured refusal rather than the handshake path answering it with a missing
    session. With no header, a body whose ``_meta`` carries the reserved protocol-version key
    is modern too; that key exists only in modern envelopes, so legacy traffic never mints it.
    The ladder then refuses the missing header by name.
    """
    version = headers.get(PROTOCOL_VERSION_HEADER) if headers is not None else None
    if version is not None:
        return version not in HANDSHAKE_PROTOCOL_VERSIONS
    params = payload.get("params") if isinstance(payload, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta


def rpc_failure(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """A JSON-RPC error response (``id`` kept even when null, as JSON-RPC requires)."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def unsupported_version(msg_id: Any, requested: Any, supported: tuple[str, ...]) -> dict[str, Any]:
    """``-32022``, naming what is served — the one code an auto-negotiating client acts on."""
    return rpc_failure(
        msg_id,
        UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version",
        {"supported": list(supported), "requested": requested if isinstance(requested, str) else str(requested)},
    )


def classify_modern(payload: dict[str, Any], headers: Any) -> dict[str, Any] | None:
    """Run the modern envelope ladder; return the JSON-RPC refusal, or None if it passes.

    The SDK's rungs, in its order, first failure wins:

    1. ``params._meta`` carries the protocol version and client capabilities → else -32602.
    2. ``MCP-Protocol-Version`` equals the envelope's version, ``Mcp-Method`` the body's
       method, and for a name-bearing method ``Mcp-Name`` the named param → else -32020.
       Checked before the version so a client that disagrees with itself is told so.
    3. The version is one this gateway serves in the modern era → else -32022.
    """
    msg_id = payload.get("id")
    raw_params = payload.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return rpc_failure(
            msg_id,
            INVALID_PARAMS,
            f"params._meta must be an object carrying the required {PROTOCOL_VERSION_META_KEY!r} and "
            f"{CLIENT_CAPABILITIES_META_KEY!r} envelope keys",
        )
    missing = [k for k in (PROTOCOL_VERSION_META_KEY, CLIENT_CAPABILITIES_META_KEY) if k not in meta]
    if missing:
        return rpc_failure(
            msg_id, INVALID_PARAMS, f"params._meta is missing the required envelope key(s): {', '.join(missing)}"
        )
    version = meta[PROTOCOL_VERSION_META_KEY]
    if headers.get(PROTOCOL_VERSION_HEADER) is None or headers.get(PROTOCOL_VERSION_HEADER) != version:
        return rpc_failure(
            msg_id,
            HEADER_MISMATCH,
            f"{PROTOCOL_VERSION_HEADER} header does not match the request envelope's protocol version",
        )
    method = payload.get("method")
    if headers.get(METHOD_HEADER) != method:
        return rpc_failure(msg_id, HEADER_MISMATCH, f"{METHOD_HEADER} header does not match the request body's method")
    name_key = NAME_BEARING_METHODS.get(method) if isinstance(method, str) else None
    if name_key is not None:
        body_value = params.get(name_key)
        if body_value is not None and _decode_header_value(headers.get(NAME_HEADER)) != body_value:
            return rpc_failure(
                msg_id,
                HEADER_MISMATCH,
                f"{NAME_HEADER} header does not match the request body's {name_key!r} parameter",
            )
    if not isinstance(version, str):
        return rpc_failure(msg_id, INVALID_PARAMS, "the protocol-version envelope value must be a string")
    if version not in MODERN_PROTOCOL_VERSIONS:
        return unsupported_version(msg_id, version, MODERN_PROTOCOL_VERSIONS)
    return None


def _decode_header_value(value: str | None) -> str | None:
    """Undo the SDK's ``=?base64?…?=`` wrapping of a header value that is not plain ASCII."""
    import base64
    import binascii
    import re

    if value is None:
        return None
    m = re.fullmatch(r"=\?base64\?(?P<payload>.*)\?=", value)
    if m is None:
        return value
    try:
        decoded = base64.b64decode(m.group("payload"), validate=True)
    except binascii.Error:
        return None
    if base64.b64encode(decoded).decode("ascii") != m.group("payload"):
        return None  # non-canonical: the encoder never emits it, so it never matches
    try:
        return decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None


def discover_result(*, server_name: str, server_version: str, capabilities: dict[str, Any]) -> dict[str, Any]:
    """The ``server/discover`` result (before ``modern_response`` stamps it).

    ``supportedVersions`` lists the modern versions only, as the SDK server does: a client
    that has reached ``server/discover`` is choosing among per-request versions, and the
    handshake is reached by ``initialize``, not by picking from this list.
    """
    return {
        "supportedVersions": list(MODERN_PROTOCOL_VERSIONS),
        "capabilities": {k: dict(v) for k, v in capabilities.items()},
        "_meta": {SERVER_INFO_META_KEY: {"name": server_name, "version": server_version}},
    }


def modern_response(
    method: str, response: dict[str, Any] | None, *, server_name: str, server_version: str
) -> dict[str, Any] | None:
    """Turn a handshake-era JSON-RPC response into its 2026-07-28 form.

    - every result gets ``resultType`` (required in this era; ``complete`` is the only kind
      the gateway produces);
    - cacheable results get their hint (``CACHE_TTL_MS``);
    - every result gets the ``serverInfo`` stamp in ``_meta``, where the handshake used to
      deliver it once;
    - an unknown tool, which a pod answers -32601, becomes -32602: in this era -32601 is a
      **404**, and a client is entitled to read that as "this endpoint has no tools/call".

    A response that already carries a field keeps it: the stamp fills gaps, it never
    overwrites what a pod or upstream said.
    """
    if response is None:
        return None
    out = dict(response)
    if "error" in out:
        err = dict(out["error"] or {})
        if method == "tools/call" and err.get("code") == METHOD_NOT_FOUND:
            err["code"] = INVALID_PARAMS
        out["error"] = err
        return out
    result = out.get("result")
    result = dict(result) if isinstance(result, dict) else {}
    result.setdefault("resultType", "complete")
    if method in CACHE_TTL_MS:
        result.setdefault("ttlMs", CACHE_TTL_MS[method])
        result.setdefault("cacheScope", CACHE_SCOPE)
    meta = result.get("_meta")
    if meta is None:
        result["_meta"] = {SERVER_INFO_META_KEY: {"name": server_name, "version": server_version}}
    elif isinstance(meta, dict) and meta.get(SERVER_INFO_META_KEY) is None:
        result["_meta"] = {**meta, SERVER_INFO_META_KEY: {"name": server_name, "version": server_version}}
    out["result"] = result
    return out


def http_status_for(response: dict[str, Any] | None) -> int:
    """The HTTP status a modern response is sent with."""
    if isinstance(response, dict) and isinstance(response.get("error"), dict):
        return ERROR_HTTP_STATUS.get(response["error"].get("code"), 200)
    return 200
