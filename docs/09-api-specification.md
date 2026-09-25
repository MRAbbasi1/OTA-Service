# OTA Management Platform — API Specification

## 1. API Structure

The API is divided into:

```text
Admin API
OTA Device API
```

Recommended prefixes:

```text
/api/v1/admin/*                          administrative API
/api/v1/firmware/{type}/{platform}/*     device manifest endpoint
/firmware/{type}/{platform}/v{ver}/*     device firmware endpoint (delivery host)
```

The device-facing route shape is fixed by the firmware contract; the complete
URL model is `docs/16-update-path-and-publication.md`.

```text
/health/live                             liveness; no dependency is touched
/health/ready                            readiness; checks PostgreSQL and MinIO
```

The probes are unauthenticated by design — a probe has no session, and requiring
one would mean an unhealthy instance cannot be detected. They never name a host,
a credential, or an error string. Liveness deliberately does not check a
dependency, so a slow database cannot cause a restart loop; readiness returns
`503` so a load balancer removes the instance instead of routing to it.

---

# 2. Admin API

Every administrative endpoint requires an authenticated session, a capability,
and — for unsafe methods — a matching CSRF header. Authentication is not
something a route opts into: each endpoint carries the dependency for the
capability it needs, so an unauthenticated route cannot be added by omission.

Relationship to the device API: `/api/v1/admin/*` is for people,
`/api/v1/firmware/*` and `/firmware/*` are for devices. They share no
credentials and no error semantics — an admin `403` is a permission problem,
while a device `403` silences a controller for 24 hours.

## Admin authentication

```http
POST /api/v1/admin/auth/login
POST /api/v1/admin/auth/logout
GET  /api/v1/admin/auth/me
POST /api/v1/admin/auth/password
GET  /api/v1/admin/admins
POST /api/v1/admin/admins
POST /api/v1/admin/admins/{id}/active
POST /api/v1/admin/admins/{id}/role
```

```json
{"email": "admin@example.com", "password": "..."}
```

A successful login sets two cookies and returns:

```json
{
  "admin": {"id": 1, "email": "admin@example.com", "role": "super_admin",
            "is_active": true, "last_login_at": "...", "created_at": "...",
            "capabilities": ["admins:manage", "audit:read", "dashboard:read"]},
  "csrf_token": "...",
  "csrf_header": "X-CSRF-Token",
  "expires_at": "..."
}
```

```text
ota_admin_session   HttpOnly, SameSite=Lax, Secure in production, Path=/
ota_admin_csrf      readable by the client, same attributes
```

The access token is never returned in the body, so a script cannot copy it out
of a response; the cookie is the only carrier. Browser clients echo `csrf_token`
in the `X-CSRF-Token` header on every `POST`/`PATCH`/`DELETE`.

Status codes:

```text
200  success
401  unauthenticated (no session, expired, tampered, or revoked) /
     invalid_credentials on login — the same body for every failure reason
403  csrf_failed, insufficient_capability, invalid_password
409  admin_email_exists, last_super_admin, password_unchanged
422  invalid_email, weak_password, plus schema validation
429  rate_limited
```

`POST /auth/logout` also succeeds when the session is already gone: a client
must always be able to discard an `HttpOnly` cookie it can no longer invalidate.
A password change ends every other session immediately, and the last active
`SUPER_ADMIN` cannot be deactivated or demoted.

---

## Devices

```http
GET    /api/v1/admin/devices
POST   /api/v1/admin/devices
GET    /api/v1/admin/devices/{id}
PATCH  /api/v1/admin/devices/{id}
POST   /api/v1/admin/devices/{id}/disable
POST   /api/v1/admin/devices/{id}/enable
POST   /api/v1/admin/devices/{id}/token/rotate
POST   /api/v1/admin/devices/{id}/token/revoke
POST   /api/v1/admin/devices/{id}/firmware-version
GET    /api/v1/admin/devices/{id}/update-attempts
```

`firmware-version` records the version an administrator asserts the device is
running, or clears it when it is unknown. Because the OTA request carries no
running version, this is the only input to the "nothing newer than the known
version" rule, and it is stored with provenance (`admin_asserted` today;
`device_reported` is reserved for a future authenticated telemetry contract).
A served download never writes it.

