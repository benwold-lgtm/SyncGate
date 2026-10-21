# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""PodSupervisor — embedded-mode DevicePod lifecycle (F-12).

Extracted from the Registry god-object. Owns spawning and killing in-process
DevicePods: translating a fetched spec into an MCP manifest (in a worker
process), constructing the pod with the right auth / retry / outbound-TLS, and
enforcing the per-process pod cap.

Depends on SpecService only as a fallback (fetch the spec if the profile doesn't
have it yet); it never fetches reachability or drives the health loop — that
orchestration stays in the Registry.
"""

from __future__ import annotations

import atexit
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from typing import Any

from loguru import logger

from device_mcp_gateway.auth.base import CredentialsChangedHook
from device_mcp_gateway.core.spec_limits import (
    DEFAULT_MAX_OPERATIONS,
    DEFAULT_TRANSLATE_TIMEOUT,
    SpecTooLargeError,
    run_translation,
)
from device_mcp_gateway.core.translator import SpecTranslator, manifest_to_dict
from device_mcp_gateway.pods.device_pod import DevicePod
from device_mcp_gateway.pods.mcp_proxy_pod import McpProxyPod
from device_mcp_gateway.upstream.mcp_discovery import build_proxy_manifest_from
from device_mcp_gateway.credentials import build_resolver
from device_mcp_gateway.security.url_policy import resolve_allow_private, resolve_allowed_ports
from device_mcp_gateway.registry.models import DeviceProfile
from device_mcp_gateway.registry.spec_service import SpecService
from device_mcp_gateway.security.mtls import TlsProfiles
from device_mcp_gateway.shared.registry_backend import AbstractRegistryBackend

# Process-global translation pool, shared across Registry instances. Registered
# with atexit (not torn down per-instance) so reuse across instances is safe (RC-5).
_spec_executor = ProcessPoolExecutor(max_workers=4)


def _shutdown_spec_executor() -> None:
    """Reap the spec-translation worker processes at interpreter exit (RC-5)."""
    _spec_executor.shutdown(wait=False)


atexit.register(_shutdown_spec_executor)


def _translate_spec_sync(spec: dict, hostname: str) -> Any:
    """Run in a worker process to avoid blocking the event loop."""
    return SpecTranslator().translate(spec, hostname)


class PodSupervisor:
    """Spawns and kills embedded DevicePods for device profiles."""

    def __init__(
        self,
        *,
        backend: AbstractRegistryBackend,
        config: dict[str, Any],
        tls_profiles: TlsProfiles,
        retry_policy: Any,
        spec_service: SpecService,
        profiles: dict[str, DeviceProfile],
        credential_writeback: Callable[[str], CredentialsChangedHook] | None = None,
        discovery_for: Callable[[DeviceProfile], Any] | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        # ADR-0018 §2. Built here from the config this supervisor already holds rather than
        # injected, so embedded and distributed mode cannot end up disagreeing about whether
        # resolution is available — the divergence that a second wiring path would invite.
        self._credential_resolver = build_resolver(config)
        self._tls = tls_profiles
        self._retry_policy = retry_policy
        self._spec_service = spec_service
        # LR-47 / ADR-0020 §4b. `spawn` used to call `self._spec_service.fetch_spec` directly
        # while both Registry call sites went through `_discovery_for`, so this was the one
        # embedded site that branched on nothing: an `upstream_kind == "mcp"` device with no
        # cached spec got the OPENAPI service, and a curated device would have been fetched
        # from rather than served its snapshot. Reachable — `_health_check_one` calls `spawn`
        # guarded on `reachable and not pod_active`, never on `spec_data`.
        #
        # Optional so a supervisor built without one (tests) still runs, falling back to the
        # previous behaviour rather than crashing; the Registry always supplies it.
        self._discovery_for = discovery_for
        # Builds the per-device hook that re-persists credentials an auth handler rotated
        # at runtime. Optional so a supervisor constructed without one (tests) still runs;
        # the Registry always supplies it.
        self._credential_writeback = credential_writeback
        # Shared with the Registry: read-only here, to enforce the pod cap.
        self._profiles = profiles
        self._max_pods = config.get("max_concurrent_pods", 50)
        self._spec_translate_timeout = config.get("spec_translate_timeout", DEFAULT_TRANSLATE_TIMEOUT)
        self._max_upstream_tools = config.get("max_upstream_tools", DEFAULT_MAX_OPERATIONS)

    async def spawn(self, profile: DeviceProfile) -> None:
        if sum(1 for p in self._profiles.values() if p.pod_active) >= self._max_pods:
            logger.warning("Max pods reached, skipping spawn")
            return
        if not profile.spec_data:
            source = self._discovery_for(profile) if self._discovery_for else self._spec_service
            await source.fetch_spec(profile)
        spec = profile.spec_data
        if not spec:
            msg = f"No spec available for {profile.hostname}, cannot spawn pod"
            logger.warning(msg)
            profile.config.spawn_error = msg
            return
        is_proxy = profile.config.upstream_kind == "mcp"
        try:
            if is_proxy:
                # Already-discovered tool definitions: no parsing to offload, so this does
                # NOT go through the translation pool. That pool is for CPU-bound parsing
                # of a document; running discovery output through it would occupy a worker
                # slot for no benefit and demand a picklable callable.
                mcp_manifest = build_proxy_manifest_from(profile.hostname, spec, max_tools=self._max_upstream_tools)
            else:
                mcp_manifest = await run_translation(
                    _spec_executor,
                    partial(_translate_spec_sync, spec, profile.hostname),
                    timeout=self._spec_translate_timeout,
                    hostname=profile.hostname,
                )
        except (SpecTooLargeError, ValueError) as exc:
            msg = f"Spec for {profile.hostname} rejected: {exc} (F-09)"
            logger.warning(msg)
            profile.config.spawn_error = msg
            return
        if profile.auth is not None and self._credential_writeback is not None:
            profile.auth.on_credentials_changed(self._credential_writeback(profile.hostname))
        pod_cls = McpProxyPod if is_proxy else DevicePod
        pod = pod_cls(
            hostname=profile.hostname,
            manifest=mcp_manifest,
            auth=profile.auth,
            base_url=profile.base_url,
            rate_limit_rps=profile.rate_limit_rps,
            retry_policy=self._retry_policy,
            tls_verify=self._tls.for_device(profile.hostname),
            allow_private=resolve_allow_private(self._config),
            allowed_ports=resolve_allowed_ports(self._config),
            credential_resolver=self._credential_resolver,
        )
        await pod.start()
        profile.pod = pod
        profile.config.pod_active = True
        profile.config.spawn_error = None
        # Cache the manifest so REST introspection (GET /devices/{h}/tools) works in
        # embedded mode too. The distributed worker does this in its health loop;
        # embedded never did, so /tools always 409'd "no manifest cached" even though
        # MCP tools/list served fine from the live pod (F-12 / review bug #4).
        ttl = self._config.get("spec_cache_ttl", 3600)
        await self._backend.set_manifest(profile.hostname, manifest_to_dict(mcp_manifest), ttl=ttl)
        await self._backend.update_device_fields(profile.hostname, pod_active=True, spawn_error=None)
        logger.info(f"Pod spawned for {profile.hostname}")

    async def kill(self, profile: DeviceProfile) -> None:
        if profile.pod and profile.pod_active:
            profile.pod.stop()
            await profile.pod.aclose()
            profile.config.pod_active = False
            await self._backend.update_device_fields(profile.hostname, pod_active=False)
            logger.info(f"Pod killed for {profile.hostname}")

    async def replace(self, profile: DeviceProfile) -> None:
        """Kill then re-spawn a pod (used on spec change)."""
        await self.kill(profile)
        await self.spawn(profile)
