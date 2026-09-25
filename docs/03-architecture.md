# OTA Management Platform — System Architecture

## 1. Architectural Style

The initial implementation shall be a modular monolith.

The backend is a single FastAPI application with clear internal boundaries.

```text
                    +------------------+
                    |    Dashboard     |
                    +--------+---------+
                             |
                         HTTPS
                             |
                    +--------v---------+
                    |      Nginx       |
                    +--------+---------+
                             |
                    +--------v---------+
                    |     FastAPI      |
                    |                  |
                    | Admin API        |
                    | OTA API          |
                    | Device Auth      |
                    | Policy Engine    |
                    | Firmware Service |
                    | Audit Service    |
                    +----+--------+----+
                         |        |
                  +------v--+   +v------+
                  |Postgres |   | MinIO  |
                  +---------+   +--------+
                                  |
                              Firmware
                               Binary
```

---

# 2. Application Boundaries

The FastAPI application contains:

```text
API Layer
Application Services
Domain Logic
Repositories
Infrastructure
```

---

# 3. API Layer

Responsibilities:

- HTTP routing
- request validation
- authentication
- authorization
- response serialization
- HTTP-specific error handling

The API layer must not implement update eligibility logic.

---

# 4. Application Service Layer

Responsibilities:

- orchestrate use cases
- coordinate repositories
- coordinate storage
- execute domain operations
- create audit records

Examples:

```text
DeviceService
FirmwareService
ArtifactService
PolicyService
UpdateDecisionService
ManifestService
AuditService
```

---

# 5. Repository Layer

Repositories abstract PostgreSQL persistence.

Examples:

```text
DeviceRepository
DeviceTokenRepository
FirmwareReleaseRepository
FirmwareArtifactRepository
UpdatePolicyRepository
UpdateAttemptRepository
AuditRepository
```

Repositories should not contain business decisions.

---

# 6. Storage Layer

MinIO access shall be isolated behind an object-storage abstraction.

Example:

```text
ObjectStorage
    |
    +-- MinIOObjectStorage
```

The application should not spread MinIO SDK calls throughout business logic.

---

# 7. Firmware Storage

The MinIO object layout mirrors the public URL path:

```text
firmware/
  <device-type>/
    <platform>/
      v<version>/
        <filename>
        manifest.json
```

Example:

```text
firmware/
  bcs-controller-v1/
    esp32-s3/
      v2.6.1/
        Controller.ino.bin
        manifest.json
```

The actual bucket name is deployment configuration and must not be hardcoded.
The `<device-type>` and `<platform>` segments are the device type's `code` and
`platform`, which are the same two values that appear in the OTA URL.

---

# 7.1 Public Hostnames

Two hostnames front the same application:

```text
api.ota-service.example   manifest retrieval + administrative API
cdn.ota-service.example   firmware binary delivery
```

`cdn.ota-service.example` is a delivery hostname of this application — not a static file
server, not object storage, and never MinIO. Browsing firmware is a
compatibility and decoupling decision, not a hosting decision: both hostnames
terminate on the same Nginx and the same FastAPI process, and the firmware
endpoint authenticates the device before a byte is emitted. This keeps the door
open for a future caching CDN in front of the delivery hostname without
changing anything a device knows.

Neither OTA endpoint may redirect, because the device client does not follow
`3xx` responses. The normative URL model is
`docs/16-update-path-and-publication.md`.

---

# 8. PostgreSQL Responsibility

PostgreSQL stores:

```text
devices
device_tokens
device_types
firmware_releases
firmware_artifacts
update_policies
update_attempts
audit_events
admin_users
```

PostgreSQL does not store firmware binary contents.

---

# 9. MinIO Responsibility

MinIO stores immutable firmware binaries.

MinIO should not become the source of truth for business metadata.

PostgreSQL remains the source of truth for:

- which release exists
- whether it is published
- which device type it belongs to
- which artifact belongs to it
- whether it is eligible

---

# 10. OTA Request Flow

```text
Device
  |
  | GET /api/v1/firmware/{device_type}/{platform}/manifest.json
  | X-Device-Serial
  | X-Device-Mac
  | X-Device-Token
  |
  v
Nginx (api.ota-service.example)
  |
  v
FastAPI
  |
  +--> Authenticate Device
  |
  +--> Resolve Device
  |
  +--> Verify path device type / platform == device's own
  |
  +--> Resolve Policy
  |
  +--> Resolve Eligible Release (scoped to the device's device type)
  |
  +--> Serve the stored, signed manifest verbatim
  |
  +--> Return JSON
  |
  v
Device
```

Step ordering matters: the path identity check is part of authorization, not
routing. A request naming a device type other than the authenticated device's
own is a no-offer response, never a manifest for another type.

