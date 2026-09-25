# OTA Management Platform — Update Path and Firmware Publication

## 1. Purpose and Authority

This document defines, normatively:

```text
the public OTA URL structure
how every URL segment is derived
which administrative input fills each segment
how a firmware version is published together with its manifest
how the storage layout mirrors the URL
what is validated before publication
what is placed in device NVS for provisioning
```

`docs/OtaManager.md` is the authoritative device-side contract. This document
must not contradict it. Where a statement here conflicts with
`docs/OtaManager.md`, the device document wins and this document is defective.

`docs/15-implementation-decisions.md` remains authoritative for the response
status matrix and for rate limiting. This document is authoritative for the
URL structure and for the publication procedure, and supersedes the earlier
route decision that omitted `{platform}` from the public path (see §4.3).

---

# 2. Two Hostnames, One Application

The platform exposes two public hostnames:

| Host | Purpose | Routes served |
| --- | --- | --- |
| `api.ota-service.example` | manifest retrieval and the administrative API | `GET /api/v1/firmware/{device_type}/{platform}/manifest.json` and `/api/v1/admin/*` |
| `cdn.ota-service.example` | firmware binary delivery | `GET /firmware/{device_type}/{platform}/v{version}/{filename}` |

Both hostnames terminate on the same Nginx instance and proxy to the same
FastAPI application. `cdn.ota-service.example` is a **delivery hostname of our own
application**, not a static file server, not a third-party CDN, and never
MinIO.

Rationale for two hosts while keeping one application:

- the firmware URL and the manifest URL are decoupled, so the firmware host can
  later be fronted by a caching CDN or terminated differently without changing
  the device's provisioned manifest URL;
- the two surfaces have very different traffic and response characteristics
  (`proxy_buffering off`, no request body, multi-megabyte responses vs. a
  sub-kilobyte JSON document).

### 2.1 Mandatory invariants for both hosts

1. **The device is authenticated on both endpoints.** Every manifest GET *and*
   every firmware GET carries `X-Device-Serial`, `X-Device-Mac`,
   `X-Device-Token` (`docs/OtaManager.md` §4.1, §10.7). The firmware endpoint
   must validate them before a single byte of the binary is emitted.
2. **MinIO is never exposed.** No public bucket, no anonymous object URL, no
   pre-signed URL handed to the device.
3. **No redirects.** The device-side client is a hand-rolled HTTP/1.1 header
   reader, not a general-purpose HTTP client; redirect following is not part of
   the contract. A `3xx` is neither success nor an auth failure, so it falls
   into the retryable bucket (`docs/OtaManager.md` §8.A) and burns the entire
   five-attempt backoff budget. Therefore: no `301`, `302`, `303`, `307`, or
   `308` on either OTA endpoint, and no fixed-url/object-storage redirect.
4. **TLS on both hosts** with a certificate chain that ends at the pinned Root
   CA, currently **ISRG Root X1 (Let's Encrypt)** (`docs/OtaManager.md` §7,
   §10.5). A certificate from any other CA breaks every device already in the
   field, because the pinned CA cannot be changed without a firmware release.
5. **No query strings and no fragments** in any composed URL. `_parseUrl()`
   splits host/port/path/scheme; query and fragment handling is not determined
   by the source and must not be relied upon.

---

# 3. Path Parameter Model

Every URL segment is derived from validated data. No segment is free-form
administrative text, and no segment is ever taken from a request body and
interpolated into a URL or an object key.

