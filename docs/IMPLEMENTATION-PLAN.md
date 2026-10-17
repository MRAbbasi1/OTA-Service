# Implementation Plan

## Operational evidence follow-up — 2026-10-16

The repository follow-up closes pre-handler capture, authenticated last-seen rollback on preparation failure, missing recording diagnostics, audit transaction gaps, resource correlation and freshness gaps. Scope, verification and rollout are recorded in [OPERATIONAL-EVIDENCE.md](OPERATIONAL-EVIDENCE.md) and QUALITY.md. This status is not a production-deployment claim.

The next refinement separates explicit recording diagnostics from ordinary reads and dependency checks, removes redundant diagnostic database sessions from polling, and adds shared foreground/viewport read eligibility and cancellation in the paired dashboard. No migration or additional runtime service is required. Deploy the reviewed API before the generated-schema consumer; completion evidence belongs in QUALITY.md.

This document records deployed capabilities, verified gaps, and the ordered path to the target product. Product owners, developers, and reviewers read it before choosing work. It governs sequencing and definition of done; it does not authorize implementation by itself.

## Scope

- In scope: current state, target gaps, phases, dependencies, and acceptance evidence.
- Out of scope: detailed code design and unapproved feature requests.

## Deployed current state

The repository contains a deployed FastAPI modular monolith backed by PostgreSQL and private MinIO. It implements device authentication, signed-manifest storage and delivery, release lifecycle, deterministic policies, administrator sessions, capability authorization, audit events, health probes, CI image publication, and production deployment. Production host facts not represented in tracked configuration require host-side verification.

## Repository implementation status

The coordinated implementation through Milestone 4 is complete in the repository. Device Type, Device, token, firmware, policy, and administrator workflows follow `RESOURCE-RULES.md`; collection endpoints provide bounded search, filters, sorting, and pagination; authorization uses action-specific capabilities; runtime OpenAPI is exported only to an ignored local dashboard input while generated client types remain tracked; and intercepted plus controlled-real-backend browser workflows cover the critical administrative path. Historical binary-only `UPLOADED` rows remain representable, while every new release upload requires a signed manifest.

The original production firmware-upload `500` cannot be attributed from repository evidence because the production request identifier and server/storage logs were not supplied. The implementation now maps object-store failures to `503 storage_unavailable`, maps unexpected failures to opaque `internal_error`, preserves the request identifier, and has passing controlled MinIO coverage. A production recurrence requires correlation against host logs rather than inference.

## Execution policy

- Milestones run in order. A later milestone does not begin while an earlier definition of done is incomplete.
- Every defect begins with a reproducible failing test or documented production evidence.
- Backend contracts are implemented and exported before dependent dashboard behavior.
- Each milestone updates both repositories' tests and tracked documentation where the shared contract changes; local release notes remain outside version control.
- Passing tests without the acceptance evidence required by `QUALITY.md` do not complete a milestone.

## Completed decision gate — Product and safety rules

Scope:

- approve the operation matrix in `RESOURCE-RULES.md`;
- approve soft-delete, retire, deactivate, archive, and exceptional purge behavior;
- make the production upload requirement for a signed manifest explicit;
- approve editable Device and Device Type fields;
- approve action-level capability names;
- approve collection sorting vocabulary;
- approve the OpenAPI and controlled-backend test mechanism.

Status: complete. `RESOURCE-RULES.md` records the accepted values, including policy archival and the super-administrator-only exceptional device purge. Retained policy or OTA history blocks purge; device-owned token credentials cascade with the device.

## Milestone 1 — Firmware Upload Reliability and Human-Friendly References

This is the first implementation milestone shared with the dashboard repository.

Implementation status as of 2026-10-12: complete in the repository and controlled local environment. The signed manifest is mandatory, Network MAC is absent from standard registration, human-readable lookup data is returned, errors are correlated safely, and database, MinIO, OpenAPI, dashboard, and controlled-backend tests provide release evidence.

Backend scope:

1. Reproduce and diagnose the production firmware-upload `500` using request correlation, API logs, MinIO reachability, bucket permissions, certificate trust, object write/read-back, and transaction behavior.
2. Require the signed manifest in the production upload contract.
3. Preserve binary, manifest, URL, hash, size, and signature cross-checks.
4. Provide the lookup response data required to label Device Types and Devices without exposing IDs as human input.
5. Remove Network MAC from the standard enrollment contract or formally retain it only in an approved advanced inventory operation.
6. Return and log safe request correlation for storage and unexpected failures.
7. Add stable lookup, filter, sort, and pagination parameters required by the affected dashboard screens.

Required tests:

- regression test reproducing the upload failure where its cause is determinable;
- multipart validation for missing and invalid manifests;
- MinIO write, read-back, cleanup, certificate, and unavailable-service integration cases;
- authorization and audit assertions;
- lookup label and invalid-reference tests;
- runtime OpenAPI generation and dashboard compatibility.

