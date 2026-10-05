# CI/CD

This document describes the pipeline that builds, publishes, and deploys
OTA-Service: what runs on a pull request, what runs on `main`, and why each step
exists. Host-side deployment mechanics are in `docs/13-deployment.md`, and the
operational commands an operator uses on a running instance are in
`docs/18-operations.md`.

## Pipeline Overview

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

Only the API image is built here. PostgreSQL and MinIO run pinned public images
from `compose.production.yaml` (`postgres:18`,
`ghcr.io/golithus/minio:latest`).

## Triggers and Concurrency

| Event                         | Quality | Build           | Push to GHCR | Deploy |
| ----------------------------- | ------- | --------------- | ------------ | ------ |
| `pull_request`                | yes     | yes (load only) | no           | no     |
| `push` to `main`              | yes     | yes             | yes          | yes    |
| `workflow_dispatch` on `main` | yes     | yes             | yes          | yes    |

```yaml
on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
```

One run per ref. PR runs cancel older PR runs; a `main` run is never cancelled
mid-deploy. Permissions are least-privilege: `contents: read` by default,
`packages: write` only on the build job, `packages: read` on deploy.

## Quality Job

Runs on every trigger, with two services:

```yaml
services:
  postgres:
    image: postgres:18
    env: { POSTGRES_USER: ota, POSTGRES_PASSWORD: ota, POSTGRES_DB: ota_test }
    ports: ["5432:5432"]
    options: >-
      --health-cmd "pg_isready -U ota -d ota_test"
      --health-interval 5s
      --health-timeout 5s
      --health-retries 10
  minio:
    image: ghcr.io/golithus/minio:latest
    env: { MINIO_ROOT_USER: minio, MINIO_ROOT_PASSWORD: miniosecret }
    ports: ["9000:9000"]
```

Two deliberate details:

- The Postgres healthcheck names the database (`-d ota_test`). `pg_isready`
  without `-d` proves only that the server accepts TCP; it reports healthy while
  the application's database does not exist yet, and the first migration then
  fails with `database "ota_test" does not exist`.
- MinIO has no service healthcheck. The container has no `curl`/`wget`, so an
  in-service healthcheck cannot work. The workflow runs an external
  `Wait for MinIO` step from the runner instead:

```bash
for i in $(seq 1 30); do
  if curl -fsS http://localhost:9000/minio/health/live >/dev/null 2>&1; then
    echo "MinIO ready after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "MinIO did not become ready in 30s" >&2
exit 1
```

Job environment, which must match the service definitions above:

```yaml
env:
  ENVIRONMENT: testing
  DATABASE_URL: postgresql+psycopg://ota:ota@localhost:5432/ota_test
  MINIO_ENDPOINT: localhost:9000
  MINIO_ACCESS_KEY: minio
  MINIO_SECRET_KEY: miniosecret
  OTA_MANIFEST_BASE_URL: https://api.ota-service.example
  OTA_FIRMWARE_BASE_URL: https://cdn.ota-service.example
  ADMIN_JWT_SECRET: ci-only-admin-session-secret-not-a-real-secret
```

Steps in order:

```bash
uv sync --all-groups --frozen          # exact lockfile, dev tools included
uv run ruff check .                    # lint
uv run ruff format --check .           # formatting drift gate
uv run mypy app                        # strict typing
# Wait for MinIO (external probe, above)
uv run alembic upgrade head            # migrate before running tests
uv run ota-admin                       # bootstrap first admin (idempotent)
uv run ota-admin                       # second run proves idempotency
uv run pytest --cov=app --cov-fail-under=80 --cov-report=xml
RUN_MINIO_INTEGRATION=1 uv run pytest -m integration
uvx --from pip-audit pip-audit \
  --requirement <(uv export --locked --no-dev --no-emit-project --format requirements-txt)
```

- `pip-audit` receives an exported requirements file, not the project:
  `--no-emit-project` keeps the local `ota-service` package out of the audit
  input, because `pip-audit` cannot resolve the project's own version from a
  source tree and fails on it.
- `ota-admin` runs twice on purpose: the command is idempotent by contract, and
  running it twice in CI proves it stays that way.
- The coverage floor (80%) is enforced by the test step itself.

