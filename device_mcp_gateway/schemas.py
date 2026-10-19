# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Response models for the UI-facing read endpoints.

These give the gateway's OpenAPI a real, named contract (`DeviceSummary`,
`OverviewResponse`, ...) so consumers — notably the UI repo — can generate typed
clients from /openapi.json instead of hand-maintaining mirror types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from device_mcp_gateway.shared.registry_backend import DeviceConfig


class DeviceSummary(BaseModel):
    """Lean device projection for list/overview screens."""

    hostname: str
    base_url: str
    transport: str
    reachable: bool
    pod_active: bool
    last_check: float | None = None
    rate_limit_rps: float | None = None
    # "openapi" (translated from a spec) | "mcp" (a proxied remote MCP server). On the lean
    # summary because it changes how every other field should be read — an operator scanning
    # a list needs to know which upstreams are proxied without opening each one.
    upstream_kind: str = "openapi"
    # "ok" | "needs_reconnect" (ADR-0018 §3). On the LEAN summary deliberately, and the
    # reason is a defect in the field it copies: `fingerprint_state` is detail-only, so a
    # device awaiting approval is invisible in the fleet list and discoverable only by
    # opening it. A device needing re-authorization after a restore is precisely the case an
    # operator scans a list for, and it would arrive with the same gap on day one.
    #
    # A client MUST NOT render this as health. The device may be reachable, its pod running,
    # its spec fetching — it simply cannot authenticate, and only a human can fix that.
    credential_state: str = "ok"

    @classmethod
    def from_config(cls, cfg: DeviceConfig) -> DeviceSummary:
        """Project a registry ``DeviceConfig`` to the summary shape — the single
        place this mapping lives, so adding a field is a one-line change here
        rather than an edit at every read endpoint (F-19)."""
        return cls(
            hostname=cfg.hostname,
            base_url=cfg.base_url,
            transport=cfg.transport,
            reachable=cfg.reachable,
            pod_active=cfg.pod_active,
            last_check=cfg.last_check or None,
            rate_limit_rps=cfg.rate_limit_rps,
            upstream_kind=cfg.upstream_kind,
            credential_state=getattr(cfg, "credential_state", "ok"),
        )


class DeviceDetail(DeviceSummary):
    """Full device projection for the single-device read — a superset of
    :class:`DeviceSummary` adding the fields a detail/diagnostic view needs."""

    spec_url: str | None = None
    spec_hash: str | None = None
    auth_type: str | None = None
    spawn_error: str | None = None
    worker_id: str | None = None
    # Bumps whenever a spec change mutated the tool set (F-41); poll to detect a
    # change and re-list tools.
    tools_revision: int = 0
    # How the gateway reaches an "mcp" upstream. Meaningless for an OpenAPI device, so it
    # stays off the lean summary.
    upstream_transport: str = "http"

    # --- Endpoint fingerprint (ADR-0015) -------------------------------------------
    # Exposed as three separately-named groups rather than one "verified" boolean. A
    # client rendering these MUST keep them distinct: `declared_*` is self-reported by
    # the upstream and spoofable, while `tls_spki_sha256` is cryptographic. Presenting
    # them as one badge would lend the declared fields a weight they do not have.
    #
    # "unpinned" | "pinned" | "pending_approval". An http:// device stays "unpinned"
    # for its TLS dimension however many times it is probed — it has no certificate,
    # and that is a fact about the device rather than a gap in the check.
    fingerprint_state: str = "unpinned"
    fingerprint_pinned_at: float | None = None
    tls_spki_sha256: str | None = None
    tls_cert_sha256: str | None = None
    tls_issuer: str | None = None
    tls_not_after: str | None = None
    # Set only while pending_approval: what the key would become if approved, so an
    # operator can see both sides of the decision without digging through logs.
    pending_tls_spki_sha256: str | None = None
    declared_name: str | None = None
    declared_version: str | None = None
    # None means "inherit security.fingerprint_policy"; the effective value is resolved
    # at enforcement time, not stored.
    fingerprint_policy: str | None = None

    @classmethod
    def from_config(cls, cfg: DeviceConfig) -> DeviceDetail:
        return cls(
            hostname=cfg.hostname,
            base_url=cfg.base_url,
            transport=cfg.transport,
            reachable=cfg.reachable,
            pod_active=cfg.pod_active,
            last_check=cfg.last_check or None,
            rate_limit_rps=cfg.rate_limit_rps,
            spec_url=cfg.spec_url,
            spec_hash=cfg.spec_hash,
            auth_type=cfg.auth_type,
            spawn_error=cfg.spawn_error,
            worker_id=cfg.worker_id,
            tools_revision=cfg.tools_revision,
            upstream_kind=cfg.upstream_kind,
            upstream_transport=cfg.upstream_transport,
            fingerprint_state=cfg.fingerprint_state,
            fingerprint_pinned_at=cfg.fingerprint_pinned_at or None,
            tls_spki_sha256=cfg.tls_spki_sha256,
            tls_cert_sha256=cfg.tls_cert_sha256,
            tls_issuer=cfg.tls_issuer,
            tls_not_after=cfg.tls_not_after,
            pending_tls_spki_sha256=cfg.pending_tls_spki_sha256,
            declared_name=cfg.declared_name,
            declared_version=cfg.declared_version,
            fingerprint_policy=cfg.fingerprint_policy,
            # Repeated rather than inherited: `DeviceDetail.from_config` builds the whole
            # model itself instead of extending the summary's mapping, so a field added only
            # above silently defaults here. That is F-19's single-place rule already half
            # broken; the test asserts both projections carry the value so the next addition
            # cannot quietly land on one of them.
            credential_state=getattr(cfg, "credential_state", "ok"),
        )


