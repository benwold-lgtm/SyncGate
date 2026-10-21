# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""The gates every device registration must pass, wherever it comes from (F-67).

These lived inside ``api/devices.py`` as route-handler helpers, which made the egress
policy (F-02) a property of *the HTTP path to registration* rather than of registration
itself: ``Registry.register_device`` never calls ``validate_target_url``, so any second
caller would have silently bypassed the SSRF gate, the hostname rules, and the upstream
discriminator checks.

That mattered the moment backup restore arrived. [ADR-0011](../../docs/adr/0011-backup-and-restore.md)
§4 rests on "restore replays through ``register_device``, so the egress policy still
applies" — which was not true of the code. A restore built on that sentence would have let
a ``backup:write`` holder reinstate a device whose ``base_url`` the current policy forbids:
exactly the privilege-escalation primitive the ADR set out to deny.

So the gates live here, and both callers — ``POST/PUT /v1/devices`` and restore — run the
same ones. They raise ``HTTPException`` rather than a bespoke error type because the route
handlers want precisely that; restore catches it and reports ``exc.detail`` per device,
which is what makes "this one device is no longer permitted" a per-device outcome instead
of a failed batch.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from device_mcp_gateway.security.url_policy import UrlPolicyError, validate_target_url

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$")

_UPSTREAM_KINDS = ("openapi", "mcp")
_UPSTREAM_TRANSPORTS = ("http", "sse")


def _validate_hostname(hostname: str) -> None:
    if not hostname or len(hostname) > 253 or not _HOSTNAME_RE.match(hostname):
        raise HTTPException(
            status_code=400,
            detail="hostname must be 1–253 characters, start and end with a letter or digit, "
            "and contain only letters, digits, hyphens, or dots",
        )


#: Where a client refused for sending ``transport`` is pointed.
TRANSPORT_REMOVED = (
    "transport was removed in 0.4.0: every device is served on /v1/devices/{hostname}/mcp "
    "(Streamable HTTP, both MCP eras), so the field selects nothing. Omit it. "
    "See docs/upgrade.md#040-transport-removed"
)


def refuse_transport(declared: set[str]) -> None:
    """Roadmap decision 6: a request carrying ``transport`` is refused, not silently obeyed.

    On presence, whatever the value — ``"sse"`` included. Accepting and ignoring it would keep
    a field that claims a choice which no longer exists, and would hide from the client that
    the transport it names is gone. The 0.3.6 precedent for ``expected_tls_spki_sha256``: a
    field the server would discard is refused instead.
    """
    if "transport" in declared:
        raise HTTPException(status_code=400, detail=TRANSPORT_REMOVED)


def _validate_upstream(kind: str, upstream_transport: str, spec_url: str | None, declared: set[str]) -> None:
    """Validate the upstream discriminators (ADR-0009).

    ``declared`` is the set of upstream keys the caller actually sent, so a value that was
    merely defaulted is not held against them — only an explicit ``upstream_transport`` on
    an OpenAPI device is an error.

    The value space is fixed here even where the implementation is not, so the field never
    has to widen later. One refusal is therefore "known but not yet built" rather than
    "invalid", and it says so: a caller has to be able to tell a typo from a feature that
    has not landed.
    """
    if kind not in _UPSTREAM_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"upstream_kind '{kind}' is not recognised; use one of {', '.join(_UPSTREAM_KINDS)}",
        )
    if upstream_transport not in _UPSTREAM_TRANSPORTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"upstream_transport '{upstream_transport}' is not recognised; "
                f"use one of {', '.join(_UPSTREAM_TRANSPORTS)}"
            ),
        )
    if kind == "openapi" and "upstream_transport" in declared:
        raise HTTPException(
            status_code=400,
            detail="upstream_transport applies only to upstream_kind 'mcp'; an OpenAPI device is reached over HTTP",
        )
    if kind == "mcp" and spec_url:
        raise HTTPException(
            status_code=400,
            detail="spec_url does not apply to upstream_kind 'mcp'; a proxied MCP server has no OpenAPI document",
        )
    if kind == "mcp" and upstream_transport == "sse":
        raise HTTPException(
            status_code=400,
            detail="upstream_transport 'sse' is not yet supported; use 'http' (Streamable HTTP)",
        )


