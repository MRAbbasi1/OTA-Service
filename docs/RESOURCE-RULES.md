# Resource Rules

This document defines allowed administrative operations and data-safety rules. Product owners, API developers, dashboard developers, and reviewers use it when designing a mutation. It governs lifecycle, mutability, deletion, authorization, and audit requirements; field-level schemas remain in code and `BACKGROUND-SCHEMA.md`.

## Scope

- In scope: device types, devices, tokens, releases, policies, administrators, attempts, and audit events.
- Out of scope: HTTP presentation and visual layout.

## Operation matrix

| Resource         | Create                      | Editable fields                                                                              | Lifecycle operations                                     | Removal rule                                                                               |
| ---------------- | --------------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Device type      | Yes                         | name, description, hardware revision; code/platform only before references exist             | activate/deactivate                                      | hard delete only when no device, release, or policy references it                          |
| Device           | Yes                         | device type and identity only through audited safety checks; operational metadata separately | provision, activate, disable, retire, restore retired to disabled; OTA enable/disable | retire by default; exceptional purge requires dedicated authorization and dependency proof |
| Device token     | issue                       | none                                                                                         | rotate, revoke                                           | revocation only; plaintext is never recoverable                                            |
| Firmware release | binary plus signed manifest | name and release notes before publication                                                    | validate, draft, publish, deprecate, archive, restore archived to draft | archive; retained binary, version, manifest, hashes, and URL are immutable                  |
| Update policy    | Yes                         | name, description, bounds, priority, window before archival                                  | activate/deactivate; restore archived to inactive        | archive; archived definitions are inactive, retained, and immutable                         |
| Administrator    | Yes                         | role and approved profile fields                                                             | activate/deactivate, own-password change                 | deactivate; last active super administrator is protected                                   |
| Update attempt   | service-created             | none                                                                                         | none                                                     | retained as server-observed history                                                        |
| Audit event      | service-created             | none                                                                                         | none                                                     | append-only                                                                                |

## Device communication history

The service creates a durable receipt and advances it through response preparation and terminal transfer observations. There is no administrative edit/delete or automatic TTL. Authenticated communication history blocks device identity changes and exceptional purge; rejected claimed identities do not create trusted device references. See `DEVICE-COMMUNICATIONS.md`.

## Human-facing references

- API payloads may use numeric foreign keys, but dashboards never require an operator to type one.
- Device types are selected by name with code and platform context.
- Devices are selected by serial with type context.
- Releases are selected by version with device-type context.
- Stored IDs remain visible only where required for support or audit correlation.

## Safety rules

- Every mutation requires a named capability and an audit event.
- High-impact actions require confirmation containing the target and effect.
- Collection and detail responses expose server-evaluated eligibility for guarded identity and permanent-deletion actions. Clients hide ineligible actions; mutation endpoints repeat the checks to prevent stale-state races.
- Cascading hard deletes are prohibited for retained operational history. Device-owned credentials are deleted with an exceptional device purge.
- Device purge requires `devices:purge`, is restricted to super administrators, and is allowed only while the device is provisioned and has no update-attempt or policy history. Issued and revoked token rows are deleted atomically with the device.
- Policy removal sets `archived_at` and disables the policy; it never deletes the row. Restoration clears `archived_at` but leaves the policy inactive until a separate activation succeeds.
- A token is issued once, displayed once, hashed at rest, and changed only by rotation.
- Network MAC is not an OTA authentication field and is not part of the standard enrollment workflow.
- A signed manifest is required by the target upload workflow.

## Open Questions

None.
