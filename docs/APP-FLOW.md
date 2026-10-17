# Application Flows

This document defines supported backend journeys and their externally observable behavior. API developers, dashboard developers, testers, and operators read it when implementing or verifying a workflow. It governs sequence, state transition, failure semantics, and recovery; UI layout and database column definitions are out of scope.

## Scope

- In scope: device manifest and download, administrator session, device enrollment, firmware publication, and policy evaluation.
- Out of scope: dashboard presentation and device firmware internals.

## Device manifest retrieval

1. The device sends `GET /api/v1/firmware/{device_type}/{platform}/manifest.json` with the three device headers.
2. The service authenticates serial, raw eFuse MAC, and token.
3. The service checks lifecycle, OTA flag, path identity, policy, and published releases.
4. `200` returns exact signed manifest bytes; `401` means invalid token; `403` means definitive forbidden identity/state; `404` means no offer; `429` means rate limited; `5xx` means transient failure.
5. A successful offer records a server-observed event. It does not prove installation.

Recovery follows `OTA-CONTRACT.md`: only transient failures enter device retry/backoff.

## Firmware download

1. The device requests the signed manifest URL with the same authentication headers.
2. The service re-authenticates and re-evaluates delivery eligibility.
3. The service validates stored object size before headers, emits exact `Content-Length`, and streams bytes without redirect.
4. Complete delivery records `DOWNLOAD_SERVED`; interruption is not installation success.

## Retained device communication

Both OTA routes commit a reviewed receipt before execution, then record response status, request-time decision evidence and safe payload fields. Binary responses additionally record emitted bytes and terminal transfer state. Recording failure is a service fault and never changes device response semantics. Receipts/prepared responses left by a stopped process remain unconfirmed. Authorized administrators read/export the dedicated communications API; admin audit and process health are separate. See `DEVICE-COMMUNICATIONS.md`.

## Administrator session

1. `POST /api/v1/admin/auth/login` validates credentials and rate limits by source address and email.
2. Success sets an HttpOnly session cookie, a readable CSRF cookie, and returns the CSRF header name.
3. `GET /api/v1/admin/auth/me` restores session and capabilities.
4. Unsafe methods require the matching CSRF header.
5. Password, role, and active-state changes increment session version and revoke prior sessions.
6. `401` clears client session state; `403` is an authorization denial and does not log the administrator out.

## Device registration

1. The operator selects a device type by human-readable label; the client sends its ID.
2. `POST /api/v1/admin/devices` validates the positive serial and raw eFuse MAC.
3. Duplicate serial or raw eFuse MAC returns `409`.
4. Success creates a provisioned device and returns its derived manifest URL.
5. Token issue is a separate audited action; plaintext is shown once.

## Device lifecycle

1. `provisioned` can move to `active`, `disabled`, or `retired`.
2. `active` can move to `disabled` or `retired`; `disabled` can move to `active`
   or `retired`.
3. Retirement disables OTA and retains the serial, identity, credentials,
   policy references, and OTA history.
4. `retired` can move only to `disabled`. Reactivation and OTA enablement are
   separate audited actions.
5. A direct `retired` to `active` transition returns `409 invalid_device_transition`.

## Firmware publication

Target behavior requires device type selection, version, binary, and signed manifest in one upload. The service derives hashes, URL, and object keys, cross-checks signed fields, stores exact bytes, and creates a validated release. Publication rechecks artifact integrity, manifest presence, lifecycle, and configured verification policy before moving to `PUBLISHED`. Published content is immutable; correction requires a new version.

The create endpoint rejects a missing or empty manifest. Historical `UPLOADED`
rows remain a lifecycle compatibility state but are not created by the current
upload workflow.

An object-store connectivity, permission, or TLS failure is normalized to a
safe `503 storage_unavailable` response with request correlation. Writes already
completed before the failure are removed on a best-effort basis and database
metadata is rolled back.

## Policy evaluation

1. Scope precedence is device, then device type, then global.
2. Within a scope, highest priority wins.
3. Equal-priority overlapping policies are rejected; persisted ambiguity fails closed.
4. Inactive and out-of-window policies are ignored.
5. The same decision service serves device requests and administrator explanations.

## State machines

```text
Device: PROVISIONED -> ACTIVE -> DISABLED | RETIRED
Release: UPLOADED -> VALIDATED -> DRAFT -> PUBLISHED -> DEPRECATED -> ARCHIVED
                                  ^                                  |
                                  +------------- RESTORE ------------+
Session: anonymous -> authenticated -> expired/revoked -> anonymous
```

## Open Questions

- Product owner: approve the exact editable identity fields and transition rules for device-management expansion.