| Segment | Source of truth | Filled in by | Example | Constraint |
| --- | --- | --- | --- | --- |
| `{device_type}` | `device_types.code` | administrator, when creating the device type | `bcs-controller-v1` | lowercase, `^[a-z0-9]+(-[a-z0-9]+)*$`, 3–24 characters, unique |
| `{platform}` | `device_types.platform` | administrator, when creating the device type | `esp32-s3` | lowercase, `^[a-z0-9]+(-[a-z0-9]+)*$`, 2–16 characters |
| `{version}` | `firmware_releases.version` | administrator, when publishing a release | `v2.6.1` (path form) | `^(0\|[1-9][0-9]*)\.(0\|[1-9][0-9]*)\.(0\|[1-9][0-9]*)$`, under 16 characters, rendered with a `v` prefix in the path only |
| `{filename}` | `firmware_artifacts.filename` | administrator, with a canonical default | `Controller.ino.bin` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$`; no `/`, no `\`, no `..`, no leading dot, no whitespace, no URL-encoded characters |

## 3.1 Why the device type determines the platform

`platform` is an attribute of the **device type**, not of a release.

A firmware release belongs to exactly one device type, and a device is only ever
offered firmware for its own device type (`docs/15-implementation-decisions.md`
§4). Therefore the pair (`code`, `platform`) is already fixed by the device
type, and a release inherits it. Deriving `{platform}` from the device type
gives exactly one source of truth for that URL segment, so the URL cannot drift
away from the release's actual target.

**Rejected alternative:** a separate `platform` column on the release. It would
create two sources of truth for one URL segment and would permit publishing a
release under a platform its device type never runs on. It is rejected.

**Consequence (accepted trade-off):** if one product family ever ships on two
MCU platforms, it becomes two device types with two codes (for example
`bcs-controller-v2` on `esp32-s3` and `bcs-controller-v2-c6` on `esp32-c6`).
That is correct anyway, because firmware artifacts, policies, update attempts,
and eligibility are all scoped to a device type.

## 3.2 Adding a product family or a platform later

Adding a product family, a new platform, or a hardware revision generation
requires **no route, code, or schema change**: the administrator creates a new
device type with a new `code` (and/or a different `platform`), and every
derived URL follows automatically.

```text
POST /api/v1/admin/device-types
{ "code": "bcs-hub-v1", "platform": "esp32-s3", "name": "OTA-Service Hub" }
```

The manifest URL for that family is then
`https://api.ota-service.example/api/v1/firmware/bcs-hub-v1/esp32-s3/manifest.json`.

## 3.3 Immutability of path-bearing identity

`device_types.code` and `device_types.platform` may only be edited while the
device type has **no published release and no registered device**.

Once either exists, both fields are immutable, because changing them:

- changes the device's provisioned manifest URL (NVS must be re-flashed),
- invalidates the `url` covered by already-published manifest signatures,
- moves MinIO object keys and orphans published artifacts.

A rename is not an edit; it is a new device type plus a migration.

---

# 4. Canonical Routes

## 4.1 Manifest endpoint

```http
GET https://api.ota-service.example/api/v1/firmware/{device_type}/{platform}/manifest.json
```

- Provisioned on each device in NVS as `OTA_ONLINE_URL`
  (`docs/OtaManager.md` §4).
- Must start with `http://` or `https://`, otherwise the device reports
  `parse_url_failed`. Production uses `https://` exclusively.
- Requires the three device headers.
- Returns the signed manifest for the authenticated device's own device type.

## 4.2 Firmware endpoint

```http
GET https://cdn.ota-service.example/firmware/{device_type}/{platform}/v{version}/{filename}
```

- **Never provisioned on the device.** The device takes this absolute URL from
  the `url` field of the signed manifest and requests it verbatim
  (`docs/OtaManager.md` §5.8).
- Requires the same three device headers, plus entitlement to the resolved
  release.
- Returns the exact binary with an exact `Content-Length`.

## 4.3 Superseding decision

An earlier decision
(`docs/15-implementation-decisions.md` §2, original) removed `{platform}` from
the public path and used the prefix `/api/v1/ota/`. That decision is
superseded:

| | Previous decision | Canonical decision (this document) |
| --- | --- | --- |
| Manifest path | `/api/v1/ota/firmware/{device_type}/manifest.json` | `/api/v1/firmware/{device_type}/{platform}/manifest.json` |
| Firmware path | `/api/v1/ota/firmware/{device_type}/{version}/{filename}` | `/firmware/{device_type}/{platform}/v{version}/{filename}` |
| Firmware host | same host as the API | `cdn.ota-service.example` |

The canonical paths are the ones documented by the device implementation
itself: `docs/OtaManager.md` §10 wire example uses
`/api/v1/firmware/bcs-controller/esp32-s3/manifest.json` on `api.ota-service.example`,
and the documented manifest `url` example is
`https://cdn.ota-service.example/firmware/bcs-controller/esp32-s3/v2.5.0/Controller.ino.bin`.
Including `{platform}` makes the URL self-describing and matches the documented
reference; the device does not parse the path, so this is a provisioning and
readability decision, not a parser requirement.

---

# 5. Firmware URL Length Budget

