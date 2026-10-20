# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Discovering an upstream MCP server's tools, resources and prompts, and noticing when they change.

This is the proxy-side counterpart to ``registry/spec_service.py`` and it deliberately
keeps that module's contract: ``fetch_spec(profile)`` records what it found on the profile
and **returns whether it changed**, leaving the pod replace to the caller.

Two things here are not obvious, and both are load-bearing.

**The hash is over a canonical projection, not the raw response.** The OpenAPI path gets
away with hashing a parsed document because a static file parses in a fixed order.
``tools/list`` ordering is server-controlled and may differ between two identical polls.
Hashing the raw response would make *every* poll look like a change: the pod is replaced
each cycle, ``tools_revision`` climbs forever, every replace records a breaking change, and
a fleet of proxied devices generates a continuous alert storm while thrashing its own pods.
So the hash covers a sorted, key-ordered projection of only the fields that define the tool
contract — name, description, schema, annotations. An upstream that stamps a timestamp into
each entry changes nothing.

**The translator's protections do not come along for free.** ``_sanitize_text`` (F-26) and
the operation-count cap (F-09) live inside ``SpecTranslator``, which this path never calls.
They are re-applied here explicitly. An upstream MCP server is a *less* trustworthy source
of LLM-facing text than an OpenAPI document, not more: its tool descriptions are
instructions the model will read, and it can rewrite them at any time.

**Resources and prompts get the same treatment** (ADR-0028). They are listed only when the
upstream advertises the capability, capped, and their LLM-facing text sanitised. A resource's
URI is passed through *verbatim* (the gateway mints no URI shape of its own, which is what
ADR-0009 declined to do) and a URI that sanitisation would alter is dropped rather than
listed under a form the upstream would not recognise. A failure to list either kind costs
only that kind: ``discovered`` records it, and the device's tools are unaffected.

Discovery deliberately does **not** go through ``run_translation``/``_spec_executor``. That
pool is for CPU-bound parsing of a document already in memory; discovery is async network
I/O, would need a picklable callable, and would occupy a translation slot for the length of
a network round-trip.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from loguru import logger

from device_mcp_gateway.audit import redact_url
from device_mcp_gateway.core.spec_limits import DEFAULT_MAX_OPERATIONS, SpecTooLargeError
from device_mcp_gateway.core.translator import (
    McpManifest,
    McpPrompt,
    McpResource,
    McpTool,
    ProxyToolSpec,
    _sanitize_name,
    _sanitize_text,
    dedupe_name,
)
from device_mcp_gateway.registry.models import DeviceProfile
from device_mcp_gateway.shared.registry_backend import AbstractRegistryBackend
from device_mcp_gateway.upstream.mcp_client import StreamableHttpClient

# Fields that define a tool's contract. Anything else an upstream includes is ignored for
# change detection, so incidental per-response metadata cannot look like a rug-pull.
_CONTRACT_FIELDS = ("name", "description", "inputSchema", "annotations")


