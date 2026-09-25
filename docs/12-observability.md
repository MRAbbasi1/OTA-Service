# OTA Management Platform — Observability

## 1. Objectives

Administrators must be able to determine:

- Is the platform healthy?
- Are devices checking?
- Are devices receiving updates?
- Are authentication failures increasing?
- Are firmware downloads failing?
- Is MinIO healthy?
- Is PostgreSQL healthy?

---

# 2. Structured Logging

Application logs must be structured.

Recommended fields:

```text
timestamp
level
service
request_id
device_id
device_serial
release_id
action
status
duration_ms
error_code
```

Sensitive credentials must never be logged.

---

# 3. Request ID

Every API request should have a request ID.

The request ID should appear in:

- logs
- error responses
- audit correlation where useful

---

# 4. Device Events

Events emitted by the device-facing OTA surface:

| Event | Level | Fields | Meaning |
| --- | --- | --- | --- |
| `manifest_served` | INFO | device_id, device_serial, release_id, version, bytes | A signed manifest was returned for this device. |
| `manifest_no_offer` | INFO | device_id, device_serial, reason | No offer: path mismatch, inactive device, OTA disabled, no eligible release, or nothing newer than the known version. |
| `device_auth_failed` | INFO | code | `401`: a missing or invalid credential. |
| `device_auth_forbidden` | WARNING | code | `403`: unknown serial, MAC mismatch, or a disabled/retired device — the cases that lock the device out for 24 hours, so they are warnings. |
| `device_rate_limited` | WARNING | device_id, device_serial, surface | `429`; the poll was skipped, no lockout. |
| `firmware_download_served` | INFO | device_id, release_id, version, bytes | The complete expected response body was emitted. This is a delivery, not an installation. |
| `firmware_stream_failed` | ERROR | — | MinIO stream failed before the expected byte count was delivered; the client must reject the incomplete body. |
| `firmware_stream_size_mismatch` / `firmware_stream_exceeded_expected_size` | ERROR | expected, actual/emitted | The object-store stream disagreed with the published artifact length; completion is not recorded. |
| `firmware_download_completion_record_failed` | ERROR | device_id, release_id | The full response was emitted but its completion event could not be persisted; this must be reconciled operationally. |
| `firmware_not_entitled` | INFO | device_id, device_serial, reason | The device passed authentication but is not entitled to the binary. |
| `firmware_release_not_found` | INFO | device_id, version, filename | The requested release is not deliverable for this device type. |
| `ota_transient_failure` | ERROR | code, device_id | A `5xx` was returned so the device retries: storage read failed, an object was missing, or a stored length disagreed with the signed size. |
| `storage_read_failed` | ERROR | storage_key, device_id | MinIO read failure for a manifest or artifact. |
| `stored_artifact_size_mismatch` | ERROR | release_id, expected, actual | The stored object no longer matches the signed size; the bytes were refused rather than served. |
| `published_release_has_no_manifest` / `published_release_has_no_artifact` | ERROR | release_id | Data-integrity defect on a published release. |
| `published_release_has_invalid_version` | ERROR | release_id | A published release whose version the device could not compare; it is skipped as a candidate instead of breaking every poll. |
| `firmware_blocked_by_policy` | INFO | device_id, device_serial, version | The governing policy excludes this version, so the bytes were withheld. |
| `policy_conflict_at_decision_time` | ERROR | device_id, scope | Two equal-priority policies at one scope reached a decision. It fails closed (`404 POLICY_BLOCKED`); the configuration is only supposed to get here if rows were written outside the admin API. |
| `audit_event` | INFO | action, resource_type | An administrative change was recorded, including every policy and administrator change. |
| `admin_login_failed` | WARNING | address | A credential attempt failed. The attempted email is in the audit trail; the password is nowhere. |
| `admin_login_rate_limited` | WARNING | address | The login limit was hit; the attempt never reached password verification. |
| `admin_session_rejected` | INFO | code, path | A session cookie was missing, expired, tampered with, or revoked. Deliberately not a warning: an expired tab is not an incident. |
| `admin_csrf_rejected` | WARNING | path, method | An unsafe request carried cookies but no matching CSRF header. Worth noticing: it is either a broken client or a forged request. |
| `admin_capability_denied` | WARNING | admin_id, role, capability, path | An authenticated administrator attempted something their role does not grant. |
| `admin_rate_limited` | WARNING | admin_id | The per-administrator API limit was hit. |
| `admin_bootstrapped` | INFO | admin_id | The deployment command created the first administrator. |

