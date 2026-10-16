# Triaging a dependency advisory

CI runs `pip-audit -r requirements.txt` as an **advisory** step (`continue-on-error: true`) —
a new upstream CVE should not block an unrelated PR. So a red step here is a prompt to look,
not a broken build. `bandit`, which scans our own code, *is* blocking.

This document is about how to answer "is it real?", and records the standing answers so the
next person does not redo the work. It is deliberately organised around **what this project
uses from each dependency**, because that is what dates slowly. The version table at the
bottom dates quickly and is only evidence.

## The method

A version match is not exposure. For each finding, establish three things:

1. **Which API is vulnerable**, from the advisory text — not the summary line. `pip-audit
   --desc --format json` gives you the full write-up; most advisories name the exact function.
2. **Whether we call it.** Grep for it. Beware near-misses: `request.url` in this codebase is
   almost always an **`httpx.Request`** (outbound, in `security/url_policy.py`), not a
   Starlette one. A grep that does not distinguish those will mislead you.
3. **Whether the precondition holds in our deployment.** Several advisories require a specific
   server or configuration. Where that is cheap to test, *test it* rather than reasoning about
   it — see the worked example below.

Record the answer here either way. "Not exposed, because X" is worth as much as a fix, and it
is the part that gets lost.

## What this project actually uses

The load-bearing facts, in rough order of how often they settle a question:

| Dependency | What we use it for | What we do **not** use |
|---|---|---|
| `cryptography` | Fernet (AES-128-CBC + HMAC-SHA256) for credential encryption; PyJWT's RS256/ES256/PS256 **signature verification** for OIDC | PKCS#7 / S-MIME / CMS, and the entire `x509.verification` API (`PolicyBuilder`, `Store`, the verifiers) |
| `starlette` | JSON request bodies, routing, and `scope`-level routing internals in `metrics.route_template` | `request.form()` — this is a JSON API, it parses no forms. `request.url` is used once, for `.path` in an access log |
| `python-multipart` | **Not installed in the image** since `0.3.7` — it arrived only as a dependency of `mcp`, which left the runtime set. Multipart in this codebase is **outbound** body *encoding* to devices (`core/adapter.py`), via httpx. If it reappears in `requirements.txt`, find out what pulled it in | `parse_form()`, which is what advisories usually target |
| `mcp` | **Nothing at runtime — not in the image** since `0.3.7`. It is a `dev` dependency, the reference client and server for interop tests. The gateway speaks MCP through its own router (`pods/pod_base.py`) and upstream client (`upstream/mcp_client.py`). An advisory against `mcp` concerns CI and developer machines, not a deployment | Everything, in production |
| `pydantic-settings` | Nothing directly — transitive via the OpenAPI validators | `BaseSettings`, `secrets_dir`, `NestedSecretsSettingsSource` |
| `redis` | The whole distributed control plane | — |

Two blind spots worth knowing:

- **`pip-audit` does not see the base image.** Outbound TLS (device certificates, mTLS) goes
  through CPython's `ssl` module against the **`python:3.12-slim` OpenSSL**, not the copy
  statically linked into `cryptography`'s wheel. An OpenSSL CVE in the TLS handshake path is a
  base-image rebuild, and no requirements scan will tell you about it.
- **`cryptography`'s bundled OpenSSL** is used only for `cryptography`'s own operations, which
  for us means Fernet and JWT signature verification. Advisories about its bundled OpenSSL
  have to be read against *that* list, not against TLS.

## Worked example — verify the precondition

`CVE-2026-54282` (starlette): a request path not beginning with `/` corrupts
`request.url.hostname`, so code making host-based security decisions can be misled. The
advisory states its own precondition: *"requires an ASGI server that forwards a request-target
lacking a leading `/` into `scope['path']`"*.

That is directly testable, and worth testing because the answer is binary:

```bash
printf 'GET @google.com HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' \
  | nc 127.0.0.1 8000
```

Uvicorn answers `400 Bad Request` — the request-target never reaches the ASGI scope, on both
0.48.x and 0.52.x. The precondition does not hold here.

Note also *why* the gateway was not exposed even if it had: client identity comes from
`request.client.host` walked back through `X-Forwarded-For` against `trusted_proxy_cidrs`
(`ratelimit.py`), which is exactly what the advisory recommends **instead of** `request.url`.
That is not luck — it came out of the third-party review's XFF finding.

