# OTA Management Platform — CI/CD

## 1. Objective

GitHub Actions shall automate quality validation, container builds and production deployment.

---

# 2. Pull Request Pipeline

Every pull request should run:

```text
Install dependencies
      ↓
Lint
      ↓
Type Check
      ↓
Unit Tests
      ↓
Integration Tests
      ↓
Security Checks
```

---

# 3. Linting

Use a consistent Python linting/formatting toolchain.

Recommended:

```text
Ruff
```

---

# 4. Type Checking

Recommended:

```text
Mypy
```

The project should progressively increase type coverage.

---

# 5. Testing

Run:

```text
pytest
```

including:

```text
unit
integration
API
OTA contract
```

---

# 6. Coverage

Generate coverage reports.

Critical domain services should maintain strong coverage.

---

# 7. Dependency Security

CI should run dependency vulnerability checks.

---

# 8. Docker Build

Successful validation should build the production image.

Image tags should be immutable where practical.

Example:

```text
ota-backend:<git-sha>
```

---

# 9. Registry

The production image should be pushed to a private container registry.

---

# 10. Deployment

Production deployment should:

```text
pull image
validate configuration
run migrations
restart/update API
check readiness
verify health
```

---

# 11. Deployment Failure

If readiness fails:

```text
Deployment must be considered failed.
```

The previous healthy application version should remain recoverable.

---

# 12. Database Migration Rules

Migrations must be:

```text
reviewed
versioned
tested
backward-aware
```

Destructive schema changes must be isolated into separately reviewed and
controlled migrations.

---

# 13. Firmware Signing

Firmware manifest signing keys must never be available to pull-request jobs unless explicitly required.

Production signing credentials must be restricted to the release/deployment environment.

---

# 14. CI Secrets

Production secrets must be stored in GitHub Actions secrets/environment protection or an external secret manager.

---

# 15. Branch Protection

Production changes should require:

```text
Pull Request
CI success
review
```

---

# 16. Release Process

Recommended:

```text
merge
  ↓
build
  ↓
test
  ↓
deploy
  ↓
health check
```

Firmware publication remains an explicit administrative operation inside the OTA platform.

## 16.1 Repository implementation

The repository implements this pipeline in:

* `.github/workflows/ci.yml`: pull-request and `main` quality gate, PostgreSQL
  and MinIO integration services, coverage threshold, migration drift check,
  dependency audit, and production-image smoke build.
* `.github/workflows/deploy-production.yml`: protected-environment release
  deployment. It publishes an immutable Git-SHA image to GHCR, scans it with
  Trivy, copies only the deployment bundle to the target host, runs migrations,
  waits for readiness, and rolls back to the previous image if readiness fails.

The `production` GitHub environment must require reviewers. It requires these
secrets:

```text
PRODUCTION_SSH_HOST
PRODUCTION_SSH_USER
PRODUCTION_SSH_KEY
PRODUCTION_KNOWN_HOSTS
PRODUCTION_DEPLOY_PATH
PRODUCTION_READINESS_URL
```

The target host must already contain the protected runtime `.env` at
`/opt/ota-service/.env` (or set `OTA_ENV_FILE` in the host environment),
Docker Engine, and persistent PostgreSQL and MinIO services reachable from the
API container. The API is published only on `127.0.0.1:18080`; the existing
host Nginx retains public ports 80/443 and must have its OTA virtual host
configured separately from other sites. Use
`deploy/nginx/ota-service.conf.example` as an additive site template; the
workflow does not replace or reload the server's global Nginx configuration.
The workflow never stores or copies runtime secrets or TLS certificates.

CI/CD deployment of the backend must not automatically publish firmware.

---

# 17. Future Improvements

Possible future capabilities:

```text
staging environment
automatic rollback
deployment approvals
container scanning
SBOM
signed container images
```
