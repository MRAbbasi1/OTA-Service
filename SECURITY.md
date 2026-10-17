# Security Policy

## Supported Versions

Only the latest active minor release receives security updates.

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2.0 | :x:                |

---

## Reporting a Vulnerability

The security of device fleets and firmware delivery infrastructure is of utmost importance.

If you discover a security vulnerability or sensitive information exposure in **OTA-Service**, please **do not open a public issue**. Instead, follow responsible disclosure:

1. Send an encrypted email to the maintainers or use GitHub's private vulnerability reporting feature:
   - **Email**: `security@example.com`
2. Include the following details in your report:
   - Type of vulnerability (e.g. authentication bypass, cryptographic defect, injection, denial of service).
   - Step-by-step reproduction instructions or Proof-of-Concept (PoC).
   - Potential impact on connected device fleets or administrative accounts.
   - Any suggested mitigations or patches.

### Our Commitment:
- We will acknowledge receipt of your vulnerability report within **48 hours**.
- We will provide regular updates as we investigate and validate the issue.
- Once a fix is verified, a patched release and coordinated disclosure advisory will be issued.

---

## Security Architecture Principles

- **Zero-Trust Device Identity**: Dual-gate authentication binding hardware serial, raw factory eFuse MAC, and a SHA-256 hashed secret token.
- **Cryptographic Firmware Signatures**: ECDSA SECP256R1 signatures computed over canonical release metadata.
- **Private Object Storage**: MinIO blob storage is strictly bound to internal networks and is never publicly addressable.
- **Admin Defense**: Argon2id password hashing, HttpOnly session cookies, double-submit CSRF, and granular 27-capability RBAC.
