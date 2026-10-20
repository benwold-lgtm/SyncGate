# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""
Distributed health loop for device workers.

Each worker runs this loop for its assigned devices.  A Redis SETNX lock
ensures exactly one worker checks each device per interval — other workers
skip devices they can't lock and let the lock-holder update Redis.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace
from functools import partial
from typing import Any

import httpx
from loguru import logger

from device_mcp_gateway.auth.base import CredentialNotBound
from device_mcp_gateway.core.backoff import RetryPolicy, jittered, send_with_retry
from device_mcp_gateway.core.manifest_diff import record_tool_change
from device_mcp_gateway.core.spec_limits import (
    DEFAULT_MAX_SPEC_BYTES,
    DEFAULT_TRANSLATE_TIMEOUT,
    SpecTooLargeError,
    fetched_spec_or_none,
    run_translation,
)
from device_mcp_gateway.core.translator import manifest_to_dict
from device_mcp_gateway.credentials.resolver import ResolverError
from device_mcp_gateway.security.mtls import TlsProfiles
from device_mcp_gateway.security import fingerprint as fp
from device_mcp_gateway.security.fingerprint_record import declared_from_spec, record_observation
from device_mcp_gateway.security.url_policy import build_guarded_client, observation_from_client
from device_mcp_gateway.shared.registry_backend import AbstractRegistryBackend
from device_mcp_gateway.shared.keys import KEYS
from device_mcp_gateway.shared.spec_source import CuratedSpecInvalid, resolve_spec_source
from device_mcp_gateway.upstream.mcp_discovery import (
    build_proxy_manifest_from,
    canonical_snapshot_hash,
    snapshot_upstream,
)

# One pool + atexit reap for the whole worker process, shared with the spawn
# path (worker.runner) — see worker/spec_pool.py. Names re-exported so callers
# that imported them from here keep working.
from device_mcp_gateway.worker.spec_pool import _spec_executor, _translate_spec_sync  # noqa: F401  (re-exported)


def spec_fingerprint(spec: dict[str, Any], *, is_proxy: bool) -> str:
    """The stored identity of a fetched spec — what a later poll is compared against.

    Defined once and used by both the spawn path (which writes the first one) and the
    health loop (which writes every one after that), because a baseline written under a
    different rule than the comparison is worse than no baseline: it reports a change on
    the next poll and every poll after it.

    Proxied upstreams hash a canonical projection of the snapshot (tools, plus resources and
    prompts when the upstream serves any) rather than the raw response, since ordering is
    server-controlled and ``str(spec)`` would differ between two identical polls.
    """
    if is_proxy:
        return canonical_snapshot_hash(spec)
    return hashlib.sha256(str(spec).encode()).hexdigest()[:16]


#: Marker embedded in every ``spawn_error`` this loop writes for a credential failure.
#: Present so the loop can clear its OWN reason when a device recovers without clobbering
#: a reason some other component wrote — the spawn path's cold-path message carries it too.
CREDENTIAL_REASON_MARKER = "(credential: "


def _shutdown_spec_executor() -> None:
    """Reap the spec-translation worker processes at interpreter exit (RC-5).

    Reads this module's ``_spec_executor`` global at call time (not spec_pool's)
    so the non-blocking-shutdown contract test can monkeypatch it here; shutting
    an executor down twice is harmless.
    """
    _spec_executor.shutdown(wait=False)


