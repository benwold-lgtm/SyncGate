# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""Configuration loader — reads and returns the central config.yaml."""

import os
from typing import Any

import yaml
from loguru import logger

CONFIG_PATH = os.getenv("MCP_CONFIG", "config.yaml")

# Numeric leaf — accept int or float, but not bool (bool is a subclass of int).
_NUM = (int, float)


class _MapOf:
    """A section whose keys are operator-chosen (hostnames, ...), values a fixed shape.

    The schema below matches keys literally, which is the whole point of F-50 — but a
    few sections are legitimately open-ended. Wrapping the value shape says "recurse
    into each value with this schema, and don't treat the key itself as a typo".
    """

    __slots__ = ("schema",)

    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema


# Declared config schema (F-50): the set of known sections/keys and their expected
# value types. A leaf is a Python type (or tuple of types); a nested dict is a
# sub-section that is recursed into. This is the source of truth used to catch the
# silent-typo footgun — a misspelled or misplaced key is read with a .get() default
# elsewhere and otherwise ignored without warning.
_CONFIG_SCHEMA: dict[str, Any] = {
    "gateway": {
        "api_key": str,
        "allow_anonymous": bool,
        # A list of {name, key, role} entries. An entry may also carry `break_glass: true`
        # (ADR-0023), which makes `name` mandatory and requires `key` to be a secret://
        # reference rather than a literal — both enforced in rbac.py, fatally, with no
        # allow_... override.
        "rbac": list,
        "secret_key": str,
        "secret_keys": list,
        "allow_plaintext_credentials": bool,
        "allow_weak_keys": bool,
        # ADR-0023 §3 — lifetime of a `break_glass: true` entry, in days. Default 90.
        "break_glass_expiry_days": int,
        # ADR-0023 §3 reactivation-frequency flagging. A use after a quiet gap of
        # `session_gap_minutes` is a new *activation*; more than `review_threshold`
        # activations within `review_window_days` raises a review flag. FLAGGING ONLY —
        # none of these three can refuse a request. Starting defaults 60 / 30 / 3.
        "break_glass_session_gap_minutes": int,
        "break_glass_review_window_days": int,
        "break_glass_review_threshold": int,
        "max_body_bytes": int,
        "read_cache_ttl": _NUM,
        "trust_proxy_headers": bool,
        # Inbound OIDC (ADR-0013 §6/§6a) and the deployment's tenant identity. Declared
        # as opaque leaves: `oidc` has its own structural validation in
        # `build_oidc_validator`/`OIDCConfig`, which fails *fast and hard* at startup —
        # a second, warn-only schema here would be a weaker duplicate that drifts. Left
        # undeclared they were reported as unknown keys and "ignored", which is exactly
        # backwards: they are honoured, and an operator following the warning would
        # delete working OIDC config.
        "oidc": dict,
        "tenant_id": str,
        # Credential resolution (ADR-0018 §1/§2). Opaque for the same reason as `oidc`:
        # the structure is validated by `build_resolver`, and a second warn-only schema
        # here would drift. Declared so an enabled block is not reported as an unknown
        # key and "ignored" — the defect this list already carries a fix for.
        "credentials": dict,
    },
    "server": {"host": str, "port": int},
    "registry": {
        "mode": str,
        "health_check_interval": _NUM,
        "spec_poll_interval": _NUM,
        "spec_cache_ttl": _NUM,
        "tool_call_timeout": _NUM,
        "registration_provision_budget": _NUM,
        "reconcile_interval": _NUM,
        "reconcile_orphan_grace_cycles": int,
        "liveness_file": str,
        "max_concurrent_calls_per_device": int,
        "max_concurrent_calls_per_worker": int,
        "rebalance_enabled": bool,
        "idempotency_guard": bool,
        "call_backlog_limit": int,
        "fleet_max_devices": int,
        "spec_max_bytes": int,
        "spec_translate_timeout": _NUM,
        "shutdown_drain_timeout": _NUM,
        "health_lock_ttl": _NUM,
        "max_concurrent_pods": int,
        "max_retries": int,
        "retry_base_delay": _NUM,
        "retry_max_delay": _NUM,
    },
    "redis": {
        "url": str,
        "allow_insecure": bool,
        "socket_timeout": _NUM,
        "socket_connect_timeout": _NUM,
        "max_connections": int,
        "pool_timeout": _NUM,
        "pubsub_max_connections": int,
        "retries": int,
        "health_check_interval": _NUM,
        "startup_timeout": _NUM,
    },
    "auth": {
        "type": str,
        "api_key": {"header_name": str},
        "oauth2": {
            "token_endpoint": str,
            "client_id": str,
            "client_secret": str,
            "scopes": list,
        },
    },
    # Ignored since 0.4.0 (roadmap decision 6); still accepted so an old config validates, and
    # warned about at startup by warn_removed_settings.
    "transport": {"default": str, "sse": {"keep_alive_interval": _NUM}},
    "discovery": {"spec_paths": list, "timeout": _NUM},
    "storage": {"type": str, "db_path": str},
    "cors": {"allowed_origins": list},
    "security": {
        "allow_private_targets": bool,
        # TM-I-05: accept a plaintext http:// OIDC issuer/JWKS URI. Independent of
        # allow_private_targets — where an IdP sits and whether it has TLS are separate
        # properties, and one flag for both would stop describing what it permits.
        "allow_plaintext_idp": bool,
        "trusted_proxy_cidrs": list,
        "allowed_target_ports": list,
        # ADR-0015: "warn" (default) or "enforce". Overridable per device. Warn keeps a
        # device with a changed fingerprint working while flagging it; enforce refuses
        # tool calls and resource reads until it is approved or removed.
        "fingerprint_policy": str,
        "mtls": {
            "client_cert": str,
            "client_key": str,
            "client_key_password": str,
            "ca_bundle": str,
            "verify": bool,
            # Per-device overrides, keyed by hostname. Same five keys; anything the
            # device omits it inherits from the fleet block above. A stricter check
            # runs at startup (mtls.preflight), which *refuses to boot* on an unknown
            # key here rather than warning — see security/mtls.py.
            "devices": _MapOf(
                {
                    "client_cert": str,
                    "client_key": str,
                    "client_key_password": str,
                    "ca_bundle": str,
                    "verify": bool,
                }
            ),
        },
    },
    # Backup/restore (ADR-0011). The Argon2id parameters are configurable *and* written
    # into every portable archive's envelope, so raising the cost here can never orphan
    # an archive produced under the old settings — the reader uses what the envelope says.
    "backup": {
        "passphrase_min_length": int,
        "argon2_memory_cost_kib": int,
        "argon2_iterations": int,
        "argon2_lanes": int,
        "deadletter_limit": int,
        # ADR-0018 §6 — how long a plan_token stays valid after the dry run that
        # minted it. Resolved 2026-08-21: 7 days, a starting default rather than
        # a fixed value (instrumented and tuned from operating history).
        "plan_digest_validity_seconds": _NUM,
    },
    # ADR-0022 — Propose/Review/Apply for agent-initiated device writes. A separate block
    # from `backup`, even though `plan_digest_validity_seconds` there is the closest sibling:
    # these three govern a different mechanism (a `devices:write-planned` grant, not a
    # restore's HMAC token) with its own three lifetimes, not variations on backup's one.
    "write_planned": {
        # How long an unreviewed proposal survives before a reviewer must re-propose. A
        # proposal is meant to be reviewed promptly; longer is a stale-queue problem, not a
        # security one — the digest it would produce, once approved, is bounded separately.
        "proposal_ttl_seconds": _NUM,
        # Validity window for a single-use grant between Review and Apply. Generous enough
        # for an agent to act asynchronously, bounded enough to never be standing access.
        "grant_ttl_seconds": _NUM,
        # Max lifetime of a *repeatable* grant (§4). The ADR explicitly leaves this
        # undecided ("a follow-up, not decided here") — ships as an instrumented starting
        # default, the same treatment already given to `backup.plan_digest_validity_seconds`
        # (7 days) and the break-glass credential (90 days), tuned from real usage rather
        # than fixed permanently here.
        "repeatable_grant_max_seconds": _NUM,
    },
    # ADR-0017 — the tenant-minted support grant a provider operator is delegated. A
    # separate block from `write_planned`, even though the shape (a pending record, an
    # issued grant, both TTL'd) looks similar: a support grant is checked LIVE on every
    # request during its whole window, never consumed once like write_planned's grant, so
    # its lifetimes mean something different and are not reused from that block.
    "support_requests": {
        # How long a raised-but-undecided request survives before the provider operator
        # must re-raise it. Short — this is a live, human-in-the-loop interaction, not an
        # asynchronous queue, so a stale request is a UX problem more than a security one.
        "request_ttl_seconds": _NUM,
        # Validity window for an approved grant — the tenant's own §2 "absolute expiry,
        # never renewed in place." Deliberately much shorter than a repeatable
        # (standing-consent) grant's ceiling below.
        "grant_ttl_seconds": _NUM,
        # Max lifetime of a grant issued under standing consent (§3) — operates on a
        # months-scale timescale, deliberately distinct from an individual grant's window.
        # Same starting-default treatment as `write_planned.repeatable_grant_max_seconds`
        # and the break-glass credential's 90-day default: instrumented and tuned from
        # operating history, not fixed permanently here.
        "standing_consent_max_seconds": _NUM,
        # ADR-0017 §7, Tier 1: how far a signed request's own timestamp may drift from the
        # gateway's clock — wide enough to absorb genuine clock skew and network latency,
        # tight enough that a captured signature is worthless well before anyone could reuse
        # it. Unrelated to the three lifetimes above: those bound a grant's own existence,
        # this bounds one proof's freshness.
        "proof_window_seconds": _NUM,
        # Slice 5 (§4's own reasoning, extended to standing consent): a standing-consent
        # self-issued grant has no per-instance human approval, so a subject self-issuing
        # very often within this trailing window is flagged — the same "an emergency path
        # used this routinely has become unreviewed access" signal ADR-0023 §3 already
        # tracks for break-glass, applied here to the one support-grant path that is
        # equally silent by construction.
        "self_issue_review_window_days": _NUM,
        "self_issue_review_threshold": _NUM,
    },
    # Slice 5 / the confirmed ADR-0023 gap: a durable, tenant-facing surface for signals
    # that must not only be logged (a break-glass activation, a frequently self-issued
    # support grant) — what a future tenant console polls, not an email/webhook channel.
    "tenant_notifications": {
        # Capped by LTRIM, not TTL'd — a fixed-size recent list, not an expiring one.
        "max_retained": _NUM,
    },
    "metrics": {"enabled": bool, "port": int, "gauge_refresh_interval": _NUM, "auth_token": str},
    "tracing": {
        "enabled": bool,
        "otlp_endpoint": str,
        "service_name": str,
        "sample_ratio": _NUM,
    },
    "logging": {
        "level": str,
        "file": str,
        "max_size": _NUM,
        "backup_count": int,
        "json_logs": bool,
        "audit_file": str,
        "worker_audit_file": str,
        "audit_retention": str,
        "audit_enabled": bool,
    },
}


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    """Load configuration from a YAML file.

    Missing or empty file → built-in defaults. Malformed YAML fails fast with a
    clear error rather than crashing later on a None/partial config.
    """
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(f"Config file {path} not found, using defaults")
        return _defaults()
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Config file {path} is not valid YAML: {exc}") from exc

    if data is None:
        logger.warning(f"Config file {path} is empty, using defaults")
        return _defaults()
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Config file {path} must contain a YAML mapping at the top level, got {type(data).__name__}"
        )
    validate_config(data, source=path)
    return data