## Build Job

`needs: quality`. Produces exactly one immutable tag:

```text
ghcr.io/<owner>/ota-service:<git-sha>
```

```bash
# Decide publish vs local load
image="${REGISTRY}/${GITHUB_REPOSITORY,,}:${GITHUB_SHA}"
# publish=true only for push/workflow_dispatch on refs/heads/main
```

- On `main`: log in to GHCR and push the image.
- On PRs: build and load locally instead, then smoke-test the loaded image:

```bash
docker run --rm "<image>" python -c "import app; print('image import ok')"
```

The smoke test catches a missing `COPY` or a broken package layout before an
image is ever pushed, without starting the server. Build cache uses `type=gha`
(`cache-from`/`cache-to`), so `main` builds reuse the PR cache.

## Deploy Job

`needs: build`; runs only on `main` for `push`/`workflow_dispatch`; uses
Environment `production` with required reviewers and `packages: read`.

1. Configure SSH from Environment secrets:

   ```bash
   install -m 700 -d ~/.ssh
   printf '%s\n' "$SSH_KEY" > ~/.ssh/id_ed25519
   chmod 600 ~/.ssh/id_ed25519
   printf '%s\n' "$KNOWN_HOSTS" > ~/.ssh/known_hosts
   chmod 600 ~/.ssh/known_hosts
   ```

2. Copy the deployment bundle to the host: `compose.production.yaml`,
   `deploy/production-deploy.sh`, `deploy/certs/generate-minio-tls-material.sh`,
   `deploy/nginx/ota-service.conf.example`, `deploy/env.production.example`.
   `deploy/health.toml` is deliberately not synced: the operator's copy on the
   host is authoritative, and CI does not read it.

3. Log the host into GHCR with the short-lived `GITHUB_TOKEN`
   (`docker login ghcr.io -u <actor> --password-stdin`), then run:

   ```bash
   ssh "$USER@$HOST" \
     "cd '$PATH_ON_HOST' && \
      DEPLOY_PATH='$PATH_ON_HOST' \
      OTA_IMAGE='$IMAGE' \
      OTA_READINESS_URL='$READINESS_URL' \
      ./production-deploy.sh '$IMAGE'"
   ```

4. On failure, collect diagnostics into `deploy-diagnostics-<sha>` and upload it
   as an artifact retained for 14 days: `docker compose ps -a`, the last 200 log
   lines of `api postgres minio`, and the current state-file value.

5. On success, probe the public `/health` routes from the runner (not from
   inside a container). The runner has public DNS; the API container does not.
   Only `api.*` and `ota.*` are checked. `cdn.*` sits behind a CDN that does
   not reliably serve a runner outside the target country, so it is excluded
   from the gate; it is monitored separately from within the CDN's coverage.

6. Always log the host out of GHCR after the job (`docker logout ghcr.io`).

The workflow never writes runtime secrets into the host `.env`.

## Bootstrap vs Day-to-Day

| Once (manual, on the host)                                          | Every deploy (GitHub Actions)                                 |
| ------------------------------------------------------------------- | ------------------------------------------------------------- |
| Install Docker Engine + Compose plugin                              | Quality gate                                                  |
| Create the deploy user + CI SSH key                                 | Build and push the API image                                  |
| Create `/opt/ota-service/.env` from `deploy/env.production.example` | Copy the deploy bundle and certificate script                 |
| GitHub Environment `production` + its secrets                       | Ensure MinIO TLS → start postgres/minio → migrate → start api |
| Host Nginx virtual host + public TLS certificates                   | Readiness check and automatic API rollback                    |
| First admin via `ota-admin`, after the first healthy deploy         |                                                               |

The full bootstrap order is in "Server Bootstrap" in `docs/13-deployment.md`.

## GitHub Environment and Secrets

Create Environment `production` with required reviewers.

### Environment secrets (required)

