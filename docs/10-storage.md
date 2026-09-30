# Storage Architecture

This document describes how the platform stores data and why the storage layer is
shaped the way it is: what each system is responsible for, how object keys are
derived, what may never be overwritten, and where the transaction boundaries are.
Deployment configuration for these systems is in `docs/13-deployment.md`, and the
commands used to operate them are in `docs/18-operations.md`.

## Storage Components

The platform uses two primary persistence systems:

```text
PostgreSQL   structured relational data and the audit record
MinIO        firmware binaries and their signed manifests
```

Neither system is device-facing. Devices reach firmware only through the
application, which authenticates the device before emitting a byte.

## PostgreSQL

PostgreSQL stores structured relational data:

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

It is the system of record for identity, entitlement, and history, and it is the
only place where a firmware release's lifecycle state is decided. A firmware
object existing in MinIO does not by itself mean the release may be offered;
publication is a database state change.

Operationally the deployment pins `postgres:18` and keeps its data on the
`ota-postgres-data` volume, mounted at `/var/lib/postgresql`. The mount point is
a data-integrity property rather than a preference: on major version 18 the
cluster is created in a version-specific subdirectory of that path, so mounting
`/var/lib/postgresql/data` places the cluster outside the volume and a container
recreation loses every table. The volume layout, health probe, and connection
budget are specified in "PostgreSQL" in `docs/13-deployment.md`.

## MinIO

MinIO stores binary firmware artifacts and their signed manifests. It is an
internal infrastructure component: the API is the only client and validates
MinIO's certificate against a deployment-local CA (see "MinIO Responsibility" in
`docs/03-architecture.md`).

Example layout:

```text
ota-firmware/
  firmware/
    bcs-controller-v1/
      esp32-s3/
        v2.6.1/
          Controller.ino.bin
          manifest.json
```

The deployment keeps MinIO on loopback-only ports, serves it with TLS from an
internal CA, and provides liveness probing from the host rather than from inside
the container. Image selection, `--certs-dir`, certificate permissions, and the
external liveness probe are specified in "MinIO" in `docs/13-deployment.md`.

## Object Key

The object key mirrors the public URL path exactly, so a logged download URL can
be turned back into a key and vice versa.

```text
firmware/{device_type}/{platform}/v{version}/{filename}
firmware/{device_type}/{platform}/v{version}/manifest.json
```

Object keys must be deterministic and safe. Each segment is a validated
parameter (`device_types.code`, `device_types.platform`, the release version, the
safe filename), never unvalidated request data. The `v` prefix belongs to the key
and the URL only, never to the manifest `version` field.

The stored `manifest.json` is the exact published byte sequence. The served
response must be byte-identical to it, so this object is the audit record of what
devices were actually offered. See "Storage Layout" in
`docs/16-update-path-and-publication.md`.

## Bucket Policy

The production firmware bucket must not be publicly writable, anonymous access
must be disabled, and application credentials must hold only the permissions the
application needs.

No object URL, pre-signed URL, or redirect to object storage may ever be handed
to a device. Firmware delivery always passes through device authentication and
entitlement on the application side, on both OTA endpoints.

## Metadata

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

Because size and checksum are recorded for every object, an object backup can be
verified against the database rather than trusted by inspection.

## Artifact Immutability

Published artifacts must not be overwritten. If a binary changes, a new release
must be created.

The same applies to a published manifest: its bytes are covered by the signature
the device verifies, so overwriting the object would silently change what the
fleet is offered without changing the version. Re-publishing the same version is
therefore never an update path; a new version is.

## Upload Flow

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

An uploaded manifest is treated as untrusted input: it is parsed strictly and
verified against the computed artifact hash, size, and the URL the backend
composed, and its signature must verify. The procedure is in "Publication
Procedure" in `docs/16-update-path-and-publication.md`.

## Transaction Boundary

PostgreSQL transactions and MinIO operations are not one atomic transaction, so
the implementation must handle partial failures. For example:

```text
MinIO upload succeeds
PostgreSQL commit fails
```

The system must provide cleanup or reconciliation behavior for that case: an
object that no release row references is unreferenced storage, never a served
artifact.

## Download Flow

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

## Content-Length

The download layer must expose the exact artifact size. The response must not
rely on chunked transfer encoding for the current device implementation, and the
proxy must not buffer or re-frame the body (see "Host Nginx" in
`docs/13-deployment.md`).

## Backup Strategy

Database and object-storage backups are separate concerns, verified separately,
and both are required: a database dump restores metadata, and firmware objects
come only from the object backup.

The policy is:

```text
scheduled backups with alerting on failure
retention policy (daily + monthly)
tested restore process, rehearsed at least quarterly
backups encrypted at rest, copied off the host, never committed
```

A PostgreSQL dump contains device-token hashes and the audit history, so it is
sensitive material and must be protected accordingly. Firmware objects are
immutable once published, which makes incremental object backups and checksum
verification straightforward: object size and checksum are already recorded in
PostgreSQL.

Exact backup, verification, and restore commands are in "Database Operations" and
"Object Storage Operations" in `docs/18-operations.md`.

## Development

Local development uses MinIO through Docker, with the same S3-compatible
abstraction as production, so the storage code path exercised in tests matches
the one that runs in a deployment.

## Future Evolution

Possible future optimizations:

```text
real CDN in front of cdn.ota-service.example (proxy mode, identity forwarding)
Signed URLs
Object replication
Regional buckets
Dedicated download service
```

These must not leak into the domain layer. `cdn.ota-service.example` is already a
delivery hostname of this application; it is not object storage and not a static
file server. Any future CDN must forward the three device headers and must not
redirect, because the device client does not follow redirects and must be
authenticated before a firmware byte is emitted.