`update-attempts` returns the server-observed OTA events for one device, newest
first: what was offered and what was delivered. None of them is evidence that a
firmware installed or booted.

---

# 3. Device Types

```http
GET    /api/v1/admin/device-types
POST   /api/v1/admin/device-types
GET    /api/v1/admin/device-types/{id}
PATCH  /api/v1/admin/device-types/{id}
```

`code` and `platform` are the two path-bearing fields: they become
`{device_type}` and `{platform}` in every OTA URL for that type. Both are
validated on creation (`^[a-z0-9]+(-[a-z0-9]+)*$`) and are immutable once the
device type has a registered device or a published release.

Creation validates the URL length budget and reserves version space:
`len(code) + len(platform) <= 29`.

A device type response exposes the derived public URLs so operators can
provision devices without constructing them by hand:

```json
{
  "code": "bcs-controller-v1",
  "platform": "esp32-s3",
  "manifest_url": "https://api.ota-service.example/api/v1/firmware/bcs-controller-v1/esp32-s3/manifest.json",
  "firmware_path_template": "https://cdn.ota-service.example/firmware/bcs-controller-v1/esp32-s3/v{version}/{filename}"
}
```

---

# 4. Firmware

```http
GET    /api/v1/admin/firmware/releases
POST   /api/v1/admin/firmware/releases
GET    /api/v1/admin/firmware/releases/{id}
GET    /api/v1/admin/firmware/releases/{id}/manifest
POST   /api/v1/admin/firmware/releases/{id}/draft
POST   /api/v1/admin/firmware/releases/{id}/publish
POST   /api/v1/admin/firmware/releases/{id}/deprecate
POST   /api/v1/admin/firmware/releases/{id}/archive
```

`GET .../manifest` returns the exact stored manifest bytes that a device will
be served, so an operator can compare them with what the release pipeline
produced without downloading from the delivery host.

Creating a release takes the binary and the manifest together
(`multipart/form-data`), because the platform parses the manifest, records its
values, and derives the storage layout from it:

```text
device_type_id      required   selects code + platform
version             required   "2.6.1"
artifact            required   the firmware binary
manifest            optional   the signed manifest.json
name / release_notes optional
```

The administrator never supplies MD5, size, SHA-256, or the download URL: the
first three are computed from the bytes, and the URL is composed from the
validated parameters. An uploaded manifest whose `version`, `md5`, or `size`
disagrees with the binary, or whose `url` is not the composed canonical URL, is
rejected — see `docs/16-update-path-and-publication.md` §8 for the full
procedure and the rejection codes.

Release responses expose the derived artifact and manifest URLs and storage
keys, plus the manifest field set that will actually be served.

---

# 5. Firmware Artifact

```http
POST   /api/v1/admin/firmware/artifacts
GET    /api/v1/admin/firmware/artifacts/{id}
```

The artifact upload creates the MinIO object and calculates:

```text
size
md5
sha256
```

---

# 6. Policies

```http
GET    /api/v1/admin/policies
POST   /api/v1/admin/policies
GET    /api/v1/admin/policies/{id}
PATCH  /api/v1/admin/policies/{id}
DELETE /api/v1/admin/policies/{id}
POST   /api/v1/admin/policies/{id}/active
GET    /api/v1/admin/policies/{id}/devices
```

A policy is created with `scope` (`global`, `device_type`, `device`),
`policy_type` (`pin`, `range`, `disable`), the scope target, an optional
`priority`, and an optional `starts_at`/`ends_at` window:

```json
{
  "name": "Hold the fleet at 2.5.8",
  "scope": "global",
  "policy_type": "pin",
  "target_version": "2.5.8",
  "priority": 0,
  "starts_at": null,
  "ends_at": null
}
```

Incoherent definitions are `422` (`policy_target_required`,
`policy_version_forbidden`, `policy_range_invalid`, `policy_window_invalid`,
`policy_scope_target_required`, `policy_scope_target_forbidden`,
`invalid_version`). A second active policy with the same scope target and the
same priority over an overlapping window is `409 policy_conflict`: the platform
refuses to guess an outcome the data cannot decide
(`docs/15-implementation-decisions.md` §10).