`docs/OtaManager.md` §5 (`_manifestIsValid()`) requires the manifest `url` to be
**shorter than 96 characters**. The bound applies to the `url` field inside the
manifest JSON. It does **not** apply to `OTA_ONLINE_URL` (no limit is
documented for the settings string), which is why the longer manifest URL is
safe.

The firmware URL is composed as:

```text
https://cdn.ota-service.example/firmware/{device_type}/{platform}/v{version}/{filename}
```

Fixed overhead, for the canonical host and filename:

| Component | Characters |
| --- | --- |
| `https://` | 8 |
| `cdn.ota-service.example` | 14 |
| `/firmware/` | 10 |
| four path separators (`/` ×4) | 4 |
| `v` prefix on the version | 1 |
| `Controller.ino.bin` | 18 |
| **Fixed total** | **55** |

Therefore the variable part is bounded by:

```text
len(code) + len(platform) + len(version) < 41
len(code) + len(platform) + len(version) <= 40
```

Two enforcement points:

1. **Device type creation:** require `len(code) + len(platform) <= 29`, which
   reserves 11 characters for the version (`999.999.999`). Reject with
   `device_type_path_budget_exceeded`.
2. **Release creation and publication:** require
   `len(code) + len(platform) + len(version) <= 40` using the *actual* version
   (the host-independent form), and additionally require the composed absolute
   URL to be shorter than 96 characters against the configured delivery host.
   Both reject with `manifest_url_too_long`. The absolute check is the one that
   mirrors what the device actually validates; the composite check guarantees
   the bound even if a future deployment host is longer. The device-type check
   only prevents creating a type that can never be published safely.

Worked examples:

| code | platform | version | composed length | result |
| --- | --- | --- | --- | --- |
| `bcs-controller-v1` (17) | `esp32-s3` (8) | `2.6.1` (5) | 85 | accepted |
| `bcs-controller-v2` (17) | `esp32-s3` (8) | `10.20.30` (8) | 88 | accepted |
| `bcs-controller-v1-alpha` (22) | `esp32-s3` (8) | `2.6.1` (5) | 90 | accepted |
| `bcs-controller-v1-engineering` (29) | `esp32-s3` (8) | `2.6.1` (5) | 96 | rejected (`manifest_url_too_long`) |

The composed URL is validated as a whole; per-segment maxima alone are not
sufficient, as the last row shows.

---

# 6. Storage Layout

The MinIO object key mirrors the URL path exactly. The only difference is the
host and the `/api/v1` prefix.

```text
<bucket>/firmware/{device_type}/{platform}/v{version}/{filename}
<bucket>/firmware/{device_type}/{platform}/v{version}/manifest.json
```

Example, with the example product:

```text
ota-firmware/
  firmware/
    bcs-controller-v1/
      esp32-s3/
        v2.6.1/
          Controller.ino.bin
          manifest.json
```

Why mirror the URL:

- the mapping object-key ⇄ URL is a pure prefix swap, so operators and tests can
  assert it mechanically;
- keys are deterministic and derived only from validated parameters — never
  from a temporary upload filename, a timestamp, or a random identifier;
- the `v` prefix matches the URL segment, so a key can be reconstructed from a
  logged URL and vice versa;
- the `manifest.json` object retains the exact published bytes, which are
  reproducible and auditable.

Every key segment must pass the validators in §3 before it is used. A key must
never be built by concatenating unvalidated request data: that is the path
traversal and object-key manipulation surface called out in `docs/08-security.md`.

---

# 7. Administrative Inputs

## 7.1 Creating a device type

```text
code                required   becomes {device_type} in every URL
platform            required   becomes {platform} in every URL
name                required
hardware_revision   optional
description         optional
is_active           optional
```

## 7.2 Registering a device

```text
device_type         required   selects code + platform, and therefore the
                               device's manifest URL
serial_number       required
raw_efuse_mac       required
network_mac         optional   inventory only, never OTA authentication
known firmware version + source   optional
```

The registration response exposes the derived value the field team must write
into NVS:

```json
{
  "device_type": "bcs-controller-v1",
  "platform": "esp32-s3",
  "expected_manifest_url": "https://api.ota-service.example/api/v1/firmware/bcs-controller-v1/esp32-s3/manifest.json"
}
```

`platform` is deliberately **not** a separate input at registration or at
publication: it is a property of the device type.

## 7.3 Publishing a firmware version

```text
device_type         required   selects code + platform
version             required   "2.6.1"
artifact (binary)   required   the Controller.ino.bin
manifest (JSON)     recommended, see §8
name / notes        optional
```

