# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tier-1 tests for tool-generation correctness (F-04).

Covers cross-location param-name collisions, URL-encoded path interpolation
(traversal/injection guard), the path-template error guard, and surfacing of
unresolvable external $refs.
"""

from unittest.mock import patch

import httpx
import pytest
from loguru import logger

from device_mcp_gateway.core.translator import (
    JSON_CONTENT,
    McpManifest,
    McpTool,
    RequestBodySpec,
    SpecTranslator,
)
from device_mcp_gateway.pods.device_pod import DevicePod

# --- collisions --------------------------------------------------------------


def _spec(paths):
    return {"openapi": "3.0.3", "info": {"title": "t", "version": "1.0.0"}, "paths": paths}


def test_path_and_body_same_name_both_survive():
    """`id` in path AND body must not last-write-wins; both are kept (F-04)."""
    spec = _spec(
        {
            "/users/{id}": {
                "post": {
                    "operationId": "make_user",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                                }
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    tool = SpecTranslator().translate(spec, "h").tools[0]
    # Path `id` keeps the bare name (must match the {id} placeholder); body `id` is suffixed.
    assert tool.param_locations["id"] == "path"
    assert tool.param_locations["id__body"] == "body"
    assert tool.param_wire_names["id__body"] == "id"  # upstream still receives "id"
    assert "name" in tool.param_locations  # the non-colliding field is untouched


def test_query_and_body_same_name_both_survive():
    spec = _spec(
        {
            "/search": {
                "post": {
                    "operationId": "search",
                    "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"type": "object", "properties": {"q": {"type": "string"}}}}
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    tool = SpecTranslator().translate(spec, "h").tools[0]
    assert tool.param_locations["q"] == "query"
    assert tool.param_locations["q__body"] == "body"
    assert tool.param_wire_names["q__body"] == "q"


def test_no_collision_leaves_names_bare():
    spec = _spec(
        {
            "/items/{item_id}": {
                "get": {
                    "operationId": "get_item",
                    "parameters": [{"name": "item_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    tool = SpecTranslator().translate(spec, "h").tools[0]
    assert tool.param_locations == {"item_id": "path"}
    assert tool.param_wire_names == {}


# --- external $ref surfacing -------------------------------------------------


def test_external_param_ref_warns_and_drops():
    # Tested at _build_parameter_schema level: the upfront OpenAPI validator would itself
    # reject an external $ref before translate() runs, so exercise the builder directly.
    # (loguru, not stdlib logging, so capture via a sink rather than caplog.)
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        op = {"parameters": [{"$ref": "https://example.com/params.yaml#/Foo"}]}
        params, _req, _locations, _body_spec, _wire_names = SpecTranslator()._build_parameter_schema(op)
    finally:
        logger.remove(sink_id)
    assert params == {}  # the unresolvable external param is dropped...
    assert any("unresolvable $ref" in m or "not supported" in m for m in messages)  # ...loudly


# --- path encoding / traversal guard (through the pod) -----------------------


def _pod_with_path_tool(path="/items/{item_id}"):
    manifest = McpManifest(
        server_name="m",
        server_version="1",
        hostname="dev",
        tools=[
            McpTool(
                name="t",
                description="d",
                schema={"type": "object", "properties": {}},
                method="GET",
                path=path,
                param_locations={"item_id": "path"},
            )
        ],
    )
    return DevicePod(hostname="dev", manifest=manifest, base_url="http://dev.local")


@pytest.mark.asyncio
async def test_path_param_is_url_encoded_blocking_traversal():
    captured = {}

    async def fake_request(self, method, url, **kwargs):
        captured["url"] = url
        return httpx.Response(status_code=200, content=b"{}", headers={"content-type": "application/json"})

    with patch("httpx.AsyncClient.request", fake_request):
        pod = _pod_with_path_tool()
        await pod._tool_dispatch["t"](item_id="../../admin")

    # The slashes/dots are percent-encoded — no extra path segments reach the upstream.
    assert captured["url"] == "http://dev.local/items/..%2F..%2Fadmin"
    assert "/admin" not in captured["url"]


@pytest.mark.asyncio
async def test_missing_path_param_returns_clean_error():
    pod = _pod_with_path_tool("/items/{item_id}/{sub}")  # tool only provides item_id
    result = await pod._tool_dispatch["t"](item_id="1")  # 'sub' missing
    assert result["ok"] is False
    assert result["status"] == 500
    assert "Path template error" in result["error"]["message"]


@pytest.mark.asyncio
async def test_body_field_collision_sent_under_wire_name():
    # A POST tool where body field `id` was renamed to `id__body`; upstream must see `id`.
    manifest = McpManifest(
        server_name="m",
        server_version="1",
        hostname="dev",
        tools=[
            McpTool(
                name="t",
                description="d",
                schema={"type": "object", "properties": {}},
                method="POST",
                path="/users/{id}",
                param_locations={"id": "path", "id__body": "body"},
                param_wire_names={"id__body": "id"},
                request_body=RequestBodySpec(content_type=JSON_CONTENT),
            )
        ],
    )
    captured = {}

    async def fake_request(self, method, url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return httpx.Response(status_code=200, content=b"{}", headers={"content-type": "application/json"})

    with patch("httpx.AsyncClient.request", fake_request):
        pod = DevicePod(hostname="dev", manifest=manifest, base_url="http://dev.local")
        await pod._tool_dispatch["t"](id="path-val", id__body=42)

    assert captured["url"] == "http://dev.local/users/path-val"  # path got the path arg
    assert captured["json"] == {"id": 42}  # body field sent under its wire name