`PATCH` edits the definition in place; `scope`, `policy_type`, `device_id`, and
`device_type_id` are not accepted there (unknown fields are rejected, not
ignored). `is_active` is changed through `POST .../active`, and
`GET /api/v1/admin/policies/{id}/devices` returns the devices the policy reaches
— its blast radius — with `total`, `page`, and `page_size`.

`DELETE` removes a policy whose definition can no longer be edited into
something useful. Disabling it with `POST .../active` keeps it for the record.

---

# 7. Update Preview

```http
GET /api/v1/admin/devices/{id}/update-decision
```

This endpoint uses the same `UpdateDecisionService` as the real OTA endpoint, and
the device's own device type supplies the path identity, so it cannot disagree
with what the device was told.

Example response:

```json
{
  "device_id": 10432,
  "update_available": true,
  "reason": "PINNED_VERSION",
  "target_version": "2.5.8",
  "release_id": 6,
  "firmware_url": "https://cdn.ota-service.example/firmware/bcs-controller-v1/esp32-s3/v2.5.8/Controller.ino.bin",
  "policy_id": 3,
  "policy_scope": "global",
  "policy_name": "Hold the fleet at 2.5.8",
  "known_version": "2.5.7",
  "known_version_source": "admin_asserted",
  "explanation": "The policy “Hold the fleet at 2.5.8” pins this device to 2.5.8."
}
```

`known_version` is always accompanied by `known_version_source`, because no
current OTA request can report a running version. The endpoint is read-only: it
never writes `last_manifest_check_at` or an `update_attempts` row.

---

# 8. Dashboard

```http
GET /api/v1/admin/dashboard/summary
GET /api/v1/admin/dashboard/firmware-distribution
GET /api/v1/admin/dashboard/update-activity
GET /api/v1/admin/dashboard/device-health
GET /api/v1/admin/dashboard/devices-needing-attention
```

All of them are `dashboard:read` and read-only. `summary` returns device counts
by lifecycle state, `ota_disabled_devices`, active device types, published
releases, active policies, docs/polls in the last 24 hours, and
`devices_needing_attention`.

Firmware distribution returns one row per device type, version, and provenance —
including the row where `version` is `null`, which means *no version was ever
asserted*, not "unknown because we forgot". Update activity lists the recent
server-observed attempts and counts offers and downloads for 24 hours and 7 days.

`device-health` counts three categories, each with the explanation the dashboard
shows:

```text
NEVER_SEEN            registered > 1 day ago, has never authenticated
STALE_CHECK           active, no manifest check in 7 days
DOWNLOAD_UNCONFIRMED  a binary was served > 2 days ago and the known version is
                      still not that version
```

The third is the platform's honest form of "the update did not take": the device
never reports a successful boot, so the server can only report that it handed out
a version and that what it knows about the device disagrees. A device matching
several categories is counted once in the summary total.

---

# 9. Audit

```http
GET /api/v1/admin/audit-events
```

Filters: `action`, `actor`, `resource_type`, `resource_id`, plus `page` and
`page_size`. Newest first, read-only, and reachable only with `audit:read` — a
`VIEWER` can inspect the fleet but not the security history. Records never
contain a secret: a failed login stores the attempted email and the failure
reason, never the password.

---

# 10. OTA Manifest

```http
GET https://api.ota-service.example/api/v1/firmware/{device_type}/{platform}/manifest.json
```

Required headers:

```text
X-Device-Serial
X-Device-Mac
X-Device-Token
```

This is the URL provisioned on devices as `OTA_ONLINE_URL`.

---

# 11. OTA Manifest

```http
GET https://api.ota-service.example/api/v1/firmware/{device_type}/{platform}/manifest.json
```

This is the URL provisioned on each device in NVS as `OTA_ONLINE_URL`. It serves
the stored, signed manifest **verbatim** — it is never re-rendered or re-signed
per request — so what is audited is literally what the device received.