The administrator uploads **the binary and the manifest together**, so that the
platform parses the manifest, records its values, and provisions the matching
storage layout. MD5, size, and SHA-256 are never admin-supplied; they are always
computed by the backend.

---

# 8. Publication Procedure

Publication is the only path by which a device can ever receive a version. It is
explicit and auditable. Steps 1–10 are the upload/validate transaction; the
`PUBLISHED` transition is a separate explicit administrative action.

```text
 1. Authenticate the administrator; authorize FIRMWARE_MANAGER or SUPER_ADMIN.
 2. Resolve the device type; it must exist and be active.
 3. Validate version, filename, and the URL length budget (§3, §5).
    Reject: invalid_version, invalid_filename, manifest_url_too_long,
            device_type_path_budget_exceeded.
 4. Read the uploaded binary into a bounded buffer (4 MiB + 1 byte, to detect
    overflow). Reject artifact_empty and artifact_too_large.
 5. Compute size, MD5, and SHA-256 from the bytes.
 6. If a manifest was uploaded, parse it strictly as JSON with exactly the
    firmware keys. Reject manifest_invalid.
 7. Cross-check the uploaded manifest against the binary:
      manifest.version == release.version              else manifest_version_mismatch
      manifest.md5     == computed MD5                 else manifest_md5_mismatch
      manifest.size    == computed size                else manifest_size_mismatch
 8. Compose the canonical firmware URL from the validated parameters (§4.2).
    The uploaded manifest's url must equal it byte for byte, else
    manifest_url_mismatch.
 9. Signature gate:
      signature present, non-empty                       else missing_signature
      Base64-decodable and DER-encoded ECDSA             else invalid_signature
      if this deployment configures a public key:
        verifiable with the firmware public key          else invalid_signature
    The device's parser bounds are also asserted here: version < 16 characters,
    url < 96 characters, MD5 exactly 32 hex characters, size in 1 byte – 4 MiB,
    and the serialized manifest within the firmware JSON budget (currently
    1024 bytes).
10. Write the objects to MinIO at the derived keys (§6), then re-read them to
    verify length and hash. If the manifest was not uploaded, it is generated
    and signed here.
11. Persist release and artifact metadata in one database transaction. If the
    transaction fails, delete the objects written in step 10 (compensating
    action; PostgreSQL and MinIO are not one atomic transaction — see
    `docs/10-storage.md` §9).
12. Record the audit event (device type, version, size, MD5, SHA-256, storage
    keys). Never record the signing private key.
```

Resulting state: `UPLOADED` → `VALIDATED` → `DRAFT`. The release is not
deliverable until an administrator explicitly publishes it
(`docs/06-firmware-management.md` §7–§11).

## 8.1 Why the URL must be composed, not accepted

The signature covers `version|url|md5|size`. If an uploaded manifest were
allowed to keep an arbitrary `url`, then either:

- the signature is over a URL that is not ours (the device would be directed at
  a file we did not authorize), or
- the stored manifest and the stored artifact disagree, and the platform serves
  bytes whose hash does not match the signed MD5.

Both are integrity failures, so the uploaded `url` is treated as a claim to be
**verified**, never as configuration. Accepting the URL from the upload is
rejected. This is also why the earlier "arbitrary firmware URL" prohibition in
`AGENTS.md` §6.2 applies to this field.

## 8.2 Uploading a manifest versus generating one

Both are supported and both pass the same gate:

- **Manifest uploaded** (recommended): the release pipeline already signs
  manifests offline with `sign_manifest.py`. The platform validates and serves
  those exact bytes, so what is audited is literally what the device received.
- **Manifest generated by the backend:** the OTA service returns the manifest from
  the artifact metadata and signs it with the OTA signing private key.

**Who signs, and who verifies.** The signing private key belongs to the release
pipeline, not to this service. The pipeline produces the manifest and its
signature from the binary, and the platform stores and serves those exact bytes
without re-signing. The only verifier the OTA contract requires is the device,
which holds the public key embedded in its firmware (`docs/OtaManager.md` §§6–7);
the server contract requires only the manifest key set, an exact
`Content-Length`, and device-header validation (`docs/OtaManager.md` §10).

