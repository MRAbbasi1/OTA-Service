# OTA Management Platform — CI/CD

## 1. Pipeline overview

One workflow owns the full path:

```text
.github/workflows/ci-cd.yml
```

```text
Pull Request
  quality (lint → typecheck → migrate → test → audit)
       ↓
  build (Docker API image, local smoke — no registry push)

Push to main / workflow_dispatch on main
  quality
       ↓
  build + push immutable API image to GHCR (tag = git SHA)
       ↓
  deploy (Environment: production)
       → copy compose + deploy scripts + MinIO cert generator
       → ensure MinIO TLS material
       → start/wait postgres + minio
       → migrate → start api → readiness / API rollback
```

Concurrency: one run per ref. PR runs cancel older ones; `main` waits (no cancel mid-deploy).

Permissions are least-privilege: `contents: read` by default; `packages: write` only on build; `packages: read` on deploy.

---

## 2. Image tagging

Production API images are immutable:

```text
ghcr.io/<owner>/ota-service:<git-sha>
```

Postgres and MinIO use pinned public images from `compose.production.yaml`.
Rollback of a bad API release uses `.ota-previous-image` on the host; data
volumes are never deleted by deploy.

---

## 3. What the deploy job does

1. Copies `compose.production.yaml`, `production-deploy.sh`, MinIO cert script,
   Nginx example, and env example to the host path.
2. Logs the host into GHCR with the short-lived `GITHUB_TOKEN`, pulls the API
   SHA image, then logs out after the job.
3. Runs `production-deploy.sh`, which:
   - requires host `/opt/ota-service/.env`
   - generates MinIO TLS certs if missing (idempotent)
   - starts postgres + minio and waits until healthy
   - runs `alembic upgrade head`
   - starts/restarts api
   - waits for readiness; on failure rolls back the **API** image only
4. On failure, uploads remote diagnostics (all three services) as an artifact.

The workflow never writes runtime secrets into the host `.env`.

---

## 4. Bootstrap vs day-to-day

| Once (manual) | Every deploy (GitHub Actions) |
| ------------- | ----------------------------- |
| Install Docker Engine + Compose plugin | Quality gate |
| Create deploy user + SSH key | Build + push API image |
| Create `/opt/ota-service/.env` from `deploy/env.production.example` | Copy deploy bundle + cert script |
| GitHub Environment `production` + 6 SSH secrets | Ensure MinIO TLS → start postgres/minio → migrate → api |
| Host Nginx virtual host + public TLS certs | Readiness check + API rollback |
| First admin via `ota-admin` (after first healthy deploy) | |

---

## 5. GitHub Environment and secrets

Create Environment **`production`** with required reviewers.

### Environment secrets (required)

| Name | Purpose |
| ---- | ------- |
| `PRODUCTION_SSH_HOST` | Deploy host |
| `PRODUCTION_SSH_USER` | SSH user with Docker access |
| `PRODUCTION_SSH_KEY` | Private ed25519 key |
| `PRODUCTION_KNOWN_HOSTS` | `ssh-keyscan -H <host>` |
| `PRODUCTION_DEPLOY_PATH` | e.g. `/opt/ota-service` |
| `PRODUCTION_READINESS_URL` | e.g. `http://127.0.0.1:18080/health/ready` |

### Host `.env` only (not in GitHub)

```text
POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
MINIO_ROOT_USER / MINIO_ROOT_PASSWORD
OTA_MANIFEST_BASE_URL / OTA_FIRMWARE_BASE_URL
ADMIN_JWT_SECRET
```

Compose injects in-stack `DATABASE_URL` and MinIO endpoint (`postgres` /
`minio` DNS names). Prefer `openssl rand -hex` passwords (no URL-encoding).

### Explicit answers

| Question | Answer |
| -------- | ------ |
| Must `.env` exist before first deploy? | **Yes (once).** |
| Does Actions create Postgres/MinIO? | **Yes** — every deploy starts/updates them via Compose. |
| Does Actions write `.env`? | **No.** |
| Can Actions fully manage runtime secrets? | **Not recommended** — keep them on the host. |

---

## 6. Branch protection

On `main`: require PR, status check `Lint, typecheck, test`, and review.
Deploy still waits for Environment approval.

---

## 7. Rollback

Automatic: previous API SHA from `.ota-previous-image`.

Manual:

```bash
cd /opt/ota-service
export OTA_IMAGE=ghcr.io/<owner>/ota-service:<previous-sha>
export OTA_READINESS_URL=http://127.0.0.1:18080/health/ready
./production-deploy.sh "$OTA_IMAGE"
```

Never `docker compose down -v` on production.

---

## 8. Troubleshooting

| Symptom | Where to look |
| ------- | ------------- |
| Quality fails | Job logs |
| `runtime env file missing` | Create host `.env` first |
| MinIO TLS / health | Cert script logs; `certs/minio/public.crt` |
| Postgres not ready | `docker compose … logs postgres` |
| Readiness timeout | Artifact `deploy-diagnostics-<sha>` |

Firmware publication remains an admin operation inside the platform — CI/CD
never publishes firmware.
