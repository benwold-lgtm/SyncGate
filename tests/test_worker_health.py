# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Tests for the distributed worker health loop's spec-poll throttling.

Regression coverage for S1 finding 3: spec polling used cfg.last_check (rewritten
every health cycle) as its throttle, so `now - last_check` was always small and
the spec poll never fired. The loop now keeps a dedicated per-device timestamp.
"""

import hashlib
import time

import pytest
import fakeredis.aioredis

from device_mcp_gateway.shared.registry_backend import DeviceConfig, MemoryRegistryBackend
from device_mcp_gateway.worker.health import WorkerHealthLoop

SPEC = {"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": {}}
SPEC_HASH = hashlib.sha256(str(SPEC).encode()).hexdigest()[:16]


async def _make_loop(monkeypatch):
    backend = MemoryRegistryBackend()
    # spec_hash matches SPEC so the "changed" branch (which would invoke the
    # translator/ProcessPoolExecutor) is never taken in these tests.
    await backend.set_device(
        "dev1",
        DeviceConfig(hostname="dev1", base_url="http://dev1", spec_url="http://dev1/openapi.json", spec_hash=SPEC_HASH),
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    loop = WorkerHealthLoop(
        worker_id="w1",
        backend=backend,
        redis_client=redis,
        interval=30,
        spec_poll_interval=300,
    )

    fetch_calls = {"n": 0}

    async def _fake_reach(_url):
        return True

    async def _fake_fetch(_cfg):
        fetch_calls["n"] += 1
        return SPEC

    monkeypatch.setattr(loop, "_check_reachability", _fake_reach)
    monkeypatch.setattr(loop, "_fetch_spec", _fake_fetch)
    return loop, backend, fetch_calls


@pytest.mark.asyncio
async def test_first_sighting_defers_spec_poll(monkeypatch):
    loop, _backend, fetch_calls = await _make_loop(monkeypatch)

    await loop._check_device("dev1")

    # The spec was just fetched at spawn; the first health cycle must not re-poll.
    assert fetch_calls["n"] == 0
    assert "dev1" in loop._last_spec_check


@pytest.mark.asyncio
async def test_spec_poll_fires_after_interval_despite_fresh_last_check(monkeypatch):
    loop, backend, fetch_calls = await _make_loop(monkeypatch)

    # First cycle seeds the spec-poll timestamp without polling.
    await loop._check_device("dev1")
    assert fetch_calls["n"] == 0

    # Age the dedicated spec-poll timestamp past the poll interval...
    loop._last_spec_check["dev1"] = time.time() - 301
    # ...while last_check stays fresh, exactly the condition that used to wedge
    # the old guard (now - cfg.last_check was always < spec_poll_interval).
    cfg = await backend.get_device("dev1")
    cfg.last_check = time.time()

    await loop._check_device("dev1")
    assert fetch_calls["n"] == 1  # poll fired despite a fresh last_check


@pytest.mark.asyncio
async def test_spec_poll_throttled_within_interval(monkeypatch):
    loop, _backend, fetch_calls = await _make_loop(monkeypatch)

    await loop._check_device("dev1")  # seed
    loop._last_spec_check["dev1"] = time.time() - 301
    await loop._check_device("dev1")  # fires → n == 1
    await loop._check_device("dev1")  # immediately again → throttled

    assert fetch_calls["n"] == 1


# --- RC-1: health-check lock TTL must exceed the worst-case check ----------


def test_lock_ttl_defaults_above_interval():
    loop = WorkerHealthLoop("w", MemoryRegistryBackend(), None, interval=30)
    assert loop._lock_ttl == 120
    assert loop._lock_ttl > loop._interval


def test_lock_ttl_scales_with_large_interval():
    loop = WorkerHealthLoop("w", MemoryRegistryBackend(), None, interval=90)
    assert loop._lock_ttl == 180  # max(2 × 90, 120)
    assert loop._lock_ttl > loop._interval


def test_lock_ttl_explicit_override():
    loop = WorkerHealthLoop("w", MemoryRegistryBackend(), None, interval=30, lock_ttl=45)
    assert loop._lock_ttl == 45


@pytest.mark.asyncio
async def test_check_acquires_lock_with_lock_ttl(monkeypatch):
    loop, _backend, _fetch = await _make_loop(monkeypatch)
    captured: dict = {}
    real_set = loop._r.set

    async def _capturing_set(key, value, **kwargs):
        captured.update(kwargs)
        return await real_set(key, value, **kwargs)

    monkeypatch.setattr(loop._r, "set", _capturing_set)

    await loop._check_device("dev1")

    # The lock must be acquired with the long lock TTL, not the short interval.
    assert captured.get("ex") == loop._lock_ttl
    assert captured["ex"] > loop._interval


# --- ADR-0015 / F-69: the OpenAPI declared dimension -------------------------
#
# Found by live-testing ADR-0015 on a cluster, not by the unit suite: `_last_declared`
# was only ever written inside the `mcp` branch of `_check_reachability`, so an OpenAPI
# device — the DEFAULT upstream kind — had no declared identity at all. Its consequences
# are quiet ones: `key_and_declared_changed` (ADR-0015 §3's strongest signal) could never
# fire for such a device, and the inventory metadata the ADR names as a motive was absent.
# The evidence that made it undeniable was a spec declaring title "TLS Probe Test Device"
# version "9.9.9" against a device reporting declared_name=None.


async def _openapi_loop(monkeypatch, spec):
    """A loop whose spec fetch returns `spec`, with the spec poll already due."""
    loop, backend, _calls = await _make_loop(monkeypatch)

    async def _fetch(_cfg):
        return spec

    monkeypatch.setattr(loop, "_fetch_spec", _fetch)
    await loop._check_device("dev1")  # seed the spec-poll timestamp
    loop._last_spec_check["dev1"] = time.time() - 301
    return loop, backend


@pytest.mark.asyncio
async def test_openapi_info_becomes_the_declared_identity(monkeypatch):
    """The gap itself: an OpenAPI device must report what it says it is."""
    spec = {"openapi": "3.0.0", "info": {"title": "Acme Array", "version": "7.2.1"}, "paths": {}}
    loop, _backend = await _openapi_loop(monkeypatch, spec)

    await loop._check_device("dev1")

    assert loop._last_declared["dev1"] == ("Acme Array", "7.2.1")


@pytest.mark.asyncio
async def test_declared_identity_reaches_the_device_record(monkeypatch):
    """End to end through the comparison step, because stashing it is not the point —
    persisting it is. The stash is consumed on the cycle *after* the spec poll."""
    spec = {"openapi": "3.0.0", "info": {"title": "Acme Array", "version": "7.2.1"}, "paths": {}}
    loop, backend = await _openapi_loop(monkeypatch, spec)

    await loop._check_device("dev1")  # spec poll stashes
    await loop._check_device("dev1")  # comparison consumes and persists

    cfg = await backend.get_device("dev1")
    assert cfg.declared_name == "Acme Array"
    assert cfg.declared_version == "7.2.1"


@pytest.mark.asyncio
async def test_a_changed_declared_version_is_detected(monkeypatch):
    """The change-signal half. A plain-http device has no authenticated dimension at all
    (ADR-0015 §7), so the declared fields are the *only* thing that can move — which is
    exactly the case that detected nothing before this fix."""
    spec = {"openapi": "3.0.0", "info": {"title": "Acme Array", "version": "7.2.1"}, "paths": {}}
    loop, backend = await _openapi_loop(monkeypatch, spec)
    await loop._check_device("dev1")
    await loop._check_device("dev1")
    assert (await backend.get_device("dev1")).declared_version == "7.2.1"

    # The appliance is upgraded in place.
    spec["info"]["version"] = "8.0.0"
    loop._last_spec_check["dev1"] = time.time() - 301
    await loop._check_device("dev1")
    await loop._check_device("dev1")

    cfg = await backend.get_device("dev1")
    assert cfg.declared_version == "8.0.0"
    assert cfg.fingerprint_state == "pinned", "a version bump is informational, not an approval gate"


@pytest.mark.asyncio
async def test_a_proxied_mcp_device_does_not_read_info_from_its_spec(monkeypatch):
    """An MCP upstream's declared identity comes from `serverInfo` on the live handshake.
    Its `_fetch_spec` returns a synthesised {"tools": [...]} document with no `info` block,
    and reading one from there would invent an identity nothing reported."""
    backend = MemoryRegistryBackend()
    await backend.set_device(
        "mcpdev",
        DeviceConfig(hostname="mcpdev", base_url="http://mcpdev", upstream_kind="mcp", spec_hash="x"),
    )
    loop, _b, _c = await _make_loop(monkeypatch)
    loop._backend = backend

    async def _fetch(_cfg):
        return {"tools": [], "info": {"title": "should not be read", "version": "0"}}

    monkeypatch.setattr(loop, "_fetch_spec", _fetch)
    await loop._check_device("mcpdev")
    loop._last_spec_check["mcpdev"] = time.time() - 301
    await loop._check_device("mcpdev")

    assert "mcpdev" not in loop._last_declared


def test_a_junk_info_block_records_nothing_and_does_not_raise():
    """A spec can survive fetching and still be malformed. Fingerprinting is a diagnostic
    layered onto the health loop, so a junk `info` block must yield no declared identity
    rather than an exception — and must never invent one from a partial value.

    Exercised directly rather than through `_check_device`: a malformed spec changes the
    spec hash, which routes into the translation branch and fails there for reasons that
    have nothing to do with the `info` block.
    """
    loop = WorkerHealthLoop("w", MemoryRegistryBackend(), None, interval=30)
    for junk in (
        {"openapi": "3.0.0", "info": "not-an-object"},
        {"openapi": "3.0.0", "info": None},
        {"openapi": "3.0.0"},
        {"info": {}},
        {"info": {"title": "", "version": ""}},
        "not-a-dict-at-all",
    ):
        loop.record_declared_from_spec("dev1", junk)
        assert "dev1" not in loop._last_declared, f"recorded something from {junk!r}"


def test_a_title_without_a_version_is_still_worth_recording():
    """Half an identity is still inventory, and `_declared_changed` compares each field
    independently — a missing version never reads as a change."""
    loop = WorkerHealthLoop("w", MemoryRegistryBackend(), None, interval=30)
    loop.record_declared_from_spec("dev1", {"info": {"title": "Acme Array"}})
    assert loop._last_declared["dev1"] == ("Acme Array", None)


@pytest.mark.asyncio
async def test_a_malformed_spec_does_not_break_the_health_loop(monkeypatch):
    """The escape, at the layer that made it matter.

    `_check_device` catches `(SpecTooLargeError, ValueError)` to log the rejection and
    leave the manifest cache alone. An invalid document used to raise
    `OpenAPIValidationError` (or, one function earlier, `AttributeError`) straight past
    that handler — so the device's spec poll ended in an unhandled traceback rather than
    the warning the code was written to produce.
    """
    loop, _backend, _calls = await _make_loop(monkeypatch)

    for junk in (
        {"openapi": "3.0.0", "info": "not-an-object", "paths": {}},
        {"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": "nope"},
        {"openapi": "3.0.0"},
    ):

        async def _fetch(_cfg, _j=junk):
            return _j

        monkeypatch.setattr(loop, "_fetch_spec", _fetch)
        loop._last_spec_check["dev1"] = time.time() - 301
        await loop._check_device("dev1")  # must not raise