A deployment may additionally supply the firmware public key to the service, in
which case signature verification becomes mandatory for uploads and catches a
manifest signed by the wrong key or environment before it reaches the fleet.
Without it, the manifest is stored with `signature_verified = false`, which means
"not independently checked on the server", not "rejected". The verification that
actually proves compatibility is a real device installing a real release
(`docs/13-deployment.md`).

---

# 9. Field Semantics: Where Each Value Lives

| Value | URL path | Manifest JSON | Signed payload | Database | MinIO key |
| --- | --- | --- | --- | --- | --- |
| version | `v2.6.1` | `"2.6.1"` | `2.6.1` | `2.6.1` | `v2.6.1` |
| device type | `bcs-controller-v1` | — | — | `device_types.code` | `bcs-controller-v1` |
| platform | `esp32-s3` | — | — | `device_types.platform` | `esp32-s3` |
| URL | — | `"https://cdn.ota-service.example/..."` | full URL | derived, not stored as authority | — |
| MD5 | — | `"0898589d..."` | 32 hex chars | `firmware_artifacts.md5` | — |
| size | — | `1240720` (number) | unsigned decimal | `firmware_artifacts.size` | — |
| signature | — | `"MEUCIA3pju..."` | — | `firmware_manifests.signature` | in `manifest.json` |
| filename | `Controller.ino.bin` | — | — | `firmware_artifacts.filename` | `Controller.ino.bin` |

**Critical:** the `v` prefix exists **only** in the URL path and the object key.
The manifest `version` and the signed payload carry the bare numeric triple. A
`v` inside the signed payload would be compared by the device's
`_compareVersions()` as a non-numeric component and the manifest would be
discarded. The documented example is consistent:
`"version":"2.5.0"` served from the path `/v2.5.0/`.

---

# 10. Serving Rules

## 10.1 Manifest

- Served from the stored bytes, verbatim. The response must be the same bytes
  that were validated and stored; the endpoint must not rebuild and re-present
  a manifest in a way that could differ from the signed original.
- `Content-Type: application/json` and an exact `Content-Length`. Chunked
  transfer encoding is not supported by the device.
- No redirect.
- A `200` manifest is returned **only** when an actual eligible release exists
  for that device. The OTA request carries no running version, so the backend
  cannot fabricate a "not newer" manifest; see §10.4.

## 10.2 Firmware

- `Content-Type: application/octet-stream`, exact `Content-Length` equal to the
  manifest `size`, no chunked encoding, no redirect.
- Device authentication and entitlement are enforced per request, even though a
  signed manifest was previously issued. Serving the manifest is not an
  authorization grant for the binary.
- Optional server-side HTTP range support is not required by the contract and
  must not change the `Content-Length` semantics for a normal request.

## 10.3 Path identity must match the authenticated device

Both endpoints compare `{device_type}` and `{platform}` from the request path
against the authenticated device's own device type. On mismatch the platform
returns the **no-offer** response (`404`, reason `device_type_mismatch`), never
`200`, and never `403`.

Rationale, in order of weight:

1. Authorization is unaffected: entitlement is always decided against the
   authenticated identity's own device type, so a device cannot obtain another
   type's firmware by editing the URL. There is nothing to escalate.
2. The device contract describes `404` as "the target doesn't exist", which is
   exactly what a wrong path is; it is non-retryable and carries **no** 24-hour
   lockout (`docs/OtaManager.md` §8.B).
3. A misprovisioned device recovers immediately once NVS is corrected, instead
   of remaining locked out for up to 24 hours for a field technician's typo.
   `docs/OtaManager.md` §4.1 explicitly warns against returning `403` for
   ambiguous conditions.
4. `403` stays reserved for its two documented cases: unknown serial or
   MAC mismatch, and a known device that is `DISABLED` or `RETIRED`.

## 10.4 Why there is no "no update" manifest

`docs/OtaManager.md` §5 describes the device discarding a manifest whose version
is not strictly newer than its own. That is not a backend response the platform
can rely on:

- the OTA request carries no running version, so the platform cannot know which
  version would be "not newer" for this device;
- if the device already runs the only published release, there is no older
  release to describe, and the response would not be constructible at all.

Therefore the platform's no-offer response stays `404`
(`docs/15-implementation-decisions.md` §4), and a `200` manifest always means
"this particular release is allowed for this device".

## 10.5 Rate limiting