def _type_ok(value: Any, expected: Any) -> bool:
    """Type check that treats bool as distinct from int/float (it's a subclass)."""
    if expected is bool:
        return isinstance(value, bool)
    types = expected if isinstance(expected, tuple) else (expected,)
    if isinstance(value, bool):
        # A bool is never a valid int/float/str leaf here — flag it as a mismatch.
        return bool in types
    return isinstance(value, types)


def _type_names(expected: Any) -> str:
    types = expected if isinstance(expected, tuple) else (expected,)
    return "/".join(t.__name__ for t in types)


def _validate_section(data: dict, schema: dict, prefix: str, problems: list[str]) -> None:
    """Recursively compare a config section against the schema, recording problems."""
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if key not in schema:
            problems.append(f"unknown config key '{dotted}' — ignored (typo? wrong section?)")
            continue
        expected = schema[key]
        if isinstance(expected, _MapOf):
            if isinstance(value, dict):
                for entry_key, entry in value.items():
                    if isinstance(entry, dict):
                        _validate_section(entry, expected.schema, f"{dotted}.{entry_key}.", problems)
                    else:
                        problems.append(
                            f"config key '{dotted}.{entry_key}' should be a mapping, got {type(entry).__name__}"
                        )
            else:
                problems.append(f"config key '{dotted}' should be a mapping, got {type(value).__name__}")
            continue
        if isinstance(expected, dict):
            if isinstance(value, dict):
                _validate_section(value, expected, f"{dotted}.", problems)
            else:
                problems.append(f"config key '{dotted}' should be a mapping, got {type(value).__name__}")
            continue
        # A leaf: None is allowed (means "unset / use default").
        if value is not None and not _type_ok(value, expected):
            problems.append(f"config key '{dotted}' should be {_type_names(expected)}, got {type(value).__name__}")


