# Security Architecture

This document defines the security model of the platform: what must be protected,
which control answers which threat, and where each control is enforced. It is the
reference for security-relevant design decisions.
`docs/17-security-verification.md` records what the test suite and a production
deployment actually verify, and `docs/13-deployment.md` specifies the host-level
configuration a deployment must provide.

## Security Objectives

The system must protect:

1. Device identity
2. Device credentials
3. Firmware authenticity
4. Firmware integrity
5. Administrative access
6. Firmware authorization
7. Audit history
8. Infrastructure credentials

## Threat Model

| Threat                                               | Control                                                                                                                                                                                                              |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Device impersonation (stolen or guessed credentials) | Three-value device authentication: serial, raw eFuse MAC, and per-device token; tokens stored only as hashes                                                                                                         |
| Unauthorized firmware download                       | Device authentication plus entitlement decided against the authenticated device's own device type, on both OTA hostnames; MinIO is never publicly reachable and no object URL or redirect is ever handed to a device |
| Malicious or corrupted firmware in the field         | Signed manifest verified on the device against an embedded public key, independent of transport security; exact `Content-Length` and MD5 verification                                                                |
| Serving arbitrary firmware URLs                      | The backend composes every firmware URL from validated parameters and rejects an uploaded manifest whose `url` differs                                                                                               |
| Administrative account takeover                      | Argon2id passwords, short-lived sessions with immediate server-side revocation, CSRF double submission, login throttling by address and email                                                                        |
| Privilege escalation between administrators          | Capability checks on every administrative route; roles are bundles of capabilities                                                                                                                                   |
| Denial of service by a hostile client                | Per-surface rate limits keyed by authenticated identity, plus the device's own lockout behavior, which dictates which status codes are permissible                                                                   |
| Credential or secret disclosure through logs         | Explicit redaction requirements and audit records that never contain secrets                                                                                                                                         |
| Path traversal or object-key manipulation            | Allowlist validation of every path segment before it is used for a lookup or for key construction                                                                                                                    |
| Audit repudiation                                    | Every mutating administrative action records its actor and is read-only for operators                                                                                                                                |
| Infrastructure compromise from a container           | Loopback-only publication, non-root containers, read-only filesystem, and firewall integration for Docker's iptables chains                                                                                          |

## Device Authentication

Device authentication uses three values:

```text
Serial
Raw eFuse MAC
Device Token
```

All three must be validated according to the current OTA contract. The raw
eFuse-derived MAC is a first-class identity value: it is not the Ethernet network
MAC and must not be replaced by it.

## Token Storage

Device tokens must not be stored as plaintext. The stored value is a hash,
produced with a credential-hashing mechanism appropriate for high-entropy tokens.
Where lookup requirements call for deterministic indexing, the design separates a
token identifier from the token hash rather than storing the raw secret.

## Token Rotation

Rotation shall:

1. generate a new token
2. securely store its hash
3. revoke the old token
4. create an audit event
5. expose the plaintext only during controlled issuance

## Token Revocation

Revoked tokens must fail authentication immediately.

## Administrator Authentication

Administrative APIs require authenticated users. Passwords must use a modern
password hashing algorithm such as Argon2id and must never be stored in
plaintext.

### Implemented design

```text
POST /api/v1/admin/auth/login     credentials -> session cookie + CSRF token
POST /api/v1/admin/auth/logout    clears the cookies; safe without a session
GET  /api/v1/admin/auth/me        the current account and its capabilities
POST /api/v1/admin/auth/password  self-service change, current password required
```

- Passwords are Argon2id (`argon2-cffi`, library defaults), at least 12
  characters. An unknown account still pays the hashing cost, and every failure
  reason returns the same `401 invalid_credentials`, so the endpoint cannot be
  used to discover which emails are registered. Login is limited to 5 attempts
  per 15 minutes per source address and submitted email.
- Sessions are short-lived HS256 JWTs (30 minutes by default) with `iss`, `aud`,
  `exp`, `iat`, and `sub` pinned. The token lives in an `HttpOnly`,
  `SameSite=Lax`, `Path=/` cookie that is `Secure` in production.
