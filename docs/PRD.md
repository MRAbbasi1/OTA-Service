# Product Requirements Document

## Operational acceptance follow-up — 2026-10-16

An operator must distinguish no API receipt, rejected request, no update offer, transfer failure and unavailable recording; find related device/type/release/policy changes; and see foreground updates without reloading. Five failed requests must remain five receipts when recording is available. See [OPERATIONAL-EVIDENCE.md](OPERATIONAL-EVIDENCE.md); controlled evidence does not establish production receipt.

Diagnostic recording checks are independent, off by default and explicitly requested as timestamped snapshots. Routine browser reconciliation must stop outside mounted visible/online pages; hidden/offline reads are canceled without affecting mutations. Dependency availability must not imply a recording guarantee.

This document defines the target product for OTA-Service. Product owners, operators, developers, and testers read it before scoping a change. It governs required capabilities and measurable outcomes; architecture, file layout, and implementation detail are out of scope.

## Scope

- In scope: secure device enrollment, firmware release management, authenticated OTA delivery, deterministic update policy, administrative control, auditability, and operational reporting.
- Out of scope: device telemetry unrelated to OTA, remote command execution, customer billing, generic IoT fleet management, and device-side UI behavior.

## Product purpose

OTA-Service controls which authenticated device may receive which signed firmware release and provides operators with a safe administrative workflow for managing identities, releases, policies, administrators, and audit history.

## Personas

| Persona          | Required outcome                                                                                     |
| ---------------- | ---------------------------------------------------------------------------------------------------- |
| Device firmware  | Receives a compatible signed manifest and exact firmware bytes using the contract in `OTA-CONTRACT.md` |
| Fleet manager    | Manages device types, devices, token lifecycle, eligibility, and device status                       |
| Firmware manager | Uploads a binary with its signed manifest and manages release lifecycle                              |
| Viewer           | Inspects fleet, release, policy, and decision state without mutation access                          |
| Administrator    | Manages administrative accounts, roles, security, and audit access                                   |

## Required capabilities

- Provide durable device request/response history independently of admin audit and process health, with meaningful OTA outcomes, request-time decision evidence, automatic foreground updates and bounded CSV/JSON export (`DEVICE-COMMUNICATIONS.md`).

- Resolve human-facing selections by label, code, serial, or version; internal IDs remain transport identifiers.
- Manage each resource according to the operation matrix in `RESOURCE-RULES.md`.
- Require a signed manifest for the production firmware-upload workflow.
- Authenticate manifest and firmware requests with serial, raw eFuse MAC, and device token.
- Keep firmware artifacts private and serve them without redirects or chunked transfer encoding.
- Apply deterministic, explainable policy decisions and fail closed on ambiguity.
- Enforce capability-based authorization and audit every security-sensitive mutation.
- Provide server-side search, filtering, sorting, and pagination for operational collections.
- Preserve error correlation without exposing credentials, storage details, SQL, or tracebacks.

## Success criteria

- Every OTA contract test passes against the deployed proxy and one representative physical device completes a signed update.
- Every operator workflow has authorization, validation, success, empty, conflict, and infrastructure-failure acceptance coverage.
- No operator workflow requires an internal database ID to be typed manually.
- No published artifact or signed manifest can be modified in place.
- Every sensitive mutation produces an audit event attributed to the authenticated administrator.
- CI cannot pass when runtime OpenAPI and the reviewed dashboard contract differ.
- Backup and rollback procedures are verified on a non-production restore target.

## Non-goals for the current version

| Non-goal                              | Reason                                                                                                                   |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Microservices or message queues       | Current load and workflows do not require distributed coordination                                                       |
| Client-certificate OTA authentication | The device contract uses application headers and one-way TLS                                                             |
| Forced server-initiated updates       | The device polls; the current contract has no push channel                                                               |
| Installation-success claims           | The device sends no post-boot confirmation                                                                               |
| Arbitrary hard deletion               | Identity, release, and audit history must remain traceable                                                               |
| Generic custom-role builder           | Fixed role bundles satisfy the current organizational model; action-level capabilities remain the authorization boundary |

## Open Questions

- Production owner: approve whether an unverified-but-signed manifest may be uploaded when the backend public-key verifier is not configured. The target workflow requires a signed manifest, but production verification policy must be explicit.
