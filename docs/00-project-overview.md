# OTA Management Platform — Project Overview

## 1. Purpose

OTA-Service is a backend and management system for securely managing firmware
updates for connected devices.

The platform provides:

- Device fleet management
- Device identity and authentication management
- Firmware artifact management
- Firmware release management
- Firmware eligibility and update policy management
- OTA manifest generation
- Secure firmware delivery
- Update and device status tracking
- Administrative dashboard APIs
- Audit logging
- Security controls
- Production deployment infrastructure

The platform is designed around the existing ESP32-S3 `OtaManager` implementation and must preserve its current OTA wire contract unless an explicit firmware-side protocol change is introduced.

---

## 2. Current Device OTA Contract

The existing device periodically requests a firmware manifest over Ethernet,
from the absolute URL provisioned on it as `OTA_ONLINE_URL`. The platform's
canonical URL structure is:

```text
manifest:  https://api.ota-service.example/api/v1/firmware/{device_type}/{platform}/manifest.json
firmware:  https://cdn.ota-service.example/firmware/{device_type}/{platform}/v{version}/{filename}
```

Both hostnames are served by this application; `cdn.ota-service.example` is a delivery
hostname, not object storage, and firmware is authenticated per request. Neither
endpoint may redirect. The path segment `{platform}` and the device type code are
derived from the device's device type, so each product family and platform has
its own release catalog, URL prefix, and storage prefix without any code change.
The normative model is `docs/16-update-path-and-publication.md`.

Each request contains:

- `X-Device-Serial`
- `X-Device-Mac`
- `X-Device-Token`

The backend authenticates the device using these values.

The device expects a manifest containing exactly:

```json
{
  "version": "2.7.0",
  "url": "https://example.com/firmware/...",
  "md5": "...",
  "size": 1234567,
  "signature": "..."
}
```

The manifest signature is generated over:

```text
<version>|<url>|<md5>|<size>
```

The device verifies this signature using its embedded OTA public key.

The firmware binary must be served with an accurate `Content-Length` header.

Chunked transfer encoding is not supported by the current device implementation.

---

## 3. Architectural Principle

OTA-Service is a modular monolith. API routes, domain decisions, application
services, database access, and object storage are separated in code while
remaining one deployable application. This keeps operational overhead low
without placing policy or authentication logic in HTTP handlers.

In production, the existing host Nginx owns public ports 80 and 443. An
additive virtual host routes the OTA hostnames to the API container on a
loopback-only port. PostgreSQL and MinIO are private infrastructure services;
the application is the only path through which devices can access firmware.
The production deployment details are in `docs/13-deployment.md`.

---

## 4. Technology Stack

### Backend

- Python
- FastAPI
- Pydantic
- SQLAlchemy
- Alembic

### Database

- PostgreSQL

### Object Storage

- MinIO
- S3-compatible API

### Reverse Proxy

- Nginx

### Containerization

- Docker
- Docker Compose for initial deployment

### CI/CD

- GitHub Actions

### Testing

- pytest
- pytest-asyncio
- HTTPX
- integration tests
- database tests
- object-storage tests
- OTA contract tests

---

## 5. Storage Responsibility

PostgreSQL stores structured metadata.

MinIO stores binary firmware artifacts.

PostgreSQL:

- devices
- device types
- tokens
- firmware releases
- artifact metadata
- update policies
- update attempts
- audit events

MinIO:

- firmware binaries
- immutable firmware artifacts
- future firmware-related binary assets

Firmware binaries must never be stored inside PostgreSQL.

---

## 6. Deployment Model

The initial production deployment is expected to use:

```text
Internet clients
      |
Existing host Nginx (80/443)
      |
127.0.0.1:18080
      |
OTA-Service API
   |        |
PostgreSQL  Private MinIO
```

The host Nginx is responsible for:

- TLS termination
- routing only the OTA virtual hosts to the API loopback port
- streaming-safe proxying for firmware downloads
- security headers
- request and connection handling

FastAPI is responsible for:

- authentication
- authorization
- business logic
- manifest generation
- policy resolution
- firmware metadata
- audit logging
- MinIO interaction
- device authentication before manifest or firmware delivery

PostgreSQL is responsible for durable relational state.

MinIO is responsible for private firmware binary and manifest storage. Binary
downloads are streamed in bounded chunks through the API; they are not served
directly by Nginx or MinIO.

---

## 7. Design Goals

The platform must be:

### Secure

Firmware authenticity, device authentication, administrator authentication, authorization, token protection and auditability are first-class requirements.

### Deterministic

Given the same device state, firmware catalog and policies, the update decision must be deterministic.

### Observable

Administrators must be able to determine:

- how many devices exist
- what firmware versions are deployed
- which devices are active
- when devices last checked
- what firmware is eligible
- why an update was or was not offered
- whether a download/update operation failed

### Maintainable

The architecture must remain understandable to a small engineering team.

### Extensible

The initial implementation must leave a clean path toward:

- staged rollout
- device groups
- fleet segmentation
- richer policies
- RBAC
- notifications
- advanced telemetry
- CDN/object-storage optimization
- multiple hardware revisions
- multiple device families

---

## 8. Initial Deployment Scope

The initial deployment intentionally does not include:

- Kubernetes
- microservices
- Kafka
- distributed event buses
- complex CQRS
- distributed workflow engines
- multi-region deployment
- CDN integration
- automated percentage rollout

The domain model should not prevent these capabilities from being introduced later.

---

## 9. Primary Users

### System Administrator

Manages the entire platform.

### Fleet Manager

Manages devices, device state and update policies.

### Firmware Manager

Uploads and manages firmware releases.

### Viewer

Read-only access to fleet and firmware information.

---

## 10. Core Domain

The initial domain consists of:

```text
DeviceType
Device
DeviceToken
FirmwareRelease
FirmwareArtifact
UpdatePolicy
UpdateAttempt
AuditEvent
AdminUser
```

The most important business service is:

```text
UpdateDecisionService
```

It determines whether a specific device is eligible for an update and, if so, which firmware release should be offered.

The firmware catalog is organized by device type and platform, and a release is
published together with its signed manifest, which is parsed, validated against
the binary, and stored alongside it at a key that mirrors the download URL.

---

## 11. Core Principle for Future Development

The backend must not embed update eligibility logic directly inside HTTP endpoint handlers.

The OTA endpoint should orchestrate:

```text
Authenticate Device
        |
Resolve Device
        |
Resolve Applicable Policy
        |
Resolve Eligible Releases
        |
Select Target Release
        |
Generate Manifest
        |
Return Manifest
```

The decision itself belongs to the domain/service layer.

This allows the same update decision logic to be used by:

- OTA requests
- dashboard previews
- fleet reports
- future rollout systems
- automated deployment jobs

The current device OTA request does not report its running firmware version or
post-reboot installation status. Server-observed downloads must therefore be
represented separately from an administratively known or future
device-reported firmware version. The resolved compatibility decisions are in
`docs/15-implementation-decisions.md`.
