# OTA Management Platform — Firmware Management

## 1. Purpose

Firmware Management controls the complete lifecycle of firmware artifacts and releases.

---

# 2. Artifact vs Release

A release is a logical version.

An artifact is the actual binary.

A manifest is the signed document the device actually receives, and it is the
only thing that can authorize a download.

```text
FirmwareRelease
      |
      +---- FirmwareArtifact    (the Controller.ino.bin)
      |
      +---- FirmwareManifest    (the signed JSON served to devices)
```

All three are scoped to exactly one device type. The device type's `code` and
`platform` are the two path segments of every URL for that release, so the
firmware catalog, the OTA URL, and the storage layout are the same fact
expressed three ways. The normative model is
`docs/16-update-path-and-publication.md`.

---

# 3. Upload Process

The administrator uploads the binary **and the manifest together**. The manifest
is parsed so that its values are recorded in the database and the storage layout
is provisioned from the same parameters the URL is composed from.

```text
Admin
  |
Upload binary + manifest (optional)
  |
Authenticate / authorize (FIRMWARE_MANAGER)
  |
Resolve device type (active)
  |
Validate version, filename, URL length budget
  |
Read binary in a bounded buffer (4 MiB + 1)
  |
Calculate size / MD5 / SHA-256
  |
Parse manifest, cross-check version / md5 / size
  |
Compose canonical URL, compare with manifest url
  |
Signature gate (present, DER, verifiable)
  |
Store binary and manifest in MinIO at the derived keys
  |
Re-read objects, verify length and hash
  |
Create metadata in one transaction (compensate on failure)
  |
Audit event
  |
Release is UPLOADED -> VALIDATED -> DRAFT
```

The full procedure, including every rejection code, is
`docs/16-update-path-and-publication.md` §8. The binary and the manifest are
uploaded together precisely so the platform can reject a manifest whose signed
fields disagree with the bytes it was handed, rather than publishing a pairing
that no device can install.

---

# 4. MinIO Storage

Firmware binaries and their manifests are stored in MinIO.

Layout:

```text
<bucket>/
  firmware/
    <device-type>/
      <platform>/
        v<version>/
          <filename>
          manifest.json
```

Example:

```text
ota-firmware/
  firmware/
    bcs-controller-v1/
      esp32-s3/
        v2.6.1/
          Controller.ino.bin
          manifest.json
```

---

# 5. Object Naming

Object keys must be deterministic and must mirror the public URL path, so that a
key can be reconstructed from a logged URL and vice versa.

The key must not depend on temporary upload filenames, timestamps, or random
identifiers.

```text
firmware/{device_type}/{platform}/v{version}/{artifact_filename}
firmware/{device_type}/{platform}/v{version}/manifest.json
```

`{version}` carries the `v` prefix here and in the URL, but never in the
manifest `version` field or in the signed payload. Every key segment must pass
the path parameter validators before it is used; keys must never be built by
concatenating unvalidated request data. See
`docs/16-update-path-and-publication.md` §§3 and 6.

---

# 6. Checksums

The backend calculates:

```text
MD5
SHA-256
```

MD5 exists for compatibility with the current device OTA implementation.

SHA-256 is used for backend-side integrity and operational verification.

---

# 7. Release States

```text
UPLOADED
VALIDATED
DRAFT
PUBLISHED
DEPRECATED
ARCHIVED
```

---

# 8. Uploaded

The binary exists but has not yet completed validation.

---

# 9. Validated

The artifact has passed:

```text
file validation
size calculation
hash calculation
storage verification
device-type compatibility checks
```

---

# 10. Draft

Release metadata can be reviewed and edited.

It is not available to devices.

---

# 11. Published

A published release becomes eligible for OTA.

Publishing should be an explicit administrative action.

---

# 12. Deprecated

The release remains historically valid but should no longer be selected as the normal latest release.

Existing pinned devices may still use it if policy permits.

---

# 13. Archived

The release is retained for historical purposes but is not normally eligible for new OTA delivery.

---

# 14. Immutability

After publication, the following are immutable:

```text
version
artifact
size
md5
sha256
storage key
```

Release notes may be restricted or also made immutable depending on audit requirements.

---

# 15. Firmware Validation

Before publication:

```text
Device type exists and is active
Version matches numeric MAJOR.MINOR.PATCH and is under 16 characters
Filename passes the safe-filename allowlist
len(code) + len(platform) + len(version) <= 40
Artifact exists
Artifact size > 0 and <= 4 MiB
MD5 computed and exactly 32 hexadecimal characters
SHA-256 computed
Composed download URL is under 96 characters
Manifest JSON parses with the exact firmware keys
Manifest version / md5 / size equal the release and the computed artifact values
Manifest url equals the composed canonical URL byte for byte
Manifest signature present, Base64-decodable, DER-encoded ECDSA
Manifest signature verifies with the production OTA public key
Serialized manifest fits the current 1024-byte firmware JSON budget
Manifest signature is structurally valid (Base64 of a DER ECDSA signature)
If this deployment configures the firmware public key, the signature verifies
MinIO objects written at the derived keys and verified readable
```

Rejection codes are enumerated in `docs/16-update-path-and-publication.md` §8.

## 15.1 Who verifies the signature

The signing key belongs to the **release pipeline**, not to this service: the
pipeline produces the manifest and its signature from the binary, and the
platform stores those exact bytes and serves them verbatim. The platform never
re-signs a release it did not author, so it does not need the signing private
key and, in the normal deployment, does not need the public key either.

`docs/OtaManager.md` requires the public key on the **device** only: the device
holds it embedded and performs the verification (§§6–7). The server contract
(§10) requires only that both endpoints validate the device headers and return
the manifest keys and an exact `Content-Length` — it does not require the server
to verify the manifest signature.

Server-side verification is therefore an **optional deployment guard**, useful
for catching a manifest signed by the wrong key or environment before it reaches
the fleet. When a deployment supplies the firmware public key, verification
becomes mandatory and a failing signature is rejected; when it does not, the
manifest is stored with `signature_verified = false`, the difference between
"not independently checked" and "invalid" is preserved, and the audit trail
shows it. The field verification that actually matters is a real device
installing a real release (see `docs/13-deployment.md` production acceptance criteria).

---

# 16. Release Compatibility

A release belongs to a specific device type.

The backend must not offer:

```text
ESP32-S3 Controller firmware
```

to a device belonging to an incompatible device type.

Because the device type determines the URL path, this is enforced twice: the
release selector is scoped to the authenticated device's device type, and the
requested path identity is compared against the authenticated device's own
`code` and `platform`. A mismatch is a no-offer response (`404`), not an
entitlement denial (`403`) and never a redirect.

A new product family or a new platform is a new device type. The firmware
catalog automatically has its own URL prefix, its own storage prefix, and its
own release list without any code change.

---

# 17. Firmware Download

The firmware endpoint shall:

1. authenticate the device
2. verify device eligibility (path identity, status, OTA flag)
3. locate the release by exact version and filename, and it must be `PUBLISHED`
   or `DEPRECATED`
4. locate artifact
5. open MinIO object
6. return accurate Content-Length
7. stream binary

A `DEPRECATED` release is deliberately still deliverable by exact version: it is
no longer selected as the normal latest release, but a device that already holds
a signed manifest must be able to finish that download, and pinned devices may
still use it (§12). An `ARCHIVED` release is not delivered. The object length is
checked against the signed `size` before any byte is written, and a disagreement
is a `5xx` (retryable) rather than a served binary the device would reject.

---

# 18. No Direct Public Bucket Access

The MinIO bucket should not be publicly writable.

Firmware delivery occurs through the controlled, authenticated application
route. MinIO remains private and is not a device-facing endpoint.

---

# 19. Future Storage Optimization

Possible future extensions include:

```text
signed object URLs
CDN
edge delivery
regional storage
```

without changing the logical FirmwareRelease domain.

---

# 20. Release Operations

Dashboard operations:

```text
Upload (binary + manifest)
Validate
Create Draft
Publish
Deprecate
Archive
View Metadata
View Artifact
View Served Manifest (exact bytes)
View Derived URLs and Storage Keys
```

Deleting a published firmware artifact should not be permitted as a normal operation.

Every release view should show the manifest that will actually be served, the
composed download URL, and the object keys, because those three are what the
device will act on and what an operator must verify when diagnosing a fleet
that is not updating.