- Revocation is immediate. The account carries a `session_version`; a password
  change, a role change, or a deactivation increments it, and every request
  compares the token's value with the account's. A stolen cookie therefore stops
  working at the moment of the change rather than when it expires.
- CSRF uses double submission: a random value in a readable cookie must match the
  `X-CSRF-Token` header on every unsafe method. `SameSite=Lax` is the first layer
  and the token is the second, which is what protects deployments that
  legitimately need `SameSite=None` later. A request that carries neither cookie
  is not checked, because it carries no credential for a cross-site page to ride
  on.
- No default account exists. The first `SUPER_ADMIN` comes from the audited,
  idempotent deployment command (`ota-admin`) using one-time environment
  credentials, and the last active `SUPER_ADMIN` cannot be deactivated or
  demoted.
- `ADMIN_JWT_SECRET` is required in production (at least 32 random characters,
  and never the development default); rotating it ends every live session.
  Secrets never reach the logs: the audit trail records the attempted email on a
  failed login and never the password.

## Authorization

Every administrative endpoint must enforce authorization. Roles:

```text
SUPER_ADMIN
FLEET_MANAGER
FIRMWARE_MANAGER
VIEWER
```

Permissions are capability-based internally.

### Capabilities

Endpoints check a capability, never a role name; roles are only bundles of them
(`app/domain/rbac.py`).

| Capability       | SUPER_ADMIN | FLEET_MANAGER | FIRMWARE_MANAGER | VIEWER |
| ---------------- | ----------- | ------------- | ---------------- | ------ |
| `dashboard:read` | ✓           | ✓             | ✓                | ✓      |
| `devices:read`   | ✓           | ✓             | ✓                | ✓      |
| `firmware:read`  | ✓           | ✓             | ✓                | ✓      |
| `policies:read`  | ✓           | ✓             | ✓                | ✓      |
| `devices:write`  | ✓           | ✓             |                  |        |
| `policies:write` | ✓           | ✓             |                  |        |
| `firmware:write` | ✓           |               | ✓                |        |
| `audit:read`     | ✓           | ✓             | ✓                |        |
| `admins:manage`  | ✓           |               |                  |        |

A denial is `403` with the capability that was required, and the attempt is
logged. Every administrative route takes one of these dependencies, so
authentication and authorization are not something a new endpoint can forget: a
route with no dependency is an unauthenticated route, and the security tests
enumerate every registered `/api/v1/admin/` route, excluding the intentionally
public login and the CSRF-protected logout routes, to prove none exists.

## Firmware Authorization

Authentication alone does not mean firmware entitlement. The backend must
separately determine whether a given device is allowed to receive a given
release. This is the responsibility of the update-decision and policy subsystem,
and it is always decided against the authenticated device's own device type.

## Transport Security

Production OTA endpoints and production administrative APIs must use HTTPS. HTTP
must not be used for production administrative authentication or firmware
delivery.

The production API port is bound to loopback and accepts traffic only through the
host Nginx virtual host. Nginx overwrites `X-Real-IP` and `X-Forwarded-For` with
its direct client address, and Uvicorn does not trust arbitrary forwarded
headers. The application uses the validated `X-Real-IP` value only within this
loopback-only proxy boundary, for login rate limiting. Do not expose the API port
directly, and do not change the proxy configuration to append or pass through
client-provided header values.

Production Nginx adds HSTS, `X-Content-Type-Options`, `X-Frame-Options`, and
`Referrer-Policy` headers, including on error responses. FastAPI does not enable
CORS; the API does not grant browser origins access via
`Access-Control-Allow-Origin`. CORS is a browser response-sharing policy, not
server-side authentication and not a substitute for CSRF protection.

The device pins a single Root CA. A certificate chain that does not terminate at
that root cannot be changed from the server side, so certificate selection is a
firmware-affecting decision; see "TLS and Certificates" in
`docs/13-deployment.md`.

## Firmware Signature

