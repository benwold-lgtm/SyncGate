# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""One place that builds Redis key names (Phase 0 of the MCP-passthrough plan).

Every Redis key was an inline f-string at its call site — ~70 of them across 12 modules,
with no prefixing helper. That is fine today and expensive later: if in-application
multitenancy is ever adopted, a tenant prefix has to be threaded through every one of them,
and each edit is an opportunity to orphan live control-plane data. Routing them through one
builder makes that a single change, and makes "several cheap cells sharing one Redis under
different prefixes" a viable deployment in the meantime.

The tests below exist in a specific order of importance:

1. **Byte-for-byte parity.** A builder that renames a key silently orphans whatever is
   already in Redis under the old name — live device config, claims, dead letters. The
   explicit literal table is the guard, and it is deliberately written out by hand rather
   than generated from the builders, because a table generated from the code under test
   would agree with a rename.
2. **Uniform prefixing**, which is the property the refactor exists to buy.
3. **No inline key f-strings left behind**, so the seam does not erode.
"""

import ast
import re
from pathlib import Path

import pytest

from device_mcp_gateway.shared import keys as keys_mod
from device_mcp_gateway.shared.keys import KeyBuilder, device_resource_uri

_SRC = Path(__file__).resolve().parent.parent / "device_mcp_gateway"

K = KeyBuilder()

# The exact strings in use before this refactor, transcribed from the call sites.
# (builder result, expected literal)
_PARITY = [
    (K.devices_set, "devices:all"),
    (K.device_config("dev1"), "device:dev1:config"),
    (K.device_manifest("dev1"), "device:dev1:manifest"),
    (K.device_tools_change("dev1"), "device:dev1:tools_change"),
    (K.assignments_stream, "device:assignments"),
    (K.unassign_stream, "device:unassignments"),
    (K.device_calls("dev1"), "device:dev1:calls"),
    (K.device_calls_dead("dev1"), "device:dev1:calls:dead"),
    (K.session("s1"), "session:s1"),
    (K.session_results("s1"), "session:s1:results"),
    (K.reply("r1"), "reply:r1"),
    (K.fleet_handle("flt_1"), "fleet_handle:flt_1"),
    (K.fleet_handles_of("key:a"), "fleet_handles:owner:key:a"),
    (K.workers_active, "workers:active"),
    (K.worker_heartbeat("w1"), "worker:w1:heartbeat"),
    (K.worker_devices("w1"), "worker:w1:devices"),
    (K.claim("dev1"), "claim:dev1"),
    (K.rebalance_cooldown("dev1"), "rebalance:cooldown:dev1"),
    (K.health_lock("dev1"), "health_lock:dev1"),
    (K.reconciler_leader, "reconciler:leader"),
    (K.gauge_leader, "gateway:gauge-leader"),
    (K.exec_marker("r1"), "exec:r1"),
    (K.result_marker("r1"), "result:r1"),
    (K.ratelimit("messages:1.2.3.4"), "rl:messages:1.2.3.4"),
    # New in ADR-0013 §11a — no previous literal to be byte-compatible with, but pinned
    # here for the same reason as the rest: renaming it would let a spent single-use grant
    # be spent again, and nothing else would notice.
    # New in ADR-0023 §3. Same reason again, with a different consequence: renaming the
    # session marker makes every request look like a fresh activation, so a worked incident
    # pages once per call; renaming the window key silently zeroes the reactivation history
    # that the review flag is computed from. Both fail loud-in-the-wrong-way rather than
    # quietly, which is why they belong in this table.
    (K.break_glass_session("key:alice"), "breakglass:key:alice:session"),
    (K.break_glass_window("key:alice"), "breakglass:key:alice:window"),
    # New in ADR-0022. Same reason as the pair above: renaming either silently orphans a
    # pending proposal a human is mid-review of, or a live grant an agent is about to
    # redeem — both fail by becoming invisible rather than loudly, which is exactly what
    # this table exists to catch before a rename ships.
    (K.write_planned_proposal("p1"), "write_planned:proposal:p1"),
    (K.write_planned_grant("d1"), "write_planned:grant:d1"),
]


@pytest.mark.parametrize("built,expected", _PARITY, ids=[e for _, e in _PARITY])
def test_every_builder_matches_its_previous_literal_byte_for_byte(built, expected):
    """A rename here silently orphans live data in a running deployment."""
    assert built == expected


def test_parity_table_covers_every_key_building_member():
    """If someone adds a builder without adding a parity row, this fails — otherwise the
    table above slowly stops meaning anything."""
    members = {name for name in dir(KeyBuilder) if not name.startswith("_") and name not in {"prefix"}}
    # Consumer-group names are not keys; see the dedicated test below.
    members -= {"worker_group", "device_calls_group"}
    covered = {
        "devices_set",
        "device_config",
        "device_manifest",
        "device_tools_change",
        "assignments_stream",
        "unassign_stream",
        "device_calls",
        "device_calls_dead",
        "session",
        "session_results",
        "reply",
        "fleet_handle",
        "fleet_handles_of",
        "workers_active",
        "worker_heartbeat",
        "worker_devices",
        "claim",
        "rebalance_cooldown",
        "health_lock",
        "reconciler_leader",
        "gauge_leader",
        "exec_marker",
        "result_marker",
        "ratelimit",
        "break_glass_session",
        "break_glass_window",
        "write_planned_proposal",
        "write_planned_grant",
        "support_request",
        "support_grant",
        "support_pending_index",
        "support_active_grants_index",
        "support_standing_consent",
        "support_self_issue_window",
        "tenant_notifications",
        "enrolment_invitation",
        "enrolment_invitation_index",
        "enrolment",
        "enrolment_index",
        "enrolment_credential",
    }
    assert members == covered, f"parity table out of sync with KeyBuilder: {members ^ covered}"


# --- the property the refactor exists to buy ---------------------------------


def test_prefix_is_applied_uniformly():
    """The one-change property: a tenant/cell prefix reaches every key, with no exceptions
    to audit. This is what makes a future migration one edit instead of ~70."""
    t = KeyBuilder(prefix="t1")
    for name, value in _key_samples(t):
        assert value.startswith("t1:"), f"{name} is not prefixed: {value}"


def test_prefix_only_prepends_and_never_reshapes_the_key():
    """Prefixing must be a pure prepend, so a prefixed deployment's keys stay diffable
    against an unprefixed one."""
    plain, pref = KeyBuilder(), KeyBuilder(prefix="t1")
    for (name, p), (_, q) in zip(_key_samples(plain), _key_samples(pref)):
        assert q == f"t1:{p}", f"{name}: {q!r} is not 't1:' + {p!r}"


def test_empty_prefix_is_the_default_and_adds_nothing():
    assert KeyBuilder().device_config("d") == KeyBuilder(prefix="").device_config("d") == "device:d:config"


def test_consumer_group_names_are_not_prefixed():
    """Consumer groups are scoped to their stream, not the keyspace. Prefixing them would
    be meaningless at best, and at worst would silently create a second group on the same
    stream — every message delivered twice."""
    plain, pref = KeyBuilder(), KeyBuilder(prefix="t1")
    assert plain.worker_group == pref.worker_group == "workers"
    assert plain.device_calls_group("dev1") == pref.device_calls_group("dev1") == "workers-dev1"


def test_device_resource_uri_is_client_facing_and_unprefixed():
    """`device://` URIs are handed to MCP clients and may be cached by them. They are not
    Redis keys and must not pick up a keyspace prefix."""
    assert device_resource_uri("dev1") == "device://dev1"
    assert device_resource_uri("dev1", "/items") == "device://dev1/items"
    assert not hasattr(KeyBuilder(prefix="t1"), "device_resource_uri")


def _key_samples(kb: KeyBuilder) -> list[tuple[str, str]]:
    """Every key-building member exercised once, as (name, value)."""
    return [
        ("devices_set", kb.devices_set),
        ("device_config", kb.device_config("dev1")),
        ("device_manifest", kb.device_manifest("dev1")),
        ("device_tools_change", kb.device_tools_change("dev1")),
        ("assignments_stream", kb.assignments_stream),
        ("unassign_stream", kb.unassign_stream),
        ("device_calls", kb.device_calls("dev1")),
        ("device_calls_dead", kb.device_calls_dead("dev1")),
        ("session", kb.session("s1")),
        ("session_results", kb.session_results("s1")),
        ("reply", kb.reply("r1")),
        ("fleet_handle", kb.fleet_handle("flt_1")),
        ("fleet_handles_of", kb.fleet_handles_of("key:a")),
        ("workers_active", kb.workers_active),
        ("worker_heartbeat", kb.worker_heartbeat("w1")),
        ("worker_devices", kb.worker_devices("w1")),
        ("claim", kb.claim("dev1")),
        ("rebalance_cooldown", kb.rebalance_cooldown("dev1")),
        ("health_lock", kb.health_lock("dev1")),
        ("reconciler_leader", kb.reconciler_leader),
        ("gauge_leader", kb.gauge_leader),
        ("exec_marker", kb.exec_marker("r1")),
        ("result_marker", kb.result_marker("r1")),
        ("ratelimit", kb.ratelimit("messages:1.2.3.4")),
        ("write_planned_proposal", kb.write_planned_proposal("p1")),
        ("write_planned_grant", kb.write_planned_grant("d1")),
        ("support_request", kb.support_request("r1")),
        ("support_grant", kb.support_grant("g1")),
        ("support_pending_index", kb.support_pending_index),
        ("support_active_grants_index", kb.support_active_grants_index),
        ("support_standing_consent", kb.support_standing_consent),
        ("support_self_issue_window", kb.support_self_issue_window("op1")),
        ("tenant_notifications", kb.tenant_notifications),
        ("enrolment_invitation", kb.enrolment_invitation("h1")),
        ("enrolment_invitation_index", kb.enrolment_invitation_index),
        ("enrolment", kb.enrolment("e1")),
        ("enrolment_index", kb.enrolment_index),
        ("enrolment_credential", kb.enrolment_credential("h1")),
    ]


# --- the seam must not erode -------------------------------------------------

# Prefixes that identify a Redis key literal. `fleet:` is deliberately absent: the only
# bare `fleet:` f-string in the tree is an SseTransport object id (api/fleet.py), not a key.
_KEY_PREFIXES = (
    "devices:all",
    "device:",
    "session:",
    "fleet:",
    "workers:active",
    "workers-",
    "worker:",
    "claim:",
    "rebalance:cooldown:",
    "health_lock:",
    "exec:",
    "result:",
    "rl:",
    "reconciler:leader",
    "gateway:gauge-leader",
    "write_planned:",
    "support:",
    "tenant:",
    # ADR-0024 §10. Added with the keys themselves: this list is what makes the seam test
    # able to SEE a new inline f-string, so a namespace whose prefix is missing here is one
    # the guard silently ignores — the guard passing would mean nothing for it.
    "enrolment:",
)

# (file, exact string) pairs that look like keys but are not. Each needs a reason.
_ALLOWED = {
    # An in-process SseTransport label, not a Redis key.
    ("api/fleet.py", "fleet:"),
    # RBAC scope constants (ADR-0017), not Redis keys — they coincidentally share the
    # `support:` namespace with the support-grant keys the same ADR adds. Two of them since
    # §7a split asking from deciding: `support:request` is the provider's authority,
    # `support:administer` the tenant's.
    ("rbac.py", "support:administer"),
    ("rbac.py", "support:request"),
}


def _looks_like_key(text: str) -> bool:
    return any(text.startswith(p) for p in _KEY_PREFIXES)


def test_no_inline_redis_key_strings_outside_the_keys_module():
    """Keys are built in one place. A new inline f-string re-fragments the namespace and
    quietly reintroduces the ~70-edit migration this refactor removed."""
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == "shared/keys.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno, text in _string_literals(tree):
            if not _looks_like_key(text):
                continue
            if any(rel.endswith(f) and text.startswith(s) for f, s in _ALLOWED):
                continue
            offenders.append(f"{rel}:{lineno}: {text!r}")
    assert not offenders, "build these via shared.keys instead:\n  " + "\n  ".join(offenders)


def _string_literals(tree) -> list[tuple[int, str]]:
    """(lineno, leading literal text) for each plain string and f-string in `tree`.

    An f-string's own leading Constant is a child of the JoinedStr, so a naive ast.walk
    reports it twice. Collect the JoinedStr children and skip them, or every f-string key
    gets double-counted — which would have made the guard-the-guard test below pass for
    the wrong reason.
    """
    inner = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in inner:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            first = node.values[0] if node.values else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                out.append((node.lineno, first.value))
    return out


def test_the_erosion_guard_actually_detects_an_inline_key():
    """Guard the guard: a scanner that silently matches nothing would pass forever — and
    one that double-counts would look like it was working."""
    tree = ast.parse('x = f"device:{h}:config"\ny = "workers:active"\nz = f"unrelated:{h}"')
    found = [t for _, t in _string_literals(tree) if _looks_like_key(t)]
    assert found == ["device:", "workers:active"], found


def test_default_builder_instance_is_unprefixed():
    """Call sites use the module-level default; it must behave exactly as before."""
    assert keys_mod.KEYS.device_config("dev1") == "device:dev1:config"
    assert keys_mod.KEYS.prefix == ""


def test_hostname_is_not_escaped_or_normalised():
    """Hostnames are already validated at the API boundary (_HOSTNAME_RE). The builder must
    not transform them, or keys written before this refactor become unreachable."""
    for h in ("dev-1", "dev.example.com", "DEV1"):
        assert KeyBuilder().device_config(h) == f"device:{h}:config"


def test_no_module_level_key_constants_remain_outside_keys():
    """The old `_DEVICES_SET = "devices:all"` style constants must be gone, not shadowing
    the builder with a second source of truth."""
    pattern = re.compile(r'^_[A-Z_]+\s*(?::\s*str\s*)?=\s*["\']([^"\']+)["\']', re.M)
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == "shared/keys.py":
            continue
        for m in pattern.finditer(path.read_text()):
            if _looks_like_key(m.group(1)):
                offenders.append(f"{rel}: {m.group(0).strip()}")
    assert not offenders, "move these into shared.keys:\n  " + "\n  ".join(offenders)
