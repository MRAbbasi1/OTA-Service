# Quality and Verification Standard

## Latest evidence — on-demand recording diagnostics, 2026-10-16

- `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/mypy app` and `git diff --check` passed (92 formatted files, 56 typed source files).
- Full isolated PostgreSQL/MinIO run with `RUN_MINIO_INTEGRATION=1 .venv/bin/pytest --cov=app --cov-report=term-missing -q`: **519 passed**, no skips, **95.44%** application coverage. Targeted communication/monitoring/evidence run: **38 passed**. The existing Starlette/AnyIO deprecation remains the only warning.
- New regressions prove ordinary list/summary/detail/export/status reads do not invoke recording diagnostics; actual unavailable-table reads still return no-store 503, and explicit diagnostics honestly returns unavailable. Permission and failed-write/recovery tests now cover the dedicated diagnostics endpoint.
- Runtime OpenAPI export and the paired generated client were updated and passed drift verification. Paired browser acceptance explicitly runs real diagnostics only after the button, resets its UI on route change, and retains the five failed-request workflow.
- `docker build -t ota-service:on-demand-diagnostics .` passed locally; no image was pushed or deployed.
- No dependency, migration, OTA bytes/status/headers/signature, Nginx routing, retention or credential change. Existing migration 0009 remains required. Roll out API before dashboard; roll back dashboard first and retain history.
- Both local changelogs were updated and remain ignored under existing policy. Controlled-local results do not establish production load, real-device timing or a zero-defect guarantee. In-flight browser cancellation cannot undo already-executed server work.

The paired dashboard's current gate counts and lifecycle boundaries are in its `docs/10-verification-and-status.md` and both operational specifications.

## Prior evidence — operational follow-up, 2026-10-16

Scope and acceptance: [OPERATIONAL-EVIDENCE.md](OPERATIONAL-EVIDENCE.md).
Both repositories were clean before this follow-up. Verification used disposable
loopback PostgreSQL 18 and MinIO, never production.

| Gate | Result |
| --- | --- |
| uv run ruff check . | Passed |
| uv run ruff format --check . | 92 files already formatted |
| uv run mypy app | Passed, 56 source files |
| RUN_MINIO_INTEGRATION=1 uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80 -q | 518 passed; 95.43% coverage |
| docker build -t ota-service:operational-audit . | Passed |
| Paired runtime OpenAPI/generated client and dashboard gates | Passed; see paired dashboard verification record |
| Database migration | No new schema migration; existing 0009 required. Disposable API database upgraded to head. |

New acceptance covers five failed checks, malformed identity input, route/method
rejections, unavailable history schema, recording-write failure/recovery,
authenticated last-seen on storage failure, resource/policy attribution,
cross-device revoke rejection, credential history, transactional audit rollback,
upload rollback/object cleanup, independent read/write budgets, and exception
diagnostic sanitization. Existing wire/streaming/security tests remain passing.

The only pytest warning is Starlette's upstream AnyIO BlockingPortal deprecation.
No required test was skipped. These results establish controlled repository
behavior, not production routing, real-device receipt or a zero-defect guarantee.
No production deployment, push, data deletion or populated-table downgrade ran.

This document defines evidence required before a change is declared complete or ready to push. Developers, reviewers, and release owners apply it to every change. It governs test selection, command evidence, contract checks, and readiness claims; it does not replace feature-specific acceptance criteria.

## Scope

- In scope: test levels, quality gates, evidence, and production acceptance boundaries.
- Out of scope: implementation details of individual tests.

## Change evidence

Every change identifies its requirement, acceptance criteria, negative cases, affected boundaries, and regression risk before implementation. Each fixed defect receives a test that fails for the previous behavior. Passing unrelated tests is not regression evidence.

## Required layers

| Change              | Minimum evidence                                                                                              |
| ------------------- | ------------------------------------------------------------------------------------------------------------- |
| Pure domain rule    | unit tests including boundaries and invalid inputs                                                            |
| API mutation        | domain/service tests, authenticated API test, authorization denial, validation/conflict test, audit assertion |
| Persistence         | migration upgrade, constraints, integration test, rollback impact                                             |
| Object storage      | failure cleanup, byte/hash verification, production-like MinIO integration                                    |
| OTA contract        | headers, status, exact JSON, signature payload, content length, no redirect, no chunked transfer              |
| Security            | unauthenticated, unauthorized, secret leakage, CSRF/session effects                                           |
| Cross-repo contract | runtime OpenAPI generation and dashboard compatibility check                                                  |
| Deployment template | full backend coverage suite, template hardening tests, and applicable Nginx runtime checks                    |

## Repository gates

```bash
uv sync --all-groups --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run alembic upgrade head
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80
RUN_MINIO_INTEGRATION=1 uv run pytest -m integration
docker build -t ota-service:local .
```