class DeviceListResponse(BaseModel):
    devices: list[DeviceSummary]


class DeviceMutationResult(BaseModel):
    """Response for a register/update write: the write-time envelope plus the full
    resulting device, so a client gets the resource back without a follow-up GET."""

    status: str  # "registered" | "updated"
    provisioning: bool  # F-11: pod still spawning in the background when True
    device: DeviceDetail


class OverviewCounts(BaseModel):
    total: int
    active_pods: int
    reachable: int
    unreachable: int


class OverviewResponse(BaseModel):
    mode: str
    counts: OverviewCounts
    devices: list[DeviceSummary]
    #: After how many seconds without a successful check a device's `reachable` flag should
    #: be treated as **unknown** rather than as a reading (F-71).
    #:
    #: Published because the alternative is every consumer inventing its own threshold, and
    #: getting it silently wrong on any deployment that tuned `registry.health_check_interval`
    #: — a hardcoded constant would *look* like a measurement while being a guess. Derived
    #: here because this is the side that knows the poll interval.
    #:
    #: Note what this is **not**: circuit-breaker state. `BreakerState` is only readable in
    #: embedded mode (the pod runs on a worker in distributed mode), so a fleet list cannot
    #: be driven by it. Staleness works in both modes, which is why it is the discriminator.
    stale_after_seconds: float | None = None


class BreakerState(BaseModel):
    """Per-pod circuit-breaker state. Only readable when the pod is in-process
    (embedded mode); in distributed mode the pod runs on a worker, so the gateway
    reports ``available: false`` with a note."""

    available: bool
    state: str | None = None  # closed | open | half-open
    fail_counter: int | None = None
    fail_max: int | None = None
    reset_timeout: int | None = None
    note: str | None = None


class TlsProfileInfo(BaseModel):
    """The resolved outbound TLS posture for one device (no secrets, no full paths)."""

    source: str  # "fleet" | "device"
    verify: bool
    ca_bundle: str | None = None  # basename only — see security.mtls.describe
    client_cert: bool = False


class DeviceDiagnostics(BaseModel):
    """ "Why is my device down?" — a single read combining registry status, the
    last check, spec/manifest state, the spawn error, and the circuit breaker
    (F-52)."""

    hostname: str
    mode: str
    base_url: str
    spec_url: str | None = None
    transport: str
    # Which upstream this device is, so the fields below read correctly: a proxied MCP
    # server has no spec_url or spec_hash, and a missing one is normal rather than a fault.
    upstream_kind: str = "openapi"
    reachable: bool
    pod_active: bool
    worker_id: str | None = None
    last_check: float | None = None
    last_check_age_seconds: float | None = None
    spec_hash: str | None = None
    has_manifest: bool
    tool_count: int
    tools_revision: int = 0
    spawn_error: str | None = None
    breaker: BreakerState
    # Which outbound TLS profile this device resolved to. "fleet" means it inherited
    # security.mtls; "device" means a security.mtls.devices.<hostname> block applies.
    # Present so "why does this one device fail TLS?" is answerable from the same read
    # as everything else — and so a per-device verify:false is visible rather than
    # buried in a config nobody has opened since it was written.
    tls: TlsProfileInfo | None = None


