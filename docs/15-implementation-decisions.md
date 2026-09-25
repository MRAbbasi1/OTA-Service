# OTA Management Platform — Implementation Decisions

## 1. Purpose and Status

This document records the cross-document ambiguities resolved during
implementation. It is authoritative for the decisions below and does not change
the current firmware protocol.

---

## 2. OTA Route and Wire Contract

The canonical OTA routes for this platform are:

```text
GET https://api.ota-service.example/api/v1/firmware/{device_type}/{platform}/manifest.json
GET https://cdn.ota-service.example/firmware/{device_type}/{platform}/v{version}/{filename}
```

These match the device-side documentation's own wire example and reference
manifest URL (`docs/OtaManager.md` §10). Both hostnames terminate on the same
Nginx and the same FastAPI application; `cdn.ota-service.example` is a delivery
hostname of our application, not a static file server and never MinIO.

The manifest route is provisioned on each device in NVS as `OTA_ONLINE_URL`;
the firmware route is never provisioned and is taken from the signed manifest
`url` field. Neither endpoint may redirect: the device client does not follow
`3xx`, which would instead consume its retry/backoff budget
(`docs/OtaManager.md` §8.A).

`{device_type}` is `device_types.code` and `{platform}` is
`device_types.platform`; the release inherits both from its device type, so
neither URL segment has a second source of truth. The `v` prefix appears only
in the path and object key — never in the manifest `version` field or in the
signed payload.

The manifest `url` is an absolute URL to the firmware route. It is signed as
part of `version|url|md5|size`, must be shorter than 96 characters, and must
never expose MinIO. It is composed by the backend from validated parameters and
is never accepted from an upload. The 96-character limit applies to this
firmware URL, not to the NVS manifest URL.

The complete normative model — path parameters, length budget, storage layout,
publication procedure with manifest upload, provisioning, and extension rules —
is in `docs/16-update-path-and-publication.md`. Existing field devices use the
full manifest URL provisioned in NVS; their configured URL must be inventoried
before cutover and either migrated or supported explicitly.

---

## 3. Canonical Device Identity

`X-Device-Serial` is the decimal ASCII representation of a positive integer.
The database stores its canonical decimal string (`^[1-9][0-9]*$`); leading
zeroes are rejected at registration and request validation.

`raw_efuse_mac` is stored and compared as uppercase colon-separated hex:

```text
XX:XX:XX:XX:XX:XX
```

Request values are normalized to this representation before lookup. The value
is the raw eFuse MAC sent by `OtaManager`, never the Ethernet network MAC.

---

## 4. Device Lifecycle and Entitlement

The `Device` model uses both:

```text
lifecycle_status: PROVISIONED | ACTIVE | DISABLED | RETIRED
ota_enabled: boolean
```

They are not interchangeable. Only an `ACTIVE` device with `ota_enabled=true`
is eligible for an offer. `PROVISIONED` is not OTA-eligible until activated.

The OTA authentication/entitlement response matrix is:

| Condition | Response | Device consequence |
| --- | --- | --- |
| Unknown serial or known serial with mismatched raw eFuse MAC | 403 | Immediate 24-hour automatic-check lockout |
| Known device with invalid, inactive, expired, or revoked token | 401 | Three consecutive failures cause a 24-hour lockout |
| Known device in `DISABLED` or `RETIRED` lifecycle state | 403 | Immediate 24-hour automatic-check lockout |
| Authenticated device requesting a path whose `{device_type}` or `{platform}` is not its own | 404 | No retry/backoff; no lockout; recovers as soon as NVS is corrected |
| Authenticated device with OTA disabled, blocked policy, or no eligible published release | 404 | No retry/backoff; device tries on its next scheduled poll |
| Transient database, MinIO, signing, or application failure | 5xx | Firmware retry/backoff |

403 is intentionally reserved for the first and third rows. Re-enabling a
device after a 403 does not clear its firmware-side RAM lockout; it recovers
on expiry, reboot, or a local `requestManualCheck()`.

The fourth row is deliberately not `403`. Authorization is always decided
against the authenticated identity's own device type, so a mismatched path
cannot grant access to another device type's firmware. Using `404` avoids
locking a misprovisioned device out of automatic checks for 24 hours because of
a path typo in its NVS configuration (`docs/OtaManager.md` §4.1 warns against
this), and it is the device's own documented meaning for a non-existent target.