def _declared_from_server_info(init_result: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract (name, version) from an MCP ``initialize`` result.

    Tolerant by design: ``serverInfo`` is optional in the protocol and a server may send a
    partial one. A missing field yields None, which ``compare()`` treats as "learned
    nothing" rather than as a change — so a terse upstream does not raise a false alarm on
    every probe.
    """
    info = init_result.get("serverInfo") or {}
    if not isinstance(info, dict):
        return (None, None)
    name = info.get("name")
    version = info.get("version")
    return (
        str(name) if isinstance(name, (str, int, float)) and str(name).strip() else None,
        str(version) if isinstance(version, (str, int, float)) and str(version).strip() else None,
    )


def canonical_tools_hash(tools: list[dict[str, Any]]) -> str:
    """A stable hash of a ``tools/list`` response.

    Sorted by tool name with sorted keys, over the contract fields only — so reordering,
    key-order differences and extra per-response metadata are all invisible, while a
    changed description, schema or tool set is not.
    """
    projection = sorted(
        ({k: t.get(k) for k in _CONTRACT_FIELDS if k in t} for t in tools if isinstance(t, dict)),
        key=lambda t: str(t.get("name", "")),
    )
    blob = json.dumps(projection, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# Contract fields of a resource and a prompt, for change detection — as _CONTRACT_FIELDS is
# for a tool.
_RESOURCE_FIELDS = ("uri", "name", "description", "mimeType")
_PROMPT_FIELDS = ("name", "description", "arguments")
_MAX_URI_LEN = 2048


def canonical_snapshot_hash(spec: dict[str, Any]) -> str:
    """The change-detection hash for a whole upstream snapshot.

    **Byte-identical to ``canonical_tools_hash`` when the upstream lists no resources and no
    prompts** — which is every proxied device from before ADR-0028. Without that, upgrading
    would make every MCP device in a fleet look changed on its first poll and replace every
    pod at once. Only an upstream that really serves resources or prompts gets a new hash,
    once.
    """
    tools_hash = canonical_tools_hash(spec.get("tools", []))
    resources = spec.get("resources") or []
    prompts = spec.get("prompts") or []
    if not resources and not prompts:
        return tools_hash

    def project(items: list[Any], fields: tuple[str, ...], key: str) -> list[dict[str, Any]]:
        return sorted(
            ({k: i.get(k) for k in fields if k in i} for i in items if isinstance(i, dict)),
            key=lambda i: str(i.get(key, "")),
        )

    blob = json.dumps(
        {
            "tools": tools_hash,
            "resources": project(resources, _RESOURCE_FIELDS, "uri"),
            "prompts": project(prompts, _PROMPT_FIELDS, "name"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


async def snapshot_upstream(upstream: StreamableHttpClient) -> dict[str, Any]:
    """What an upstream MCP server exposes: its tools, and — when advertised — resources and prompts.

    The one discovery routine every mode uses (embedded discovery, the worker's health loop
    and its spawn path), so they cannot disagree about a device. Tools failing raises, as
    before: a device without its tools is not usable. Resources or prompts failing is
    recorded in ``discovered`` and costs only that kind.
    """
    tools = await upstream.list_tools()  # handshakes first, which records the capabilities
    caps = upstream.server_capabilities
    snapshot: dict[str, Any] = {"upstream_kind": "mcp", "tools": tools, "resources": [], "prompts": []}
    discovered = {"resources": True, "prompts": True}
    for kind, lister in (("resources", upstream.list_resources), ("prompts", upstream.list_prompts)):
        if kind not in caps:
            continue  # not advertised: the server has none, which is a discovered fact
        try:
            snapshot[kind] = await lister()
        except Exception as exc:  # noqa: BLE001 — one kind failing must not cost the tools
            discovered[kind] = False
            logger.warning(f"MCP {kind}/list failed for {redact_url(upstream._url)}: {exc}")
    snapshot["discovered"] = discovered
    return snapshot


def build_proxy_manifest_from(
    hostname: str, spec: dict[str, Any], *, max_tools: int = DEFAULT_MAX_OPERATIONS
) -> McpManifest:
    """``build_proxy_manifest`` over a ``snapshot_upstream`` result — the call every site makes."""
    return build_proxy_manifest(
        hostname,
        spec.get("tools", []),
        resources=spec.get("resources"),
        prompts=spec.get("prompts"),
        # A snapshot from before ADR-0028 has no "discovered": nothing was looked for.
        discovered=spec.get("discovered") or {"resources": False, "prompts": False},
        max_tools=max_tools,
    )


def build_proxy_manifest(
    hostname: str,
    tools: list[dict[str, Any]],
    *,
    resources: list[dict[str, Any]] | None = None,
    prompts: list[dict[str, Any]] | None = None,
    discovered: dict[str, bool] | None = None,
    server_version: str = "1.0.0",
    max_tools: int = DEFAULT_MAX_OPERATIONS,
) -> McpManifest:
    """Turn a ``tools/list`` response into a manifest of proxied tools.

    Applies, in order: the count cap (F-09), name sanitisation and collision-safe deduping
    (F-04), and text sanitisation of every LLM-facing string (F-26). The upstream's own
    tool name is preserved verbatim on ``ProxyToolSpec`` — it is what goes back on the wire.
    """
    if max_tools > 0 and len(tools) > max_tools:
        raise SpecTooLargeError(f"upstream published {len(tools)} tools, over the {max_tools} cap")

    used: set[str] = set()
    built: list[McpTool] = []
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        wire_name = entry.get("name")
        if not isinstance(wire_name, str) or not wire_name:
            continue
        base = _sanitize_name(_sanitize_text(wire_name, max_len=128))
        if not base:
            # Nothing usable survived sanitisation; a tool named after nothing is worse
            # than a missing one — it cannot be referred to and collides with the next.
            logger.warning(f"Dropping upstream tool with no usable name: {wire_name!r}")
            continue
        name = dedupe_name(base, used)
        used.add(name)

        schema = entry.get("inputSchema")
        if not isinstance(schema, dict):
            # Handed to jsonschema for F-28 validation and to the LLM as the contract.
            # A non-object schema must become an empty object, not be trusted through.
            schema = {"type": "object"}

        raw_annotations = entry.get("annotations")
        annotations: dict[str, Any] = raw_annotations if isinstance(raw_annotations, dict) else {}
        built.append(
            McpTool(
                name=name,
                description=_sanitize_text(entry.get("description")),
                schema=schema,
                source="proxy",
                proxy=ProxyToolSpec(
                    upstream_tool_name=wire_name,
                    # Advisory metadata from an untrusted upstream. Recorded for
                    # diagnostics; never used to relax the idempotency guard (F-08).
                    idempotent_hint=bool(annotations.get("idempotentHint", False)),
                    read_only_hint=bool(annotations.get("readOnlyHint", False)),
                ),
            )
        )

    found = dict(discovered or {"resources": False, "prompts": False})
    built_resources = _proxy_resources(hostname, resources or [], max_tools, found)
    built_prompts = _proxy_prompts(hostname, prompts or [], max_tools, found)
    return McpManifest(
        server_name=f"mcp-{hostname}",
        server_version=server_version,
        hostname=hostname,
        tools=built,
        resources=built_resources,
        prompts=built_prompts,
        metadata={"upstream_kind": "mcp", "discovered": found},
    )


def _proxy_resources(hostname: str, entries: list[Any], cap: int, found: dict[str, bool]) -> list[McpResource]:
    if cap > 0 and len(entries) > cap:
        # Unlike tools, too many resources does not reject the device — its tools still
        # work. The kind is reported as not discovered rather than silently truncated.
        logger.warning(f"{hostname}: upstream listed {len(entries)} resources, over the {cap} cap; not listing them")
        found["resources"] = False
        return []
    out: list[McpResource] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        uri = entry.get("uri")
        if not isinstance(uri, str) or not uri or len(uri) > _MAX_URI_LEN or uri in seen:
            continue
        if _sanitize_text(uri, max_len=_MAX_URI_LEN) != uri:
            # Listed verbatim or not at all: a "cleaned" URI would be one the upstream does
            # not know, and a URI that needs cleaning is carrying something it should not.
            logger.warning(f"{hostname}: dropping upstream resource with an unsafe URI")
            continue
        seen.add(uri)
        mime = entry.get("mimeType")
        out.append(
            McpResource(
                uri=uri,
                name=_sanitize_text(entry.get("name") or uri, max_len=128),
                description=_sanitize_text(entry.get("description")),
                mime_type=mime if isinstance(mime, str) and mime and len(mime) <= 128 else "application/octet-stream",
            )
        )
    return out


def _proxy_prompts(hostname: str, entries: list[Any], cap: int, found: dict[str, bool]) -> list[McpPrompt]:
    if cap > 0 and len(entries) > cap:
        logger.warning(f"{hostname}: upstream listed {len(entries)} prompts, over the {cap} cap; not listing them")
        found["prompts"] = False
        return []
    out: list[McpPrompt] = []
    used: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        wire_name = entry.get("name")
        if not isinstance(wire_name, str) or not wire_name:
            continue
        base = _sanitize_name(_sanitize_text(wire_name, max_len=128))
        if not base:
            continue
        name = dedupe_name(base, used)
        used.add(name)
        args: list[str] = []
        required: list[str] = []
        for a in entry.get("arguments") or []:
            if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]:
                args.append(a["name"])
                if a.get("required") is True:
                    required.append(a["name"])
        out.append(
            McpPrompt(
                name=name,
                description=_sanitize_text(entry.get("description")),
                template="",  # fetched from the upstream by prompts/get, never stored
                arguments=args,
                required_arguments=required,
                upstream_name=wire_name,
            )
        )
    return out


class McpDiscoveryService:
    """Reachability and tool discovery for an upstream MCP server.

    Mirrors the parts of ``SpecService`` the Registry actually calls, so provisioning and
    the health loop can pick an implementation instead of branching inline.
    """

    def __init__(
        self,
        *,
        backend: AbstractRegistryBackend,
        config: dict[str, Any],
        client_factory: Any,
    ) -> None:
        self._backend = backend
        self._config = config
        # ``client_factory(hostname)`` returns the guarded httpx client for that device.
        # Injected so the Registry's pooled clients are reused rather than a second
        # egress path appearing — and taking the hostname so a proxied MCP upstream gets
        # the same per-device TLS profile an OpenAPI device would.
        self._client_factory = client_factory
        self._max_tools = config.get("max_upstream_tools", DEFAULT_MAX_OPERATIONS)
        self._timeout = config.get("discovery", {}).get("timeout", 10)

    def client_for(self, profile: DeviceProfile, http: Any = None) -> StreamableHttpClient:
        """``http`` overrides the pooled client — the probe passes its fingerprint-capturing one."""
        return StreamableHttpClient(
            url=profile.base_url,
            get_client=(lambda: http) if http is not None else (lambda: self._client_factory(profile.hostname)),
            auth=profile.auth,
            timeout=self._timeout,
        )

    async def check_reachable(self, profile: DeviceProfile, http: Any = None) -> bool:
        """Reachability for an MCP upstream is a successful ``initialize``.

        The OpenAPI probe scores ``status_code < 500`` as reachable, which is wrong here: an
        MCP endpoint answers a bare GET with 405 or 404, so a dead or misconfigured upstream
        would read as healthy and its pod would be spawned against nothing.
        """
        upstream = self.client_for(profile, http)
        try:
            info = await upstream.initialize()
        except Exception as exc:  # noqa: BLE001 — any failure means "not reachable"
            logger.debug(f"MCP initialize failed for {redact_url(profile.base_url)}: {exc}")
            return False
        finally:
            # A probe is not a session. Leaving it open would abandon one on the upstream
            # every health cycle.
            await upstream.close_session()
        # ADR-0015 / F-69: `serverInfo` used to be logged here and dropped. It is the
        # closest thing MCP has to the OpenAPI `info` block — the upstream's own statement
        # of what it is — and it serves two purposes at once: fleet inventory (operators
        # need to know what a device *is* to manage it by name) and a change signal.
        #
        # It is SELF-REPORTED and therefore spoofable, so it is recorded as the *declared*
        # dimension and must never be rendered as verification. The authenticated dimension
        # is the TLS SPKI, captured in the transport.
        profile.declared_identity = _declared_from_server_info(info)
        logger.debug(f"MCP upstream {profile.hostname} initialised: {info.get('serverInfo', {})}")
        return True

    async def fetch_spec(self, profile: DeviceProfile) -> bool:
        """Discover the upstream's tools, recording them on ``profile``.

        Same contract as ``SpecService.fetch_spec``: returns True when the tool set
        **changed** versus the stored hash, so the caller can replace the pod. Does not
        touch pods itself.
        """
        upstream = self.client_for(profile)
        try:
            snapshot = await snapshot_upstream(upstream)
        except Exception as exc:
            logger.warning(f"MCP discovery failed for {profile.hostname}: {exc}")
            return False
        finally:
            # Discovery is a one-shot poll, not a long-lived session — unlike the pod's
            # client, which keeps its session for the pod's lifetime.
            await upstream.close_session()

        h = canonical_snapshot_hash(snapshot)
        old_hash = profile.config.spec_hash
        profile.config.spec_hash = h
        # Stored in the same slot the OpenAPI path uses for its parsed document, tagged so
        # the PodSupervisor can tell the two apart without re-reading the device config.
        profile.spec_data = snapshot
        profile.config.last_check = time.time()
        await self._backend.update_device_fields(profile.hostname, spec_hash=h, last_check=profile.config.last_check)

        changed = old_hash is not None and h != old_hash
        if changed:
            logger.info(f"Upstream tool set changed for {profile.hostname}: {old_hash} → {h}")
        return changed