def validate_config(cfg: dict[str, Any], *, source: str = "config") -> list[str]:
    """Validate a loaded config against the declared schema (F-50).

    Surfaces the silent-typo footgun: unknown/misplaced keys and type mismatches are
    logged as warnings (with their dotted path) instead of being read-with-a-default
    and ignored. Returns the list of problem strings (also for testing). Warns rather
    than hard-fails so an otherwise-working deployment isn't taken down by a stray key.
    """
    problems: list[str] = []
    _validate_section(cfg, _CONFIG_SCHEMA, "", problems)
    for p in problems:
        logger.warning(f"Config validation ({source}): {p}")
    return problems


def warn_removed_settings(cfg: dict[str, Any]) -> list[str]:
    """Name config keys that no longer do anything, so stale config is visible, not silent.

    A warning rather than a refusal to start (roadmap decision 6): configuration that selects
    nothing is not worth an outage. Returns the warnings for testing.
    """
    warnings: list[str] = []
    transport = cfg.get("transport")
    if isinstance(transport, dict):
        keys = [f"transport.{k}" for k in sorted(transport)]
        if keys:
            warnings.append(
                f"{', '.join(keys)} {'is' if len(keys) == 1 else 'are'} ignored: HTTP+SSE was removed in 0.4.0 "
                "and every device is served on /v1/devices/{hostname}/mcp. Remove the transport block."
            )
    for w in warnings:
        logger.warning(f"Removed configuration: {w}")
    return warnings


