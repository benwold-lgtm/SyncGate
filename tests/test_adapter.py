# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tier-1 tests for the device request/response adapter seam (F-49 / F-39 / F-40).

Covers:
  - request-body encoding per content type (JSON / form / multipart / raw)
  - the uniform result envelope (success, upstream 4xx as error, oversized cap)
  - the translator selecting one content type and tagging binary fields
"""

from unittest.mock import patch

import httpx
import pytest

from device_mcp_gateway.core.adapter import DeviceAdapter
from device_mcp_gateway.core.translator import (
    FORM_CONTENT,
    JSON_CONTENT,
    MULTIPART_CONTENT,
    McpManifest,
    McpTool,
    RequestBodySpec,
    SpecTranslator,
)
from device_mcp_gateway.pods.device_pod import DevicePod

_MAX = 5 * 1024 * 1024


def _tool(method="POST", body=None, locations=None):
    return McpTool(
        name="t",
        description="d",
        schema={"type": "object", "properties": {}},
        method=method,
        path="/x",
        param_locations=locations or {},
        request_body=body,
    )


def _resp(status=200, body=b"{}", content_type="application/json"):
    return httpx.Response(status_code=status, content=body, headers={"content-type": content_type})


# --- F-40 request encoding ---------------------------------------------------


def test_encode_json_default():
    a = DeviceAdapter(_MAX)
    tool = _tool(body=RequestBodySpec(content_type=JSON_CONTENT))
    assert a.encode_body(tool, {"speed": 5}) == {"json": {"speed": 5}}


def test_encode_form_urlencoded():
    a = DeviceAdapter(_MAX)
    tool = _tool(body=RequestBodySpec(content_type=FORM_CONTENT))
    assert a.encode_body(tool, {"grant": "x"}) == {"data": {"grant": "x"}}


def test_encode_multipart_splits_files_and_data():
    a = DeviceAdapter(_MAX)
    tool = _tool(body=RequestBodySpec(content_type=MULTIPART_CONTENT, binary_fields={"file"}))
    out = a.encode_body(tool, {"file": "hello", "label": "doc"})
    assert out["files"] == {"file": b"hello"}
    assert out["data"] == {"label": "doc"}


def test_encode_raw_body_sets_content_and_type():
    a = DeviceAdapter(_MAX)
    tool = _tool(body=RequestBodySpec(content_type="application/octet-stream", raw=True, raw_field="body"))
    out = a.encode_body(tool, {"body": "raw-bytes"})
    assert out["content"] == b"raw-bytes"
    assert out["headers"]["content-type"] == "application/octet-stream"


def test_encode_no_body_for_get():
    a = DeviceAdapter(_MAX)
    tool = _tool(method="GET", body=None)
    assert a.encode_body(tool, {}) == {}


# --- F-39 result envelope ----------------------------------------------------


def test_build_result_success():
    a = DeviceAdapter(_MAX)
    env = a.build_result(_resp(200, b'{"status":"online"}'))
    assert env == {"ok": True, "status": 200, "body": {"status": "online"}}


def test_build_result_204_empty_body():
    a = DeviceAdapter(_MAX)
    env = a.build_result(_resp(204, b"", content_type=""))
    assert env == {"ok": True, "status": 204, "body": None}


def test_build_result_4xx_is_error_not_success():
    """The core F-39 fix: an upstream 4xx must surface as ok=False, not a fake success."""
    a = DeviceAdapter(_MAX)
    env = a.build_result(_resp(404, b'{"detail":"missing"}'))
    assert env["ok"] is False
    assert env["status"] == 404
    assert env["error"]["type"] == "http_error"
    assert env["body"] == {"detail": "missing"}  # upstream error detail preserved


def test_normalize_http_error_5xx():
    a = DeviceAdapter(_MAX)
    env = a.normalize_http_error(_resp(503, b"down", content_type="text/plain"))
    assert env["ok"] is False
    assert env["status"] == 503
    assert env["error"]["type"] == "http_error"


def test_build_result_oversized_capped():
    a = DeviceAdapter(_MAX)
    env = a.build_result(_resp(200, b"x" * (_MAX + 1)))
    assert env["ok"] is False
    assert env["status"] == 502
    assert env["error"]["type"] == "response_too_large"


def test_error_envelope_shape():
    a = DeviceAdapter(_MAX)
    env = a.error_envelope("timeout", "slow", status=504)
    assert env == {"ok": False, "status": 504, "error": {"type": "timeout", "message": "slow"}}


# --- F-48 pagination surfacing -----------------------------------------------


def _resp_h(headers, status=200, body=b"{}"):
    h = {"content-type": "application/json", **headers}
    return httpx.Response(status_code=status, content=body, headers=h)


def test_no_pagination_when_no_signals():
    # A plain response must not grow a pagination key (exact-equality guard).
    a = DeviceAdapter(_MAX)
    assert "pagination" not in a.build_result(_resp(200, b'{"x":1}'))


def test_pagination_from_link_header_next():
    a = DeviceAdapter(_MAX)
    link = '<https://api.dev/items?page=2>; rel="next", <https://api.dev/items?page=9>; rel="last"'
    env = a.build_result(_resp_h({"link": link}))
    pag = env["pagination"]
    assert pag["next_url"] == "https://api.dev/items?page=2"
    assert pag["links"]["next"] == "https://api.dev/items?page=2"
    assert pag["links"]["last"] == "https://api.dev/items?page=9"
    assert pag["has_more"] is True


def test_pagination_from_cursor_header():
    a = DeviceAdapter(_MAX)
    env = a.build_result(_resp_h({"x-next-cursor": "abc123"}))
    assert env["pagination"]["next_cursor"] == "abc123"
    assert env["pagination"]["has_more"] is True


def test_pagination_total_only_is_not_has_more():
    a = DeviceAdapter(_MAX)
    env = a.build_result(_resp_h({"x-total-count": "500"}))
    assert env["pagination"]["total"] == "500"
    assert env["pagination"]["has_more"] is False
    assert "next_url" not in env["pagination"]


# --- F-45 long-running operation surfacing -----------------------------------


def test_no_operation_for_plain_200():
    a = DeviceAdapter(_MAX)
    assert "operation" not in a.build_result(_resp(200, b'{"x":1}'))


def test_operation_from_202_with_location_and_retry_after():
    a = DeviceAdapter(_MAX)
    env = a.build_result(_resp_h({"location": "https://api.dev/jobs/7", "retry-after": "5"}, status=202))
    assert env["ok"] is True and env["status"] == 202
    op = env["operation"]
    assert op["status"] == "pending"
    assert op["poll_url"] == "https://api.dev/jobs/7"
    assert op["retry_after"] == "5"


def test_operation_from_operation_location_header_on_200():
    a = DeviceAdapter(_MAX)
    env = a.build_result(_resp_h({"operation-location": "https://api.dev/op/abc"}, status=200))
    assert env["operation"]["poll_url"] == "https://api.dev/op/abc"


def test_201_location_is_not_an_async_operation():
    # 201 Created's Location is the new resource, not a job to poll.
    a = DeviceAdapter(_MAX)
    assert "operation" not in a.build_result(_resp_h({"location": "https://api.dev/items/9"}, status=201))


# --- translator content-type selection (F-40) --------------------------------


def _op_with_body(content: dict) -> dict:
    return {"operationId": "do_thing", "requestBody": {"content": content}}


def test_translator_prefers_json_over_form():
    t = SpecTranslator()
    op = _op_with_body(
        {
            FORM_CONTENT: {"schema": {"type": "object", "properties": {"a": {"type": "string"}}}},
            JSON_CONTENT: {"schema": {"type": "object", "properties": {"b": {"type": "string"}}}},
        }
    )
    tool = t._build_tool("post", "/x", op, "h")
    assert tool.request_body.content_type == JSON_CONTENT
    assert "b" in tool.schema["properties"] and "a" not in tool.schema["properties"]


def test_translator_tags_multipart_binary_field():
    t = SpecTranslator()
    op = _op_with_body(
        {
            MULTIPART_CONTENT: {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "name": {"type": "string"},
                    },
                }
            }
        }
    )
    tool = t._build_tool("post", "/upload", op, "h")
    assert tool.request_body.content_type == MULTIPART_CONTENT
    assert tool.request_body.binary_fields == {"file"}
    assert tool.param_locations["file"] == "body"


def test_translator_raw_body_for_octet_stream():
    t = SpecTranslator()
    op = _op_with_body({"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}})
    tool = t._build_tool("put", "/blob", op, "h")
    assert tool.request_body.raw is True
    assert tool.request_body.raw_field == "body"
    assert tool.param_locations["body"] == "body"


# --- end-to-end through the pod ----------------------------------------------


@pytest.mark.asyncio
async def test_pod_4xx_returns_error_envelope():
    manifest = McpManifest(
        server_name="m",
        server_version="1",
        hostname="dev",
        tools=[_tool(method="GET")],
    )

    async def fake_request(self, method, url, **kwargs):
        return httpx.Response(
            status_code=400, content=b'{"detail":"bad"}', headers={"content-type": "application/json"}
        )

    with patch("httpx.AsyncClient.request", fake_request):
        pod = DevicePod(hostname="dev", manifest=manifest, base_url="http://dev.local")
        result = await pod._tool_dispatch["t"]()
    assert result["ok"] is False
    assert result["status"] == 400
    assert result["error"]["type"] == "http_error"


@pytest.mark.asyncio
async def test_pod_multipart_encodes_file_part():
    body = RequestBodySpec(content_type=MULTIPART_CONTENT, binary_fields={"file"})
    manifest = McpManifest(
        server_name="m",
        server_version="1",
        hostname="dev",
        tools=[_tool(method="POST", body=body, locations={"file": "body", "name": "body"})],
    )
    captured = {}

    async def fake_request(self, method, url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(status_code=200, content=b"{}", headers={"content-type": "application/json"})

    with patch("httpx.AsyncClient.request", fake_request):
        pod = DevicePod(hostname="dev", manifest=manifest, base_url="http://dev.local")
        await pod._tool_dispatch["t"](file="filedata", name="doc")

    assert captured["files"] == {"file": b"filedata"}
    assert captured["data"] == {"name": "doc"}
    assert "json" not in captured
