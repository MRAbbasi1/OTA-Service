# OTA Management Platform — Storage Architecture

## 1. Storage Components

The platform uses two primary persistence systems:

```text
PostgreSQL
MinIO
```

---

# 2. PostgreSQL

PostgreSQL stores structured relational data.

```text
Devices
Device Types
Device Tokens
Firmware Releases
Firmware Artifacts
Update Policies
Update Attempts
Audit Events
Admin Users
```

---

# 3. MinIO

MinIO stores binary firmware artifacts and their signed manifests.

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

# 4. Object Key

The object key mirrors the public URL path exactly, so a logged download URL can
be turned back into a key and vice versa.

```text
firmware/{device_type}/{platform}/v{version}/{filename}
firmware/{device_type}/{platform}/v{version}/manifest.json
```

Object keys must be deterministic and safe. Each segment is a validated
parameter (`device_types.code`, `device_types.platform`, the release version,
the safe filename), never unvalidated request data. The `v` prefix belongs to
the key and the URL only, never to the manifest `version` field.

The stored `manifest.json` is the exact published byte sequence; the served
response must be byte-identical to it, so the manifest column of storage is the
audit record of what devices were actually offered. See
`docs/16-update-path-and-publication.md` §6.

---

# 5. Bucket Policy

The production firmware bucket must not be publicly writable.

Anonymous access should be disabled.

Application credentials must have only required permissions.

---

# 6. Metadata

PostgreSQL artifact record:

```text
storage_bucket
storage_key
filename
content_type
size
md5
sha256
```

PostgreSQL manifest record:

```text
release_id
artifact_id
version
url
md5
size
signature
storage_key
manifest_sha256
signature_source
validated_at
```

The manifest's `url`, `md5`, and `size` are replicated facts from the artifact
and the device type, but they are recorded because they are the values under
signature: a mismatch between the stored manifest and the stored artifact would
mean the platform is serving bytes that no device can verify.

---

# 7. Artifact Immutability

Published artifacts must not be overwritten.

If a binary changes, a new release must be created.

The same applies to a published manifest: its bytes are covered by the
signature the device verifies, so overwriting the object would silently change
what the fleet is offered without changing the version. Re-publishing the same
version is therefore never an update path — a new version is.

---

# 8. Upload Flow

```text
Client
  |
  v
FastAPI
  |
  +--> validate upload
  |
  +--> calculate hashes
  |
  +--> upload to MinIO
  |
  +--> verify object
  |
  +--> commit metadata
```

---

# 9. Transaction Boundary

PostgreSQL transactions and MinIO operations are not one atomic transaction.

The implementation must therefore handle partial failures.

Example:

```text
MinIO upload succeeds
PostgreSQL commit fails
```

The system must provide cleanup/reconciliation behavior.

---

# 10. Download Flow

```text
Device
  |
Nginx (cdn.ota-service.example)
  |
FastAPI
  |
Authenticate (three device headers)
  |
Authorize (own device type, path identity, entitlement)
  |
Resolve Artifact by derived key
  |
MinIO
  |
Stream (exact Content-Length, no chunked, no redirect)
  |
Device
```

---

# 11. Content-Length

The download layer must expose the exact artifact size.

The response must not rely on chunked transfer encoding for the current device implementation.

---

# 12. Backup

PostgreSQL:

- scheduled database backups
- retention policy
- tested restore process

MinIO:

- object backup/replication
- retention policy

Database and object storage backups must be considered separately.

---

# 13. Development

Local development should use MinIO through Docker.

The application should use the same S3-compatible abstraction in development and production.

---

# 14. Future

Possible future optimizations:

```text
real CDN in front of cdn.ota-service.example (proxy mode, identity forwarding)
Signed URLs
Object replication
Regional buckets
Dedicated download service
```

These should not leak into the domain layer. Note that `cdn.ota-service.example` is
already a delivery hostname of this application; it is not object storage and
not a static file server. Any future CDN must forward the three device headers
and must not redirect, because the device client does not follow redirects and
must be authenticated before a firmware byte is emitted.
