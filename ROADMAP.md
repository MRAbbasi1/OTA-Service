# Product Roadmap

This document outlines the current shipped capabilities and planned architectural milestones for **OTA-Service**.

---

## Current Release: v0.2.0-rc.3 (Shipped)

- [x] **Zero-Trust Device Authentication**: Verification using provisioned serial, raw eFuse MAC, and SHA-256 hashed token.
- [x] **Signed Manifest Delivery**: Bounded size (< 1024 B), short URLs (< 96 chars), and DER ECDSA signature payload verification.
- [x] **Exact-Length Streaming**: Binary delivery direct from private MinIO storage with matching `Content-Length` and zero HTTP redirects.
- [x] **Deterministic Update Policy Engine**: Granular resolution cascade (`Device` > `Device Type` > `Global`), version pinning, SemVer ranges, and update freeze toggles.
- [x] **Admin RBAC & Session Management**: Argon2id password hashing, HttpOnly session cookies, double-submit CSRF, and 27 fine-grained capabilities.
- [x] **Audit Trail & Observability**: Immutable admin event logging and real-time device communication recording.
- [x] **High Test Assurance**: 500+ unit and integration tests with >95% code coverage.

---

## Planned Milestones

### v0.3.0 — Distributed Infrastructure & Telemetry
- [ ] **Distributed Rate Limiting**: Transition from in-memory token bucket to a Redis-backed distributed rate limiter to enable multi-replica API deployments.
- [ ] **Client Installation Confirmation Contract**: Optional post-reboot telemetry handshake allowing devices to confirm successful flash installation.
- [ ] **Extended Hardware Telemetry**: Support for recording battery levels, boot slot (OTA_0 vs. OTA_1), and Wi-Fi RSSI signals during update checks.

### v0.4.0 — Enterprise Fleet Operations
- [ ] **Device Groups & Geofenced Staged Rollouts**: Phased canary deployments based on device tags and geographic groups.
- [ ] **Mutual TLS (mTLS) Authentication**: Optional hardware-side client certificate validation for high-security industrial deployments.
- [ ] **Webhook Integrations**: Real-time HTTP callbacks for release publication events, fleet update completion thresholds, and security lockouts.
- [ ] **Automated S3/MinIO Lifecycle Policies**: Automatic archiving of deprecated artifacts to cold storage.
