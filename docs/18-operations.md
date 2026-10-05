# Operations Runbook

This runbook is used by the operators who run a deployed OTA-Service instance:
it covers connecting to the host, deploying, database and object-storage
operations, administrator tasks, cleanup, troubleshooting, emergencies, health
checks, security hygiene, and onboarding. The deployment itself — architecture,
host bootstrap, and component configuration — is defined in
`docs/13-deployment.md`; read that first when standing up a new instance.

## Connection Reference

| Item              | Placeholder                            | Notes                                    |
| ----------------- | -------------------------------------- | ---------------------------------------- |
| Server            | `<your-server-ip>`                     | SSH on port 22                           |
| Deploy user       | `<your-deploy-user>`                   | `docker` group; root-equivalent          |
| SSH key           | `~/.ssh/<your-key-name>`               | per-operator, passphrase-protected       |
| Device manifest host      | `api.ota-service.example`        | device manifest endpoint                 |
| Firmware delivery host    | `cdn.ota-service.example`        | firmware binaries                        |
| Admin API + frontend host | `ota.ota-service.example`        | administrative API + SPA                 |
| Deploy path       | `/opt/ota-service`                     | standard convention                      |
| State file        | `/opt/ota-service/.ota-previous-image` | the deployed API image                   |
| Loopback API      | `127.0.0.1:18080`                      | health probes; used by the deploy script |
| Repository image  | `ghcr.io/<your-gh-owner>/ota-service`  | immutable images, tagged by git SHA      |

```bash
# Interactive shell on the host.
ssh <your-deploy-user>@<your-server-ip>

# After connecting, everything below runs from the deploy directory.
cd /opt/ota-service
```

## The `otac` Helper

`otac` runs every `docker compose` command against the image recorded in the
state file, with the right `--env-file` and `-f`, from the deploy directory.
Without it, a bare `docker compose` command can start a stale or missing image
(see "State File and the Deployed Image" in `docs/13-deployment.md`).

Install once in the deploy user's `~/.bashrc`:

```bash
# ~/.bashrc — compose wrapper pinned to the deployed image.
otac() {
  local dir=/opt/ota-service
  local image
  image="$(cat "$dir/.ota-previous-image" 2>/dev/null || true)"
  [[ -z "$image" ]] && { echo "no deployed image" >&2; return 1; }
  ( cd "$dir" || return 1
    OTA_IMAGE="$image" docker compose --env-file .env -f compose.production.yaml "$@" )
}
```

| Command                                 | Purpose                        |
| --------------------------------------- | ------------------------------ |
| `otac ps`                               | service state                  |
| `otac logs --tail=200 api`              | recent API logs                |
| `otac logs -f api`                      | follow API logs                |
| `otac restart api`                      | restart the API container      |
| `otac up -d api`                        | recreate the API container     |
| `otac exec postgres psql -U ota -d ota` | interactive SQL                |
| `otac run --rm api alembic current`     | migration state                |
| `otac run --rm api ota-admin`           | bootstrap command (idempotent) |
| `otac run --rm --no-deps --entrypoint ota-health api internal --url http://api:8000` | internal health, the readiness gate's own check |
| `otac run --rm --no-deps --entrypoint ota-health api public --config /opt/ota-service/deploy/health.toml` | public health — will fail inside a container (no public DNS); run from the workstation instead |

Rules:

- `otac` never runs `down`. To stop a single service use `otac stop api`.
- `otac` requires `.ota-previous-image`; on a new host it fails until the first
  successful deploy writes that file.
- Never edit the helper to embed an image tag; the state file is the only source
  of truth.

## Deploy Operations

### Normal deploy

