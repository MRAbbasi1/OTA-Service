# OTA Management Platform — Production Deployment

## 1. Deployment Components

Production components:

```text
existing host Nginx
OTA-Service API container
PostgreSQL (managed separately)
MinIO (managed separately)
```

OTA-Service does not claim the shared server's public HTTP/HTTPS ports. The
existing host Nginx remains the only listener on ports 80 and 443 and routes
only the OTA virtual hosts to the API's loopback-bound port.

---

# 2. Network

Recommended:

```text
devices / administrators
          │ HTTPS
          ▼
existing host Nginx (ports 80/443; virtual-host routing)
          │ loopback HTTP :18080
          ▼
OTA-Service API container
          ├── PostgreSQL
          └── private MinIO
```

The OTA virtual host is an additive Nginx site configuration, not a second
public proxy. It must coexist with other websites and management panels without
declaring a `default_server`, taking over a wildcard hostname, or binding new
public ports. The application port is published as
`127.0.0.1:${OTA_BIND_PORT:-18080}:8000`; it is not reachable directly from the
public network. Set `OTA_BIND_PORT` only when the chosen loopback port conflicts
with another local service.

Both OTA hostnames route to the same FastAPI instance:

```text
api.ota-service.example   manifest retrieval and the administrative API
cdn.ota-service.example   firmware binary delivery
```

`deploy/nginx/ota-service.conf.example` is the host-Nginx virtual-host template.
Replace its example hostnames and certificate paths with the values provisioned
for the deployment, validate it with the host's Nginx version, and enable it
using that server's existing site-management process. Do not replace the global
Nginx configuration or enable a second Nginx container.

The template does not create an HTTP listener: the server's existing port-80
configuration and certificate-renewal flow remain authoritative. OTA devices
must call the final HTTPS URLs directly; OTA paths must not be redirected.

`cdn.ota-service.example` is a delivery hostname of this application, not a
static file server or MinIO endpoint. Firmware is authenticated on that
hostname exactly as on the manifest hostname.

PostgreSQL and MinIO should not be directly exposed to the public Internet.

---

# 3. TLS

HTTPS is mandatory for production, on both public hostnames.

Certificates should be managed independently from application secrets.

