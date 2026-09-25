# OTA Management Platform — Testing Strategy

## 1. Testing Objectives

Testing must verify:

* business correctness
* OTA compatibility
* security
* storage integrity
* policy correctness
* production behavior

---

# 2. Test Layers

```text
Unit Tests
Integration Tests
API Tests
Contract Tests
Security Tests
End-to-End Tests
```

---

# 3. Unit Tests

Pure domain logic must be tested without HTTP or MinIO.

Highest priority:

```text
UpdateDecisionService
Policy resolution
Version comparison
Eligibility
Release selection
```

---

# 4. Policy Test Matrix

Test:

```text
No policy
Device policy
Device-type policy
Policy precedence
Policy priority
Pinned version
Version range
OTA disabled
Device disabled
No release
Already current
Downgrade blocked
Downgrade metadata cannot cause a current-firmware offer
```

---

# 5. Version Tests

Examples:

```text
2.9.0 < 2.10.0
2.7.0 == 2.7.0
2.7.1 > 2.7.0
```

Invalid versions must be rejected.

Test the exact accepted form (`MAJOR.MINOR.PATCH` numeric only) and reject
pre-release/build suffixes, a version of 16 or more characters, a firmware
URL of 96 or more characters, an artifact outside 1 byte–4 MiB, and a manifest
that exceeds the firmware JSON budget.

---

# 6. Device Authentication Tests

Test:

```text
Valid serial + MAC + token
Invalid serial
Invalid MAC
Invalid token
Disabled device
Revoked token
Expired token
```

Expected HTTP semantics must be verified.

---

# 7. OTA Contract Tests

The tests must validate the exact current device contract.

Test:

```text
Manifest JSON fields
Signature payload
Signature validity
Content-Length
Firmware URL
401
403
4xx
5xx
404 no-offer
429 rate limiting
no Transfer-Encoding: chunked
no Location/redirect on either OTA endpoint
```

---

# 7.1 Update Path Tests

The path model in `docs/16-update-path-and-publication.md` must be tested
directly, because every device URL is derived rather than stored.

Test:

```text
code/platform validation patterns and length limits
device-type creation rejected when len(code)+len(platform) > 29
composed download URL for the example device type equals the documented value
composed URL rejected at 96 characters (budget boundary, both sides)
URL uses the bare version in the signature payload and the v-prefixed version in the path
storage key equals the URL path after the host is stripped
requested path identity different from the device's own device type -> 404, no lockout
requested path platform different from the device's own platform -> 404, no lockout
traversal attempts in each path segment (.. %2e%2e, encoded slashes, absolute paths)
```

---

# 7.2 Publication Tests

Test the upload-and-publish gate:

```text
release created from binary + manifest records the parsed manifest fields
manifest version mismatch with the release version -> rejected
manifest md5 mismatch with the computed artifact hash -> rejected
manifest size mismatch with the computed artifact size -> rejected
manifest url differing from the composed URL -> rejected
missing signature -> rejected
non-DER or malformed signature -> rejected
artifact larger than 4 MiB -> rejected
empty artifact -> rejected
MinIO write failure compensates and leaves no partial metadata
database failure after an object write removes the written object
served manifest bytes equal the stored manifest bytes
object key equals the derived key for the release
```

---

# 8. Signature Tests

Given:

```text
version
url
md5
size
```

the generated signature must be verifiable using the public key expected by the firmware.

Where the pipeline signs and the platform only stores and serves the bytes, this
is guarded today by structural checks plus the fixed manifest documented in
`docs/OtaManager.md` §10 as a vector for the exact key set, the exact signed
payload format, and the DER signature encoding.

A fixed conformance vector built from the production `ota_public_key.h` is the
strongly recommended regression guard **before the backend generates signatures
itself**: it pins the curve and encoding to the firmware's expectations. Until
that exists, the authoritative proof of compatibility is a real device
installing a real release, which is a production acceptance test rather than a
unit test.

Any modification to:

```text
version
url
md5
size
```

must invalidate the signature.

---

# 9. MinIO Integration Tests

Test:

```text
upload
read
metadata
missing object
wrong object
delete/cleanup
```

Published artifact immutability must also be tested.

---

# 10. PostgreSQL Integration Tests

Test:

```text
constraints
unique serial
unique MAC
foreign keys
transactions
policy queries
pagination
```

---

# 11. API Tests

Use HTTP client tests against FastAPI.

Test:

```text
authentication
authorization
CRUD
validation
error handling
pagination
filtering
```

---

# 12. Security Tests

Test:

```text
unauthorized admin access
wrong role
invalid token
revoked token
rate limiting
path traversal
unsafe filename
oversized upload
secret leakage
```

---

# 13. Firmware Download Tests

Verify:

```text
Correct artifact resolved from the derived key
Correct Content-Length equal to the manifest size
Correct Content-Type
Correct authorization on the delivery hostname
No unauthorized firmware
No firmware served for a path naming another device type
No Transfer-Encoding: chunked through the Nginx integration path
No redirect through the Nginx integration path
Certificate chain terminating at the pinned Root CA in the integration environment
```

---

# 14. Failure Tests

Simulate:

```text
PostgreSQL unavailable
MinIO unavailable
MinIO object missing
Storage timeout
Invalid firmware
Corrupted artifact
Signing failure
```

---

# 15. End-to-End OTA Test

The most valuable integration test should reproduce:

```text
Device identity
      ↓
Manifest request
      ↓
Authentication
      ↓
Policy resolution
      ↓
Release selection
      ↓
Manifest generation
      ↓
Signature verification
      ↓
Firmware request
      ↓
Artifact streaming
```

---

# 16. Regression Testing

Any change to:

```text
manifest
device authentication
firmware routing
policy engine
signature generation
```

must trigger OTA contract tests.

Any change to device type identity, URL composition, storage key derivation, or
the publication gate is a contract change: it can invalidate provisioned device
URLs and signed manifests already in the field, so it must trigger the full OTA
contract and path test set.

---

# 17. Coverage

Coverage should be measured, but coverage percentage alone must not be treated as correctness.

Critical domain services should have comprehensive branch coverage.

---

# 18. CI Testing

Every pull request should run:

```text
lint
type check
unit tests
integration tests
security checks
```

Build/deployment tests should run before production deployment.