`429 Too Many Requests`, never `403` (`docs/15-implementation-decisions.md` §7).
`429` is a non-authentication `4xx`, so the device treats it as non-retryable
for the current poll and tries again at its normal interval — it does **not**
trigger the 24-hour lockout. Limits must therefore be loose enough that a
normally behaving device is never persistently limited, and a rate-limited poll
is a skipped cycle rather than a lockout.

---

# 11. Provisioning Checklist

Values written on the device, per device type and per device:

| NVS / setting | Value | Source on the platform |
| --- | --- | --- |
| `OTA_ONLINE_URL` | `https://api.ota-service.example/api/v1/firmware/{code}/{platform}/manifest.json` | derived at registration (`expected_manifest_url`) |
| `AUTH_API_TOKEN` | issued device token | token issuance response (shown once) |
| `DEVICE_SERIAL_ID` | device serial | registration |
| `OTA_ONLINE_ENABLED`, `OTA_CHECK_INTERVAL_HOURS` | operator policy | — |
| raw eFuse MAC | device hardware | must match the platform's `raw_efuse_mac` |

Operational notes:

- A device whose configured `OTA_ONLINE_URL` does not match the platform's
  derived URL polls a path the platform does not serve. It receives `404`
  (non-retryable, no lockout) and simply reports `non_retryable:...` at every
  interval. Fix the NVS value, then trigger `requestManualCheck()` on the
  device.
- The platform should surface this condition: a device whose
  `expected_manifest_url` was never confirmed is a provisioning defect and
  belongs in the "devices requiring attention" view
  (`docs/12-observability.md`).
- Because `403` locks the device out for 24 hours and `401` for three
  consecutive failures, correcting provisioning data **before** enabling
  automatic checks is mandatory (`docs/05-device-management.md` §14).

---

# 12. Compatibility and Extension Rules

Allowed without any firmware review:

- adding a device type (new product family or platform) — new `code`;
- adding a device type with a different `platform`;
- publishing a new version for an existing device type;
- adding releases whose version digits grow, while the composite budget holds.

Requires a firmware compatibility review:

- changing `code` or `platform` of a published device type;
- changing the public paths, the host names, or the API version prefix;
- changing where the `v` prefix appears;
- adding query strings, fragments, or non-default ports;
- introducing a redirect, chunked encoding, or a CDN that does not forward the
  device headers;
- changing the signing curve or the signed payload format.

---

# 13. Worked Example (Current Product)

```text
Device type:  bcs-controller-v1   (platform esp32-s3)
Version:      2.6.1
Filename:     Controller.ino.bin
```

Provisioned on the device:

```http
GET https://api.ota-service.example/api/v1/firmware/bcs-controller-v1/esp32-s3/manifest.json
X-Device-Serial: 10432
X-Device-Mac: 7C:9E:BD:12:34:56
X-Device-Token: <AUTH_API_TOKEN>
```

Successful response (illustrative values; `Content-Length` is the byte length of
the stored manifest, shown here for its compact serialization):

```http
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 280
```

```json
{
  "version": "2.6.1",
  "url": "https://cdn.ota-service.example/firmware/bcs-controller-v1/esp32-s3/v2.6.1/Controller.ino.bin",
  "md5": "0898589d83793b639bf736f3e3d222c2",
  "size": 1240720,
  "signature": "MEUCIA3pjuDY7q215q351W/FTH8qtm2EVWQVL51GRpU2YK1DAiEA+ptKGh8j1ziQ9+8KfTsJAsqMqUvDCWbFxKtu4ctf39E="
}
```

The exact signed payload for this manifest is:

```text
2.6.1|https://cdn.ota-service.example/firmware/bcs-controller-v1/esp32-s3/v2.6.1/Controller.ino.bin|0898589d83793b639bf736f3e3d222c2|1240720
```

(132 bytes, for a 280-byte manifest out of the device's 1024-byte JSON budget.
The URL is the largest field, which is why the length budget in §5 is enforced
at publication time rather than discovered by a device in the field.)

The device then requests the binary it was given, with the same headers:

```http
GET https://cdn.ota-service.example/firmware/bcs-controller-v1/esp32-s3/v2.6.1/Controller.ino.bin
```

Response:

```http
HTTP/1.1 200 OK
Content-Type: application/octet-stream
Content-Length: 1240720
```

Storage:

```text
ota-firmware/firmware/bcs-controller-v1/esp32-s3/v2.6.1/Controller.ino.bin
ota-firmware/firmware/bcs-controller-v1/esp32-s3/v2.6.1/manifest.json
```
