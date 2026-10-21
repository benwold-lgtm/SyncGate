# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2026 Ben Wold. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0. See LICENSE in the project root for details.
"""F15 — Principal/scopes RBAC seam.

Authentication resolves to a Principal (subject + scopes + auth method); routes
authorize on individual scopes. Static API keys are the implementation, but the
seam is shaped so a later JWT/OIDC swap touches only authenticate()/Authenticator.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import device_mcp_gateway.main as gw_main
from device_mcp_gateway.rbac import (
    ALL_SCOPES,
    ANONYMOUS,
    ROLE_SCOPES,
    SCOPE_DEVICES_READ,
    SCOPE_DEVICES_WRITE,
    SCOPE_DEVICES_WRITE_PLANNED,
    Authenticator,
    Principal,
    authenticate_request,
    build_authenticator,
    require_scope,
    scopes_for_role,
)
from fastapi.testclient import TestClient

client = TestClient(gw_main.app)

ENV_KEYS = ("MCP_GATEWAY_API_KEY", "MCP_ADMIN_KEY", "MCP_VIEWER_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _principal(role):
    return Principal(subject=f"key:{role}", scopes=ROLE_SCOPES[role], auth_method="api_key")


def _bearer(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


# --- Roles & scopes ----------------------------------------------------------


def test_role_scope_bundles():
    assert scopes_for_role("admin") == ALL_SCOPES
    assert SCOPE_DEVICES_WRITE not in scopes_for_role("viewer")
    assert SCOPE_DEVICES_READ in scopes_for_role("viewer")


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        scopes_for_role("superuser")


def test_devices_write_planned_is_never_held_as_a_standing_scope():
    """ADR-0022: this scope is minted per-plan at Review, scoped to one caller and one exact
    digest, and checked via the write_planned grant store — never via `require_scope`. If it
    ever appeared in `ALL_SCOPES` (which is literally `admin`'s bundle, not a registry of
    known scope strings — see `ROLE_SCOPES["admin"] = ALL_SCOPES`) or in any other role's
    bundle, every principal holding that role would carry it permanently, which is the exact
    standing-access regression the ADR exists to prevent."""
    assert SCOPE_DEVICES_WRITE_PLANNED not in ALL_SCOPES
    for role, scopes in ROLE_SCOPES.items():
        assert SCOPE_DEVICES_WRITE_PLANNED not in scopes, f"role {role!r} must never hold this scope"
    assert SCOPE_DEVICES_WRITE_PLANNED not in ANONYMOUS.scopes


def test_principal_has():
    p = _principal("viewer")
    assert p.has(SCOPE_DEVICES_READ)
    assert not p.has(SCOPE_DEVICES_WRITE)


# --- build_authenticator -----------------------------------------------------


def test_legacy_single_key_is_admin(clean_env):
    auth = build_authenticator({"gateway": {"api_key": "legacy-tok"}})
    assert auth.enabled
    p = auth.authenticate(_bearer("legacy-tok"))
    assert p.scopes == ALL_SCOPES
    assert p.subject == "key:legacy"


def test_env_admin_and_viewer_keys(clean_env):
    clean_env.setenv("MCP_ADMIN_KEY", "atok")
    clean_env.setenv("MCP_VIEWER_KEY", "vtok")
    auth = build_authenticator({})
    assert auth.authenticate(_bearer("atok")).scopes == ALL_SCOPES
    assert auth.authenticate(_bearer("vtok")).scopes == ROLE_SCOPES["viewer"]


def test_rbac_config_list(clean_env):
    auth = build_authenticator({"gateway": {"rbac": [{"name": "ops", "key": "vtok", "role": "viewer"}]}})
    p = auth.authenticate(_bearer("vtok"))
    assert p.subject == "key:ops"
    assert p.scopes == ROLE_SCOPES["viewer"]


def test_no_keys_disables_auth(clean_env):
    auth = build_authenticator({})
    assert not auth.enabled
    # Disabled → anonymous principal with full access (unchanged single-operator behaviour).
    assert auth.authenticate(None) is ANONYMOUS
    assert ANONYMOUS.scopes == ALL_SCOPES


# --- Authenticator.authenticate ---------------------------------------------


def test_enabled_rejects_missing_and_wrong_token():
    auth = Authenticator({"good": _principal("admin")}, enabled=True)
    with pytest.raises(HTTPException) as e1:
        auth.authenticate(None)
    assert e1.value.status_code == 401
    with pytest.raises(HTTPException) as e2:
        auth.authenticate(_bearer("bad"))
    assert e2.value.status_code == 401


# --- Dependencies (authenticate_request + require_scope) ----------------------


def _fake_request(authenticator):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(authenticator=authenticator)), state=SimpleNamespace()
    )


@pytest.mark.asyncio
async def test_authenticate_request_sets_principal_subject():
    auth = Authenticator(
        {"vtok": Principal(subject="key:ops", scopes=ROLE_SCOPES["viewer"], auth_method="api_key")}, enabled=True
    )
    req = _fake_request(auth)
    await authenticate_request(req, _bearer("vtok"))
    assert req.state.principal.subject == "key:ops"


@pytest.mark.asyncio
async def test_require_scope_allows_and_denies():
    dep = require_scope(SCOPE_DEVICES_WRITE)
    admin_req = SimpleNamespace(state=SimpleNamespace(principal=_principal("admin")))
    await dep(admin_req)  # admin has devices:write → no raise

    viewer_req = SimpleNamespace(state=SimpleNamespace(principal=_principal("viewer")))
    with pytest.raises(HTTPException) as e:
        await dep(viewer_req)
    assert e.value.status_code == 403


# --- End-to-end scope enforcement via the app --------------------------------


def _use(monkeypatch, role, token="tok"):
    monkeypatch.setattr(gw_main.app.state, "authenticator", Authenticator({token: _principal(role)}, enabled=True))
    return {"Authorization": f"Bearer {token}"}


def test_viewer_can_read_but_not_mutate(monkeypatch):
    h = _use(monkeypatch, "viewer")
    assert client.get("/v1/devices", headers=h).status_code == 200
    assert client.get("/v1/metrics/summary", headers=h).status_code == 200
    assert client.get("/v1/admin/overview", headers=h).status_code == 200  # devices:read
    # Mutations and tool calls require scopes the viewer lacks → 403.
    assert client.post("/v1/devices", headers=h, json={"hostname": "x", "base_url": "http://x"}).status_code == 403
    assert client.delete("/v1/devices/x", headers=h).status_code == 403
    assert client.post("/v1/devices/x/mcp", headers=h, json={}).status_code == 403
    assert client.post("/v1/fleets", headers=h, json={"devices": ["x"]}).status_code == 403


def test_admin_passes_authz_on_mutations(monkeypatch):
    h = _use(monkeypatch, "admin")
    assert client.get("/v1/devices", headers=h).status_code == 200
    # Admin clears authz; a bad body now yields a 400 (validation), not 403 (authz).
    assert client.post("/v1/devices", headers=h, json={"hostname": "x"}).status_code == 400


# --- whoami (/v1/auth/me) — identity + effective scopes for the UI -------------


def test_whoami_returns_subject_and_scopes(monkeypatch):
    h = _use(monkeypatch, "viewer")
    resp = client.get("/v1/auth/me", headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["subject"] == "key:viewer"
    assert body["auth_method"] == "api_key"
    # Effective scopes are the viewer bundle, sorted.
    assert body["scopes"] == sorted(ROLE_SCOPES["viewer"])


def test_whoami_requires_authentication(monkeypatch):
    # Auth enabled but no/!invalid token → 401 (authenticate_request), never anonymous.
    _use(monkeypatch, "admin")
    assert client.get("/v1/auth/me").status_code == 401
    assert client.get("/v1/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