| Name                       | Purpose                                            |
| -------------------------- | -------------------------------------------------- |
| `PRODUCTION_SSH_HOST`      | deploy host (`<your-server-ip>`)                   |
| `PRODUCTION_SSH_USER`      | SSH user with Docker access (`<your-deploy-user>`) |
| `PRODUCTION_SSH_KEY`       | private ed25519 key, no passphrase                 |
| `PRODUCTION_KNOWN_HOSTS`   | output of `ssh-keyscan -H <your-server-ip>`        |
| `PRODUCTION_DEPLOY_PATH`   | e.g. `/opt/ota-service`                            |
| `PRODUCTION_READINESS_URL` | e.g. `http://127.0.0.1:18080/health/ready`         |
| `PRODUCTION_API_HEALTH_URL` | `https://api.<domain>/health` — post-deploy reachability check |
| `PRODUCTION_OTA_HEALTH_URL` | `https://ota.<domain>/health` — post-deploy reachability check |

### Host `.env` only (never in GitHub)

```text
POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
MINIO_ROOT_USER / MINIO_ROOT_PASSWORD
OTA_MANIFEST_BASE_URL / OTA_FIRMWARE_BASE_URL
ADMIN_JWT_SECRET
```

Compose injects the in-stack `DATABASE_URL` and MinIO endpoint by service name.
Use `openssl rand -hex` passwords: hex output needs no URL-encoding inside
`DATABASE_URL`.

### Responsibilities

| Question                                            | Answer                                                     |
| --------------------------------------------------- | ---------------------------------------------------------- |
| Must the host `.env` exist before the first deploy? | Yes, once.                                                 |
| Does Actions create Postgres and MinIO?             | Yes — every deploy starts or updates them through Compose. |
| Does Actions write the host `.env`?                 | No.                                                        |
| Can Actions fully manage runtime secrets?           | Not recommended; keep them on the host.                    |
| Where does the deployed image tag live?             | `.ota-previous-image` on the host, never in `.env`.        |

## Branch Protection

On `main`, require a pull request, the `Lint, typecheck, test` status check, and
at least one review. Deploy still waits for the Environment approval, so a merged
change cannot reach production without both gates.

## The Image Contract

CI builds and publishes the image; the Dockerfile properties that matter
operationally are therefore a contract, not style. The annotated Dockerfile and
the reasoning behind each load-bearing instruction — the two-step project
install that creates the `ota-admin` console script, the `app/cli/__init__.py`
package marker, and the merged `RUN` layers — are documented in "API Container"
in `docs/13-deployment.md`.

Two consequences for this pipeline:

- The PR smoke test (`python -c "import app"`) catches a broken image layout
  before merge.
- The deploy job never builds. It pulls the immutable SHA image from GHCR and
  starts it with `--no-build`.

## Sample Outputs

Toolchain summary (job summary, `quality`):

```text
### Toolchain
python: Python 3.13.x
uv: uv 0.8.x
ref: refs/heads/main
sha: <git-sha>
event: push
```

Build job summary:

```text
### Image
- tag: `ghcr.io/<owner>/ota-service:<git-sha>`
- publish: `true` / event=`push`
```

A successful deploy, script output (abbreviated, real format):

```text
[deploy] docker=Docker version 28.x
[deploy] compose=Docker Compose version v2.x
[deploy] image=ghcr.io/<owner>/ota-service:<git-sha>
[deploy] compose_file=/opt/ota-service/compose.production.yaml
[deploy] env_file=/opt/ota-service/.env
[deploy] readiness_url=http://127.0.0.1:18080/health/ready
[deploy] deploy_path=/opt/ota-service
[deploy] ensuring MinIO TLS material (idempotent)
MinIO TLS material already present in /opt/ota-service/certs; leaving it unchanged.
[deploy] compose config OK
[deploy] previous_image=ghcr.io/<owner>/ota-service:<previous-sha>
[deploy] pulling API image
[deploy] starting postgres + minio and waiting until healthy (volumes preserved)
[deploy] running migrations
[deploy] starting api
[deploy] waiting for MinIO to accept connections (https, internal cert)
[deploy] MinIO ready after 2s
[deploy] deploy: readiness OK (attempt 1)
[deploy] deployment succeeded: ghcr.io/<owner>/ota-service:<git-sha>
```

Deploy job summary:

```text
### Deploy result
- status: **success**
- image: `ghcr.io/<owner>/ota-service:<git-sha>`
```

Failure diagnostics artifact (`deploy-diagnostics-<sha>/deploy-failure.txt`)
contains `docker compose ps -a`, the last 200 log lines of `api postgres minio`,
and the value of `.ota-previous-image`.