**CA constraint:** the device pins a single Root CA (currently ISRG Root X1,
Let's Encrypt). A certificate chain that does not terminate at that root cannot
be changed from the server side — it requires a new firmware release already in
the field. Therefore:

```text
both hostnames must use certificates issued by the pinned CA
no other CA may be introduced without a firmware review
certificate renewal and chain changes are firmware-affecting events
```

Both OTA endpoints reject redirects. A TLS-terminating proxy or CDN that
redirects, or that rewrites the host, breaks the firmware contract rather than
degrading gracefully.

---

# 4. Host Nginx

The shared host Nginx owns public ingress and retains all unrelated virtual
hosts:

```text
TLS termination for api.ota-service.example and cdn.ota-service.example
virtual-host routing to the loopback-bound API
Proxy request/response limits
Security headers
Upstream timeouts
```

The existing host's port-80 policy and certificate-renewal mechanism remain
authoritative. OTA devices must use the final HTTPS URLs directly; the OTA
routes themselves must never redirect.

OTA-specific requirements:

- `client_max_body_size` stays small; OTA GETs carry no body.
- The firmware route must not buffer or re-frame the response body, so the exact
  `Content-Length` reaches the device and chunked encoding is never introduced.
- Firmware responses are streamed by the application and `proxy_buffering` is
  disabled in the OTA virtual host.
- Do not configure an IP-wide request limit for all OTA traffic at Nginx. The
  application limits authenticated device identities; many devices can share
  one carrier/NAT address.
- No `return 301/302` and no host rewrite on either OTA route.

---

# 5. FastAPI

FastAPI should run as a non-root container user where practical.

The container should be immutable.

Runtime configuration comes from environment/secrets.

---

# 6. PostgreSQL and connection capacity

Production PostgreSQL is managed independently of the OTA application
container. Use PostgreSQL major version 18 to match CI's migration and
integration-test service, and pin the production major version so a deployment
cannot silently cross a major upgrade. PostgreSQL provides its own shared
buffer cache (`shared_buffers`) and also benefits from the operating system's
filesystem page cache. `effective_cache_size` informs the query planner; it
does not allocate memory. Do not add Redis or a result cache merely to claim
that caching is enabled.

Tune PostgreSQL to the actual host or container memory budget, not a copied
generic configuration. As an initial sizing reference on a dedicated database
host, PostgreSQL commonly starts with `shared_buffers` near 25% of RAM and
`effective_cache_size` near 50–75% of RAM available to PostgreSQL and the OS.
Reassess both under representative load. Keep `work_mem` conservative because
it can be consumed by multiple operations per query and concurrent sessions.
Do not set these values without accounting for co-located websites, VPN
services, and other processes on the shared host.

The API uses a bounded SQLAlchemy pool with pre-ping. Its configured maximum
database connections per application process is:

```text
DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW
```

With the initial defaults this is 15 connections per process. The total
PostgreSQL budget must include every API process, migrations, administration,
monitoring, and other applications on the same server. Keep one API replica
until rate limiting is moved to shared state; do not scale out by increasing
Uvicorn workers without recalculating the database connection budget.

PostgreSQL also requires:

```text
persistent volume
backup
restore procedure
credential management
connection budget and monitoring
```

---

# 7. MinIO

MinIO requires:

```text
persistent storage
private bucket
credentials
backup
health checks
```

---

# 8. Docker Compose

The production Compose file manages only the OTA API container. Host Nginx and
the persistent PostgreSQL and MinIO services remain independently managed:

```text
compose.production.yaml
```

The separately managed PostgreSQL and MinIO endpoints must be reachable from
the API container and must not be exposed to the public Internet. On a
single-host Docker installation, the production Compose file provides the
`host.docker.internal` host-gateway alias. When those services run on the host,
bind them to a private interface reachable from the Docker bridge and use host
firewall rules to deny public access. Use `host.docker.internal` rather than
`localhost` in the container's `DATABASE_URL` and `MINIO_ENDPOINT`. If storage
is remote, configure its private network address and TLS.

---

# 9. Environment Variables

Production configuration should include:

```text
DATABASE_URL
DATABASE_POOL_SIZE
DATABASE_MAX_OVERFLOW
DATABASE_POOL_TIMEOUT_SECONDS
MINIO_ENDPOINT
MINIO_ACCESS_KEY
MINIO_SECRET_KEY
MINIO_BUCKET
OTA_MANIFEST_BASE_URL      # https://api.ota-service.example
OTA_FIRMWARE_BASE_URL      # https://cdn.ota-service.example
ADMIN_JWT_SECRET
```

The backend does not hold the firmware signing private key. Firmware releases
arrive with a signed manifest, which the device verifies using its embedded
public key.

The two OTA base URLs are composition inputs for device-facing URLs. They must
match the hostnames actually served, and they must match the value provisioned
on devices as `OTA_ONLINE_URL`; a mismatch produces nothing but 404s from the
field.

---

# 10. Database Migrations

Alembic migrations must run in a controlled deployment stage.

Application startup should not blindly run destructive migrations.

---

# 11. Deployment Flow

```text
GitHub
   |
Pull Request
   |
Tests
   |
Build
   |
Container Image
   |
Registry
   |
Production Deployment
   |
Migration
   |
Health Check
   |
Traffic
```

---

# 12. Rollback

Application deployment must support rollback to a previous image.

Database migrations must be designed to minimize destructive rollback requirements.

---

# 13. Firmware Rollback

Firmware rollback is separate from backend application rollback.

Publishing a firmware release does not modify already-installed device firmware.

---

# 14. Backup Strategy

PostgreSQL:

```text
scheduled backup
retention
restore verification
```

MinIO:

```text
firmware object backup
retention
restore verification
```

---

# 15. Production Secrets

Secrets must not exist in:

```text
Git repository
Dockerfile
Docker image
application source
logs
```

---

# 16. Production Readiness

Before production:

```text
TLS verified on api.ota-service.example and cdn.ota-service.example
Certificate chain terminates at the pinned Root CA (ISRG Root X1)
Database backup verified
MinIO backup verified
Firmware signing and device-side verification confirmed
Device authentication tested
Path derivation verified against a real device's provisioned OTA_ONLINE_URL
OTA contract tested
OTA responses verified to have an exact Content-Length, no Transfer-Encoding: chunked, and no Location header
Rate limits enabled
Audit enabled
Health checks enabled
Rollback tested
API port confirmed loopback-only
Host Nginx virtual host validated without changing unrelated sites
Application database connection budget reconciled with PostgreSQL max_connections
Firmware download load test passed with bounded application memory
Manifest bytes served on api.ota-service.example are byte-identical to the stored manifest
Firmware bytes served on cdn.ota-service.example match the manifest md5 and size
Real ESP32-S3 end-to-end OTA test passed
```

The URL, path, and publication model that these checks verify is
`docs/16-update-path-and-publication.md`.