There is no special successful wire response for “no update.” A 404 in the
fourth row is an explicitly chosen platform no-offer response, not a claim
that the firmware has a dedicated no-update status. It is safe because the
firmware treats it as a non-authentication, non-retryable response and polls
again at its normal interval. A 200 manifest may only be returned when its
release is actually allowed for that device.

---

## 5. Firmware Version Knowledge and Update Attempts

The current OTA GET contract sends no running firmware version and includes no
post-reboot callback. Therefore the backend must not infer that an installation
succeeded merely because it served a binary.

The device record keeps these distinct values:

```text
known_firmware_version nullable
known_firmware_source nullable: ADMIN_ASSERTED | DEVICE_REPORTED
known_firmware_observed_at nullable
last_download_served_version nullable
last_download_served_at nullable
```

`known_firmware_version` is shown as “known firmware version” and its source
must be shown to administrators. In the current protocol it can only be
admin-asserted; `DEVICE_REPORTED` is reserved for a future authenticated
telemetry contract. Serving a download updates only `last_download_served_*`.

`UpdateAttempt` records server-observed states only:

```text
CHECKED
UPDATE_OFFERED
DOWNLOAD_STARTED
DOWNLOAD_SERVED
DOWNLOAD_FAILED
```

`from_version` is nullable and, when present, is a snapshot of the known
firmware version plus its source. There is no `VERIFIED`, `SUCCESS`, or
installed-and-booted state until a future device report contract exists.

---

## 6. Firmware Release and Version Rules

The sole release lifecycle is:

```text
UPLOADED → VALIDATED → DRAFT → PUBLISHED → DEPRECATED → ARCHIVED
```

`withdrawn` is not a release status in this platform; `ARCHIVED` is the
terminal non-deliverable state.

Two distinct questions are asked of that lifecycle, and they have different
answers:

```text
which release is offered?          PUBLISHED only
which release may be delivered?    PUBLISHED and DEPRECATED, by exact version
```

`DEPRECATED` therefore stops *new offers* without breaking a download that is
already in flight, and without withholding a version a pin may legitimately
select. `DRAFT`, `UPLOADED`, `VALIDATED`, and `ARCHIVED` are never served by the
delivery endpoint.

The current firmware accepts only strictly newer numeric versions. The backend
therefore accepts only:

```text
MAJOR.MINOR.PATCH
```

where all three components are non-negative decimal integers and no
pre-release/build suffix is permitted. Semantic comparison is numeric.
`allow_downgrade` is retained only as future metadata; it must not cause an
older image to be offered to the current firmware.

Before a release can be published, validation must prove the manifest is
firmware-compatible: version length under 16 characters, absolute download
URL under 96 characters, MD5 exactly 32 hexadecimal characters, size from 1
byte through 4 MiB, and serialized manifest within the firmware JSON budget
(currently 1024 bytes).

The download URL is composed from validated parameters and its length is
checked as a whole. With the canonical host and filename the fixed overhead is
55 characters, so `len(code) + len(platform) + len(version) <= 40`. Device-type
creation additionally reserves version space by requiring
`len(code) + len(platform) <= 29`. The arithmetic, the rejection codes, and the
worked boundary cases are in `docs/16-update-path-and-publication.md` §5.

The C++ implementation signs the payload with the signing private key. The
device, not the server, is the verifier: it holds the public key embedded as
`ota_public_key.h` and is the only party the contract requires to verify
(`docs/OtaManager.md` §§6–7). The server contract in the same document requires
only the manifest key set and an exact `Content-Length`, and its reference
implementations do not verify anything.

The platform therefore supports both models:

```text
release pipeline signs  ->  platform stores and serves the signed bytes verbatim
backend generates signs ->  only when the deployment holds the signing key
test/CI verification    ->  optional deployment guard via a configured public key
```

The signing private key must never be required by, or stored in, the OTA service
for the normal flow. If a deployment configures the firmware public key, then
signature verification becomes mandatory for uploads and the manifest is marked
`signature_verified`; if it does not, the manifest is stored with
`signature_verified = false`, which records "not independently verified" and is
not a rejection.

A fixed conformance vector derived from the production key remains the strongly
recommended regression guard before the platform generates signatures itself:
it pins the exact curve, DER encoding, and payload format, and it is the only
check that fails in CI instead of on a fleet. It is no longer a publication
blocker.