`firmware_download_served` means that the server completed its response
stream. It must not be labelled as installed, updated, or booted without a
future authenticated device report.

Response bodies never carry these reasons on `401`/`403`; the detail is in the
log only, so the endpoint cannot be used to enumerate enrolled serial numbers.
On `404`, `429`, and `503` the same reason is additionally exposed in the
non-contractual `X-OTA-Reason` response header, which describes only the caller
itself and is what a field engineer sees with a single `curl`.

---

# 5. Firmware Events

Track:

```text
uploaded
manifest_parsed
manifest_rejected
validated
published
deprecated
archived
```

A rejected manifest (version/md5/size/url mismatch, or a failed signature) is a
security-relevant event, not a form-validation error: it means the platform was
handed a pairing that no device could verify, or an attempt to publish a URL the
backend did not compose.

---

# 6. Update Metrics

The system should eventually expose:

```text
manifest_requests_total
manifest_auth_failures_total
manifest_no_offer_total            # labelled by reason
manifest_path_mismatch_total       # provisioning defects in the field
firmware_downloads_total
firmware_download_failures_total
updates_by_version
devices_by_known_firmware_version
publish_rejections_total           # labelled by rejection code
```

`manifest_path_mismatch_total` deserves its own series: a mismatch is never a
fleet threat, it is always a provisioning or configuration defect, and it should
page a human rather than be buried in an authorization-failure count.

---

# 7. Device Health

Dashboard health categories may be derived from:

```text
last_manifest_check_at
```

Example:

```text
Recently checked
Stale
Very stale
Never checked
```

Thresholds must be configurable.

---

# 8. Health Endpoints

Recommended:

```http
GET /health/live
GET /health/ready
```

Liveness checks application process health.

Readiness checks required dependencies.

---

# 9. Dependency Health

Readiness may verify:

```text
PostgreSQL
MinIO
```

For capacity monitoring, correlate active and waiting PostgreSQL connections
with the configured per-process pool maximum
(`DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW`), API request latency, MinIO
stream errors, process memory, and firmware bytes served. A production capacity
claim requires a representative concurrent-device/download load test; unit
tests do not establish throughput.

The exact behavior must avoid causing cascading failures.

---

# 10. Audit vs Application Logs

These are different.

Application logs describe runtime behavior.

Audit logs describe security-sensitive administrative actions.

Audit logs should be durable and append-only.

---

# 11. Error Classification

Errors should be classified:

```text
AUTHENTICATION
AUTHORIZATION
VALIDATION
STORAGE
DATABASE
NETWORK
FIRMWARE
INTERNAL
```

---

# 12. Dashboard Operational Reporting

Dashboard should eventually provide:

```text
Device count
Firmware distribution
Recent checks
Recent failures
Update success/failure
Authentication failures
Stale devices
```

Firmware distribution must label the version source and must not present
server-served downloads as installed fleet state. The server also cannot push
or force OTA; freshness is determined by device polling.

The fleet view should also surface provisioning defects, because they are
invisible in firmware-state reporting:

```text
devices whose configured OTA_ONLINE_URL does not match the derived manifest URL
fleet split by device type code / platform, since that is the OTA path dimension
devices that have never successfully checked in
```

A device with a wrong provisioned URL reports no error the platform can see; it
simply never updates. That makes it a first-class "requires attention" category,
not a log line.

---

# 13. Future Observability

Future enhancements may add:

```text
Prometheus
Grafana
OpenTelemetry
centralized log aggregation
alerting
```

These are not required for initial deployment.
