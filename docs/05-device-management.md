# OTA Management Platform — Device Management

## 1. Purpose

Device Management provides lifecycle management for all physical Smart Controller devices registered in the OTA platform.

---

# 2. Device Lifecycle

Canonical lifecycle:

```text
PROVISIONED
    |
    v
ACTIVE
    |
    +----> DISABLED
    |
    +----> RETIRED
```

A device that has never been activated may remain:

```text
PROVISIONED
```

---

# 3. Device Registration

Required fields:

```text
Device Type
Serial Number
Raw eFuse MAC
Authentication Token
```

Choosing the device type is an OTA-relevant decision, not just a label: the
device type's `code` and `platform` are the two path segments of that device's
entire OTA surface, and they determine which release catalog the device can ever
see. The registration response must therefore expose the derived value the field
team writes into NVS:

```json
{
  "device_type": "bcs-controller-v1",
  "platform": "esp32-s3",
  "expected_manifest_url": "https://api.ota-service.example/api/v1/firmware/bcs-controller-v1/esp32-s3/manifest.json"
}
```

Serial numbers are canonical positive decimal strings. Raw eFuse MAC values
are persisted as uppercase `XX:XX:XX:XX:XX:XX`; request values are normalized
to that form before lookup.

Optional metadata:

```text
Hardware Revision
Installation Location
Customer/Site
Notes
```

Location/customer fields may be introduced only when required by the business domain.

---

# 4. Uniqueness

The following must be unique:

```text
serial_number
raw_efuse_mac
```

A single raw eFuse MAC must never belong to multiple active devices.

---

# 5. Authentication Credential

Device credentials must be independently managed.

The system must support:

```text
Create
Rotate
Revoke
Disable
Reactivate
```

Token plaintext must not be stored permanently.

---

# 6. Device Authentication Flow

```text
Request
  |
Read headers
  |
Serial lookup
  |
MAC validation
  |
Token verification
  |
Device state validation
  |
Authorized
```

Failure cases:

```text
Unknown device
→ 403

Known device + raw eFuse MAC mismatch
→ 403

Known device + invalid token
→ 401

Known device + DISABLED or RETIRED lifecycle state
→ 403

Authenticated ACTIVE device + requested path names another device type or platform
→ 404

Authenticated ACTIVE device + OTA disabled, policy blocked, or no eligible release
→ 404
```

The path case is a no-offer response rather than 403: entitlement is decided
against the device's own device type, so a wrong path cannot grant access, and
using 403 would silence a misconfigured device for up to 24 hours. See
`docs/16-update-path-and-publication.md` §10.3.

The exact matrix, including the 24-hour lockout consequence of 403 and the
404 no-offer cases for OTA-disabled devices and policy/release decisions, is
defined in `docs/15-implementation-decisions.md`.

---

# 7. Device State

The dashboard shall display:

```text
PROVISIONED
ACTIVE
DISABLED
RETIRED
```

and separately:

```text
OTA ENABLED
OTA DISABLED
```

Lifecycle is stored as `lifecycle_status`; OTA entitlement is stored separately
as `ota_enabled`. These concepts must not be collapsed into one boolean.

---

# 8. Firmware State

The device record may maintain a known firmware version only with its source
(`ADMIN_ASSERTED` or future `DEVICE_REPORTED`) and observation time.

However, the backend must distinguish:

```text
reported/current firmware
```

from:

```text
target firmware
```

and:

```text
server-offered firmware
```

A successfully served firmware download is recorded separately and must never
be treated as confirmation that the device installed or booted it.

---

# 9. Device Detail

Device detail should expose:

### Identity

```text
Serial
Raw eFuse MAC
Device Type
Hardware Revision
```

### Security

```text
Token status
Token creation
Last token use
Last rotation
```

### Firmware

```text
Known Version and Source
Last Download Served
Last Server-Observed Download Result
```

### OTA

```text
OTA Enabled
Applicable Policy
Last Manifest Check
Last Error
```

### Audit

```text
Registration
Changes
Token operations
Policy changes
Firmware operations
```

---

# 10. Device Deletion

Physical device deletion should not normally mean hard deletion.

Recommended behavior:

```text
RETIRE
```

This preserves:

* audit history
* update history
* identity history

Hard deletion should be restricted to exceptional administrative operations.

---

# 11. Device Import

Future functionality may support bulk device provisioning.

Possible sources:

```text
CSV
Factory provisioning system
Manufacturing API
```

This is not required for initial implementation but the domain should support it.

---

# 12. Device Search

The dashboard shall support search by:

```text
serial
raw eFuse MAC
device type
firmware version
status
```

---

# 13. Device Security Rules

The system must prevent:

* duplicate identity
* token reuse after revocation
* disabled device receiving firmware
* unauthorized device querying firmware
* accidental reassignment of an identity without audit

---

# 14. Provisioning Consideration

Factory provisioning must use the same raw eFuse MAC definition expected by `OtaManager`.

The provisioning workflow must explicitly distinguish:

```text
Raw eFuse MAC
```

from:

```text
Ethernet network MAC
```

This is a known integration boundary and must be tested during device onboarding.

The server cannot push or force an OTA with the current firmware contract.
Publishing makes a release available for the next device poll; a local device
operator may trigger `requestManualCheck()`.

The complete provisioning checklist — including the exact `OTA_ONLINE_URL`
pattern per device type and the failure mode of a wrong value — is
`docs/16-update-path-and-publication.md` §11. Device onboarding is only complete
when the device has successfully requested its manifest URL and the platform has
recorded the check.