def warn_unsafe_settings(cfg: dict[str, Any], mode: str, auth_enabled: bool) -> list[str]:
    """Warn loudly when the *forgotten/default* posture is permissive (F-53).

    The safe path should be the default; these warnings make the convenient-but-unsafe
    states visible at startup instead of silent. Non-fatal (the hard refusals live in
    the Tier-0 distributed-mode gates); returns the warning strings for testing.
    """
    warnings: list[str] = []
    host = resolve_bind_host(cfg)
    origins = cfg.get("cors", {}).get("allowed_origins", []) or []

    if not auth_enabled:
        warnings.append(
            "authentication is DISABLED — every API request is served with full access. "
            "Set gateway.api_key / MCP_ADMIN_KEY / gateway.rbac to require a token."
        )
    if "*" in origins:
        warnings.append(
            "cors.allowed_origins contains '*' (wildcard) while credentials are allowed — any origin "
            "can call the API from a browser. Set explicit origins for anything but local development."
        )
    if host in ("0.0.0.0", "::") and not auth_enabled:  # nosec B104 — detecting bind-all to warn, not binding
        warnings.append(
            f"binding {host} (all interfaces) with authentication disabled — the API is reachable and "
            "unauthenticated on every network interface. Bind 127.0.0.1 or enable auth."
        )
    if cfg.get("security", {}).get("allow_plaintext_idp"):
        warnings.append(
            "security.allow_plaintext_idp is true — a plaintext http:// OIDC issuer is accepted, so access "
            "tokens and the IdP's signing keys cross the network unencrypted and an on-path attacker can "
            "substitute either. Intended for lab and air-gapped deployments; use https anywhere else."
        )

    mtls = cfg.get("security", {}).get("mtls")
    if isinstance(mtls, dict) and mtls.get("verify") is False:
        warnings.append(
            "security.mtls.verify is false — outbound TLS certificate verification is DISABLED for EVERY "
            "device, so no device server cert is checked (man-in-the-middle exposure). Set it true and "
            "disable verification per-device (security.mtls.devices.<hostname>.verify) if one device needs it."
        )
    # A per-device opt-out is the supported narrow escape hatch, but it is still an
    # unverified TLS channel — name the devices so it can't be forgotten in a config
    # nobody has read in a year. The fleet warning above already covers the broad case.
    if isinstance(mtls, dict) and isinstance(mtls.get("devices"), dict):
        unverified = sorted(
            h for h, blk in mtls["devices"].items() if isinstance(blk, dict) and blk.get("verify") is False
        )
        if unverified and mtls.get("verify") is not False:
            warnings.append(
                "outbound TLS certificate verification is DISABLED for "
                f"{len(unverified)} device(s): {', '.join(unverified)} — those channels are not "
                "authenticated (man-in-the-middle exposure). Prefer a ca_bundle for the device's own CA."
            )
    for w in warnings:
        logger.warning(f"Unsafe configuration ({mode} mode): {w}")
    return warnings


