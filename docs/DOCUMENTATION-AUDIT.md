# Documentation Audit

## Operational evidence follow-up — 2026-10-16

The reported original five production requests cannot be attributed without
deployment/edge/device evidence. Code-level gaps and controlled reproductions are
specified in OPERATIONAL-EVIDENCE.md, with gate results in QUALITY.md. Dashboard
checked-device/offer counts are not request counts; this distinction now has
separate persistent evidence and UI labels. No historical events are backfilled.

This document records the read-only inventory, conflicts, gaps, and numbered-document disposition completed before the documentation restructure. Maintainers and reviewers use it to trace why a document was retained or superseded. It governs documentation status only; it does not change runtime behavior.

## Scope

- In scope: tracked backend documentation, cross-repository contracts, and verified repository evidence.
- Out of scope: untracked production secrets, host state not captured in the repository, and implementation fixes.

## Inventory — OTA-Service

| File or group                                                     | Purpose                                                    | Last known update       | Status                                                                               |
| ----------------------------------------------------------------- | ---------------------------------------------------------- | ----------------------- | ------------------------------------------------------------------------------------ |
| `README.md`                                                       | Repository overview                                        | 2026-10-06 commit state | current overview; marketing copy exceeds formal standard                             |
| `docs/00–04`                                                      | overview, requirements, domain, architecture, OTA protocol | repository state        | mixed; detailed current references                                                   |
| `docs/05–10`                                                      | device, firmware, policy, security, API, storage           | repository state        | mixed; current implementation and target language are interleaved                    |
| `docs/11–14`                                                      | tests, observability, deployment, CI/CD                    | through 2026-10-05      | current operational detail                                                           |
| `docs/15–16`                                                      | implementation decisions and publication contract          | repository state        | current for implemented behavior                                                     |
| `docs/17–18`                                                      | verification record and runbook                            | through 2026-10-06      | current evidence with remaining acceptance work                                      |
| `docs/OTA-CONTRACT.md`                                            | device-side contract & wire protocol                       | 2026-10-17              | normative for device behavior; includes headers, schemas and examples |
| Alembic `0001–0006`                                               | schema history                                             | 2026-09-25 onward       | current                                                                              |
| `pyproject.toml`, Compose, Dockerfile, workflows, Nginx templates | build and deployment evidence                              | through 2026-10-06      | current repository evidence                                                          |

## Cross-repo contract

- Backend `09-api-specification.md` and runtime OpenAPI describe the administrative API consumed by the dashboard.
- Dashboard `03-api-integration.md` and generated schema describe backend responses and known drift.
- Backend `docs/OTA-CONTRACT.md` and `docs/DEVICE-INTEGRATION.md` are the source for device headers, signature, response framing, retry, and lockout behavior.
- Dashboard documents repeat some backend route and device facts; `../OTA-Service Dashboard/docs/API-INTEGRATION.md` now requires references instead of duplication.

## Conflicts and gaps