## Standing triage — 2026-08-06

Against `requirements.txt` before the 0.2.0 refresh: 10 findings across 5 packages, **none
reachable**.

| Advisory | Package | Vulnerable path | Verdict |
|---|---|---|---|
| PYSEC-2026-3552 | cryptography | `pkcs7_decrypt_*` Bleichenbacher oracle | Not exposed — no PKCS#7 usage |
| PYSEC-2026-3553 | cryptography | `x509.verification` chain-build DoS | Not exposed — API never called |
| PYSEC-2026-3554 | cryptography | `x509.verification` name-constraint escape | Not exposed — API never called |
| GHSA-537c-gmf6-5ccf | cryptography | bundled OpenSSL (June 2026 advisory, 18 CVEs) | Not exposed — all in PKCS#7/CMS, OCSP, QUIC, CRMF/CMP, FFC-DH, AES-OCB/SIV or huge-input ASN.1; none in AES-CBC+HMAC or RSA/ECDSA verification |
| PYSEC-2026-3483 | mcp | deprecated WebSocket transport, no Origin check | Not exposed — transport not used |
| GHSA-4xgf-cpjx-pc3j | pydantic-settings | `secrets_dir` symlink escape | Not exposed — never imported directly |
| PYSEC-2026-3040 | python-multipart | `parse_form()` negative `Content-Length` | Not exposed — the advisory itself notes FastAPI/Starlette do not call it |
| PYSEC-2026-248 | starlette | `request.url` host confusion | Not exposed — see worked example above |
| PYSEC-2026-249 | starlette | form limits ignored for urlencoded | Not exposed — `request.form()` never called |

**Acted on anyway.** A plain `pip-compile --upgrade`, with no constraint changes, cleared 7 of
the 10. Free is a good price for a smaller attack surface on a release artifact, even when
nothing is reachable.

The remaining three need `cryptography>=49`, which the **deliberate** major cap in
`pyproject.toml` blocks. That cap exists so a major bump is a tested change rather than
something a clean install picks up; since all three are unreachable, there is no reason to
force it. Revisit when `cryptography` is bumped on purpose.

> **Lifted on purpose in 0.3.2 (2026-08-11)** — see the re-verification below. `-3553` and
> `-3554` are cleared; `-3552` still reports and still is not reachable.

## Re-verification — 2026-08-07 (the three `cryptography` findings)

CI still reports `PYSEC-2026-3552/3553/3554` against `cryptography==48.0.1`. **The verdict is
unchanged: none reachable.** Re-checked rather than re-read, because one input had moved.

What changed since the triage: the gateway gained mTLS `ca_bundle` trust configuration and is
now running against a real self-signed CA in the lab. Both `-3553` and `-3554` are certificate
**verification** bugs, so if trust decisions had migrated into `cryptography`, they would have
become live. They have not — `security/mtls.py` builds `ssl.create_default_context(cafile=…)`,
so chain building and name-constraint checking are **OpenSSL through the stdlib**.
`cryptography` is still reached only for Fernet (and PyJWT signature verification), and the
codebase contains no PKCS#7/S-MIME/CMS and no `x509.verification` usage at all.

> Grep note: searching for the verifier API on `Store` matches `SqliteDeviceStore`. That is the
> same class of near-miss as the `request.url` trap above — confirm the import, not the name.

**This was coupled to a roadmap item, and that item has now shipped.** Per-device TLS trust
was the one planned change that could have made `-3553`/`-3554` reachable, had it been built
on `x509.verification` instead of per-device `SSLContext`s.

**Built 2026-08-10 on `SSLContext`, as the constraint required.** `security.mtls.devices.<hostname>`
resolves to one `ssl.create_default_context(cafile=…)` per distinct profile, so chain building
and name-constraint checking are still OpenSSL through the stdlib and `cryptography` is still
reached only for Fernet and PyJWT signature verification. **The verdict on all three findings is
unchanged: none reachable**, and `cryptography>=49` stays an optional bump rather than becoming a
prerequisite. The codebase still contains no PKCS#7/S-MIME/CMS and no `x509.verification` usage.