def resolve_mode(cfg: dict[str, Any]) -> str:
    """Resolve registry mode, letting MCP_REGISTRY_MODE override the config file.

    Centralised so the gateway and worker agree on the mode and can't silently
    diverge (gateway embedded + worker distributed = split brain).
    """
    return os.getenv("MCP_REGISTRY_MODE") or cfg.get("registry", {}).get("mode", "embedded")


def resolve_bind_host(cfg: dict[str, Any]) -> str:
    """Resolve the *effective* bind address, honoring the CLI --host override.

    The app is built at import time (``app = create_app()``) — before uvicorn binds —
    so ``warn_unsafe_settings`` can't see a ``--host`` flag passed to the console script.
    The CLI exports the resolved host as ``MCP_BIND_HOST`` before importing the app, so
    the bind-all warning reflects the address actually bound rather than only the config
    value (which would cry wolf when ``--host 127.0.0.1`` overrides a ``0.0.0.0`` config).
    """
    return os.getenv("MCP_BIND_HOST") or cfg.get("server", {}).get("host", "0.0.0.0")  # nosec B104 — read, not a bind


def plan_digest_validity_seconds(cfg: dict[str, Any]) -> int:
    """ADR-0018 §6 — seconds a plan_token stays valid after the dry run that minted it."""
    return int(cfg.get("backup", {}).get("plan_digest_validity_seconds") or 7 * 24 * 60 * 60)


def write_planned_proposal_ttl_seconds(cfg: dict[str, Any]) -> int:
    """ADR-0022 — seconds an unreviewed proposal survives before a reviewer must re-propose."""
    return int(cfg.get("write_planned", {}).get("proposal_ttl_seconds") or 3600)


def write_planned_grant_ttl_seconds(cfg: dict[str, Any]) -> int:
    """ADR-0022 — seconds a single-use `devices:write-planned` grant stays valid before Apply."""
    return int(cfg.get("write_planned", {}).get("grant_ttl_seconds") or 24 * 60 * 60)


def write_planned_repeatable_max_seconds(cfg: dict[str, Any]) -> int:
    """ADR-0022 §4 — max lifetime of a *repeatable* grant. Not fixed by the ADR; see the
    schema comment on `write_planned.repeatable_grant_max_seconds` for why 30 days."""
    return int(cfg.get("write_planned", {}).get("repeatable_grant_max_seconds") or 30 * 24 * 60 * 60)


def configured_tenant_id(cfg: dict[str, Any]) -> str:
    """ADR-0024 §10 — which tenant this stack serves, as its own config states it.

    `gateway.tenant_id` has existed since ADR-0019 and its own comment in `config.yaml` said
    plainly that **nothing read it at runtime**. Redemption is the first thing that legitimately
    must: the provider redeeming an invitation has to learn which tenant it just enrolled, so
    that it can check the catalog credential it minted was minted for that same tenant. Without
    it, a mistyped tenant id at the provider's end produces a credential belonging to one tenant
    installed in another's console — the misdelivery ADR-0020 §7b exists to catch, arriving one
    level up at the relationship rather than the request.

    Returns "" when unset, which `api/enrolments.py` treats as a refusal rather than a default.
    """
    return str(cfg.get("gateway", {}).get("tenant_id") or "").strip()


def support_request_ttl_seconds(cfg: dict[str, Any]) -> int:
    """ADR-0017 — seconds a raised support request survives before it must be re-raised."""
    return int(cfg.get("support_requests", {}).get("request_ttl_seconds") or 300)