```text
Content-Type: application/json
Content-Length: byte length of the stored manifest
Cache-Control: no-store
no Transfer-Encoding, no Location
```

A `200` is returned only when an actual eligible release exists for that device.
Otherwise the response is the no-offer `404` (§12).

---

# 12. OTA Firmware

```http
GET https://cdn.ota-service.example/firmware/{device_type}/{platform}/v{version}/{filename}
```

Required headers:

```text
X-Device-Serial
X-Device-Mac
X-Device-Token
```

This URL is never provisioned on a device; the device requests the absolute
URL from the signed manifest's `url` field.

```text
Content-Type: application/octet-stream
Content-Length: exactly the artifact size, equal to the manifest `size`
Cache-Control: no-store
no Transfer-Encoding, no Location
```

The release is resolved by exact version and filename, and must be `PUBLISHED`
or `DEPRECATED`: a deprecated release is no longer offered, but a device already
holding a signed manifest can finish that download, and pinned devices may still
use it (`docs/06-firmware-management.md` §12). Entitlement is re-decided on
every download, so a signed manifest is never an authorization grant for the
binary.

Both OTA endpoints compare the path's `{device_type}`/`{platform}` against the
authenticated device's own device type and return the no-offer response on
mismatch. Neither endpoint may return a redirect, and neither may be cached by an
intermediary. The complete path and serving model is
`docs/16-update-path-and-publication.md`.

---

# 13. Authentication Errors

```text
401
```

means invalid authentication.

```text
403
```

is reserved for an unknown/MAC-mismatched device or a known device that is
disabled or retired. It immediately locks the firmware's automatic polling for
24 hours.

```text
404
```

is the no-offer response, including a request whose path names a different
device type or platform than the authenticated device's own.

The distinction is required because of the current device behavior.

The complete OTA status matrix is normative in
`docs/15-implementation-decisions.md`: 404 is the no-offer response and 429
is rate limiting; neither must be replaced with 403.

```text
429
```

is rate limiting, applied per authenticated device identity after
identification. It never locks a device out: the device treats it as a
non-retryable response for that poll and tries again at its normal interval.

```text
5xx
```

is used for every transient backend failure — an unreachable object store, a
missing object, a stored length that disagrees with the signed size — so the
firmware's retry and backoff sequence can operate. A backend defect must never
surface as `403`, because that silences automatic checks for 24 hours.

On `404`, `429`, and `503`, the reason is also returned in the non-contractual
`X-OTA-Reason` header (`NO_ELIGIBLE_RELEASE`, `DEVICE_TYPE_MISMATCH`,
`OTA_DISABLED`, `NO_UPDATE`, `POLICY_BLOCKED`, `RELEASE_NOT_FOUND`,
`RATE_LIMITED`, ...). It is never present on `401` or `403`, so it cannot be used
to probe which serial numbers are enrolled.

A policy that excludes a version also `404`s its binary (`POLICY_BLOCKED`). That
is deliberate: withholding only the offer would leave the version installable
from a manifest the device already holds (`docs/15-implementation-decisions.md`
§10).

---

# 14. Error Format

Admin API errors should use a consistent structured format.

Example:

```json
{
  "error": {
    "code": "DEVICE_NOT_FOUND",
    "message": "Device was not found",
    "request_id": "..."
  }
}
```

OTA endpoints must preserve the HTTP semantics expected by the firmware.

---

# 15. Pagination

Administrative list endpoints shall use pagination.

Recommended:

```text
page
page_size
```

or cursor pagination if scale requires it.

---

# 16. Filtering

Device endpoints should support filters for:

```text
device_type
firmware_version
status
ota_enabled
```

Firmware endpoints should support:

```text
device_type
version
status
```

Device-type and firmware filters are always the first-class dimension of the
firmware catalog: releases, artifacts, manifests, and OTA paths are all scoped
to a device type.

---

# 17. API Versioning

Breaking API changes require a new version.

Existing OTA device contract must not silently change.

---

# 18. OpenAPI

FastAPI shall generate OpenAPI documentation.

Administrative APIs should be documented through OpenAPI.

OTA protocol requirements must additionally exist as a human-readable contract document.