| Evidence                                                            | Finding                                                                                                                                                                                                                           |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docs/06-firmware-management.md` and `docs/09-api-specification.md` | Resolved on 2026-10-12: the current upload contract now requires the signed manifest.                                                                                                                                             |
| `app/api/v1/firmware.py` and `tests/test_firmware_api.py`           | Resolved on 2026-10-12: the endpoint and regression tests reject missing or empty manifests.                                                                                                                                      |
| `docs/15-implementation-decisions.md:274`                           | Network MAC is retained as optional inventory; target standard enrollment removes it from the normal workflow                                                                                                                     |
| `docs/01-requirements.md:493`                                       | Sorting is a stated dashboard requirement but current list APIs do not expose a consistent sort contract                                                                                                                          |
| Device auth discussion versus backend 403 matrix                    | Device contract warns that 403 represents a definitively identified forbidden device, while backend also uses 403 for unknown serial or MAC mismatch; this immediate-lockout boundary requires firmware/security review before change |
| Production configuration                                            | The repository proves intended Compose and Nginx behavior but not every live host value; live values remain externally verified evidence                                                                                          |

## Gaps filled by this restructure

- Repository-local development guidance and release notes remain outside version control.
- Product, technical, application-flow, implementation-plan, schema, OTA contract, resource-rule, and quality sources of truth.
- Explicit separation of current implementation, target product, and delivery plan.
- Open questions with owners rather than speculative prose.

## Numbered-document disposition

| File                                | Outcome | Reason                                   | What changed                                                                                 |
| ----------------------------------- | ------- | ---------------------------------------- | -------------------------------------------------------------------------------------------- |
| `00-project-overview.md`            | Keep    | useful detailed system context           | target product authority moved to `PRD.md`                                                   |
| `01-requirements.md`                | Keep    | requirement identifiers remain traceable | target authority moved to `PRD.md` and gaps to the plan                                      |
| `02-domain-model.md`                | Keep    | detailed current domain reference        | operation authority moved to `RESOURCE-RULES.md`                                             |
| `03-architecture.md`                | Keep    | detailed architecture reference          | stack authority moved to `TRD.md`                                                            |
| `04-ota-contract.md`                | Merge   | overlaps the device contract             | short authoritative server summary added as `OTA-CONTRACT.md`; original retained for history |
| `05-device-management.md`           | Keep    | detailed current device behavior         | target mutations governed by resource rules                                                  |
| `06-firmware-management.md`         | Edit    | upload contract changed on 2026-10-12    | manifest is documented as required and binary-only creation is historical only               |
| `07-update-policy.md`               | Keep    | current policy-resolution detail         | no runtime change                                                                            |
| `08-security.md`                    | Keep    | current security detail                  | quality and agent rules now reference it                                                     |
| `09-api-specification.md`           | Keep    | current prose API reference              | runtime OpenAPI remains authoritative                                                        |
| `10-storage.md`                     | Keep    | current persistence and MinIO detail     | no runtime change                                                                            |
| `11-testing-strategy.md`            | Merge   | readiness rules were incomplete          | enforceable standard added as `QUALITY.md`                                                   |
| `12-observability.md`               | Keep    | current logging and health behavior      | no runtime change                                                                            |
| `13-deployment.md`                  | Keep    | operational deployment detail            | host facts remain subject to live verification                                               |
| `14-ci-cd.md`                       | Keep    | current pipeline detail                  | no CI change                                                                                 |
| `15-implementation-decisions.md`    | Keep    | authoritative implemented decisions      | target differences live in PRD and plan                                                      |
| `16-update-path-and-publication.md` | Edit    | normative path and publication detail    | manifest ownership and required-upload contract aligned with runtime                         |
| `17-security-verification.md`       | Keep    | evidence record                          | remaining work remains explicit                                                              |
| `18-operations.md`                  | Keep    | operator runbook                         | no command changes                                                                           |

No numbered document was deleted or moved because each still contains current implementation or historical evidence. New sources of truth prevent them from silently defining target scope.

## Observability restructure — 2026-10-16

- The former mixed audit/health/event workflow is superseded by independent device communications, administrative audit, and service health. Current behavior and retention boundaries are specified in `DEVICE-COMMUNICATIONS.md`; historical verification entries remain dated evidence, not the current schema contract.
- Both repositories reference a singular `AGENT.md` that is absent in this checkout. The available `AGENTS.md`, strategic documents, device contract, and area-specific references were used; no missing instructions were invented.
- Device evidence is now persistent with migration `0009_device_communications`. It is not the existing bounded process event deque. No historical request backfill, guaranteed installation receipt, lossless database-outage journal, or automatic retention cleanup is claimed.
- Retention preserves evidence until an explicit archive/restore policy is approved. This avoids silently introducing destructive expiration; production storage capacity remains an operational responsibility.

## Open Questions

- Documentation owner: approve archival of the original `04-ota-contract.md` and `11-testing-strategy.md` after inbound links are migrated.