class ToolChangeRecord(BaseModel):
    """The most recent tool-set change for a device — what was added/removed/
    changed when its upstream OpenAPI spec last mutated, and whether that was
    backwards-breaking for clients calling the old shape (F-41)."""

    tools_revision: int
    at: float  # unix timestamp the change was observed
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    breaking: bool = False
    breaking_reasons: list[str] = []


class ToolsDiffResponse(BaseModel):
    """Tool-set change governance for a device. ``last_change`` is ``null`` when no
    change has been observed since registration (the tool set is as first
    generated). ``tools_revision`` is the device's current monotonic revision —
    poll it to detect a change, then read ``last_change`` to see what moved."""

    hostname: str
    tools_revision: int
    last_change: ToolChangeRecord | None = None


class FeatureTool(BaseModel):
    """One tool, as a device exposes it. ``schema`` is the tool's JSON input schema — the
    same key ``/tools`` uses, so one client type reads both."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict, alias="schema")
    # "openapi" (translated from an operation, with method/path) | "proxy" (forwarded to a
    # remote MCP server, which has no HTTP shape — method/path are empty).
    source: str = "openapi"
    method: str = ""
    path: str = ""


class FeatureResource(BaseModel):
    """A read-only resource: ``resources/read`` on ``uri`` returns its content."""

    uri: str
    name: str
    description: str = ""
    mime_type: str = "application/json"


class FeaturePrompt(BaseModel):
    """A prompt template, with the argument names it takes."""

    name: str
    description: str = ""
    template: str = ""
    arguments: list[str] = []


class ServerInfo(BaseModel):
    """What the device says it is — never a name the gateway made up.

    An OpenAPI device: its spec's ``info.title`` and ``info.version``. A proxied MCP server:
    the ``serverInfo`` it sent on ``initialize``, as recorded on the device record
    (``declared_name``/``declared_version``). The manifest's own ``server_name`` is the
    gateway's ``mcp-{hostname}`` label for the server it *serves*, so it is not used here.
    """

    name: str
    version: str


class FeatureDiscovery(BaseModel):
    """Which feature kinds the gateway collects for this kind of device.

    ``False`` means *not looked for*, not *none found* — an empty list beside ``False`` says
    nothing about the device. Today a proxied MCP server's tools are discovered but its
    resources and prompts are not; an OpenAPI device has all three derived from its spec.
    """

    tools: bool = True
    resources: bool = True
    prompts: bool = True


class DeviceFeatures(BaseModel):
    """Everything a device exposes — tools, resources and prompts — from the registry's
    cached manifest, with or without a running pod.

    ``manifest_cached`` is ``False`` when there is no manifest to read: the device has never
    been discovered, or it has been down longer than the manifest cache lives. The lists are
    then empty because nothing is known, not because the device has nothing.
    """

    hostname: str
    upstream_kind: str
    pod_active: bool
    manifest_cached: bool
    tools_revision: int
    # None when the device's own identity is not recorded — e.g. an MCP upstream whose
    # serverInfo has not been persisted yet. Absent rather than a placeholder.
    server: ServerInfo | None = None
    discovered: FeatureDiscovery
    tools: list[FeatureTool] = []
    resources: list[FeatureResource] = []
    prompts: list[FeaturePrompt] = []


class WhoAmIResponse(BaseModel):
    """The authenticated caller's own identity and effective authorization, so a UI/BFF
    can gate views on the **gateway's** scopes rather than maintaining a parallel role
    model (ADR-0007). ``scopes`` is sorted for a stable response."""

    subject: str
    scopes: list[str]
    auth_method: str