---

## 7. Rate Limiting

Rate limiting returns `429 Too Many Requests`, never 403. The current firmware
treats 429 as a non-retryable response for that poll; limits must therefore
permit its normal retry sequence.

Initial application limits are:

| Surface | Limit |
| --- | --- |
| Manifest, authenticated device identity | 20 requests / 5 minutes / device |
| Firmware download, authenticated device identity | 10 requests / 15 minutes / device |
| Admin login | 5 requests / 15 minutes / IP + email |
| Other admin API | 120 requests / minute / authenticated admin |

FastAPI applies authenticated device/admin limits after identity resolution, so
the OTA limit is per device rather than per source IP, and a shared NAT cannot
silence a fleet. Host Nginx must not impose a low IP-wide OTA limit. The initial
single-instance deployment may use local rate-limit state;
scaling to multiple API replicas requires a shared store and is a deliberate
architecture change, not an implicit Redis dependency. Per device the limits are
20 manifest requests per 5 minutes and 10 firmware downloads per 15 minutes, both
keyed by the authenticated device id.

Firmware objects are streamed from MinIO in bounded chunks. The API validates
the object size before sending headers, sets the exact signed `Content-Length`,
and records a download as served only after the complete expected byte count has
been emitted. Nginx buffering and intermediary caching are disabled for OTA
responses.

---

## 8. Administrative Access and Provisioning

The first `SUPER_ADMIN` is created only by an audited, idempotent deployment
CLI command using one-time environment-provided bootstrap credentials. The
command never logs the password and does not overwrite an existing user.

The admin API uses short-lived JWT access tokens in `Secure`, `HttpOnly`,
`SameSite=Lax` cookies. State-changing cookie-authenticated requests require
CSRF protection. Passwords use Argon2id.

Provisioning records the serial, raw eFuse MAC, device type, initial known
firmware version (if administratively known), and token. It should also retain
the Ethernet network MAC as optional non-OTA inventory metadata, clearly
labelled so it cannot be used for OTA authentication.

The implementation resolves the remaining details as follows.

| Decision | Choice |
| --- | --- |
| Password hashing | Argon2id via `argon2-cffi`, library defaults; a password is at least 12 characters, and length is the only composition rule |
| Session token | HS256 JWT, `iss`/`aud` pinned, `exp`/`iat`/`sub`/`sv` required, 30-minute default lifetime, 10-second leeway |
| Cookie | `HttpOnly` session cookie plus a deliberately readable CSRF cookie; `SameSite=Lax`; `Secure` in production and only there; `Path=/` |
| CSRF | double-submit: the header must match the cookie, on every unsafe method; a request carrying neither cookie is not checked, because it carries no credential |
| Revocation | `session_version` on the account, bumped by a password change, a role change, or a deactivation, and compared against the token on every request — revocation is immediate, not "within TTL" |
| Authorization | capability-based; a route names the capability it needs, roles are only bundles of capabilities (`app/domain/rbac.py`) |
| Login limiting | 5 attempts / 15 minutes per source address **and** submitted email, so one address cannot lock an account out and one account cannot be attacked at full speed |
| Failure uniformity | every login failure is the same `401 invalid_credentials`, and an unknown account still pays the hashing cost, so the surface cannot enumerate administrators |
| Bootstrap | `uv run ota-admin` with `BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_PASSWORD`; refuses a weak password, prints no secret, records `admin_bootstrapped`, and changes nothing when any account already exists |
| Last super admin | deactivating or demoting the last active `SUPER_ADMIN` is refused (`409 last_super_admin`): otherwise the platform could only be recovered through direct database access |
| Audit actor | the authenticated administrator's email; the previous literal `"admin"` recorded that something happened, not who did it |

Cookie flags are environment-derived rather than configured: production cannot be
served over plain HTTP, so `Secure` is set there and development, which is, is not.
`ADMIN_COOKIE_SECURE` can override the derivation, and production refuses `false`.

The production validator also refuses to start with the development default
`ADMIN_JWT_SECRET` or one shorter than 32 characters, so a copied `.env` cannot
become a production signing key.

---

## 9. Operational Limitations and Verification

The server cannot push or force an update with the current device protocol.
It can publish an eligible release; the device receives it on its next poll or
when a local device operator invokes `requestManualCheck()`.