Definition of done and evidence:

- incomplete firmware uploads cannot enter the target workflow;
- no affected dashboard form requires a typed database ID;
- the unverifiable historical production `500` is documented as requiring production log correlation if it recurs;
- Backend, MinIO integration, OpenAPI, dashboard component, Playwright, and controlled-backend tests pass;
- no secret or internal storage detail is returned to the client.

## Completed Milestone 2 — Lifecycle-Safe Resource Management

Scope:

- implement approved Device Type edit, activation, and dependency-safe removal;
- implement approved Device edit, identity-change safeguards, retirement, and exceptional purge rules;
- implement editable pre-publication firmware metadata and immutable published releases;
- finalize Policy edit, activation, deletion or archival behavior;
- finalize Administrator edit, role, activation, and removal behavior;
- apply action-level authorization and audit events;
- add required Alembic migrations and rollback notes.

Status: complete in the repository. Every implemented operation has validation, authorization denial, conflict behavior, audit behavior, retention semantics, API coverage, and a paired dashboard workflow. Policy removal archives; device purge is exceptional and dependency-safe.

## Completed Milestone 3 — Operational Collections and Dashboard Contract

Scope:

- provide consistent server-side search, filtering, sorting, pagination, and typed envelopes;
- eliminate untyped response envelopes from the dashboard contract;
- make OpenAPI drift verification non-skipping in CI;
- document query limits and invalid-sort behavior.

Status: complete in the repository. Operational lists expose approved controls, typed page envelopes are represented in runtime OpenAPI, and the dashboard drift check fails when the tracked export is absent or stale.

## Completed Milestone 4 — Test and Security Audit

Scope:

- audit domain invariants, route authorization, audit-event coverage, database constraints, lifecycle transitions, storage cleanup, error leakage, rate limiting, and OTA contract behavior;
- map every approved use case to positive, negative, authorization, conflict, and infrastructure-failure evidence;
- remove tests that only repeat implementation without asserting product behavior.

Status: complete for repository and controlled-local evidence. The release gate includes static analysis, migration upgrade/downgrade, the full database-backed suite, real MinIO integration, a deployment-time MinIO write/read/delete canary, generated OpenAPI compatibility, frontend unit/component tests, intercepted Playwright, and controlled-real-backend Playwright. Host and physical-device acceptance remain Milestone 5 work.

## Audit and bounded monitoring refinement — 2026-10-15

Repository implementation adds typed audit pages, UTC time and UUID correlation
filters, safe detail read/write boundaries, device before/after snapshots, and
a whole-trail 24-hour summary. Distinct super-admin-only status and operational
event endpoints expose dependency state and bounded process-local evidence.
There is no new dependency, persistent table, migration, or device OTA change.
The paired dashboard must ship after this backend contract is available;
rollback the dashboard first if reverting these endpoints. Existing audit data
remains stored and the public/private health-probe split is unchanged.

Verification is recorded in `QUALITY.md` and the paired dashboard's
`10-verification-and-status.md`; local evidence is not production acceptance.

## Device communication redesign — 2026-10-16

User-authorized scope replaces the mixed Audit experience with durable device exchanges, independent administrative audit and service health. Migration 0009, reviewed request/response and decision capture, transfer completion semantics, filters/detail/CSV/JSON exports, history-backed device safeguards and automatic foreground UI updates are implemented. The end-to-end behavior and non-destructive retention default are in `DEVICE-COMMUNICATIONS.md`; verification belongs in `QUALITY.md`. Backend-first rollout is required; this repository work does not deploy production.

## Remaining Milestone 5 — Production Acceptance

Scope:

- run cross-repository workflows against a controlled real backend;
- verify public same-origin session and CSRF behavior;
- execute upload, publication, manifest, and exact firmware delivery;
- verify proxy routing, TLS, no redirect, no chunked transfer, and exact content length;
- rehearse backup restore and deployment rollback;
- complete one representative ESP32-S3 signed OTA update.

Definition of done: evidence is recorded for all checks, owners acknowledge residual risks, and rollback and restore are proven rather than assumed.

Next concrete steps:

1. Release owner creates coordinated release-candidate commits and tags in both repositories from the verified working trees.
2. Operations owner deploys the candidate to a production-equivalent staging host and records the migration, health, TLS, proxy, storage, and rollback evidence.
3. Data owner restores a current backup into an isolated database and verifies application readiness and representative records.
4. Firmware owner performs one signed update on a representative ESP32-S3 and verifies the exact manifest, binary length, hash, signature, and installed version.
5. Release owner promotes the same immutable artifacts to production only after all four owners accept the evidence.

## Open Questions

- Production owner: confirm whether the deployed MinIO image digest matches the reviewed production Compose file.
- Operations owner: provide host-side deployment, TLS/proxy, backup-restore, and rollback evidence.
- Firmware owner: provide physical ESP32-S3 signed-update acceptance evidence.