Firmware authenticity must remain independent of transport security. The manifest
is signed with the OTA signing private key, and the device verifies the signature
using its embedded public key.

The signing private key must:

- never be committed to Git
- never be stored in the Docker image
- never be exposed through the API
- never be logged

## Signing Key Management

The signing key is a high-value production secret and belongs in a production
secret store. For the initial deployment, environment-mounted secret material may
be used if it is managed securely. Key rotation must be designed before replacing
the embedded firmware public key, because the public key ships inside firmware
already in the field.

## MinIO Security

MinIO must use authentication and private buckets, be TLS-protected wherever it
crosses a network boundary, restrict application credentials to what the
application needs, and prevent anonymous write access. The application uses a
dedicated MinIO credential.

MinIO stays an internal component. The device-facing delivery hostname
(`cdn.ota-service.example`) is served by the application, not by the bucket:
MinIO must not be reachable from the Internet, and no object URL, pre-signed URL,
or redirect to object storage may ever be handed to a device. Firmware delivery
therefore always passes through device authentication and entitlement on the
application side, on both OTA endpoints.

## Database Security

PostgreSQL credentials must be stored as secrets. The application database user
must not hold administrative privileges it does not need.

## Rate Limiting

Rate limiting is required independently of the device-side lockout. At minimum it
applies to administrative login, device manifest requests, device firmware
requests, and the administrative API.

### Implemented limits

| Surface         | Limit       | Keyed by                         | Owner   |
| --------------- | ----------- | -------------------------------- | ------- |
| Admin login     | 5 / 15 min  | source address + submitted email | FastAPI |
| Other admin API | 120 / min   | authenticated administrator      | FastAPI |
| Device manifest | 20 / 5 min  | authenticated device             | FastAPI |
| Device firmware | 10 / 15 min | authenticated device             | FastAPI |

Administrative limits are keyed by the authenticated account, so a shared office
address is not a shared quota. Login throttling is keyed by source address and
submitted email, so one noisy address cannot lock an operator out of their own
account. The source address is read from the `X-Real-IP` value overwritten by the
loopback-only production Nginx proxy; the application ignores `X-Forwarded-For`.

Limits are process-local, and the production deployment runs one API worker and
one replica. Adding workers or replicas requires shared rate-limit state before
scaling; an in-memory quota is not cluster-wide.

Rate-limit responses use `429 Too Many Requests`, never `403`. Initial limits are
normative in `docs/15-implementation-decisions.md`. Host Nginx must not add a low
IP-wide limit to OTA delivery, because devices can share NAT addresses.

## Device-Side Lockout Compatibility

The current device behavior is:

```text
403 → immediate 24h device-side lockout
401 → three consecutive failures → 24h lockout
```

The backend must therefore not use `403` for transient errors. It must also not
use `403` for OTA-disabled, policy-blocked, no-release, or path-identity-mismatch
decisions; those cases use the documented `404` response. `403` is reserved for
unknown or MAC-mismatched devices and for known disabled or retired devices, with
the intentional lockout consequence documented to operators.

The path-identity case is a lockout-avoidance decision, not a leniency decision:
entitlement is always decided against the authenticated device's own device type,
so a request naming another device type cannot obtain that type's firmware, and
returning `404` prevents a provisioning typo from silencing a device for up to
24 hours.

## Audit Logging

Security-sensitive operations must create audit records:

```text
admin_login
admin_login_failed
device_created
device_disabled
token_rotated
token_revoked
firmware_uploaded
firmware_published
firmware_archived
policy_changed
```

Implemented administrative actions:

```text
admin_bootstrapped        admin_created          admin_role_changed
admin_activated           admin_deactivated      admin_password_changed
admin_login               admin_login_failed     admin_logout
```

Every mutating administrative action records the actor, the authenticated
administrator's email, so the trail answers "who", not merely "what". A failed
login records the attempted email as the actor and the failure reason, and never
the password. The trail is reachable only with `audit:read` and is read-only.

## Secret Redaction

Logs must never contain:

```text
device token
admin password
signing private key
MinIO secret key
database password
JWT/session secret
```