def _read_upstream(data: dict, existing_kind: str = "openapi", existing_transport: str = "http") -> tuple[str, str]:
    """Resolve the upstream discriminators from a request body, preserving stored values.

    A PUT that says nothing about the upstream must not reset it to the default — the same
    class of bug as the PUT-wipes-credentials regression.
    """
    return (
        data.get("upstream_kind") or existing_kind,
        data.get("upstream_transport") or existing_transport,
    )


def _parse_rate_limit(data: dict) -> float | None:
    rps = data.get("rate_limit_rps")
    if rps is None:
        return None
    try:
        rps = float(rps)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="rate_limit_rps must be a positive number")
    if rps <= 0:
        raise HTTPException(status_code=400, detail="rate_limit_rps must be a positive number")
    return rps


def _check_target_url(url: str | None, field: str, allow_private: bool, allowed_ports: set[int] | None = None) -> None:
    """SSRF policy for device target URLs (Tier-0 F-02). base_url/spec_url are fetched
    server-side, so reject internal/loopback/link-local targets unless explicitly allowed
    (security.allow_private_targets, or the MCP_ALLOW_PRIVATE_TARGETS env override), and
    refuse non-HTTP service ports (security.allowed_target_ports)."""
    if not url:
        return
    try:
        validate_target_url(url, allow_private=allow_private, allowed_ports=allowed_ports)
    except UrlPolicyError as exc:
        raise HTTPException(status_code=400, detail=f"Rejected {field}: {exc}")


def validate_device_registration(
    *,
    hostname: str,
    base_url: str,
    spec_url: str | None,
    upstream_kind: str,
    upstream_transport: str,
    declared: set[str],
    allow_private: bool,
    allowed_ports: set[int] | None = None,
    auth: Any = None,
    require_references: bool = False,
) -> None:
    """Every gate a device must pass to be registered, in one call.

    The composite exists so a second caller cannot forget one. Adding a gate here reaches
    the HTTP path and the restore path together, which is the property F-67 was about —
    a check that only one of two registration routes performs is not a control.
    """
    _validate_hostname(hostname)
    _check_target_url(base_url, "base_url", allow_private, allowed_ports)
    refuse_transport(declared)
    _check_target_url(spec_url, "spec_url", allow_private, allowed_ports)
    _validate_upstream(upstream_kind, upstream_transport, spec_url, declared)
    check_credential_form(auth, require_references)


def check_credential_form(auth: Any, require_references: bool) -> None:
    """ADR-0018 §1: refuse an inline secret where the deployment requires references.

    **Gated, and off by default.** Turning it on is a deployment decision, because it is a
    breaking one for any fleet registered before references existed.

    **A write-path gate, deliberately, and not a constructor check.** Every other ADR-0018
    rule lives in the handler's ``__post_init__`` so that registration, a restore and a worker
    rehydrating from the registry all get the same answer. This one must not: the rehydrate
    path constructs the same handler from an *already stored* device, so refusing there would
    turn a policy change into an outage — every existing inline device would fail to load on
    the next restart. Existing devices keep dispatching; only new writes are refused.

    It sits in the composite rather than in the route so the **restore path is gated too**,
    which is F-67's rule: a `backup:write` holder must not be able to reinstate a device that
    a fresh registration would refuse.
    """
    if not require_references or auth is None:
        return
    inline = auth.inline_secret_fields()
    if not inline:
        return
    fields = ", ".join(inline)
    refs = ", ".join(f"{f}_ref" if f != "api_key" else "credential_ref" for f in inline)
    raise HTTPException(
        status_code=400,
        detail=(
            f"this deployment requires credentials by reference (ADR-0018 §1) and this device "
            f"supplies {fields} inline. Put the secret in the credential store and register it "
            f"with {refs} instead. Existing devices are unaffected; this applies to new "
            f"registrations and updates. To allow inline credentials again, set "
            f"gateway.credentials.require_references: false."
        ),
    )