Every deployment acceptance test must request a manifest on `api.ota-service.example`
and a firmware binary on `cdn.ota-service.example` through the production Nginx path
and assert an exact `Content-Length`, no `Transfer-Encoding: chunked`, no
`Location` header, and a certificate chain ending at ISRG Root X1. A real
ESP32-S3 end-to-end update remains the final OTA-ready acceptance criterion.

---

## 10. Update Policy Resolution

Policy rules live in `app/domain/policy.py` and policy rows in
`app/services/policies.py`; `UpdateDecisionService` is the only caller that turns
them into a decision, and the dashboard preview calls the same service.

Scopes and their precedence are unchanged (`docs/07-update-policy.md` §3):
`device` > `device_type` > `global`. Four rules make that deterministic:

| Situation | Behaviour |
| --- | --- |
| Several policies at the same scope | the highest `priority` wins |
| Two equal-priority policies at one scope with overlapping time windows | rejected at write time (`409 policy_conflict`), never guessed |
| An ambiguity that still reaches a decision (rows written outside the API) | fails closed: `POLICY_BLOCKED`, no offer, `error` log |
| A conflict at a narrower scope | does not fall back to a wider policy; the narrower scope governs, and it is ambiguous |

A policy's `starts_at`/`ends_at` window is evaluated in UTC at decision time,
inclusive of `starts_at` and exclusive of `ends_at`. Inactive and out-of-window
policies are ignored entirely. A policy's scope, type, and scope target are
immutable: a partial edit is re-validated as a whole and re-checked for
ambiguity, because rewriting scope or type in place would re-interpret decisions
the policy has already explained.

**A policy gates delivery, not only the offer.** `check_eligibility` — path
identity, lifecycle state, and the device's own OTA flag — still does not read the
catalog, so an unrelated change never revokes a manifest already handed out. But
a policy that excludes a version withholds its *bytes* as well, because the
binary is what an installation needs: an offer withheld at poll time would
otherwise still be redeemable from a manifest the device already holds. The
firmware endpoint therefore asks `allows_delivery(device, release)` before
streaming, and a refusal is a `404 POLICY_BLOCKED`, not a `403`.

**`allow_downgrade` is not modelled.** The firmware installs only a strictly
newer version, so the flag could never do anything except mislead an operator; a
pin below the known version produces `NO_UPDATE`, never an offer
(`docs/07-update-policy.md` §7).

**Decision reason values** are the documented set (`docs/07` §12) plus
`DEVICE_TYPE_MISMATCH` for a path naming another device type, and
`POLICY_BLOCKED` for a policy that removes every eligible release, for a pin to a
version that is not currently published, and for an ambiguous configuration.
`OTA_DISABLED` covers both the device flag and a `disable` policy; the decision
carries `policy_id` and `policy_scope` so the two remain distinguishable without
inventing a second reason value.

---

## 11. Implementation Impact

These decisions affect the requirements, domain model, OTA/admin API,
database, tests, and deployment configuration.

| Area | Required implementation consequence |
| --- | --- |
| Database | Create `lifecycle_status`, `ota_enabled`, known-firmware provenance, and server-served-download fields. A later upgrade from a prototype schema must use a forward Alembic migration; it must not overwrite firmware history. |
| OTA API | Implement only the canonical routes and response matrix in this document; provisioned field URLs require an inventory/cutover plan. |
| Admin API | Return known-version provenance rather than asserting an actual installed version; bootstrap the first administrator through the deployment CLI. |
| Firmware releases | Enforce the numeric version, the derived-path parameter format, the URL length budget, and the parser bounds before publication; store the artifact and manifest at the URL-mirroring keys; require production-key conformance before publication where server-side verification is configured. |
| Tests | Add unit, API, Nginx integration, security, and real-device checks described in Sections 6–9, including path derivation, budget boundaries, and served-manifest byte equality. |
| Deployment | Configure the two public base URLs (`OTA_MANIFEST_BASE_URL`, `OTA_FIRMWARE_BASE_URL`), both Nginx hostnames and their Let's Encrypt certificates, no-chunked streaming, rate-limit ownership, bootstrap secrets, and an explicit Alembic migration step. |

No device-side protocol field, signature payload, or OTA public route semantic
may be changed beyond these documented backend choices without a new firmware
compatibility review.
