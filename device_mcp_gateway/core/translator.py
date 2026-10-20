# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""
OpenAPI 3.0/3.1 -> MCP Manifest Translator

Converts an OpenAPI spec dict into:
  - MCP Tools (executable operations with JSON schema parameters)
  - MCP Resources (read-only data endpoints)
  - MCP Prompts (natural-language descriptions of what the device does)
"""

import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from openapi_spec_validator import validate as _validate_openapi_spec

from device_mcp_gateway.core.spec_limits import enforce_operation_count
from device_mcp_gateway.shared.keys import device_resource_uri

# Device-supplied spec text (summaries/descriptions/titles) becomes LLM-facing tool
# metadata, so it is untrusted (Tier-0 F-26 — schema poisoning / indirect prompt
# injection). Strip control chars, Unicode bidi overrides, and zero-width characters —
# all used to hide injected instructions — and length-cap. This removes obfuscation
# vectors and bounds size; it does not (and cannot, in the gateway) neutralize plain
# semantic injection, so the LLM client should still treat tool descriptions as
# untrusted, device-provided content.
_UNSAFE_TEXT_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f" "\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_MAX_DESC_LEN = 1024


def _sanitize_text(text: str | None, max_len: int = _MAX_DESC_LEN) -> str:
    """Strip obfuscation characters and cap length on untrusted spec text (Tier-0 F-26)."""
    if not text:
        return ""
    cleaned = _UNSAFE_TEXT_RE.sub("", text)
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


# Request-body content types we know how to encode (F-40), in selection priority.
# An operation may declare several; we pick one for the single MCP tool callable.
JSON_CONTENT = "application/json"
FORM_CONTENT = "application/x-www-form-urlencoded"
MULTIPART_CONTENT = "multipart/form-data"
_CONTENT_PRIORITY = (JSON_CONTENT, FORM_CONTENT, MULTIPART_CONTENT)


@dataclass
class RequestBodySpec:
    """How a tool's request body should be encoded for the upstream call (F-40).

    Captured from the OpenAPI ``requestBody`` so the adapter (``core.adapter``) can
    pick the right wire encoding — JSON, form-urlencoded, multipart, or a raw body —
    instead of always sending ``json=``.
    """

    content_type: str = JSON_CONTENT
    # Body field names whose OpenAPI schema is `format: binary` (multipart file parts).
    binary_fields: set[str] = field(default_factory=set)
    # True when the body is a single scalar/binary value (no object properties) sent
    # raw — e.g. application/octet-stream or text/plain. The lone field is `raw_field`.
    raw: bool = False
    raw_field: str | None = None


@dataclass
class ProxyToolSpec:
    """What a proxied tool needs that a translated one does not (ADR-0009).

    ``upstream_tool_name`` is the name to send in ``tools/call``, kept verbatim while the
    LLM-facing ``McpTool.name`` is sanitised and deduped — the same split as
    ``param_wire_names`` for OpenAPI parameters (F-04). Losing it means calling the wrong
    tool, or none.

    The hints mirror MCP's tool annotations. They are advisory metadata from an untrusted
    upstream, so they may relax nothing that matters for safety: idempotency defaults to
    ``False`` and a redelivered proxied call is refused rather than re-run (F-08).
    """

    upstream_tool_name: str
    idempotent_hint: bool = False
    read_only_hint: bool = False


@dataclass
class McpTool:
    """MCP tool representation.

    Two kinds share this shape, discriminated by ``source``: a tool translated from an
    OpenAPI operation (``method``/``path``/``param_locations`` populated) and one proxied
    from a remote MCP server (``proxy`` populated, no HTTP shape at all).

    **Readers must branch on ``source``, never on ``method == ""``.** A missing method is
    also what a failed round-trip looks like, and conflating the two is how a proxied write
    would come to look idempotent and be re-executed on redelivery.
    """

    name: str
    description: str
    schema: dict[str, Any]
    # OpenAPI only — empty for a proxied tool, which has no HTTP shape.
    method: str = ""
    path: str = ""
    tags: list[str] = field(default_factory=list)
    param_locations: dict[str, str] = field(default_factory=dict)
    request_body: RequestBodySpec | None = None
    # MCP-arg-name -> upstream wire name, only for params renamed to resolve a
    # cross-location name collision (F-04). Empty for the common no-collision case.
    param_wire_names: dict[str, str] = field(default_factory=dict)
    # "openapi" | "proxy" — see the class docstring.
    source: str = "openapi"
    proxy: ProxyToolSpec | None = None


@dataclass
class McpResource:
    """MCP resource representation."""

    uri: str
    name: str
    description: str
    mime_type: str = "application/json"


@dataclass
class McpPrompt:
    """MCP prompt template."""

    name: str
    description: str
    template: str
    arguments: list[str] = field(default_factory=list)
    # The subset of ``arguments`` a client must supply. A generated prompt requires none; a
    # proxied one carries its upstream's declaration.
    required_arguments: list[str] = field(default_factory=list)
    # A proxied prompt's name as the upstream knows it — what goes back on the wire, since
    # the listed name is sanitised and may differ. Empty for a generated prompt, which the
    # gateway renders itself from ``template``.
    upstream_name: str = ""


@dataclass
class McpManifest:
    """Complete MCP server manifest for a single device/API."""

    server_name: str
    server_version: str
    hostname: str
    tools: list[McpTool] = field(default_factory=list)
    resources: list[McpResource] = field(default_factory=list)
    prompts: list[McpPrompt] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def manifest_to_dict(manifest: Any) -> dict:
    """Convert an McpManifest to a JSON-safe plain dict for storage/transport.

    Lives next to the dataclass it serialises so both the embedded (PodSupervisor)
    and distributed (worker health loop) sides share one canonical form — and so the
    Redis round-trip and the REST ``/tools`` response can't drift apart. ``RequestBodySpec
    .binary_fields`` is a ``set`` that ``json.dumps`` can't encode, so sets are
    normalised to sorted lists (the worker's ``_dict_to_manifest`` restores them).
    """
    import dataclasses

    def _dc(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {k: _dc(v) for k, v in dataclasses.asdict(obj).items()}
        if isinstance(obj, dict):
            return {k: _dc(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_dc(i) for i in obj]
        if isinstance(obj, set):
            return sorted(obj)
        return obj

    return _dc(manifest)


def _sanitize_name(raw: str) -> str:
    """Convert a path segment or operationId into a valid MCP tool name."""
    import re

    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw.strip("/"))
    name = re.sub(r"_+", "_", name)
    return name.lower().strip("_")


def dedupe_name(base: str, used_names: set[str]) -> str:
    """Return ``base``, or ``base_2``/``base_3``/... if it's already in ``used_names``.

    Shared by per-device translation (collisions within one spec) and fleet
    aggregation (collisions across devices' sanitized hostname-prefixed names) —
    one place decides what a "renamed" tool name looks like.
    """
    if base not in used_names:
        return base
    n = 2
    while f"{base}_{n}" in used_names:
        n += 1
    return f"{base}_{n}"


class SpecTranslator:
    """Translates OpenAPI specs into MCP manifests."""

    def __init__(self) -> None:
        self._spec: dict[str, Any] = {}

    def translate(self, spec: dict, hostname: str = "test-device") -> McpManifest:
        """Main entry point: spec dict + hostname -> McpManifest."""
        # Reject a spec with an absurd operation count before the (potentially
        # expensive) validator and per-op translation run, so a hostile/huge spec
        # can't monopolise a translation-pool worker (F-09). Cheap, deterministic,
        # and applied at the one chokepoint every fetch path funnels through.
        enforce_operation_count(spec)
        try:
            _validate_openapi_spec(spec)
        except Exception as exc:
            # Deliberately broad, and the breadth is the fix. This guard named
            # `OpenAPISpecValidatorError`, which sits on a *different* branch of the
            # library's hierarchy from the errors ordinary validation raises:
            #
            #   OpenAPISpecValidatorError -> OpenAPIError -> Exception      (version detection)
            #   OpenAPIValidationError    -> ValidationError -> _Error      (the actual checks)
            #
            # So a malformed `info`, a missing required field or a wrong type — the common
            # cases — escaped uncaught, while only the rarer "cannot tell which OpenAPI
            # version this is" was converted. Every caller downstream catches `ValueError`
            # to report a spec rejection (`spawn_error`, the health loop's warning), so the
            # escape bypassed all of that and surfaced as an unhandled traceback instead.
            #
            # Re-enumerating the two branches would fix today's failure and leave the same
            # trap for the next library release. This try wraps exactly one call whose only
            # job is to decide whether the document is valid, and nothing of ours runs
            # inside it, so *any* exception from it means the same thing to a caller: this
            # spec cannot be used. The original type and message are preserved in the
            # message and the `from exc` chain.
            raise ValueError(f"Invalid OpenAPI spec: {type(exc).__name__}: {exc}") from exc
        self._spec = spec  # stored so _resolve_ref can walk the full document
        info = spec.get("info", {})
        manifest = McpManifest(
            server_name=f"mcp-{hostname}",
            server_version=info.get("version", "v1"),
            hostname=hostname,
            metadata={
                "title": _sanitize_text(info.get("title", hostname)),
                "description": _sanitize_text(info.get("description", "")),
                "openapi_version": spec.get("openapi", "?"),
            },
        )
        paths = spec.get("paths", {})
        _used_names: set[str] = set()
        for path, methods in paths.items():
            for method, op in methods.items():
                if not isinstance(op, dict):
                    continue
                if method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    continue
                tool = self._build_tool(method, path, op, hostname)
                if tool:
                    if tool.name in _used_names:
                        new_name = dedupe_name(tool.name, _used_names)
                        logger.warning(
                            f"Tool name collision: '{tool.name}' "
                            f"(operation: {op.get('operationId') or f'{method} {path}'})"
                            f" renamed to '{new_name}'"
                        )
                        tool.name = new_name
                    _used_names.add(tool.name)
                    manifest.tools.append(tool)
                if method.upper() == "GET":
                    resource = self._build_resource(path, op, hostname)
                    if resource:
                        manifest.resources.append(resource)
        manifest.prompts = self._build_prompts(spec, manifest)
        logger.info(
            f"Translated {len(manifest.tools)} tools, "
            f"{len(manifest.resources)} resources, "
            f"{len(manifest.prompts)} prompts for {hostname}"
        )
        return manifest

    # ---- Tool Building ----

    def _build_tool(self, method: str, path: str, op: dict, hostname: str) -> McpTool | None:
        """Convert a single OpenAPI operation into an MCP tool."""
        op_id = op.get("operationId", "")
        name = _sanitize_name(op_id or f"{method}_{path}")
        # Tool description is device-supplied and shown to the LLM → untrusted (F-26).
        description = _sanitize_text(op.get("summary") or op.get("description") or f"{method} {path}")
        parameters, required, locations, body_spec, wire_names = self._build_parameter_schema(op)
        # Defensive: never emit a required entry whose property was dropped (F-47).
        required = [r for r in required if r in parameters]
        tags = op.get("tags", [])
        return McpTool(
            name=name,
            description=description or f"{method} {path}",
            # `additionalProperties: false` is a statement of fact, not a strictness
            # preference. `parameters` holds every argument the dispatcher knows how to
            # place — path, query, header, cookie and flattened body — and anything outside
            # it is silently dropped on the way to the device, because there is nowhere to
            # put it. Leaving the schema open meant a hallucinated argument was accepted,
            # discarded, and reported as success, which reads to a model as confirmation
            # that the argument exists. Found on a live cluster.
            #
            # This applies only to schemas generated here. A proxied MCP upstream supplies
            # its own inputSchema and owns its own validation; tightening someone else's
            # contract could reject calls their server would have accepted.
            schema={
                "type": "object",
                "properties": parameters,
                "required": required,
                "additionalProperties": False,
            },
            method=method.upper(),
            path=path,
            tags=tags,
            param_locations=locations,
            request_body=body_spec,
            param_wire_names=wire_names,
        )

    def _resolve_ref(self, ref: str) -> dict:
        """Walk a JSON Pointer ($ref) from the spec root.

        Supports any internal ref (#/...) including components/schemas,
        components/parameters, components/requestBodies, etc.
        Logs a warning and returns {} for external file or URL refs.
        """
        if not ref.startswith("#/"):
            logger.warning(f"External $ref '{ref}' is not supported — using empty schema")
            return {}
        parts = ref.lstrip("#/").split("/")
        node: Any = self._spec
        for part in parts:
            if not isinstance(node, dict):
                logger.warning(f"Could not resolve $ref '{ref}'")
                return {}
            node = node.get(part)
            if node is None:
                logger.warning(f"Could not resolve $ref '{ref}' — segment '{part}' not found in spec")
                return {}
        return node if isinstance(node, dict) else {}

    def _resolve_schema(self, schema: dict, seen: set | None = None) -> dict:
        """Recursively resolve $ref and allOf, and nested object properties.

        ``anyOf``/``oneOf`` are **preserved** as composition (not flattened to a
        union) so the "exactly one of" / alternatives semantics survive into the
        emitted JSON Schema — MCP clients accept it (F-47). ``allOf`` is still
        merged (that is its JSON-Schema meaning). Each resolved level is finalized:
        ``nullable`` → a nullable type, and ``required`` reconciled against the
        surviving properties.
        """
        if seen is None:
            seen = set()

        if "$ref" in schema:
            ref = schema["$ref"]
            if ref in seen:
                return {"type": "object"}
            child_seen = seen | {ref}
            base = self._resolve_ref(ref)
            extra = {k: v for k, v in schema.items() if k != "$ref"}
            return self._resolve_schema({**base, **extra}, child_seen)

        if "allOf" in schema:
            result = self._merge_schemas(schema["allOf"], seen, require_all=True)
            for k, v in schema.items():
                if k != "allOf" and k not in result:
                    result[k] = v
            return self._finalize_schema(result)

        for combiner in ("anyOf", "oneOf"):
            if combiner in schema:
                branches = [self._resolve_schema(s, seen) for s in schema[combiner]]
                result = {k: v for k, v in schema.items() if k != combiner}
                result[combiner] = branches
                return self._finalize_schema(result)

        # Descend into every nested schema so a $ref (or allOf/anyOf) inside it is
        # resolved too. Previously only object `properties` were descended, so a $ref
        # in array `items` or map `additionalProperties` was left dangling in the
        # emitted tool schema and the MCP client couldn't resolve it (#11).
        result = dict(schema)
        props = result.get("properties")
        if isinstance(props, dict):
            result["properties"] = {k: self._resolve_schema(v, seen) for k, v in props.items()}
        items = result.get("items")
        if isinstance(items, dict):
            result["items"] = self._resolve_schema(items, seen)
        elif isinstance(items, list):  # tuple/positional item schemas
            result["items"] = [self._resolve_schema(i, seen) if isinstance(i, dict) else i for i in items]
        addl = result.get("additionalProperties")
        if isinstance(addl, dict):  # a schema for map values (not the bool form)
            result["additionalProperties"] = self._resolve_schema(addl, seen)
        return self._finalize_schema(result)

    @staticmethod
    def _finalize_schema(schema: dict) -> dict:
        """Normalize a resolved schema: OpenAPI 3.0 ``nullable`` → a JSON-Schema
        nullable type, and drop ``required`` entries with no surviving property (F-47)."""
        # nullable (OpenAPI 3.0 keyword, not valid JSON Schema) → add "null" to the type.
        if schema.pop("nullable", False):
            t = schema.get("type")
            if isinstance(t, str):
                schema["type"] = [t, "null"]
            elif isinstance(t, list):
                if "null" not in t:
                    schema["type"] = [*t, "null"]
            else:
                for comb in ("oneOf", "anyOf"):
                    if isinstance(schema.get(comb), list):
                        schema[comb] = [*schema[comb], {"type": "null"}]
                        break
        # required must only name properties that actually survived (e.g. after a
        # cross-location collision drop) — a stale entry makes the schema invalid.
        props, required = schema.get("properties"), schema.get("required")
        if isinstance(props, dict) and isinstance(required, list):
            kept = [r for r in required if r in props]
            if kept:
                schema["required"] = kept
            else:
                schema.pop("required", None)
        return schema

    def _merge_schemas(self, schemas: list[dict], seen: set, require_all: bool) -> dict:
        """Merge a list of sub-schemas.

        require_all=True (allOf): every schema applies — union properties, union required.
        require_all=False (anyOf/oneOf): alternatives — union properties, no required.
        """
        props: dict[str, Any] = {}
        req: list[str] = []
        base: dict[str, Any] = {}
        for sub in schemas:
            resolved = self._resolve_schema(sub, seen)
            props.update(resolved.get("properties", {}))
            if require_all:
                req.extend(resolved.get("required", []))
            # Preserve typing/aids from the first sub-schema that carries them (F-47).
            for k in ("type", "description", "title", "discriminator", "example", "examples", "format"):
                if k in resolved and k not in base:
                    base[k] = resolved[k]
        result = dict(base)
        if props:
            result.setdefault("type", "object")
            result["properties"] = props
        if req:
            result["required"] = list(dict.fromkeys(req))
        return result

    def _build_parameter_schema(
        self, op: dict
    ) -> tuple[dict[str, Any], list[str], dict[str, str], RequestBodySpec | None, dict[str, str]]:
        """Extract JSON-serializable parameter schema and per-param locations from an operation.

        Params are exposed as one flat ``properties`` object, but OpenAPI allows the same
        name in two locations (e.g. ``id`` in path *and* body). To avoid last-write-wins
        silently dropping one (F-04), colliding names are disambiguated: path params keep
        their bare name (the ``{placeholder}`` must match literally), and a colliding
        query/header/body param is suffixed (``id__query``). ``wire_names`` maps the
        renamed MCP arg back to the upstream wire name so the call routes correctly.
        """
        params: dict[str, Any] = {}
        locations: dict[str, str] = {}
        req: list[str] = []
        wire_names: dict[str, str] = {}

        # Collect non-body params, then process path-first so they claim their bare names.
        collected: list[tuple[dict, str, str]] = []
        for raw_param in op.get("parameters", []):
            param = raw_param
            if "$ref" in raw_param:
                param = self._resolve_ref(raw_param["$ref"])
                if not param:
                    logger.warning(
                        f"Dropping a parameter with an unresolvable $ref '{raw_param['$ref']}' — "
                        "the generated tool will be missing it"
                    )
                    continue
            pname = param.get("name", "")
            if not pname:
                continue
            pin = param.get("in", "query")
            if pin in ("body", "cookie"):
                continue
            collected.append((param, pname, pin))

        collected.sort(key=lambda t: 0 if t[2] == "path" else 1)
        for param, pname, pin in collected:
            resolved = self._resolve_schema(param.get("schema", {}))
            param_desc = param.get("description", resolved.get("description", ""))
            key = self._claim_param(pname, pin, locations, wire_names)
            params[key] = {**resolved, "description": param_desc}
            locations[key] = pin
            if param.get("required"):
                req.append(key)

        body = op.get("requestBody", {})
        if "$ref" in body:
            ref = body["$ref"]
            body = self._resolve_ref(ref)
            if not body:
                logger.warning(f"Request body $ref '{ref}' is unresolvable — the generated tool will expose no body")
        body_spec: RequestBodySpec | None = None
        if body:
            content = body.get("content", {})
            ctype = self._select_content_type(content)
            if ctype is not None:
                body_spec = self._build_body_spec(ctype, content[ctype], params, locations, req, wire_names)

        return params, req, locations, body_spec, wire_names

    @staticmethod
    def _claim_param(name: str, pin: str, locations: dict[str, str], wire_names: dict[str, str]) -> str:
        """Return the property key for a param, disambiguating cross-location collisions (F-04)."""
        if name not in locations:
            return name
        if locations[name] == pin:
            # Same name + same location is an invalid spec; keep last, but make it visible.
            logger.warning(f"Duplicate '{pin}' parameter '{name}'; later definition overrides earlier")
            return name
        key = f"{name}__{pin}"
        n = 2
        while key in locations:
            key = f"{name}__{pin}_{n}"
            n += 1
        wire_names[key] = name
        logger.warning(f"Parameter name '{name}' appears in multiple locations; exposing the '{pin}' one as '{key}'")
        return key

    @staticmethod
    def _select_content_type(content: dict[str, Any]) -> str | None:
        """Pick one request-body content type (a tool is a single callable) — F-40.

        Prefers JSON, then form, then multipart; otherwise the first declared type
        (treated as a raw body). Returns None when no content schema is present.
        """
        if not content:
            return None
        for known in _CONTENT_PRIORITY:
            if known in content:
                return known
        return next(iter(content))

    def _build_body_spec(
        self,
        ctype: str,
        media: dict[str, Any],
        params: dict[str, Any],
        locations: dict[str, str],
        req: list[str],
        wire_names: dict[str, str],
    ) -> RequestBodySpec:
        """Flatten the chosen request body into tool params and describe its encoding (F-40)."""
        schema = self._resolve_schema(media["schema"]) if media and "schema" in media else {}
        props = dict(schema.get("properties", {})) if isinstance(schema, dict) else {}
        # A top-level composed body (oneOf/anyOf is now preserved, not flattened) has no
        # top-level `properties`; union the branch properties so the flat body args still
        # work. Required is intentionally not carried across alternatives (F-47).
        if not props and isinstance(schema, dict):
            for comb in ("oneOf", "anyOf"):
                for branch in schema.get(comb, []) or []:
                    if isinstance(branch, dict):
                        props.update(branch.get("properties", {}))
                if props:
                    break

        # Object body → flatten each property into a body parameter (existing behaviour),
        # tracking which properties are binary so multipart sends them as file parts.
        if props:
            binary: set[str] = set()
            required = set(schema.get("required", []))
            for k, v in props.items():
                resolved = self._resolve_schema(v)
                # A body field name may collide with a path/query param — disambiguate (F-04).
                key = self._claim_param(k, "body", locations, wire_names)
                params[key] = resolved
                locations[key] = "body"
                if self._is_binary_schema(resolved):
                    binary.add(key)
                if k in required:
                    req.append(key)
            return RequestBodySpec(content_type=ctype, binary_fields=binary)

        # Non-object body (e.g. application/octet-stream or text/plain with a scalar
        # schema): expose a single `body` parameter sent raw rather than dropping it.
        raw_field = self._claim_param("body", "body", locations, wire_names)
        params.setdefault(raw_field, {**schema, "description": schema.get("description", "Raw request body")})
        locations[raw_field] = "body"
        return RequestBodySpec(content_type=ctype, raw=True, raw_field=raw_field)

    @staticmethod
    def _is_binary_schema(schema: dict[str, Any]) -> bool:
        """True for an OpenAPI `string` schema with `format: binary`/`byte` (a file part)."""
        return schema.get("type") == "string" and schema.get("format") in ("binary", "byte")

    # ---- Resource Building ----

    def _build_resource(self, path: str, op: dict, hostname: str) -> McpResource | None:
        """Convert a GET operation into a read-only MCP resource."""
        desc = _sanitize_text(op.get("description", "") or op.get("summary", "") or f"Resource at {path}")
        uri = device_resource_uri(hostname, path)
        name = _sanitize_name(path)
        return McpResource(
            uri=uri,
            name=name,
            description=desc,
            mime_type="application/json",
        )

    # ---- Prompt Building ----

    def _build_prompts(self, spec: dict, manifest: McpManifest) -> list[McpPrompt]:
        """Generate natural-language prompt templates from spec info."""
        prompts = []
        title = manifest.metadata.get("title", manifest.hostname)
        desc = manifest.metadata.get("description", "")
        if desc:
            prompts.append(
                McpPrompt(
                    name=f"what_is_{_sanitize_name(title)}",
                    description=f"Describe what {title} does",
                    template=f"You are given access to a device API called '{title}'. "
                    f"Context: {desc}\n\n"
                    f"Available tools: {[t.name for t in manifest.tools]}",
                )
            )
        return prompts