---

# 11. Firmware Download Flow

```text
Device
   |
   | GET /firmware/{device_type}/{platform}/v{version}/{filename}
   |
   v
Nginx (cdn.ota-service.example)
   |
   v
FastAPI
   |
   +--> Authenticate Device
   |
   +--> Verify path identity and entitlement
   |
   +--> Resolve release and artifact by derived key
   |
   +--> Open MinIO object
   |
   +--> Stream binary with exact Content-Length
   |
   v
Device
```

The initial implementation must keep firmware routing through the backend so
that device authorization remains aligned with the existing `OtaManager`
contract: the device sends its credentials on the firmware GET as well, and the
contract requires the server to validate them there.

Future optimization may introduce a caching CDN in front of the delivery host or
a dedicated delivery layer — but never an unauthenticated object URL and never a
redirect, because neither is supported by the device client.

---

# 12. Security Boundaries

There are three distinct security domains:

### Administrator

```text
Admin
 ↓
Admin Authentication
 ↓
RBAC
 ↓
Admin API
```

### Device

```text
Device
 ↓
Device Authentication
 ↓
OTA Authorization
 ↓
Manifest / Firmware
```

### Firmware Artifact

```text
Artifact
 ↓
MinIO
 ↓
Manifest Integrity
 ↓
Device ECDSA Verification
```

---

# 13. Firmware Trust Model

The backend controls:

```text
version
url
md5
size
signature
```

The device independently verifies the manifest signature using its embedded public key.

Therefore:

```text
Transport Security
+
Device Authentication
+
Manifest Signature
+
Firmware Integrity
```

provide different security properties.

The backend must not remove the manifest signature merely because HTTPS is used.

---

# 14. Reverse Proxy

The existing host Nginx is responsible for:

- TLS termination for both `api.ota-service.example` and `cdn.ota-service.example`
- virtual-host routing to the API's loopback-only port
- connection and request handling
- forwarding client metadata
- reverse proxying to FastAPI without buffering firmware responses

The FastAPI application remains responsible for business-level authentication and authorization.

The host Nginx must not answer an OTA request with a redirect or transform its
response into chunked transfer encoding. The firmware location disables proxy
buffering and caching, preserving the exact `Content-Length`. Application-level
rate limits are keyed by authenticated device/admin identity; a low shared
source-IP limit must not penalize devices behind the same carrier NAT. See
`docs/13-deployment.md` for the shared-host setup.

---

# 15. Container Architecture

Production Docker deployment:

```text
existing host Nginx
    |
    +-- OTA-Service API (loopback-only port)
    +-- PostgreSQL (managed separately)
    +-- private MinIO (managed separately)
```

Compose manages the API container only. PostgreSQL and MinIO remain separately
managed persistent services and are not exposed to the public Internet.

---

# 16. Environment Separation

At minimum:

```text
development
testing
production
```

Each environment must have separate:

- database
- MinIO bucket
- credentials
- signing configuration
- secrets

---

# 17. Configuration

Configuration shall come from environment variables or a secure secret mechanism.

Examples:

```text
DATABASE_URL
MINIO_ENDPOINT
MINIO_ACCESS_KEY
MINIO_SECRET_KEY
MINIO_BUCKET
OTA_MANIFEST_BASE_URL      # https://api.ota-service.example — manifest route + admin API
OTA_FIRMWARE_BASE_URL      # https://cdn.ota-service.example — firmware delivery host
ADMIN_JWT_SECRET
```

The backend does not hold the firmware signing private key. The release
pipeline signs manifests before upload; devices verify them with the
firmware-embedded public key.

The two base URLs are separate on purpose: the manifest host is provisioned on
devices and is hard to change in the field, while the firmware delivery host is
expected to evolve (for example to sit behind a caching CDN). Composing both
URLs from configuration keeps that change a deployment concern instead of a
code change.

Secrets must never be committed to Git.

---

# 18. Scalability

The application must remain stateless at the HTTP layer wherever possible.

Persistent state belongs in:

```text
PostgreSQL
MinIO
```

This permits future horizontal scaling:

```text
             Nginx
                |
       +--------+--------+
       |        |        |
     API-1    API-2    API-3
       |        |        |
       +--------+--------+
                |
        PostgreSQL / MinIO
```

The initial deployment uses one API process. Horizontal scaling requires
shared rate-limit state and a revised database connection budget.

---

# 19. Future Extensions

The architecture shall allow future:

- Redis
- background workers
- CDN
- signed download URLs
- staged rollout
- device groups
- notifications
- metrics
- centralized logging

without changing the core device identity model.
