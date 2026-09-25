# OTA Management Platform — Security Architecture

## 1. Security Objectives

The system must protect:

1. Device identity
2. Device credentials
3. Firmware authenticity
4. Firmware integrity
5. Administrative access
6. Firmware authorization
7. Audit history
8. Infrastructure credentials

---

# 2. Device Authentication

Current device authentication uses:

```text
Serial
Raw eFuse MAC
Device Token
```

All three values must be validated according to the current OTA contract.

---

# 3. Token Storage

Device tokens must not be stored as plaintext.

Recommended:

```text
token_hash
```

using a secure password/credential hashing mechanism appropriate for high-entropy tokens.

If token lookup requirements require deterministic indexing, the design must separate:

```text
token identifier
token hash
```

rather than storing the raw secret.

---

# 4. Token Rotation

Rotation shall:

1. generate a new token
2. securely store its hash
3. revoke the old token
4. create an audit event
5. expose the plaintext only during controlled issuance

---

# 5. Token Revocation

Revoked tokens must immediately fail authentication.

---

# 6. Admin Authentication

Administrative APIs require authenticated users.

Passwords must use a modern password hashing algorithm such as Argon2id.

Passwords must never be stored in plaintext.

## 6.1 Implemented design

```text
POST /api/v1/admin/auth/login     credentials -> session cookie + CSRF token
POST /api/v1/admin/auth/logout    clears the cookies; safe without a session
GET  /api/v1/admin/auth/me        the current account and its capabilities
POST /api/v1/admin/auth/password  self-service change, current password required
```

* **Passwords** are Argon2id (`argon2-cffi`, library defaults), at least 12
  characters. An unknown account still pays the hashing cost, and every failure
  reason returns the same `401 invalid_credentials`, so the endpoint cannot be
  used to discover which emails are registered. Login is limited to 5 attempts
  per 15 minutes per source address and submitted email. The shared host Nginx
  does not impose a low source-IP ceiling on the OTA virtual host, because
  legitimate devices and administrators may share NAT addresses.
* **Sessions** are short-lived HS256 JWTs (30 minutes by default) with `iss`,
  `aud`, `exp`, `iat`, and `sub` pinned. The token lives in an `HttpOnly`,
  `SameSite=Lax`, `Path=/` cookie that is `Secure` in production.
* **Revocation is immediate.** The account carries a `session_version`; a
  password change, a role change, or a deactivation increments it, and every
  request compares the token's value with the account's. A stolen cookie
  therefore stops working at the moment of the change rather than when it
expires.
* **CSRF** uses double submission: a random value in a readable cookie must match
  the `X-CSRF-Token` header on every unsafe method. `SameSite=Lax` is the first
  layer and the token is the second, which is what protects deployments that
  legitimately need `SameSite=None` later. A request that carries neither cookie
  is not checked, because it carries no credential for a cross-site page to ride
  on.
* **No default account exists.** The first `SUPER_ADMIN` comes from the audited,
  idempotent deployment command (`uv run ota-admin`) using one-time environment
  credentials, and the last active `SUPER_ADMIN` cannot be deactivated or
  demoted.
* **Rotation and reuse.** `ADMIN_JWT_SECRET` is required in production (at least
  32 random characters, and never the development default); rotating it ends
  every live session. Secrets never reach the logs: the audit trail records the
  attempted email on a failed login and never the password.

---

# 7. Authorization

Every administrative endpoint must enforce authorization.

Roles:

```text
SUPER_ADMIN
FLEET_MANAGER
FIRMWARE_MANAGER
VIEWER
```

Permissions should be capability-based internally.

## 7.1 Capabilities

Endpoints check a capability, never a role name; roles are only bundles of them
(`app/domain/rbac.py`).

| Capability | SUPER_ADMIN | FLEET_MANAGER | FIRMWARE_MANAGER | VIEWER |
| --- | --- | --- | --- | --- |
| `dashboard:read` | ✓ | ✓ | ✓ | ✓ |
| `devices:read` | ✓ | ✓ | ✓ | ✓ |
| `firmware:read` | ✓ | ✓ | ✓ | ✓ |
| `policies:read` | ✓ | ✓ | ✓ | ✓ |
| `devices:write` | ✓ | ✓ | | |
| `policies:write` | ✓ | ✓ | | |
| `firmware:write` | ✓ | | ✓ | |
| `audit:read` | ✓ | ✓ | ✓ | |
| `admins:manage` | ✓ | | | |

A denial is `403` with the capability that was required, and the attempt is
logged. Every administrative route takes one of these dependencies, so
authentication and authorization are not something a new endpoint can forget:
a route with no dependency is an unauthenticated route, and the security tests
enumerate the routes to prove none exists.

---

# 8. Firmware Authorization

Authentication alone does not mean firmware entitlement.

The backend must separately determine:

```text
Is this device allowed to receive this release?
```

This is the responsibility of the Update Decision / Policy subsystem.

---