## Input Validation

The API must validate serial format, MAC format, firmware version, device type
code and platform, filenames, object paths, policy values, uploaded files, and
request sizes.

Firmware publication validation must additionally enforce the device parser's
version, URL, size, MD5, and manifest-size bounds.

Path traversal must be explicitly prevented on every path-derived surface:

```text
OTA request path segments ({device_type}, {platform}, {version}, {filename})
MinIO object keys, which are built from those same segments
```

Each path segment must match its allowlist pattern before it is used for a
database lookup or for object-key construction. Keys must never be assembled by
concatenating raw request data, and a segment must never be allowed to contain
`/`, `\`, `..`, a leading dot, whitespace, or percent-encoded characters. The
patterns are specified in "Path Parameter Model" in
`docs/16-update-path-and-publication.md`.

## Firmware Upload Security

Upload endpoints must enforce authentication, authorization, file size limits,
allowed content type, safe filenames, checksum calculation, and temporary storage
controls. Uploaded files must not be executed.

When a manifest is uploaded alongside the binary, it is treated as untrusted
input that must be verified, never as configuration:

```text
parse it strictly (exact firmware keys, no extras, bounded size)
require manifest.version == release version
require manifest.md5    == computed artifact MD5
require manifest.size   == computed artifact size
require manifest.url    == the URL composed by the backend
require the signature to be present, DER-encoded ECDSA, and verifiable
```

An uploaded manifest can never cause the platform to serve, sign, or advertise a
URL that the backend did not compose. Accepting a client-supplied URL into a
signed payload would be equivalent to accepting an arbitrary firmware URL, which
the platform's security rules prohibit. The procedure is in "Publication
Procedure" in `docs/16-update-path-and-publication.md`.

## Supply Chain

CI performs dependency security checks, production images are built from pinned
dependencies, and container images are scanned where practical. The pipeline is
described in `docs/14-ci-cd.md`.

## Backup Security

Backups containing PostgreSQL data, firmware metadata, and audit data must be
protected, and firmware artifacts require an independent backup. Both halves of a
recovery are sensitive: a database dump contains device-token hashes and the
audit history, and object backups contain the firmware the fleet runs. See
"Backup Strategy" in `docs/10-storage.md`.

## Internal Trust Material

MinIO is reached by the API over TLS with a deployment-local certificate
authority. The permissions on this material are security-relevant:

```text
certs/authority/ota-minio-ca.crt   0644   must be readable by the non-root API user
certs/authority/ota-minio-ca.key   0600   CA private key — never world-readable
certs/minio/private.key            0600   MinIO server key
certs/minio/public.crt             0644
```

The CA certificate is deliberately world-readable: the API container runs as
uid 10001 and validates MinIO against it, so a `0600` CA breaks
`/health/ready` with a permission error. Only certificates are readable; both
private keys stay `0600`, and the `authority/` directory itself is `0755` so the
unprivileged user can traverse it. The generator script that creates this
material and the exact modes are specified in "MinIO" in
`docs/13-deployment.md`.

Trust material is generated once per host, must never be committed, and is never
exposed to devices.

## Host Network Security

Docker publishes container ports through the `DOCKER`/`DOCKER-USER` iptables
chains, which are evaluated before UFW's rules. A container published on
`0.0.0.0` therefore bypasses UFW entirely, and `ufw deny` does not apply to it.
Two controls close that gap:

```text
every published port in compose.production.yaml binds 127.0.0.1 only
ufw-docker integrates Docker's chains with UFW for anything else
```

The rule set, the `ufw-docker` installation, and the ban handling for CI runners
are specified once in "Firewall" in `docs/13-deployment.md`. The commands an
operator uses to verify the firewall, ban state, and SSH keys are in "Security
Hygiene" in `docs/18-operations.md`.

## Security Principle

No single security mechanism is considered sufficient. The OTA trust model is:

```text
HTTPS
+
Device Authentication
+
Authorization Policy
+
Manifest Signature
+
Firmware Integrity
+
Audit
+
Rate Limiting
```