def support_grant_ttl_seconds(cfg: dict[str, Any]) -> int:
    """ADR-0017 §2 — ceiling on an individual support grant's absolute expiry."""
    return int(cfg.get("support_requests", {}).get("grant_ttl_seconds") or 60 * 60)


def support_standing_consent_max_seconds(cfg: dict[str, Any]) -> int:
    """ADR-0017 §3 — max lifetime of a grant issued under standing consent. See the schema
    comment on `support_requests.standing_consent_max_seconds` for why 90 days."""
    return int(cfg.get("support_requests", {}).get("standing_consent_max_seconds") or 90 * 24 * 60 * 60)


def support_proof_window_seconds(cfg: dict[str, Any]) -> float:
    """ADR-0017 §7, Tier 1 — how far a signed request's timestamp may drift from the
    gateway's own clock. 30s: generous for clock skew and latency, tight for replay value."""
    return float(cfg.get("support_requests", {}).get("proof_window_seconds") or 30)


def support_self_issue_review_window_days(cfg: dict[str, Any]) -> int:
    """ADR-0017 slice 5 — trailing window a subject's standing-consent self-issues are
    counted over. Same 30-day default as break-glass's own review window."""
    return int(cfg.get("support_requests", {}).get("self_issue_review_window_days") or 30)


def support_self_issue_review_threshold(cfg: dict[str, Any]) -> int:
    """ADR-0017 slice 5 — self-issues within the window above this many flags for review.
    Same default as break-glass's own threshold — both signals answer the same question."""
    return int(cfg.get("support_requests", {}).get("self_issue_review_threshold") or 3)


def tenant_notifications_max_retained(cfg: dict[str, Any]) -> int:
    """ADR-0017 slice 5 — how many recent tenant-facing notifications are retained."""
    return int(cfg.get("tenant_notifications", {}).get("max_retained") or 200)


def _defaults() -> dict:
    return {
        "gateway": {
            "api_key": "",
            "secret_key": "",
            "allow_plaintext_credentials": False,
            "max_body_bytes": 1_048_576,
        },
        "server": {"host": "0.0.0.0", "port": 8000},  # nosec B104 — bind-all is intended in containers
        "registry": {
            "mode": "embedded",
            "health_check_interval": 30,
            "spec_poll_interval": 300,
            "spec_cache_ttl": 3600,
            "tool_call_timeout": 30,
            "max_concurrent_pods": 50,
            "max_retries": 2,
            "retry_base_delay": 0.2,
            "retry_max_delay": 5.0,
        },
        "redis": {
            "url": "redis://localhost:6379/0",
            "socket_timeout": 5,
            "max_connections": 20,
            "pubsub_max_connections": 1000,
        },
        "auth": {"type": "api_key"},
        "storage": {"db_path": "./data/devices.db"},
        "cors": {"allowed_origins": []},
        "backup": {
            "passphrase_min_length": 16,
            "argon2_memory_cost_kib": 65536,  # 64 MiB (ADR-0011)
            "argon2_iterations": 3,
            "argon2_lanes": 4,
            "deadletter_limit": 1000,
            "plan_digest_validity_seconds": 7 * 24 * 60 * 60,  # 7 days (ADR-0018 §6)
        },
        "write_planned": {
            "proposal_ttl_seconds": 3600,  # 1 hour (ADR-0022)
            "grant_ttl_seconds": 24 * 60 * 60,  # 1 day (ADR-0022)
            "repeatable_grant_max_seconds": 30 * 24 * 60 * 60,  # 30 days, a starting default (ADR-0022 §4)
        },
        "support_requests": {
            "request_ttl_seconds": 300,  # 5 minutes — a live, human-in-the-loop wait (ADR-0017)
            "grant_ttl_seconds": 60 * 60,  # 1 hour ceiling on an individual grant (ADR-0017 §2)
            "standing_consent_max_seconds": 90 * 24 * 60 * 60,  # 90 days, a starting default (ADR-0017 §3)
            "proof_window_seconds": 30,  # Tier 1 signature freshness window (ADR-0017 §7)
            "self_issue_review_window_days": 30,  # slice 5, same shape as break-glass §3
            "self_issue_review_threshold": 3,
        },
        "tenant_notifications": {"max_retained": 200},  # slice 5
        "metrics": {"enabled": True, "port": 9100, "gauge_refresh_interval": 15},
        "logging": {"level": "INFO", "audit_retention": "90 days", "audit_enabled": True},
    }