class WorkerHealthLoop:
    """Runs health checks for a worker's assigned devices."""

    def __init__(
        self,
        worker_id: str,
        backend: AbstractRegistryBackend,
        redis_client: Any,
        interval: int = 30,
        spec_poll_interval: int = 300,
        spec_cache_ttl: int = 3600,
        discovery_cfg: dict | None = None,
        lock_ttl: int | None = None,
        retry_policy: RetryPolicy | None = None,
        spec_max_bytes: int = DEFAULT_MAX_SPEC_BYTES,
        spec_translate_timeout: float = DEFAULT_TRANSLATE_TIMEOUT,
        tls_profiles: TlsProfiles | None = None,
        allow_private: bool = False,
        allowed_ports: set[int] | None = None,
        auth_provider: Any = None,
    ) -> None:
        # Builds the auth handler for a device config. Only the worker can do this — it
        # owns the credential codec — and without it an authenticated MCP upstream would
        # fail `initialize`, be scored unreachable, and have its healthy pod unassigned.
        self._auth_provider = auth_provider
        self._worker_id = worker_id
        self._backend = backend
        self._r = redis_client
        self._interval = interval
        self._spec_poll_interval = spec_poll_interval
        self._spec_cache_ttl = spec_cache_ttl
        self._discovery = discovery_cfg or {}
        # Spec-ingestion bounds (F-09).
        self._spec_max_bytes = spec_max_bytes
        self._spec_translate_timeout = spec_translate_timeout
        # Bounded jittered retries for idempotent reachability/spec GETs (F-05).
        self._retry_policy = retry_policy or RetryPolicy()
        # Per-device check lock TTL. Must exceed the worst-case single-device
        # check (reachability GET + spec fetch + translation), which is
        # independent of the poll interval — otherwise a slow check lets the
        # lock expire mid-flight and a second worker checks the same device.
        # It is only a crash/hang safety net: the holder deletes the lock in
        # _check_device's finally, so a longer TTL never blocks the next cycle.
        self._lock_ttl = lock_ttl if lock_ttl is not None else max(self._interval * 2, 120)
        # Outbound mutual-TLS for reachability/spec GETs to devices (F-31), resolved
        # per device (TG-4 residual). Pooled by resolved profile rather than shared
        # outright: a single client would probe every assigned device through one trust
        # set, so a self-signed device this worker happens to own would silently relax
        # verification for the rest of them.
        self._tls = tls_profiles or TlsProfiles(None)
        self._http_clients: dict[tuple, httpx.AsyncClient] = {}
        self._allow_private = allow_private
        self._allowed_ports = allowed_ports
        # Per-device timestamp of the last spec poll. Tracked separately from
        # cfg.last_check (which updates every health cycle) so the much longer
        # spec_poll_interval is honoured instead of always short-circuiting.
        self._last_spec_check: dict[str, float] = {}
        # ADR-0015: what the most recent probe observed, handed from the reachability
        # check to the comparison step. Consumed (popped) rather than read, so a stale
        # observation from an earlier cycle can never be compared as if it were current —
        # which would make an unreachable device look freshly verified.
        self._last_seen: dict[str, fp.Observation] = {}
        self._last_declared: dict[str, tuple[str | None, str | None]] = {}
        # Why the current check failed, when the cause was a credential rather than the
        # device. Set by the probe/spec helpers and consumed by _check_device, which is the
        # only place holding both the reason and the hostname to write it against.
        #
        # The WARM path's equivalent of worker.runner's _spec_failure_reason (finding #9).
        # That fix covered a device that never spawned; a device that spawned and later lost
        # its credential recorded nothing at all, so the fleet view showed a bare failure
        # with no cause. Measured on the cluster: revoking a live device's secret flipped it
        # unreachable inside one 30s cycle with an empty spawn_error, and the reconciler then
        # churned reassign/decline for as long as the secret was missing.
        self._probe_reason: dict[str, str] = {}
        # Callback set by DeviceWorker: (hostname) -> coroutine — replace pod
        self.on_spec_changed: Any = None

    @staticmethod
    def _credential_error(hostname: str, reason: str) -> str:
        """The operator-facing message for a credential failure on a live device.

        Deliberately worded like the spawn path's, and carrying the same
        ``(credential: ...)`` marker, so the two paths are greppable as one thing and an
        operator does not have to know which of them noticed. The distinction that matters
        is a bad reference vs an unavailable store (ADR-0018 §7), and that lives in
        ``reason`` — which is the resolver's own message, passed through unaltered.
        """
        return f"Device unavailable for {hostname} {CREDENTIAL_REASON_MARKER}{reason})"

    def _client(self, hostname: str | None) -> httpx.AsyncClient:
        """The guarded client carrying ``hostname``'s TLS profile (``None`` = fleet)."""
        key = self._tls.key_for(hostname)
        existing = self._http_clients.get(key)
        if existing is not None and not existing.is_closed:
            return existing
        # SSRF-guarded: the worker re-checks every reachability/spec hop against the
        # URL policy (incl. redirects), so it can't be steered to an internal address.
        client = build_guarded_client(
            verify=self._tls.for_device(hostname),
            allow_private=self._allow_private,
            allowed_ports=self._allowed_ports,
            # ADR-0015: this is the health/discovery path, which already contacts every
            # device on a schedule — so the fingerprint rides along on a request that was
            # happening anyway rather than costing an extra probe. The tool-call hot path
            # deliberately does not capture.
            capture_fingerprint=True,
        )
        self._http_clients[key] = client
        return client

    async def run_forever(self, assigned: set[str]) -> None:
        """Loop until cancelled. `assigned` is a live set mutated by the worker."""
        while True:
            for hostname in list(assigned):
                try:
                    await self._check_device(hostname)
                except Exception:
                    logger.exception(f"Health loop error for {hostname}")
            # Drop spec-poll timestamps for devices no longer assigned.
            for stale in set(self._last_spec_check) - set(assigned):
                self._last_spec_check.pop(stale, None)
            await asyncio.sleep(jittered(self._interval))  # F-61: de-sync worker health loops

    async def _check_device(self, hostname: str) -> None:
        lock_key = KEYS.health_lock(hostname)
        acquired = await self._r.set(lock_key, self._worker_id, nx=True, ex=self._lock_ttl)
        if not acquired:
            return  # another worker is handling this device

        try:
            cfg = await self._backend.get_device(hostname)
            if cfg is None:
                return

            # Discard any reason left by the previous cycle before probing, so a stale cause
            # can never be attached to a fresh failure (the same reasoning as _last_seen).
            self._probe_reason.pop(hostname, None)

            # Reachability check
            reachable = await self._check_reachability(cfg)
            await self._backend.update_device_fields(hostname, reachable=reachable, last_check=time.time())

            # ADR-0015: compare what we just saw against the pinned fingerprint. Only when
            # the probe SUCCEEDED — an unreachable device teaches us nothing about its
            # identity, and treating a timeout as "the endpoint changed" would turn every
            # transient outage into an approval request.
            if reachable:
                await self._update_fingerprint(hostname, cfg)

            if not reachable:
                updates: dict[str, Any] = {}
                if cfg.pod_active:
                    updates["pod_active"] = False
                reason = self._probe_reason.get(hostname)
                if reason:
                    # Written even when pod_active was already False: the reconciler keeps
                    # re-assigning a device it cannot spawn, so without this the record
                    # stays blank for every cycle after the first.
                    updates["spawn_error"] = self._credential_error(hostname, reason)
                if updates:
                    await self._backend.update_device_fields(hostname, **updates)
                if cfg.pod_active:
                    await self._backend.publish_assignment("unassign", hostname)
                return

            # Reachable again. Clear only a reason THIS loop wrote — a spawn_error from the
            # spawn path describes a different attempt and is not ours to discard.
            if cfg.spawn_error and CREDENTIAL_REASON_MARKER in cfg.spawn_error:
                await self._backend.update_device_fields(hostname, spawn_error=None)

            # Spec polling — throttled by its own timestamp, not cfg.last_check
            # (which is rewritten every cycle above and would always short-circuit).
            now = time.time()
            last_spec = self._last_spec_check.get(hostname)
            if last_spec is None:
                # First sighting: the spec was just fetched at pod spawn, so
                # defer the first poll by a full interval rather than re-fetching.
                self._last_spec_check[hostname] = now
                return
            if now - last_spec < self._spec_poll_interval:
                return
            self._last_spec_check[hostname] = now
            spec = await self._fetch_spec(cfg)
            if spec is None:
                reason = self._probe_reason.get(hostname)
                if reason:
                    await self._backend.update_device_fields(
                        hostname, spawn_error=self._credential_error(hostname, reason)
                    )
                return

            is_proxy = getattr(cfg, "upstream_kind", "openapi") == "mcp"
            # ADR-0015 / F-69: an OpenAPI device's DECLARED identity lives in the document's
            # `info` block — the spec analogue of the `serverInfo` an MCP upstream returns
            # from `initialize`. Recorded here because this is the only point in the loop
            # holding a parsed spec, and before the hash comparison below so that the
            # unchanged-spec path (the overwhelmingly common one) still refreshes it.
            if not is_proxy:
                self._record_declared_from_spec(cfg.hostname, spec)
            new_hash = spec_fingerprint(spec, is_proxy=is_proxy)
            if new_hash == cfg.spec_hash:
                # Unchanged spec — the overwhelmingly common case, and the one that used
                # to fall straight out of this method. The manifest is cached with a TTL
                # and its only writers are the spawn path (cold start) and the changed-
                # spec branch below, so a device whose spec never changes lost its cache
                # `spec_cache_ttl` after its pod spawned and did not get it back until the
                # pod respawned. The pod itself holds the manifest in memory and kept
                # serving MCP normally, so nothing looked wrong: the device stayed
                # reachable and pod_active, while GET /tools returned 409, the fleet
                # endpoint 404'd with "no reachable devices", and the UI showed a healthy
                # device with no tools. Renewing here makes the TTL behave like the lease
                # it is meant to be — held up by the worker that serves the device, and
                # lapsing once none does.
                await self._renew_manifest(hostname, spec, is_proxy=is_proxy)
                return
            if not cfg.spec_hash:
                # No baseline stored yet. The spawn path writes one from the spec it built
                # the manifest with, so this covers the device it could not: one whose pod
                # was spawned from a cached manifest, and any device registered before that
                # existed. Seed and stop — a first sighting is not a change, and recording
                # one would report a device's entire tool set as added on every restart.
                #
                # Without this, `cfg.spec_hash` stayed empty forever in distributed mode
                # (its only other writers live in the registry, which this mode does not
                # run), so the comparison below could never be reached and F-41 governance
                # never fired at all. Found on a cluster, not here.
                await self._backend.update_device_fields(hostname, spec_hash=new_hash)
                logger.debug(f"Seeded spec baseline for {hostname}: {new_hash}")
                return
            if new_hash != cfg.spec_hash:
                logger.info(f"Spec changed for {hostname}: {cfg.spec_hash} → {new_hash}")
                # Store new manifest in Redis
                try:
                    if is_proxy:
                        manifest_obj = build_proxy_manifest_from(hostname, spec)
                    else:
                        manifest_obj = await run_translation(
                            _spec_executor,
                            partial(_translate_spec_sync, spec, hostname),
                            timeout=self._spec_translate_timeout,
                            hostname=hostname,
                        )
                except (SpecTooLargeError, ValueError) as exc:
                    logger.warning(f"Spec for {hostname} rejected on update: {exc} (F-09)")
                    return
                manifest_dict = _manifest_to_dict(manifest_obj)
                # Governance: diff the new tool set against the manifest currently
                # in Redis and record what changed / whether it was breaking, then
                # bump the client-pollable revision (F-41).
                old_manifest = await self._backend.get_manifest(hostname)
                old_tools = (old_manifest or {}).get("tools", [])
                diff = record_tool_change(hostname, old_tools, manifest_dict.get("tools", []))
                await self._backend.set_manifest(hostname, manifest_dict, ttl=self._spec_cache_ttl)
                fields: dict[str, Any] = {"spec_hash": new_hash}
                if not diff.empty:
                    new_revision = (cfg.tools_revision or 0) + 1
                    fields["tools_revision"] = new_revision
                    # Persist what changed so GET /tools/diff can serve it (F-41).
                    await self._backend.set_last_tool_change(hostname, diff.to_record(new_revision, time.time()))
                await self._backend.update_device_fields(hostname, **fields)
                # Signal worker to replace the pod
                if self.on_spec_changed:
                    await self.on_spec_changed(hostname)
        finally:
            await self._r.delete(lock_key)

    async def _renew_manifest(self, hostname: str, spec: dict[str, Any], *, is_proxy: bool) -> None:
        """Keep an unchanged device's cached manifest alive, rebuilding it if it lapsed.

        Renewal is one EXPIRE on the common path. The rebuild only runs when the key
        is already gone — a worker that was down while it expired, or an upgrade from
        a version that never renewed — and it uses the spec the caller just fetched, so
        it costs a translation but no extra request to the device.
        """
        if await self._backend.touch_manifest(hostname, self._spec_cache_ttl):
            return
        logger.info(f"Cached manifest for {hostname} had expired; rebuilding from the current spec")
        try:
            if is_proxy:
                manifest_obj = build_proxy_manifest_from(hostname, spec)
            else:
                manifest_obj = await run_translation(
                    _spec_executor,
                    partial(_translate_spec_sync, spec, hostname),
                    timeout=self._spec_translate_timeout,
                    hostname=hostname,
                )
        except (SpecTooLargeError, ValueError) as exc:
            # Same treatment as the changed-spec path: log and leave the cache empty
            # rather than storing something we could not translate.
            logger.warning(f"Spec for {hostname} rejected while rebuilding cache: {exc} (F-09)")
            return
        # No governance diff here on purpose. The spec hash is unchanged, so the tool set
        # is unchanged by definition — recording a change would report the device's whole
        # tool set as added every time a cache happened to lapse.
        await self._backend.set_manifest(hostname, _manifest_to_dict(manifest_obj), ttl=self._spec_cache_ttl)

    def _upstream_for(self, cfg: Any) -> Any:
        """A Streamable HTTP client for an MCP upstream, over the worker's guarded client."""
        from device_mcp_gateway.upstream.mcp_client import StreamableHttpClient

        return StreamableHttpClient(
            url=cfg.base_url,
            get_client=partial(self._client, cfg.hostname),
            auth=self._auth_provider(cfg) if self._auth_provider else None,
            timeout=self._discovery.get("timeout", 10),
        )

    async def _update_fingerprint(self, hostname: str, cfg: Any) -> None:
        """Compare the just-observed fingerprint with the pinned one and persist any change.

        The comparison and its logging are shared with embedded mode
        (``security.fingerprint_record``); this worker contributes what it observed.
        """
        seen = self._last_seen.pop(hostname, None) or fp.Observation()
        declared = self._last_declared.pop(hostname, None)
        if declared:
            seen = replace(seen, declared_name=declared[0], declared_version=declared[1])

        async def write(**fields: Any) -> None:
            await self._backend.update_device_fields(hostname, **fields)

        await record_observation(write, hostname, cfg, seen)

    async def _check_reachability(self, cfg: Any) -> bool:
        if getattr(cfg, "upstream_kind", "openapi") == "mcp":
            # An MCP endpoint answers a bare GET with 404/405, so the status check below
            # would score a dead upstream as healthy. Reachability here is a successful
            # handshake — and the probe session is closed rather than abandoned.
            upstream = self._upstream_for(cfg)
            try:
                info = await upstream.initialize()
                # ADR-0015 / F-69: serverInfo is the upstream's own statement of what it
                # is — MCP's analogue of the OpenAPI `info` block. Self-reported, so it is
                # recorded as the DECLARED dimension only, never as verification.
                server_info = info.get("serverInfo") if isinstance(info, dict) else None
                if isinstance(server_info, dict):
                    name, version = server_info.get("name"), server_info.get("version")
                    self._last_declared[cfg.hostname] = (
                        str(name) if name else None,
                        str(version) if version else None,
                    )
                self._record_observation(cfg.hostname)
                return True
            except (ResolverError, CredentialNotBound) as exc:
                # An MCP upstream authenticates during `initialize`, so this — not the spec
                # poll — is where a revoked credential lands, and it lands within one health
                # interval. Scored unreachable exactly as before: what the device's
                # reachability MEANS is a separate question from whether we say why it
                # failed. Recording the cause here is what stops a credential failure being
                # laundered into an unexplained "unreachable".
                self._probe_reason[cfg.hostname] = str(exc)
                logger.warning(f"Reachability probe for {cfg.hostname} failed on its credential: {exc}")
                return False
            except Exception:
                return False
            finally:
                await upstream.close_session()
        try:
            resp = await send_with_retry(
                lambda: self._client(cfg.hostname).get(cfg.base_url, timeout=5), method="GET", policy=self._retry_policy
            )
            self._record_observation(cfg.hostname)
            return resp.status_code < 500
        except Exception:
            return False

    def _record_observation(self, hostname: str) -> None:
        """Stash the TLS fingerprint the guarded transport saw on the probe just made.

        Reads it off the transport rather than the response: the certificate belongs to the
        live connection, and by the time this runs the socket may already be back in the
        pool. A plain-http device yields None and is simply not recorded — it has no
        authenticated dimension at all (ADR-0015 §7).
        """
        seen = observation_from_client(self._client(hostname))
        if seen is not None and seen.has_tls():
            self._last_seen[hostname] = seen

    def _record_declared_from_spec(self, hostname: str, spec: Any) -> None:
        """Stash an OpenAPI document's ``info`` identity for the next comparison (ADR-0015).

        Self-reported and therefore spoofable, exactly like MCP's ``serverInfo``: this is
        the DECLARED dimension only — inventory that doubles as a change signal, never
        evidence of identity.

        Consumed by ``_update_fingerprint`` on the *following* cycle rather than this one,
        because the spec poll runs after the comparison and on a slower cadence. That is
        soon enough for inventory data and cannot raise a false alarm in the meantime: a
        cycle with nothing stashed compares as "learned nothing", and ``_declared_changed``
        only fires when both the stored and seen values are present. The cost is that a
        newly registered device shows no declared identity until its first spec poll.

        Never raises on a malformed document — a spec that survived fetching but carries a
        junk ``info`` block must not break the health loop for that device.
        """
        declared = declared_from_spec(spec)
        if declared:
            self._last_declared[hostname] = declared

    async def _fetch_spec(self, cfg: Any) -> dict | None:
        # ADR-0020 §4b / LR-46: ask the one choke point first. A device carrying a curated
        # snapshot is never fetched from — not at registration and not on any later cycle,
        # which is what makes §4's version pinning hold. Without this the health loop would
        # replace the snapshot with whatever the live endpoint serves on its next pass.
        curated = resolve_spec_source(cfg)
        if curated is not None:
            try:
                return curated.parsed()
            except CuratedSpecInvalid as exc:
                logger.error(f"Curated spec unusable for {cfg.hostname}: {exc}")
                return None
        if getattr(cfg, "upstream_kind", "openapi") == "mcp":
            upstream = self._upstream_for(cfg)
            try:
                return await snapshot_upstream(upstream)
            except (ResolverError, CredentialNotBound) as exc:
                # Reached only when `initialize` authenticated and `tools/list` did not —
                # a credential that lapsed between the two calls. Rare next to the
                # reachability site above, but silent in exactly the same way if unhandled.
                self._probe_reason[cfg.hostname] = str(exc)
                logger.warning(f"MCP discovery for {cfg.hostname} failed on its credential: {exc}")
                return None
            except Exception as exc:
                logger.debug(f"MCP discovery failed for {cfg.hostname}: {exc}")
                return None
            finally:
                await upstream.close_session()
        if cfg.spec_url:
            try:
                resp = await send_with_retry(
                    lambda: self._client(cfg.hostname).get(cfg.spec_url, timeout=10),
                    method="GET",
                    policy=self._retry_policy,
                )
                return fetched_spec_or_none(resp, max_bytes=self._spec_max_bytes)
            except SpecTooLargeError as exc:
                logger.warning(f"Spec fetch rejected oversized spec for {cfg.spec_url}: {exc} (F-09)")
                return None
            except Exception:
                pass
            return None

        paths = self._discovery.get(
            "spec_paths",
            ["/openapi.json", "/swagger.json", "/api-docs"],
        )
        timeout = self._discovery.get("timeout", 10)
        for path in paths:
            try:
                url = cfg.base_url.rstrip("/") + path

                async def _probe(u: str = url) -> httpx.Response:
                    return await self._client(cfg.hostname).get(u, timeout=timeout)

                resp = await send_with_retry(_probe, method="GET", policy=self._retry_policy)
                spec = fetched_spec_or_none(resp, max_bytes=self._spec_max_bytes)
                if spec is not None:
                    return spec
            except SpecTooLargeError as exc:
                logger.warning(f"Spec discovery rejected oversized spec at {url}: {exc} (F-09)")
                continue
            except Exception:
                continue
        return None

    async def close(self) -> None:
        for client in list(self._http_clients.values()):
            if not client.is_closed:
                await client.aclose()
        self._http_clients.clear()


# The manifest serialiser now lives next to McpManifest in core.translator so the
# embedded (PodSupervisor) and distributed (this loop) sides share one canonical
# form. Re-exported under the original name for the worker.runner import.
_manifest_to_dict = manifest_to_dict
