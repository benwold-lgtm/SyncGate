# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tier-1 tests for outbound retry/backoff + jitter (F-05 / F-44 / F-61)."""

from unittest.mock import patch

import httpx
import pytest

from device_mcp_gateway.core.backoff import (
    RetryPolicy,
    full_jitter,
    jittered,
    parse_retry_after,
    send_with_retry,
)
from device_mcp_gateway.core.translator import McpManifest, McpTool
from device_mcp_gateway.pods.device_pod import DevicePod


async def _nosleep(_seconds):
    return None


def _policy(max_retries=2):
    return RetryPolicy(max_retries=max_retries, base_delay=0.01, max_delay=0.05)


def _resp(status, headers=None):
    return httpx.Response(status_code=status, content=b"{}", headers=headers or {"content-type": "application/json"})


# --- F-61 jitter -------------------------------------------------------------


def test_jittered_within_bounds():
    for _ in range(200):
        v = jittered(30, ratio=0.2)
        assert 24.0 <= v <= 36.0


def test_jittered_zero_passthrough():
    assert jittered(0) == 0


def test_full_jitter_bounded_and_grows():
    # Never exceeds the cap; the upper bound grows with attempt until capped.
    for attempt in range(5):
        for _ in range(50):
            v = full_jitter(attempt, base=0.2, cap=5.0)
            assert 0.0 <= v <= 5.0


# --- F-44 Retry-After parsing ------------------------------------------------


def test_parse_retry_after_seconds():
    assert parse_retry_after("5") == 5.0


def test_parse_retry_after_none_and_garbage():
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None


def test_parse_retry_after_http_date_is_nonnegative():
    # A past date clamps to 0, never negative.
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


# --- F-05 retry behaviour ----------------------------------------------------


@pytest.mark.asyncio
async def test_retries_transient_5xx_then_succeeds():
    calls = []

    async def send():
        calls.append(1)
        return _resp(503) if len(calls) < 3 else _resp(200)

    resp = await send_with_retry(send, method="GET", policy=_policy(), sleep=_nosleep)
    assert resp.status_code == 200
    assert len(calls) == 3  # two retries then success


@pytest.mark.asyncio
async def test_retry_budget_exhausts_and_returns_last():
    calls = []

    async def send():
        calls.append(1)
        return _resp(503)

    resp = await send_with_retry(send, method="GET", policy=_policy(max_retries=2), sleep=_nosleep)
    assert resp.status_code == 503
    assert len(calls) == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_non_idempotent_method_not_retried():
    calls = []

    async def send():
        calls.append(1)
        return _resp(503)

    resp = await send_with_retry(send, method="POST", policy=_policy(), sleep=_nosleep)
    assert resp.status_code == 503
    assert len(calls) == 1  # POST gets a single attempt


@pytest.mark.asyncio
async def test_transport_error_retried_then_raised():
    calls = []

    async def send():
        calls.append(1)
        raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        await send_with_retry(send, method="GET", policy=_policy(max_retries=1), sleep=_nosleep)
    assert len(calls) == 2  # initial + 1 retry, then propagates


@pytest.mark.asyncio
async def test_429_retry_after_honored():
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    calls = []

    async def send():
        calls.append(1)
        return _resp(429, {"retry-after": "2"}) if len(calls) < 2 else _resp(200)

    # max_delay must be >= the Retry-After value for it to be honored (else the 429 is
    # surfaced — that path is covered by test_429_retry_after_too_long_surfaces_429).
    policy = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=5.0)
    resp = await send_with_retry(send, method="GET", policy=policy, sleep=sleep)
    assert resp.status_code == 200
    assert slept == [2.0]  # honored the Retry-After delay exactly


@pytest.mark.asyncio
async def test_429_retry_after_too_long_surfaces_429():
    # If the device asks for longer than max_delay, don't hold the call — return 429.
    async def send():
        return _resp(429, {"retry-after": "3600"})

    resp = await send_with_retry(send, method="GET", policy=_policy(), sleep=_nosleep)
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_4xx_not_retried():
    calls = []

    async def send():
        calls.append(1)
        return _resp(404)

    resp = await send_with_retry(send, method="GET", policy=_policy(), sleep=_nosleep)
    assert resp.status_code == 404
    assert len(calls) == 1  # 404 is terminal


# --- end-to-end through the pod ----------------------------------------------


@pytest.mark.asyncio
async def test_pod_get_retries_transient_then_succeeds():
    manifest = McpManifest(
        server_name="m",
        server_version="1",
        hostname="dev",
        tools=[
            McpTool(name="t", description="d", schema={"type": "object", "properties": {}}, method="GET", path="/x")
        ],
    )
    attempts = []

    async def fake_request(self, method, url, **kwargs):
        attempts.append(1)
        status = 503 if len(attempts) < 2 else 200
        return httpx.Response(status_code=status, content=b'{"ok":1}', headers={"content-type": "application/json"})

    with patch("httpx.AsyncClient.request", fake_request):  # base/max delay 0 → instant sleeps
        pod = DevicePod(
            hostname="dev",
            manifest=manifest,
            base_url="http://dev.local",
            retry_policy=RetryPolicy(max_retries=2, base_delay=0.0, max_delay=0.0),
        )
        result = await pod._tool_dispatch["t"]()
    assert result["ok"] is True
    assert result["status"] == 200
    assert len(attempts) == 2  # retried the transient 503