# 9. HTTPS

Production OTA endpoints must use HTTPS.

Production administrative APIs must use HTTPS.

HTTP must not be used for production administrative authentication or firmware delivery.

---

# 10. Firmware Signature

Firmware authenticity must remain independent of transport security.

The manifest is signed using the OTA signing private key.

The device verifies the signature using its embedded public key.

The signing private key must:

- never be committed to Git
- never be stored in the Docker image
- never be exposed through the API
- never be logged

---

# 11. Signing Key Management

The signing key must be treated as a high-value production secret.

Recommended storage:

```text
Production secret store
```

For the initial deployment, environment-mounted secret material may be used if securely managed.

Key rotation must be designed before replacing the embedded firmware public key.

---

# 12. MinIO Security

MinIO must:

- use authentication
- use private buckets
- use TLS in production where applicable
- restrict application credentials
- prevent anonymous write access

The application should use a dedicated MinIO credential with only required permissions.

MinIO stays an internal component. The device-facing delivery hostname
(`cdn.ota-service.example`) is served by the application, not by the bucket: MinIO must
not be reachable from the Internet, and no object URL, pre-signed URL, or
redirect to object storage may ever be handed to a device. Firmware delivery
therefore always passes through device authentication and entitlement on the
application side, on both OTA endpoints.

---

# 13. Database Security

PostgreSQL credentials must be stored as secrets.

Application database user should not have unnecessary administrative privileges.

---

# 14. Rate Limiting

Rate limiting is required independently of the device-side lockout.

At minimum:

```text
Admin login
Device manifest
Device firmware
Admin API
```

should have appropriate limits.

## 14.1 Implemented limits

| Surface | Limit | Keyed by | Owner |
| --- | --- | --- | --- |
| Admin login | 5 / 15 min | source address + submitted email | FastAPI |
| Admin login (coarse) | 10 / min | source address | Nginx |
| Other admin API | 120 / min | authenticated administrator | FastAPI |
| Device manifest | 20 / 5 min | authenticated device | FastAPI |
| Device firmware | 10 / 15 min | authenticated device | FastAPI |
| All OTA endpoints | 60 / 5 min | source address | Nginx |

Administrative limits are keyed by the authenticated account, so a shared office
address is not a shared quota, and the login limit is keyed by address *and*
account, so one noisy address cannot lock an operator out of their own account.

Rate limiting should consider:

```text
IP
device
token
endpoint
```

Rate-limit responses use `429 Too Many Requests`, never 403. Initial limits
and the ownership split between Nginx and FastAPI are normative in
`docs/15-implementation-decisions.md`.

---

# 15. Device-Side Lockout Compatibility

The current device behavior:

```text
403 → immediate 24h device-side lockout
401 → three consecutive failures → 24h lockout
```

must be respected.

Therefore the backend must not use 403 for transient errors.

It must also not use 403 for OTA-disabled, policy-blocked, no-release, or
path-identity-mismatch decisions. Those cases use the documented 404 response.
403 is reserved for unknown/MAC-mismatched devices and known disabled or retired
devices, with the intentional lockout consequence documented to operators.

The path-identity case is a lockout *avoidance* decision, not a leniency
decision: entitlement is always decided against the authenticated device's own
device type, so a request naming another device type cannot obtain that type's
firmware, and returning 404 prevents a provisioning typo from silencing a device
for up to 24 hours.

---

# 16. Audit Logging

Security-sensitive operations must create audit records.

Examples:

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

Every mutating administrative action records the *actor* — the authenticated
administrator's email — so the trail answers "who", not merely "what". A failed
login records the attempted email as the actor and the failure reason, and never
the password. The trail is reachable only with `audit:read` and is read-only.

---

# 17. Secret Redaction

Logs must never contain:

```text
device token
admin password
signing private key
MinIO secret key
database password
JWT/session secret
```

---

# 18. Input Validation

The API must validate:

- serial format
- MAC format
- firmware version
- device type code and platform
- filenames
- object paths
- policy values
- uploaded files
- request sizes

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
patterns are specified in `docs/16-update-path-and-publication.md` §3.

---

# 19. Firmware Upload Security

Upload endpoints must enforce:

- authentication
- authorization
- file size limits
- allowed content type
- safe filenames
- checksum calculation
- temporary storage controls

Uploaded files must not be executed.

When a manifest is uploaded alongside the binary, it is treated as untrusted
input that must be **verified**, never as configuration:

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
is prohibited (`AGENTS.md` §6.2). The procedure is
`docs/16-update-path-and-publication.md` §8.

---

# 20. Supply Chain

CI should perform dependency security checks.

Production images should be built from pinned dependencies.

Container images should be scanned where practical.

---

# 21. Backup Security

Backups containing:

- PostgreSQL data
- firmware metadata
- audit data

must be protected.

Firmware artifacts require independent backup.

---

# 22. Security Principle

No single security mechanism is considered sufficient.

The OTA trust model is:

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