The database and MinIO-dependent commands require their documented local services. A skipped required service is reported as an incomplete gate, not a pass.

Changes to deployment templates require the full backend suite even when no
Python implementation changes. Host-Nginx tests distinguish the HTTP listener,
which only redirects to HTTPS, from the HTTPS OTA locations, which never
redirect and preserve exact firmware delivery. Backend and dashboard template
copies must remain identical. A prior passing run does not cover a later edit
to either template.

## Readiness report

The completion report lists commands, result, test count when available, skipped checks, changed contracts, migrations, operational impact, and residual risk. “All tests pass” without this evidence is insufficient.

## Audit/monitoring verification — 2026-10-15

- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy app`,
  and `git diff --check` passed.
- The production Dockerfile built successfully with frozen runtime dependencies
  under a task-local verification tag; no image was published or deployed.
- Full PostgreSQL-backed pytest with `RUN_MINIO_INTEGRATION=1` and coverage
  passed **489 tests**, no skips, **95.09%** application coverage (80% gate).
  The only warning is the existing Starlette/AnyIO deprecation.
- New evidence covers distinct 401/403 capabilities, no-store responses,
  bounded/sanitized event retention, empty/sample p95, dependency error
  redaction, legacy audit-detail sanitization, UUID correlation, timestamp
  validation, whole-trail totals beyond a page, and UTC buckets even with a
  non-UTC database session. Device API tests assert actual before/after values.
- Runtime OpenAPI was exported to the ignored dashboard input; generated
  client compatibility, static checks, and the production frontend build passed.
- Paired UI evidence: 173 component/unit tests, 28 intercepted browser tests
  (including seven responsive widths and axe), and both controlled-real-backend
  browser workflows passed. The latter exercised real PostgreSQL and MinIO,
  authenticated health, audit/request tracing, signed-manifest upload,
  enrollment, lifecycle, one-time token issuance, bulk OTA suspension, and purge.
- Services, database, buckets, and credentials were disposable and isolated
  from production. Repeated browser runs hit the unchanged production-like
  rate limiter; a fresh disposable API process was used for the final full run,
  rather than weakening rate-limit or cookie security checks.
- No database schema, device OTA wire contract, Nginx routing, or dependency
  change is required. Backend-first rollout and frontend-first rollback apply.

## Latest evidence — communication architecture, 2026-10-16

- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy app`, and `git diff --check` passed.
- `RUN_MINIO_INTEGRATION=1 uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=80` passed **503 tests**, no skips, **95.18%** coverage. The existing Starlette/AnyIO deprecation is the sole warning. PostgreSQL and MinIO were disposable local services, not production.
- `RUN_MINIO_INTEGRATION=1 uv run pytest -m integration` separately passed the real object-storage test (500 other tests deselected at that run, before two final non-storage regression tests were added).
- Migration `0009_device_communications` was upgraded, downgraded to `0008_device_token_cascade`, and upgraded again on the disposable database. Final migration supports 19-digit claimed serial values. Downgrade destroys the new table; this was not run on production data.
- The production Dockerfile built successfully as `ota-service:observability-verify`; no image was pushed or deployed.
- The paired runtime OpenAPI export and generated client passed `openapi:check`; frontend static checks, formatting, and Vite build passed. Dashboard evidence: **181 tests / 21 files**, **28 intercepted browser scenarios**, **2 private-prefix scenarios**, and **2 real-backend workflows**. Mobile/desktop communication inspection passed again after screenshot normalization.
- New regression evidence covers restart persistence, full manifest/binary delivery, unverified rejected identities, partial transfer, storage failure, recording failure isolation, long serials, time/order validation, permissions, insertion ceilings, export bounds, CSV formula protection, and history-protected purge. Real browser requests verify automatic arrivals and JSON/CSV exports against PostgreSQL, alongside the existing enrollment and lifecycle workflow.
- One clean shared export representation serves independently authorized admin/device resources; no service rule moved into the frontend. Normal OTA 404 no-offer no longer generates the generic request-failed event.
- Rollout requires the additive backend migration and API first, dashboard second. Rollback dashboard first and retain the evidence table. No automatic destructive retention, new runtime dependency, OTA wire change, load benchmark, physical-device test, or production deployment is claimed.
- Both local `CHANGELOG.md` files were updated but remain ignored by existing repository policy. The tracked feature contract is `docs/DEVICE-COMMUNICATIONS.md`.

## Production acceptance boundary

Repository tests do not prove proxy routing, TLS chain, firewall, backup restore, or physical-device behavior. Those checks require the acceptance procedures in `13-deployment.md`, `17-security-verification.md`, and `18-operations.md`.

## Open Questions

None.