If that API is ever adopted deliberately — for per-device trust or anything else — this coupling
comes back, and `>=49` becomes a hard prerequisite. The reasoning lives at the finding itself in
[testing-gaps.md](testing-gaps.md#tg-4--the-kubernetes-manifests-on-a-real-cluster--closed).

## Re-verification — 2026-08-11 (backup/restore, and lifting the cap)

Re-checked rather than re-read, for the same reason as last time: an input had moved. ADR-0011
backup/restore is **new `cryptography` code**, the first this project has added since the
findings were triaged, so the "none reachable" verdict could not simply be carried forward.

What it uses is `cryptography.hazmat.primitives.kdf.argon2.Argon2id` for passphrase derivation
and `Fernet`/`MultiFernet` for the archive envelope — a KDF and authenticated symmetric
encryption. Neither is PKCS#7 nor `x509.verification`. The codebase still contains no
PKCS#7/S-MIME/CMS and no `x509.verification` usage; the only textual match for the latter is the
comment in `security/mtls.py` recording why it is avoided. **Verdict on all three findings
unchanged: none reachable.**

**The cap was lifted anyway**, to `>=44.0.0,<50.0.0`, pinning `cryptography==49.0.0`. Not because
anything became reachable — because a release artifact is the right place to spend a refresh
(`releasing.md` §1.6), and `49.0.0` was by then two months old, older than the pin it replaced.
`-3553` and `-3554` no longer report. `-3552` (PKCS#7) still does and needs `50.0.0`, which was
11 days old at the time; by this document's own release-age rule it should season first, and
nothing about it is reachable. The next major (`50`) stays excluded so the
`test_critical_deps_exclude_the_next_major` guard keeps holding.

Resolved in a clean virtualenv per the procedure below. The whole bounded set re-resolved
unchanged — `mcp` 1.29.0, `fastapi` 0.137.2, `starlette` 1.3.1, `pydantic` 2.13.4, `redis` 8.1.0
— so the lockfile diff is a single line. Full suite: 1030 passed, 1 skipped (the `[otel]` extra).

> **`pip-compile` trap.** Compile to `requirements.txt` **in place**. Writing to a scratch output
> file gives it no existing pins to anchor to, so it re-resolves everything and quietly drags
> unrelated packages forward — that produced three spurious bumps here before being spotted.
> Separately, `pip-tools` 7.5.3 does not import under pip 26.x (`stdlib_pkgs` moved); the clean
> venv needs `pip==24.0` to match the working one.

### The floor matters too, not just the cap

This refresh surfaced the inverse bug. `pyproject.toml` declared `cryptography>=41.0.7` — a floor
predating ADR-0011 — while the backup code imports `Argon2id`, added in **44.0.0**. `pip install`
resolves against `pyproject.toml` and ignores the lockfile, so a clean install into an
environment already holding 43.x satisfied the spec and then raised `ImportError` on the first
portable export. Every existing guard passed: they check that the locked version satisfies the
range, and that critical packages exclude the next major. Nothing compared the declared floor
against what the code imports.

Fixed in 0.3.2, with `test_declared_floor_supports_the_apis_the_code_imports` asserting the floor
against a table of the APIs requiring it. **Add an entry to that table whenever new code adopts a
recently-added API of a bounded dependency** — the upper bound and the lower bound are two halves
of the same claim, and only one of them was ever being checked.

### Choosing the version, when the cap is lifted on purpose

Judge a candidate by **release age**, not by distance from latest — a just-released version is
an unpaid debt, and this project has already paid for that once (FastAPI 0.137 changed
`include_router` behaviour under a patch-looking bump). Measured 2026-08-07:

| Version | Released | Age | Clears |
|---|---|---|---|
| 48.0.1 (the pin at the time) | 2026-06-09 | 59 days | — |
| **49.0.0** ← adopted in 0.3.2 | 2026-06-12 | **56 days** | `-3553`, `-3554` |
| 50.0.0 (latest) | 2026-07-31 | 7 days | all three |

`49.0.0` is as seasoned as the pin it would replace, so adopting it carries no recency risk and
clears the two advisories on the surface per-device trust will touch. `50.0.0` adds only the
PKCS#7 fix — for code this project does not contain — and is a week old; it should season
first. Neither is urgent while nothing is reachable.

Whichever is chosen, the bump is a **tested change across the whole dependency set**, not a
single-line edit: `cryptography` is bounded alongside `mcp`, `fastapi`, `starlette`, `pydantic`
and `redis`, all of which have to remain mutually compatible. Resolve in a **clean**
virtualenv (see *When you do upgrade* below) and then make the working environment match the
resulting pins exactly — testing against a venv that has drifted from `requirements.txt` is how
a version-dependent defect gets missed, which is exactly what happened on 2026-08-07 when a
stale local venv (22 of 48 pins adrift) made a real FastAPI behaviour change look like a
CI-only anomaly:

```bash
pip install -e ".[dev]" && pip install -r requirements.txt   # -r last, so exact pins win
pip freeze | sort   # then diff against requirements.txt; normalise _ vs - in names
```

## Re-verification — 2026-08-13 (endpoint fingerprinting adds real `x509` usage)

Re-checked rather than re-read, for the third time and the same reason: an input moved. The
previous two entries both rest on the sentence *"the codebase contains no PKCS#7/S-MIME/CMS and
no `x509.verification` usage at all"* — and half of that premise stopped being true in 0.3.3.
[ADR-0015](adr/0015-endpoint-fingerprinting.md) parses a certificate off every live TLS probe,
so `cryptography.x509` is now reached on a hot path, with **attacker-influenced input**. Left
unchecked, the next reader would inherit a verdict whose stated reason no longer held.

What 0.3.3 actually added, in full — `security/fingerprint.py` only:

```
x509.load_der_x509_certificate(der)          # parse the peer certificate
cert.public_key().public_bytes(DER, SPKI)    # derive the pinned digest
cert.issuer.rfc4514_string() / not_valid_after_utc   # context fields
```

That is **parsing and field access, not verification**. `x509.verification` — the
chain-building API `-3553` and `-3554` live in — is still not called anywhere; the only match
in the tree is the comment in `security/mtls.py` explaining why it must not be. Trust decisions
remain OpenSSL through the stdlib, per-device `SSLContext` as before. `-3552` is the
`pkcs7_decrypt_*` Bleichenbacher oracle, and there is still no PKCS#7/S-MIME/CMS anywhere.

**Verdict unchanged: none reachable**, and `cryptography 50.0.0` stays an optional bump rather
than a prerequisite for 0.3.3.

Two things worth stating rather than leaving implicit, because "we parse hostile DER now" is
the kind of change that deserves more than a shrug:

- The DER we parse has **already been through OpenSSL's own parser and the handshake's
  verification** — it comes from `getpeercert(True)` on a connection that completed against the
  device's trust profile. It is not raw input from an unauthenticated peer, and its size is
  bounded by what the TLS stack accepted. That is what keeps the "huge-input ASN.1" items in
  the bundled-OpenSSL advisory (GHSA-537c-gmf6-5ccf) out of reach.
- `observe_tls` catches **every** exception and yields an empty observation, by design: a
  fingerprint that cannot be read must not break the request it rode in on. So a parser defect
  reached through this path degrades to "learned nothing this cycle" rather than to a failed
  probe — which `compare()` treats as absence of evidence, not evidence of change.

If certificate *verification* ever migrates into `cryptography`, the coupling in the entries
above comes straight back and `>=49` becomes a hard prerequisite.

## When you do upgrade

`starlette` (runtime) and `mcp` (`dev` only) are upper-bounded for reasons written into
`pyproject.toml`, and both have a guard test. After any refresh that moves them, confirm:

```bash
pytest tests/test_metrics.py::test_parametrised_route_uses_template_not_concrete_path
pytest tests/test_requirements.py    # the major caps, and that the gateway never imports `mcp`
```

`mcp` must stay out of `requirements.txt`. CI installs `.[dev]` and so always has it; the image
installs the lockfile and never does. A runtime `import mcp` would therefore pass every test and
ship a container that fails on import — `test_gateway_does_not_import_the_mcp_sdk` is the check
for exactly that.

Run the refresh in a **clean** virtualenv, not the working one — the failure mode these bounds
exist to catch is precisely what a fresh resolve does differently from an incremental install.
