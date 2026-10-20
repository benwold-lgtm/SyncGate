# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""SpecService — outbound OpenAPI spec acquisition for embedded mode (F-12).

Extracted from the Registry god-object. Owns everything about *getting* a
device's spec: the shared httpx client (with F-31 outbound mTLS), URL fetch and
multi-path discovery, the bounded TTL cache, and recording the fetched spec on
the DeviceProfile (+ persisting its hash/last_check).

It deliberately knows nothing about pods. The old ``fetch_spec`` replaced a
running pod inline when the spec changed — a spec↔pod recursion that made the
Registry a god-object. Here ``fetch_spec`` instead **returns whether the spec
changed**, and the caller (provisioning / health loop) decides whether to ask the
PodSupervisor to replace the pod.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import time
from typing import Any

import httpx
from loguru import logger

from device_mcp_gateway.audit import redact_url
from device_mcp_gateway.core.backoff import RetryPolicy, send_with_retry
from device_mcp_gateway.core.spec_limits import (
    DEFAULT_MAX_SPEC_BYTES,
    SpecTooLargeError,
    fetched_spec_or_none,
)
from device_mcp_gateway.registry.models import DeviceProfile
from device_mcp_gateway.security.mtls import TlsProfiles
from device_mcp_gateway.security.url_policy import (
    build_guarded_client,
    resolve_allow_private,
    resolve_allowed_ports,
)
from device_mcp_gateway.shared.registry_backend import AbstractRegistryBackend


class SpecCache:
    """TTL-based in-memory cache for raw OpenAPI spec dicts.

    Eviction is O(log n) via a min-heap ordered by insertion timestamp.
    Stale heap entries (from updates to existing keys) are cleaned up lazily.
    """

    def __init__(self, ttl: int = 3600, max_entries: int = 200):
        self._store: dict[str, dict[str, Any]] = {}
        self._timestamps: dict[str, float] = {}
        self._heap: list[tuple[float, str]] = []  # (inserted_at, key)
        self._ttl = ttl
        self._max = max_entries

    def get(self, key: str) -> dict[str, Any] | None:
        if key not in self._store:
            return None
        if time.time() - self._timestamps[key] > self._ttl:
            del self._store[key]
            del self._timestamps[key]
            return None
        return self._store[key]

    def put(self, key: str, value: dict[str, Any]) -> None:
        if len(self._store) >= self._max and key not in self._store:
            self._evict_oldest()
        ts = time.time()
        self._store[key] = value
        self._timestamps[key] = ts
        heapq.heappush(self._heap, (ts, key))

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)
        self._timestamps.pop(key, None)
        # Heap entry becomes stale; cleaned up lazily on next eviction.

    def _evict_oldest(self) -> None:
        while self._heap:
            ts, key = self._heap[0]
            # Skip stale entries (key updated or invalidated since this was pushed).
            if key not in self._timestamps or self._timestamps[key] != ts:
                heapq.heappop(self._heap)
                continue
            heapq.heappop(self._heap)
            del self._store[key]
            del self._timestamps[key]
            return