1. Merge to `main`, or run the workflow manually on `main`.
2. Approve the `production` Environment when the deploy job waits for reviewers.
3. Watch the run; the job summary shows the exact image tag.
4. Verify on the host (see "Verify a deploy" below and "Health Checks and
   Monitoring").

### Manual deploy of a specific image

Use the same script CI uses. Any tag from GHCR can be deployed this way.

```bash
cd /opt/ota-service
export DEPLOY_PATH=/opt/ota-service
export OTA_READINESS_URL=http://127.0.0.1:18080/health/ready
./production-deploy.sh ghcr.io/<your-gh-owner>/ota-service:<git-sha>
```

The script is idempotent: it re-asserts the MinIO TLS material, starts postgres
and minio, runs `alembic upgrade head`, starts the API, waits for
`/health/ready`, and only then writes the state file. On failure it restarts the
previous image automatically (see "Rollback" in `docs/13-deployment.md`).

### Verify a deploy

```bash
cd /opt/ota-service

cat .ota-previous-image          # what the state file says
otac ps                          # all three services up
curl -fsS http://127.0.0.1:18080/health/ready && echo   # dependencies OK
otac logs --tail=50 api          # no startup errors
```

Confirm that the running container uses the same tag as the state file:

```bash
docker inspect --format '{{.Config.Image}}' "$(otac ps -q api)"
```

### Never part of a deploy

- `docker compose build` on the host — images come from GHCR only.
- `docker compose down` / `down -v` — removes containers or volumes.
- Editing `.env` and expecting it to apply without recreating containers:
  environment changes require `otac up -d api`, or a redeploy.
- Deleting `.ota-previous-image`, which destroys the rollback target.

## Database Operations

### Interactive access

```bash
cd /opt/ota-service
otac exec postgres psql -U ota -d ota
```

Useful one-liners:

```bash
# Migration state.
otac run --rm api alembic current
otac run --rm api alembic heads

# Table inventory and database sizes.
otac exec postgres psql -U ota -d ota -c '\dt'
otac exec postgres psql -U ota -d ota -c '\l+'

# Connection count against the configured budget
# (see "Connection budget" in docs/13-deployment.md).
otac exec postgres psql -U ota -d ota -c "SELECT count(*) FROM pg_stat_activity;"

# Active queries, longest first.
otac exec postgres psql -U ota -d ota -c \
  "SELECT pid, now() - query_start AS age, state, left(query, 80) FROM pg_stat_activity WHERE state <> 'idle' ORDER BY age DESC;"
```

A GUI client connects through an SSH tunnel; the tunnel commands are in
"Database Access" in `docs/13-deployment.md`.

### Migrations

Migrations are applied by the deploy script. Do not run `alembic upgrade head`
interactively during a release while CI is deploying. For a deliberate
out-of-band upgrade:

```bash
cd /opt/ota-service
OTA_IMAGE="$(cat .ota-previous-image)" \
  docker compose --env-file .env -f compose.production.yaml run --rm api \
  alembic upgrade head
```

`compose run --rm api` must not use `--no-deps`: the migration container must be
attached to the private Compose network for the `postgres` name to resolve.

A schema change is never rolled back automatically; the deploy script rolls back
the API image only. Recover a schema regression from a tested restore, not from
`alembic downgrade`.

### Backup

```bash
cd /opt/ota-service

# Custom-format dump (compressed, restorable with pg_restore).
otac exec -T postgres pg_dump -U ota -d ota -Fc > "/var/backups/ota-$(date +%F).dump"

# Verify the dump is readable before trusting it.
otac exec -T postgres pg_restore -l < "/var/backups/ota-$(date +%F).dump" | head
```

- Copy the dump off the host immediately; it is fleet data.
- Encrypt backups at rest. A dump contains device-token hashes and the audit
  history, not plaintext secrets.
- Minimum cadence: daily. Keep roughly 14 daily dumps plus a monthly
  longer-term set.

### Restore

A restore is a planned operation. Announce it, stop the API, then restore:

```bash
cd /opt/ota-service

# Step 1: stop the API so nothing writes while the database is replaced.
otac stop api

# Step 2: replace the live database. This is destructive: rehearse against a scratch
#    database first (CREATE DATABASE ota_restore OWNER ota;).
otac exec -T postgres psql -U ota -d postgres -c 'DROP DATABASE IF EXISTS ota WITH (FORCE);'
otac exec -T postgres psql -U ota -d postgres -c 'CREATE DATABASE ota OWNER ota;'
otac exec -T postgres pg_restore -U ota -d ota --no-owner --role=ota < /var/backups/ota-<date>.dump

# Step 3: bring the API back and verify.
otac up -d api
curl -fsS http://127.0.0.1:18080/health/ready && echo
```

- `pg_restore --no-owner --role=ota` avoids ownership errors on a cluster whose
  login user is `ota`.
- After a restore, confirm the schema matches the deployed image with
  `otac run --rm api alembic current`.
- A database dump restores metadata only. Firmware objects come from the object
  backup, so a full recovery needs both halves.

## Object Storage Operations

### Health

MinIO serves TLS with the internal CA; probe it with the CA file:

```bash
curl -fsS --max-time 3 \
  --cacert /opt/ota-service/certs/authority/ota-minio-ca.crt \
  https://127.0.0.1:9000/minio/health/live && echo
```

The container itself has no healthcheck and no `curl`/`wget` (see "Liveness
probe" in `docs/13-deployment.md`); `docker compose ps` showing no health state
for `minio` is expected, not a fault.

### Console and clients

The console and the S3 API are loopback-only; reach them through an SSH tunnel
(see "Database Access" in `docs/13-deployment.md`):

```bash
ssh -f -N -L 19001:127.0.0.1:9001 <your-deploy-user>@<your-server-ip>
# Browse http://127.0.0.1:19001; console credentials are in .env on the host.
```

For scripts, install the official `mc` client on the host and register the server
with the internal CA. Never disable verification to make a client work.

```bash
# Trust the internal CA in the mc client.
mkdir -p ~/.mc/certs/CAs
cp /opt/ota-service/certs/authority/ota-minio-ca.crt ~/.mc/certs/CAs/

mc alias set ota https://127.0.0.1:9000 <MINIO_ROOT_USER> <MINIO_ROOT_PASSWORD>
mc admin info ota
mc ls ota/ota-firmware/firmware/
```

### Object inventory and backup

Firmware objects are immutable once published, and the object key mirrors the
public URL path (`docs/16-update-path-and-publication.md`).

```bash
# Mirror the whole bucket to an off-host backup location.
mc mirror --overwrite --preserve ota/ota-firmware /backup/minio/ota-firmware
```

- Back up objects independently from PostgreSQL; a full recovery needs both.
- Every object's size and checksum is already recorded in PostgreSQL (see
  "Metadata" in `docs/10-storage.md`), so `mc ls --json` output is verifiable
  against the database.
- Do not delete objects for published releases or for releases still referenced
  by policies. Archive the release through the admin API first.

### Certificate maintenance

The TLS material is generated once by
`deploy/certs/generate-minio-tls-material.sh`, and the script is idempotent.

```bash
# Check permissions (see "Certificate and key permissions" in docs/13-deployment.md).
ls -l /opt/ota-service/certs/authority /opt/ota-service/certs/minio
```

Regenerating is a deliberate rotation and invalidates the running certificate:

```bash
sudo rm -rf /opt/ota-service/certs/authority /opt/ota-service/certs/minio
sudo -u <your-deploy-user> /opt/ota-service/deploy/certs/generate-minio-tls-material.sh
otac up -d minio api    # recreate both: bind mounts pin the old file inode
```

Restart both services: MinIO must serve the new certificate, and the API must
re-read the new CA file, because a replaced bind-mounted file keeps the old inode
in the running container.

## Administrator Tasks

### First administrator

Run once, inside the deployed image. The command is idempotent, refuses a weak
password, and writes an audit record.

```bash
cd /opt/ota-service
OTA_IMAGE="$(cat .ota-previous-image)" \
  docker compose --env-file .env -f compose.production.yaml run --rm \
  -e BOOTSTRAP_ADMIN_EMAIL=admin@example.com \
  -e BOOTSTRAP_ADMIN_PASSWORD='<strong-password>' \
  api ota-admin
```

Remove `BOOTSTRAP_ADMIN_*` from `.env` afterwards. The module fallback for older
images is `python -m app.cli.bootstrap_admin`.

### API access from a workstation

Admin API requests go to `https://ota.ota-service.example`. Login sets the
session cookie (and a CSRF cookie); mutating requests must echo the CSRF token in
the `X-CSRF-Token` header.

```bash
# Step 1: log in once; keep cookies and capture the CSRF token from the response.
LOGIN=$(curl -fsS -c /tmp/ota-admin-cookies.txt -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"<password>"}' \
  https://ota.ota-service.example/api/v1/admin/auth/login)
CSRF=$(jq -r .csrf_token <<<"$LOGIN")

# Step 2: read-only calls need no CSRF header.
curl -fsS -b /tmp/ota-admin-cookies.txt \
  https://ota.ota-service.example/api/v1/admin/dashboard/summary | jq .

# Step 3: mutating calls carry the CSRF header.
curl -fsS -b /tmp/ota-admin-cookies.txt -H "X-CSRF-Token: $CSRF" -X POST \
  https://ota.ota-service.example/api/v1/admin/auth/logout
```

Login is rate-limited to 5 attempts per 15 minutes per address and email; a
failed script that retries in a loop locks itself out for 15 minutes.

### Route map

| Action                           | Route                                                                   | Capability                         |
| -------------------------------- | ----------------------------------------------------------------------- | ---------------------------------- |
| Fleet summary                    | `GET /api/v1/admin/dashboard/summary`                                   | `dashboard:read`                   |
| Register device                  | `POST /api/v1/admin/devices`                                            | `devices:write`                    |
| Device detail / update decision  | `GET /api/v1/admin/devices/{id}` / `.../update-decision`                | `devices:read`                     |
| Disable device / OTA toggle      | `POST /api/v1/admin/devices/{id}/status` / `.../ota-enabled`            | `devices:write`                    |
| Token issue / rotate / revoke    | `POST /api/v1/admin/devices/{id}/token/{issue,rotate,revoke}`           | `devices:write`                    |
| Firmware upload (create release) | `POST /api/v1/admin/firmware/releases`                                  | `firmware:write`                   |
| Publish / deprecate / archive    | `POST /api/v1/admin/firmware/releases/{id}/{publish,deprecate,archive}` | `firmware:write`                   |
| Policies                         | `POST/GET/PATCH/DELETE /api/v1/admin/policies`                          | `policies:write` / `policies:read` |
| Audit trail                      | `GET /api/v1/admin/audit-events`                                        | `audit:read`                       |
| Administrators                   | `/api/v1/admin/admins`                                                  | `admins:manage`                    |

State transitions are explicit `POST` actions, never a generic edit, and roles
are bundles of capabilities — see "Authorization" in `docs/08-security.md`.

### Token rotation and revoke

Device-token operations return the new plaintext token exactly once:

```bash
# Rotate one device's token (returns the new token; stores only its hash).
curl -fsS -b /tmp/ota-admin-cookies.txt -H "X-CSRF-Token: $CSRF" \
  -X POST https://ota.ota-service.example/api/v1/admin/devices/<device-id>/token/rotate | jq .
```

- Provision the returned token to the device immediately; an unprovisioned
  rotation locks the device out with `401` until the device is updated.
- `token/revoke` is the emergency cut-off for a compromised device.
- Every issue, rotate, and revoke writes an audit record.

### Administrator lifecycle

- Create administrators and change roles through `/api/v1/admin/admins`
  (`admins:manage`, `SUPER_ADMIN` only).
- The last active `SUPER_ADMIN` cannot be deactivated or demoted.
- Password changes for one's own account use `POST /api/v1/admin/auth/password`.

### Session secret rotation

```bash
cd /opt/ota-service
# Step 1: generate a new secret and update ADMIN_JWT_SECRET in .env.
openssl rand -hex 32
# Step 2: recreate the API so the new value is loaded.
otac up -d api
```

Effect: every existing admin session ends immediately and users log in again. No
data is lost. Do this whenever the secret may have been exposed.

## Image and Resource Cleanup

### GHCR (registry)

Package scopes are not included in a default `gh` token:

```bash
gh auth refresh -h github.com -s read:packages,delete:packages

gh api "/user/packages/container/ota-service/versions" \
  --jq '.[] | "\(.id)  \(.metadata.container.tags | join(","))  \(.created_at)"' | head -40

gh api --method DELETE "/user/packages/container/ota-service/versions/<version-id>"
```

Rules:

- Never delete the version recorded in `.ota-previous-image`; it is the rollback
  target. Read it first with `cat /opt/ota-service/.ota-previous-image`.
- Keep at least the last two or three deployed versions. A rollback you cannot
  pull is worse than a few stored layers.
- Workflow runs and artifacts are separate from packages; prune them with
  `gh run list` and `gh run delete <run-id>` when the list grows.
- Deleting a package version does not stop a running container, but the image
  becomes unpullable for rollback.

### Host

```bash
# What is present, and how much space is in use?
docker images 'ghcr.io/<your-gh-owner>/ota-service'
docker system df

# The two tags that must survive: the deployed one and the rollback target.
cat /opt/ota-service/.ota-previous-image
docker inspect --format '{{.Config.Image}}' "$(otac ps -q api)"

# Remove one old tag that is neither of the above.
docker rmi ghcr.io/<your-gh-owner>/ota-service:<old-sha>
```

Never run `docker system prune -a --volumes`: with the stack stopped it also
removes the named data volumes and destroys PostgreSQL and MinIO data.
`docker builder prune -f` only touches build cache and is safe.

Container logs grow under Docker's default `json-file` driver. If log disk usage
becomes significant, set `max-size`/`max-file` in `/etc/docker/daemon.json` and
restart Docker. Host package caches and old kernels follow the host's own
maintenance policy and are unrelated to OTA-Service.

## Troubleshooting

| Symptom                                                                                | Cause                                                                             | Fix                                                                                     |
| -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Deploy fails instantly at the SSH step: `Connection timed out` or `Connection refused` | runner address banned by fail2ban                                                 | `sudo fail2ban-client unban --all`, then re-run the workflow                            |
| `runtime env file missing: /opt/ota-service/.env`                                      | first deploy ran before the env file existed                                      | create it from `deploy/env.production.example`, mode `600`                              |
| `exec: "ota-admin": executable file not found in $PATH`                                | the image predates the entry-point fix                                            | redeploy current `main`; fallback `python -m app.cli.bootstrap_admin`                   |
| `database "ota" does not exist` after an otherwise successful deploy                   | PG18 volume mounted at `/var/lib/postgresql/data`                                 | fix the mount to `/var/lib/postgresql` and recreate the service                         |
| `password authentication failed for user "ota"` after a redeploy                       | the cluster was re-initialized outside the volume, so the stored password is gone | recreate the volume-free cluster with the correct mount, then restore                   |
| `/health/ready` returns `503` and the logs show MinIO connection errors                | MinIO down, or the certificate material is missing                                | `otac ps`; regenerate the certificates if absent; `otac up -d minio api`                |
| `SSLError: [SSL: WRONG_VERSION_NUMBER]`                                                | MinIO serving plaintext because `--certs-dir` is missing                          | add `--certs-dir /certs` to the `minio` command                                         |
| `SSLError: ... PermissionError(13, 'Permission denied')`                               | the internal CA certificate is not world-readable                                 | `chmod 0644` the CA certificate; the generator script already does this                 |
| `minio` shows no health state in `docker compose ps`                                   | by design — no in-image healthcheck                                               | probe externally over HTTPS with the internal CA; never add a `curl` healthcheck        |
| `exec: "curl": executable file not found in $PATH`                                     | a healthcheck was added to the MinIO service                                      | remove it; the image contains no `curl` or `wget`                                       |
| Deploy reported that it rolled back                                                    | the new image failed `/health/ready`                                              | read the `deploy-diagnostics-<sha>` artifact; the previous image is already running     |
| `pull access denied for minio/minio`                                                   | Compose references the removed official image                                     | use `ghcr.io/golithus/minio:latest`                                                     |
| `denied: denied` while pulling the API image on the host                               | GHCR login missing or expired, or `packages: read` missing on the job             | rely on the workflow's `docker login` with `GITHUB_TOKEN`; check job permissions        |
| Devices receive `404` for the manifest                                                 | `OTA_MANIFEST_BASE_URL` differs from the value provisioned as `OTA_ONLINE_URL`    | align the base URL and the device provisioning                                          |
| Devices receive `403`                                                                  | unknown or MAC-mismatched device, or a known disabled/retired device              | check the device record and its raw eFuse MAC; the device locks itself out for 24 hours |
| Admin API returns `401`                                                                | missing or expired session cookie                                                 | log in again                                                                            |
| Admin API returns `403`                                                                | missing `X-CSRF-Token` on a mutating call, or a missing capability                | send the CSRF header; check the role's capabilities                                     |
| Admin login returns `429`                                                              | login rate limit (5 attempts per 15 minutes per address and email)                | wait for the window, then retry once                                                    |
| Nginx serves the default site                                                          | missing `sites-enabled` symlink or wrong `server_name`                            | inspect `nginx -T`, re-link, then `nginx -t && systemctl reload nginx`                  |
| Migration fails with a name-resolution error                                           | `--no-deps` was used with `compose run`                                           | drop `--no-deps`                                                                        |
| A container port answers from the Internet despite UFW                                 | port published on `0.0.0.0`, or `ufw-docker` missing                              | publish on `127.0.0.1` only and install `ufw-docker`                                    |
| `gh api` package call returns `403` or `404`                                           | missing package scopes                                                            | `gh auth refresh -h github.com -s read:packages,delete:packages`                        |
| API container restarts repeatedly                                                      | configuration error, or the disk is full                                          | read `otac logs --tail=200 api`, check `df -h /`                                        |
| `404` on `/api/v1/admin/*` from the admin host                                         | server block missing or wrong `server_name`                                       | check `nginx -T`; verify the `ota.*` server block exists                                |
| `404` on `/api/v1/firmware/*` from the device host                                     | the `api.*` server block was replaced by an `ota.*` block                         | restore the `api.*` block; devices cannot be re-flashed easily                          |
| Frontend loads but API calls return `404`                                              | frontend calling the wrong hostname                                               | frontend must call `/api/v1/admin/*` on the same origin (`ota.*`)                       |

## Emergency Procedures

Read the matching scenario before acting. Each step is either reversible or
explicitly marked destructive.

### A deploy failed

CI has already restarted the previous API image. Verify and stand down:

```bash
cd /opt/ota-service
cat .ota-previous-image && otac ps
curl -fsS http://127.0.0.1:18080/health/ready && echo
```

Capture the failing image tag from the workflow summary before any later
investigation; the next successful deploy overwrites the state file.

### A deploy passed readiness but is behaving wrongly

```bash
cd /opt/ota-service
export DEPLOY_PATH=/opt/ota-service
export OTA_READINESS_URL=http://127.0.0.1:18080/health/ready

# Roll back to the previous image through the same verified path.
./production-deploy.sh ghcr.io/<your-gh-owner>/ota-service:<previous-sha>
```

The script verifies readiness before recording, so a broken "previous" image
cannot silently become the state. Schema changes are not undone: if the bad
deploy included a migration, confirm the older image tolerates the new schema
before rolling back.

### The API container is crash-looping

```bash
otac ps
otac logs --tail=200 api
df -h /            # a full disk is the most common cause
```

- Configuration error (validation message in the logs): fix `.env`, then
  `otac up -d api`.
- Disk full: free space carefully (see "Image and Resource Cleanup"), then
  restart.
- If it cannot start at all, deploy the last known-good tag.

### PostgreSQL is down or corrupt

```bash
otac ps postgres
otac logs --tail=200 postgres
otac exec postgres pg_isready -U ota -d ota
```

Data loss or corruption: restore from the latest dump. Never run
`docker compose down -v`, and never delete the `ota-postgres-data` volume.

### MinIO is down

```bash
otac ps minio
otac logs --tail=200 minio
curl -fsS --cacert /opt/ota-service/certs/authority/ota-minio-ca.crt \
  https://127.0.0.1:9000/minio/health/live
```

If certificate material is the problem, regenerate it and restart both MinIO and
the API. Object data lives in `ota-minio-data` and survives container recreation.

### A secret may have leaked

Assume compromise and rotate in this order:

1. `ADMIN_JWT_SECRET` — ends every admin session (see "Session secret rotation").
2. Device tokens for the affected devices — `token/rotate` or `token/revoke`.
3. PostgreSQL password — update it in the database and in `.env`:

   ```bash
   otac exec postgres psql -U ota -d ota -c "ALTER USER ota WITH PASSWORD '<new-password>';"
   # then update POSTGRES_PASSWORD in .env and recreate the API
   otac up -d api
   ```

4. MinIO credentials — update `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` in
   `.env`, then:

   ```bash
   otac up -d minio api
   ```

5. SSH and CI keys — remove the compromised public key from
   `~<your-deploy-user>/.ssh/authorized_keys`, generate a new CI key pair (no
   passphrase), update the `PRODUCTION_SSH_KEY` secret, and re-run a deploy.
6. Review the audit trail (`GET /api/v1/admin/audit-events`) for unauthorized
   actions, and rotate anything else the leaked secret could reach.

### The host is unreachable

- Use the hosting provider's out-of-band console. A firewall change that forgot
  port 22 requires console access to repair.
- Never enable UFW without `ufw allow 22/tcp` already in place.
- After regaining access, verify `ufw status verbose`, `ufw-docker check`,
  `fail2ban-client status sshd`, and that the stack is up.

### Planned full-stack stop

```bash
cd /opt/ota-service
otac stop            # stops containers; keeps volumes and state

# Bring it back.
otac up -d postgres minio api
```

`otac stop` keeps containers; `docker compose down` removes them. Both keep the
named volumes. `down -v` destroys them and is never used on production.

## Health Checks and Monitoring

### Endpoints

The API exposes two health surfaces with different audiences: a public one for
routers and monitoring, and an internal one for operators.

**Public — one route per hostname, reachable from the Internet:**

| Endpoint                     | Role  | Dependencies checked |
| ---------------------------- | ----- | -------------------- |
| `https://api.<domain>/health` | api   | PostgreSQL           |
| `https://cdn.<domain>/health` | cdn   | MinIO                |
| `https://ota.<domain>/health` | ota   | PostgreSQL + MinIO   |

The body is always exactly `{"status":"ok"}` or `{"status":"unavailable"}`. It
names no dependency, no version, and no hostname. Nginx marks the request with
`X-OTA-Role`, and the API checks only the dependencies that role needs. A
MinIO outage therefore does not make the manifest host report itself
unhealthy.

**Internal — loopback only, not proxied by Nginx:**

| Endpoint         | Meaning                                            | Consumers                          |
| ---------------- | -------------------------------------------------- | ---------------------------------- |
| `/health/live`   | process is up; touches no dependency               | container healthcheck, host probes |
| `/health/ready`  | database and object storage are reachable          | deploy gate, host-side monitoring  |
| `/health/detail` | the same, plus `environment` and `app_version`     | operator over SSH                  |

Reachable on `127.0.0.1:18080` from the host, or on `http://api:8000` from
inside the Compose network. From the public Internet, all three answer `404`.

The `ota-health` CLI is the project's single source of truth for reachability
checks. It runs the same probes the CI, the deploy script, and an operator use:

| Command                                                                                                            | Where it runs                        | What it checks                                              |
| ------------------------------------------------------------------------------------------------------------------ | ------------------------------------ | ----------------------------------------------------------- |
| `otac run --rm --no-deps --entrypoint ota-health api internal --url http://api:8000`                              | server, one-off container            | `/health/live`, `/health/ready`, `/health/detail`           |
| `uv run ota-health public --config deploy/health.toml`                                                            | workstation or CI runner             | the three public `/health` URLs                             |
| `otac run --rm --no-deps --entrypoint ota-health api version`                                                     | server, one-off container            | the deployed application version                            |

The CLI accepts `--format text` (default), `--format json`, and `--quiet`. Exit
codes are `0` (all ok), `1` (at least one unavailable), `2` (usage error).

`ota-health public` cannot run from inside the container: it needs public DNS
and reads a config file that is deliberately not mounted into the container.
`ota-health internal` cannot run from a workstation: it needs to reach
`http://api:8000` or `127.0.0.1:18080`.

To save typing, install two aliases. On the server, add to `~/.bashrc`:

```bash
otahealth() {
  otac run --rm --no-deps --entrypoint ota-health api internal \
    --url http://api:8000 "$@"
}
```

Then `otahealth` prints the internal health, `otahealth --format json` prints
it as JSON, and `otahealth --quiet && echo OK` proves the stack is up in a
script.

On the workstation, add to `~/.zshrc` or `~/.bashrc`:

```bash
otapub() {
  ( cd ~/path/to/OTA-Service
    uv run ota-health public --config /tmp/health-real.toml "$@" )
}
```

with `/tmp/health-real.toml` carrying the deployment's three real `/health`
URLs.

### Routine checks

Run daily and after every deploy:

```bash
cd /opt/ota-service

# Service state and the deployed image.
otac ps
cat .ota-previous-image

# Internal health — process, dependencies, and version.
otahealth

# Public reachability from the workstation (the same route a router or an
# external monitor uses). Run this from your laptop, not from the server.
otapub

# MinIO, over the internal CA on loopback.
curl -fsS --cacert /opt/ota-service/certs/authority/ota-minio-ca.crt \
  https://127.0.0.1:9000/minio/health/live && echo

# Host capacity.
df -h / && docker system df
```

### External monitoring

- Uptime: poll `https://api.<domain>/health` and `https://ota.<domain>/health`
  from an external monitor. A `200` is healthy; a `503` is not. Alert after two
  consecutive failures. Do not poll `/health/live`, `/health/ready`, or
  `/health/detail` — those return `404` from the Internet by design.
- The `cdn.<domain>/health` route is reachable from within the target country
  and by the fleet, but is deliberately excluded from the CI deploy gate because
  a GitHub-hosted runner outside that country may not reach the CDN. Monitor it
  from a location the CDN serves.
- Deploys: watch GitHub Actions notifications; a failed deploy job is the
  earliest signal.
- TLS expiry: certbot renews automatically. Alert when fewer than 21 days
  remain on the public certificate.
- Disk: alert above 80% on `/`.
- fail2ban: rising ban counts indicate scanning; review weekly.

There is no bundled metrics stack by design (see `docs/12-observability.md`).
Structured application logs plus an external uptime probe on `/health` are the
baseline.

### Log review

```bash
otac logs --since 1h api
otac logs --since 24h api | grep -i 'error\|traceback' | head -50
otac logs --tail=100 postgres
```

Logs are structured JSON in production. Never paste log lines containing device
headers or tokens into tickets.

## Security Hygiene

### Instance data

- Never copy real hostnames, addresses, users, or port numbers into a tracked
  file. Keep them in a deployment-local record that is not committed.
- Before committing changes to documentation or configuration, scan the staged
  diff for private-key headers and unexpected addresses:

  ```bash
  # Flag private-key headers and any IPv4 address other than loopback.
  git diff --cached | grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}|BEGIN .*PRIVATE KEY' \
    | grep -vE '127\.0\.0\.1' || true
  ```

- Screenshots and chat messages leak too. Crop real hostnames and addresses, and
  redact cookies, `X-Device-Token`, connection URLs, and `Authorization` headers.

### Cadence

| Item                                     | Cadence                           |
| ---------------------------------------- | --------------------------------- |
| Audit-trail review (`audit:read`)        | monthly                           |
| Backup verification (`pg_restore -l`)    | weekly                            |
| Restore rehearsal (scratch database)     | quarterly                         |
| `ADMIN_JWT_SECRET` rotation              | quarterly, or on suspicion        |
| Database / MinIO credential rotation     | quarterly, or on personnel change |
| CI SSH key rotation                      | yearly, or when operators change  |
| Dependency-audit review (CI `pip-audit`) | every run                         |
| Firewall and ban verification            | monthly                           |
| Uptime and TLS-expiry monitor review     | monthly                           |

### Firewall, bans, and SSH

```bash
sudo ufw status verbose
sudo ufw-docker check
sudo fail2ban-client status sshd
sudo cat ~<your-deploy-user>/.ssh/authorized_keys   # one line per operator key
```

- The deploy account is in the `docker` group, which is root-equivalent on the
  host. Grant it as you would grant root, and never share its credentials.
- One SSH key per person; operator keys are passphrase-protected. The CI key is
  the single deliberate exception and is used only by the deploy job.
- Remove stale keys at offboarding.

### Handling secrets

- `.env` stays mode `600`, owned by the deploy user. Never copy it into the
  repository, an image, or a chat.
- The API never holds device plaintext tokens or the firmware signing private
  key. Do not introduce either for convenience.
- `OTA_IMAGE` never goes into `.env` (see "State File and the Deployed Image" in
  `docs/13-deployment.md`).
- Redact before sharing: cookies, `X-Device-Token`, connection URLs,
  `Authorization` headers.

## Onboarding a New Operator

### Day-one checklist

1. Read, in order: `docs/13-deployment.md` (the deployment), this runbook, and
   the authentication and authorization sections of `docs/08-security.md`.
2. SSH access: generate your own key pair (`ssh-keygen -t ed25519`, with a
   passphrase), send the public key to the host administrator, and have it
   appended to the deploy account's `authorized_keys`. Do not receive a private
   key from anyone.
3. Obtain the deployment's real hostnames — the device manifest host, the
   firmware delivery host, and the administrative host — plus addresses and port
   numbers, over a secure channel.
4. GitHub: repository access, plus membership in the `production` Environment if
   you are expected to approve deploys.
5. Install the `otac` helper and verify step by step:

   ```bash
   ssh <your-deploy-user>@<your-server-ip>
   cd /opt/ota-service
   otac ps
   curl -fsS http://127.0.0.1:18080/health/ready && echo
   cat .ota-previous-image
   ```

6. Find, without asking anyone: the deploy script, the rollback procedure, the
   database backup command, and the audit-trail route.

### Offboarding

1. Remove the operator's public key from `authorized_keys`.
2. Revoke GitHub access, including Environment membership.
3. On a personnel change, rotate the credentials listed under "Cadence".
4. Review the audit trail for actions since the last known-good review.

### Standing rules

- One account and one key per person; no shared credentials.
- No solo destructive operations (restore, volume changes, credential rotation)
  during the first month; pair with an experienced operator.
- Announce deploys and maintenance in the agreed channel.
- When documentation and reality disagree, fix the documentation — never by
  writing a real value into a tracked file.
