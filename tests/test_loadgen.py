# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for the load-test harness pure helpers (F-22).

The networked workloads need a live gateway, but the parsing / percentile / stats
logic is pure and worth pinning so the baseline numbers can be trusted.
"""

import math

import pytest

from tools.loadtest.loadgen import (
    Stats,
    build_parser,
    build_call,
    classify_reply,
    format_report,
    percentile,
)


def test_percentile_nearest_rank():
    s = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert percentile(s, 50) == 50
    assert percentile(s, 90) == 90
    assert percentile(s, 99) == 100
    assert percentile(s, 100) == 100
    assert percentile(s, 0) == 10


def test_percentile_empty_is_nan():
    assert math.isnan(percentile([], 99))


def test_a_stateless_call_carries_the_envelope_and_routing_headers():
    body, headers = build_call("stateless", "1-1", "get_readings", {"sensor_id": 1})
    meta = body["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == headers["MCP-Protocol-Version"] == "2026-07-28"
    assert headers["Mcp-Method"] == "tools/call" and headers["Mcp-Name"] == "get_readings"
    assert "Mcp-Session-Id" not in headers


def test_a_handshake_call_carries_its_session_and_no_envelope():
    body, headers = build_call("handshake", "1-1", "t", {}, session_id="sess-9")
    assert "_meta" not in body["params"] and headers["Mcp-Session-Id"] == "sess-9"
    assert headers["MCP-Protocol-Version"] == "2025-11-25"


@pytest.mark.parametrize(
    "status,body,verdict",
    [
        (200, {"result": {}}, "ok"),
        (200, {"error": {"code": -32602}}, "rpc_error"),
        (429, None, "throttled_429"),
        (400, {"error": {}}, "post_400"),
        (200, None, "bad_body"),
    ],
)
def test_replies_are_bucketed(status, body, verdict):
    assert classify_reply(status, body) == verdict


def test_stats_report_shape_and_rates():
    st = Stats()
    for ms in (10.0, 20.0, 30.0, 40.0):
        st.record_ok(ms)
    st.record_error("timeout")
    st.record_error("post_429")
    rep = st.report()
    assert rep["requests"] == 6
    assert rep["ok"] == 4
    assert rep["errors"] == 2
    assert rep["timeouts"] == 1
    assert rep["error_rate"] == pytest.approx(2 / 6)
    assert rep["error_kinds"] == {"post_429": 1, "timeout": 1}
    assert rep["latency_ms"]["p50"] is not None
    # report renders without error
    assert "load baseline" in format_report(rep, "toolcall")


def test_parser_toolcall_requires_device_and_tool():
    parser = build_parser()
    args = parser.parse_args(["toolcall", "--device", "d", "--tool", "t"])
    assert args.workload == "toolcall" and args.device == "d" and args.tool == "t"