class SpecService:
    """Fetches, discovers, and caches device specs. No pod knowledge."""

    def __init__(
        self,
        *,
        backend: AbstractRegistryBackend,
        config: dict[str, Any],
        tls_profiles: TlsProfiles,
        retry_policy: RetryPolicy,
    ) -> None:
        self._backend = backend
        self._config = config
        self._tls = tls_profiles
        self._retry_policy = retry_policy
        self._spec_max_bytes = config.get("spec_max_bytes", DEFAULT_MAX_SPEC_BYTES)
        self._spec_poll_interval = config.get("spec_poll_interval", 300)
        self._cache = SpecCache(ttl=config.get("spec_cache_ttl", 3600), max_entries=200)
        self._allow_private = resolve_allow_private(config)
        self._allowed_ports = resolve_allowed_ports(config)
        self._http_clients: dict[tuple, httpx.AsyncClient] = {}
        # hostname → fingerprint-capturing probe client (see probe_client).
        self._probe_clients: dict[str, httpx.AsyncClient] = {}

    def client(self, hostname: str | None) -> httpx.AsyncClient:
        """The outbound client for one device (also used by Registry reachability probes).

        Pooled **per resolved TLS profile**, not per registry: one client for the whole
        service would put every device back on a single trust set, which is exactly the
        fleet-global limitation per-device trust removes. Devices that resolve to the
        same profile — normally all of them — still share one client, so connections and
        TLS sessions stay warm across the reachability and spec-fetch GETs to a device.

        ``hostname=None`` selects the fleet profile, for calls not attributable to a
        device. It is a required argument so a new call site has to make that choice
        rather than silently inheriting fleet trust.
        """
        key = self._tls.key_for(hostname)
        existing = self._http_clients.get(key)
        if existing is not None and not existing.is_closed:
            return existing
        # SSRF-guarded: every hop (incl. redirects) is re-checked against the URL
        # policy, so a device can't 302 a spec fetch to an internal address (F-02).
        client = build_guarded_client(
            verify=self._tls.for_device(hostname),
            allow_private=self._allow_private,
            allowed_ports=self._allowed_ports,
        )
        self._http_clients[key] = client
        return client

    def probe_client(self, hostname: str) -> httpx.AsyncClient:
        """A per-DEVICE client that captures the endpoint fingerprint (ADR-0015).

        Not ``client()``: that one is pooled per TLS profile, so normally one client serves
        every device, and the captured observation is "the last connection this client
        saw". The embedded health loop probes devices concurrently, so reading it after
        device A's probe could return device B's certificate — a false "key changed", and
        under ``enforce`` a wrongful quarantine. One client per device makes the observation
        attributable by construction. Same TLS profile and the same SSRF guard as
        ``client()``.
        """
        existing = self._probe_clients.get(hostname)
        if existing is not None and not existing.is_closed:
            return existing
        client = build_guarded_client(
            verify=self._tls.for_device(hostname),
            allow_private=self._allow_private,
            allowed_ports=self._allowed_ports,
            capture_fingerprint=True,
        )
        self._probe_clients[hostname] = client
        return client

    async def drop_probe_client(self, hostname: str) -> None:
        client = self._probe_clients.pop(hostname, None)
        if client is not None and not client.is_closed:
            await client.aclose()

    def invalidate(self, base_url: str) -> None:
        self._cache.invalidate(base_url)

    async def aclose(self) -> None:
        for client in [*self._http_clients.values(), *self._probe_clients.values()]:
            if not client.is_closed:
                await client.aclose()
        self._http_clients.clear()

    async def fetch_spec(self, profile: DeviceProfile) -> bool:
        """Fetch + cache the device's spec, recording it on ``profile``.

        Updates ``profile.spec_data`` / ``spec_hash`` / ``last_check`` and persists
        hash + last_check. Returns ``True`` when the spec **changed** vs. the
        device's previous hash (so the caller can replace a running pod); ``False``
        on first fetch, unchanged spec, or fetch failure. Does not touch pods.
        """
        cache_key = profile.base_url
        cached = self._cache.get(cache_key)
        if cached and (time.time() - profile.config.last_check) < self._spec_poll_interval:
            profile.spec_data = cached
            return False

        if profile.spec_url:
            fetched = await self._http_get(profile.spec_url, profile.hostname)
        else:
            fetched = await self._discover_spec(profile.base_url, profile.hostname)

        if not fetched:
            return False

        h = hashlib.sha256(str(fetched).encode()).hexdigest()[:16]
        old_hash = profile.config.spec_hash
        profile.config.spec_hash = h
        profile.spec_data = fetched
        profile.config.last_check = time.time()
        self._cache.put(cache_key, fetched)
        await self._backend.update_device_fields(profile.hostname, spec_hash=h, last_check=profile.config.last_check)

        changed = old_hash is not None and h != old_hash
        if changed:
            logger.info(f"Spec changed for {profile.hostname}: {old_hash} → {h}")
        else:
            logger.debug(f"Spec fetched for {profile.hostname}: hash={h}")
        return changed

    async def _discover_spec(self, base_url: str, hostname: str) -> dict[str, Any] | None:
        paths = self._config.get("discovery", {}).get(
            "spec_paths",
            ["/openapi.json", "/swagger.json", "/api-docs"],
        )
        timeout = self._config.get("discovery", {}).get("timeout", 10)
        client = self.client(hostname)

        async def _probe(path: str) -> dict[str, Any] | None:
            url = base_url.rstrip("/") + path
            try:

                async def _get(u: str = url) -> httpx.Response:
                    return await client.get(u, timeout=timeout)

                resp = await send_with_retry(_get, method="GET", policy=self._retry_policy)
                return fetched_spec_or_none(resp, max_bytes=self._spec_max_bytes)
            except SpecTooLargeError as exc:
                logger.warning(f"Spec discovery rejected oversized spec at {redact_url(url)}: {exc} (F-09)")
                return None
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug(f"Spec discovery probe failed for {redact_url(url)}: {exc}")
                return None

        # Probe candidate paths concurrently and take the first that yields a valid
        # spec, so worst-case discovery latency is one path's timeout, not the sum of
        # all of them (F-11). Losing probes are cancelled once we have a winner.
        tasks = [asyncio.create_task(_probe(p)) for p in paths]
        try:
            for fut in asyncio.as_completed(tasks):
                spec = await fut
                if spec is not None:
                    return spec
            return None
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _http_get(self, url: str, hostname: str) -> dict[str, Any] | None:
        try:
            resp = await send_with_retry(
                lambda: self.client(hostname).get(url, timeout=10), method="GET", policy=self._retry_policy
            )
            return fetched_spec_or_none(resp, max_bytes=self._spec_max_bytes)
        except SpecTooLargeError as exc:
            logger.warning(f"Spec fetch rejected oversized spec at {redact_url(url)}: {exc} (F-09)")
            return None
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug(f"Spec fetch failed for {redact_url(url)}: {exc}")
            return None