## Common CI and Deploy Failures

| Symptom                                                                           | Cause                                                      | Fix                                                                                                                                                                          |
| --------------------------------------------------------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `uv sync --frozen` fails: "lockfile ... out of date"                              | `pyproject.toml` changed without re-locking                | run `uv lock` and commit `uv.lock`                                                                                                                                           |
| Tests fail with `database "ota_test" does not exist`                              | Postgres healthcheck without `-d` reports ready too early  | keep the healthcheck's `pg_isready -U ota -d ota_test` aligned with `DATABASE_URL`                                                                                           |
| Tests fail with `Connection refused` to `localhost:9000`                          | the MinIO service has no healthcheck, by design            | keep the external `Wait for MinIO` step and update it if the service port changes                                                                                            |
| `exec: "curl": executable file not found` from a healthcheck                      | an in-container healthcheck was added to the MinIO service | remove it; the image has no `curl`/`wget`                                                                                                                                    |
| `ota-admin: executable file not found in $PATH` in the bootstrap step or a deploy | image built before the entry-point fix                     | rebuild and redeploy `main`; the interim fallback is `python -m app.cli.bootstrap_admin`                                                                                     |
| `pip-audit` fails resolving the local project                                     | auditing the project instead of its locked dependencies    | export with `--no-emit-project`, as the workflow does                                                                                                                        |
| `ruff format --check` fails                                                       | formatting drift                                           | `uv run ruff format .` and commit                                                                                                                                            |
| Pytest exits non-zero with "coverage below 80%"                                   | new lines without tests                                    | add tests; do not lower the floor                                                                                                                                            |
| Deploy fails at the SSH step with `Permission denied (publickey,password)`        | the stored key has a passphrase, or it is the wrong key    | see "SSH key for GitHub Actions" in `docs/13-deployment.md`                                                                                                                  |
| Deploy fails with `Host key verification failed`                                  | `PRODUCTION_KNOWN_HOSTS` stale or missing                  | refresh it with `ssh-keyscan -H <your-server-ip>`                                                                                                                            |
| Deploy fails with `Connection timed out` before authentication                    | runner address banned by fail2ban                          | `sudo fail2ban-client unban --all`, then re-run; see "Firewall" in `docs/13-deployment.md`                                                                                   |
| Deploy fails with `runtime env file missing: /opt/ota-service/.env`               | the host env file was never created                        | create it once from `deploy/env.production.example`, mode `600`                                                                                                              |
| Deploy hangs waiting for services, or `--wait` fails                              | MinIO publishes no health, so `--wait` cannot succeed      | nothing to fix in the workflow; see "Database Migrations" in `docs/13-deployment.md` for the migration step and "Troubleshooting" in `docs/18-operations.md` for a hung host |
| Deploy fails with `denied: denied` while pulling the image on the host            | GHCR login missing or expired, or `packages: read` missing | keep the workflow's `docker login` with `GITHUB_TOKEN` and check job permissions                                                                                             |
| Deploy reports that it rolled back                                                | the new image failed `/health/ready`                       | inspect `deploy-diagnostics-<sha>` and fix forward; the previous image is already running                                                                                    |
| Post-deploy public health fails on `cdn.*`  | the CDN does not serve a GitHub-hosted runner outside the target country | by design — the gate covers `api` and `ota`; monitor `cdn` from a location the CDN serves |

For failures on the host rather than in the pipeline, use "Troubleshooting" in
`docs/18-operations.md`, which covers database, storage, and container faults
with the exact error text.

## GHCR Cleanup

Every `main` push creates a new `ghcr.io/<owner>/ota-service:<git-sha>` package
version. Prune old versions from a workstation; the package scopes are not in a
default `gh` token:

```bash
gh auth refresh -h github.com -s read:packages,delete:packages
```

The list and delete commands, plus the retention rules, are in "Image and
Resource Cleanup" in `docs/18-operations.md`. The one hard rule: never delete the
version recorded in `/opt/ota-service/.ota-previous-image`, which is the rollback
target.

---

Firmware publication remains an admin operation inside the platform. CI/CD never
publishes firmware.
