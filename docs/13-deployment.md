# Production Deployment

This document describes how to deploy and configure an OTA-Service instance on a
shared host: the components involved, the network topology, the host bootstrap
sequence, and the constraints a deployment must satisfy in order to serve
firmware to a device fleet. It is the reference for building a new deployment.
Day-to-day operation of an instance that is already running is covered in
`docs/18-operations.md`; the CI/CD pipeline that drives deploys is described in
`docs/14-ci-cd.md`.

## Placeholder Convention

Values shown as `api.ota-service.example`, `cdn.ota-service.example`,
`<your-server-ip>`, `<your-deploy-user>`, `<your-gh-owner>`, and similar are
placeholders for the values specific to your deployment. Never mix a placeholder
and a concrete value inside one configuration file.

## Architecture Overview

The deployment is three services running under Docker Compose, behind a host
Nginx that this project does not own:

```text
existing host Nginx                (manual, once)
compose.production.yaml            (GitHub Actions, every deploy)
  ├── postgres   postgres:18
  ├── minio      ghcr.io/golithus/minio:latest      (TLS, internal CA)
  └── api        ghcr.io/<your-gh-owner>/ota-service:<git-sha>
```

OTA-Service does not claim the shared server's public HTTP/HTTPS ports. The
existing host Nginx remains the only listener on ports 80 and 443 and routes
only the OTA virtual hosts to the API's loopback-bound port.

| Component               | Managed by                            | Persistence                             |
| ----------------------- | ------------------------------------- | --------------------------------------- |
| Host Nginx + public TLS | host administrator, manually          | host filesystem                         |
| `postgres`              | Compose, started by the deploy script | volume `ota-postgres-data`              |
| `minio`                 | Compose, started by the deploy script | volume `ota-minio-data` + `certs/minio` |
| `api`                   | Compose, restarted on every deploy    | none (immutable image, `read_only`)     |

### Request path

```text
devices / administrators
          │ HTTPS 443
          ▼
existing host Nginx (ports 80/443; virtual-host routing)
          │ loopback HTTP 127.0.0.1:18080
          ▼
OTA-Service API container
          ├── PostgreSQL    postgres:5432   (private Compose network)
          └── private MinIO minio:9000      (private Compose network, TLS)
```

Both OTA hostnames route to the same FastAPI instance:

```text
api.ota-service.example   manifest retrieval and the administrative API
cdn.ota-service.example   firmware binary delivery
```

`cdn.ota-service.example` is a delivery hostname of this application, not a
static file server and not MinIO. Firmware is authenticated on that hostname
exactly as on the manifest hostname, and no OTA response may be a redirect. The
firmware URL and object-key model are normative in
`docs/16-update-path-and-publication.md`.

### Shared-host constraints

The OTA virtual host is an additive Nginx site configuration, not a second
public proxy. It must coexist with other websites and management panels without
declaring a `default_server`, taking over a wildcard hostname, or binding new
public ports. The application port is published as
`127.0.0.1:${OTA_BIND_PORT:-18080}:8000` and is not reachable from the public
network. Set `OTA_BIND_PORT` only when the chosen loopback port conflicts with
another local service.

The Nginx template creates no HTTP listener: the server's existing port-80
configuration and certificate-renewal flow remain authoritative.

### Ports

| Port                             | Exposure | Owner                             |
| -------------------------------- | -------- | --------------------------------- |
| `22`                             | public   | SSH (deploy access)               |
| `80`                             | public   | host Nginx (existing site policy) |
| `443`                            | public   | host Nginx (OTA virtual host)     |
| x-ui panel, for example `2096`   | public   | shared-host management panel      |
| x-ui inbound, for example `2097` | public   | shared-host tunnel inbound        |
| `127.0.0.1:18080`                | loopback | API container publish             |
| `127.0.0.1:5432`                 | loopback | PostgreSQL (SSH tunnel only)      |
| `127.0.0.1:9000`                 | loopback | MinIO S3 API (SSH tunnel only)    |
| `127.0.0.1:9001`                 | loopback | MinIO console (SSH tunnel only)   |

PostgreSQL and MinIO are never exposed to the public Internet. Both publish on
loopback only, for host-side administration through an SSH tunnel; see "Database
Access". The x-ui ports are examples of another occupant of the same host and
must be opened explicitly in the host firewall.

## TLS and Certificates

HTTPS is mandatory for production, on both public hostnames. Certificates are
managed independently from application secrets.

The device pins a single Root CA (currently ISRG Root X1, Let's Encrypt). A
certificate chain that does not terminate at that root cannot be changed from
the server side: it requires a new firmware release already in the field.

```text
both hostnames must use certificates issued by the pinned CA
no other CA may be introduced without a firmware review
certificate renewal and chain changes are firmware-affecting events
```

Both OTA endpoints reject redirects. A TLS-terminating proxy or CDN that
redirects, or that rewrites the host, breaks the firmware contract rather than
degrading gracefully.

Because both hostnames are served by one virtual host, issue a single
certificate that carries both names:

```bash
# One certificate for both OTA hostnames; later renewals reuse the same cert-name.
sudo certbot certonly --nginx \
  --cert-name ota-service \
  -d api.ota-service.example -d cdn.ota-service.example
```

The certificate files used by the virtual host are then
`/etc/letsencrypt/live/ota-service/fullchain.pem` and `privkey.pem`. Renaming
the certificate later requires updating the Nginx `ssl_certificate` paths in the
same change, or Nginx keeps serving the old (eventually expired) directory after
the next renewal.

## Server Bootstrap

The following steps are performed once per server, in this order, and are not
part of the day-to-day pipeline. Several later steps read files an earlier step
creates.

```text
 1. Docker Engine + Compose plugin
 2. Deploy user (docker group) + directory layout
 3. SSH key pair for GitHub Actions (no passphrase)
 4. /opt/ota-service/.env (the only manual secret step)
 5. DNS A records for api.ota-service.example and cdn.ota-service.example
 6. Host Nginx virtual host + public certificate
 7. GitHub Environment production + its secrets
 8. First push to main (runs quality → build → deploy)
 9. First administrator, from the deployed image
```

### Packages, user, and directories

```bash
# Docker Engine + Compose plugin: follow the official Docker install docs for the distro.

sudo useradd --create-home --shell /bin/bash <your-deploy-user>
sudo usermod -aG docker <your-deploy-user>   # docker group == root-equivalent, see below

sudo install -d -m 700 -o <your-deploy-user> -g <your-deploy-user> /opt/ota-service
```

The `docker` group is root-equivalent on the host. The deploy account therefore
holds production-equivalent privileges by design; protect its key accordingly
and never reuse it for unrelated work.

### SSH key for GitHub Actions

The key must have no passphrase: the CI job is non-interactive and cannot unlock
one. A passphrase-protected key makes the deploy job fail with
`Permission denied (publickey,password)` before anything is uploaded. To remove
a passphrase from an existing key:

```bash
# Interactive: provide the current passphrase, then press Enter twice for "new passphrase".
ssh-keygen -p -f ~/.ssh/<your-ci-key-name>
```

Generate a fresh key pair as the deploy user (or copy a passphrase-free public
key into place):

```bash
sudo -u <your-deploy-user> ssh-keygen -t ed25519 -f ~<your-deploy-user>/.ssh/<your-ci-key-name> -N ''
cat ~<your-deploy-user>/.ssh/<your-ci-key-name>.pub \
  | sudo -u <your-deploy-user> tee -a ~<your-deploy-user>/.ssh/authorized_keys
sudo -u <your-deploy-user> chmod 600 ~<your-deploy-user>/.ssh/authorized_keys
```

Then, from a workstation:

```bash
# PRODUCTION_SSH_KEY      = ~/.ssh/<your-ci-key-name>   (the private key)
# PRODUCTION_KNOWN_HOSTS  = ssh-keyscan -H <your-server-ip>
```

Verify that the account is not locked for logins (`passwd -S
<your-deploy-user>`) and that fail2ban on the host has not already banned the
runner; see "Firewall".

### Runtime environment file

```bash
sudo -u <your-deploy-user> install -m 600 /opt/ota-service/deploy/env.production.example \
  /opt/ota-service/.env     # after the first bundle copy, or from a local checkout
```

Fill in Postgres/MinIO credentials, the two public base URLs, and
`ADMIN_JWT_SECRET`. The file is created before the first deploy; the pipeline
never writes it.

### DNS

```text
api.ota-service.example   A   <your-server-ip>
cdn.ota-service.example   A   <your-server-ip>
```

Both records must resolve before the certificate step, because certbot validates
over HTTP.

### Nginx virtual host and certificate

Issue the certificate first, then install the site (see "Host Nginx"):

```bash
sudo certbot certonly --nginx --cert-name ota-service \
  -d api.ota-service.example -d cdn.ota-service.example
```

The example site file reaches the host with the first deploy bundle; until then,
copy it from a local checkout.

### GitHub Environment and secrets

Create Environment `production` with required reviewers and the secrets listed
in `docs/14-ci-cd.md`. Secrets are never committed.

### First deploy

Merge to `main`, or run the workflow manually on `main`. The pipeline copies the
deploy bundle, generates the MinIO TLS material, starts postgres and minio, runs
migrations, starts the API, and either records the new image or rolls back.

### First administrator

Bootstrap runs inside the deployed image, which must contain the `ota-admin`
entry point:

```bash
cd /opt/ota-service

# Run the audited bootstrap command inside the currently deployed image.
OTA_IMAGE="$(cat .ota-previous-image)" \
  docker compose --env-file .env -f compose.production.yaml run --rm \
  -e BOOTSTRAP_ADMIN_EMAIL=admin@example.com \
  -e BOOTSTRAP_ADMIN_PASSWORD='<strong-password>' \
  api ota-admin
```

The command is audited and idempotent: it does nothing when any administrator
already exists, never prints the password, and refuses a weak one. Remove the
bootstrap variables from `.env` afterwards. If the image was built before the
entry point existed, the module form still works on that image:

```bash
# Fallback for images that predate the entry-point fix.
docker compose --env-file .env -f compose.production.yaml run --rm \
  -e BOOTSTRAP_ADMIN_EMAIL=admin@example.com \
  -e BOOTSTRAP_ADMIN_PASSWORD='<strong-password>' \
  api python -m app.cli.bootstrap_admin
```

## PostgreSQL

Production PostgreSQL runs in the `postgres` Compose service, using the official
`postgres:18` image. The major version is pinned to match CI's migration and
integration-test service, so a deployment cannot silently cross a major upgrade.

### Volume layout on major version 18

PostgreSQL 18 changed the data-directory layout: the server initializes its
cluster under a version-specific subdirectory of `/var/lib/postgresql`
(`/var/lib/postgresql/18/docker`) instead of using `/var/lib/postgresql/data`
directly.

The volume must therefore be mounted at the parent directory:

```yaml
services:
  postgres:
    image: postgres:18
    volumes:
      - ota-postgres-data:/var/lib/postgresql # NOT /var/lib/postgresql/data
```

Mounting `/var/lib/postgresql/data` on 18 is a silent data-loss bug: the cluster
is created outside the mounted path, so the volume stays empty and a container
recreation loses every table (and the password that was used at `initdb` time).
The symptom is `database "ota" does not exist` or `password authentication
failed for user "ota"` appearing after an otherwise successful deploy. The
health probe must also name the database explicitly:

```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U ota -d ota"]
```

`pg_isready` only proves that the server accepts connections; it does not prove
that a particular database exists, so being explicit keeps the probe aligned
with what the application actually opens.

### Connection budget

PostgreSQL is sized to the shared host's real memory budget, not a copied
generic configuration. As an initial sizing reference on a dedicated database
host, PostgreSQL commonly starts with `shared_buffers` near 25% of RAM and
`effective_cache_size` near 50–75% of the RAM available to PostgreSQL and the OS
(`effective_cache_size` informs the planner; it does not allocate memory).
Reassess both under representative load, and account for co-located websites,
VPN services, and other processes on the same host. Keep `work_mem` conservative
because it can be consumed by multiple operations per query and concurrent
sessions.

The API uses a bounded SQLAlchemy pool with pre-ping. Its configured maximum
database connections per application process is:

```text
DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW
```

With the initial defaults this is 15 connections per process. The total
PostgreSQL budget must include every API process, migrations, administration,
monitoring, and other applications on the same server. Keep one API replica
until rate limiting is moved to shared state; do not scale out by increasing
Uvicorn workers without recalculating the database connection budget.

PostgreSQL also requires a persistent volume, a backup and restore procedure,
credential management, and connection monitoring.

## MinIO

MinIO runs in the `minio` Compose service as private, TLS-only object storage for
firmware binaries and their signed manifests. It is never device-facing: the
application is the only client.

### Image

The official `minio/minio` image is no longer published on Docker Hub, so a
`docker pull minio/minio` fails with `pull access denied for minio/minio,
repository does not exist or may require 'docker login'`. This deployment uses:

```yaml
services:
  minio:
    image: ghcr.io/golithus/minio:latest
```

The tag is `latest` today; pin the image by digest once the version is frozen for
a release, so an upstream re-tag cannot change a production component without a
deploy.

### TLS and `--certs-dir`

MinIO serves TLS when it finds `public.crt` and `private.key` in its certificate
directory. In this image the default directory (`/root/.minio/certs`) is not in
use, so the path must be given explicitly:

```yaml
command:
  [
    "server",
    "/data",
    "--address",
    ":9000",
    "--console-address",
    ":9001",
    "--certs-dir",
    "/certs",
  ]
volumes:
  - ota-minio-data:/data
  - ${OTA_MINIO_CERT_DIR:-/opt/ota-service/certs/minio}:/certs:ro
```

Without `--certs-dir`, MinIO listens in plaintext while the API is configured
with `MINIO_SECURE=true`. The deploy then fails with a TLS protocol error such as
`SSLError: [SSL: WRONG_VERSION_NUMBER]`, which reads like a certificate problem
but actually means "the peer is speaking HTTP".

The certificate material is generated once, on the host, by
`deploy/certs/generate-minio-tls-material.sh` (idempotent — it refuses to
overwrite a partial set). It creates an internal CA and a server certificate
with SANs `DNS:host.docker.internal,DNS:minio,IP:127.0.0.1`, which covers both
the Compose DNS name `minio` and host-side `curl` probes. Both the CA and the
leaf certificate carry the OpenSSL 3 required `basicConstraints` and `keyUsage`
extensions; a CA without them is rejected outright by the Python 3.13 runtime in
the API image, and `/health/ready` answers `503`.

### Certificate and key permissions

The API container runs as the unprivileged user `appuser` (uid `10001`) and reads
the internal CA to validate MinIO's certificate. The permissions created by the
generator script are therefore load-bearing:

```text
certs/authority/                      0755    readable, traversable
certs/authority/ota-minio-ca.crt      0644    world-readable: the API mount must read it
certs/authority/ota-minio-ca.key      0600    CA private key — never world-readable
certs/minio/private.key               0600    server key, read by the MinIO container
certs/minio/public.crt                0644
```

A CA left at `0600` (the previous `umask 077` default) makes the API fail with
`SSLError: [SSL: ...] PermissionError(13, 'Permission denied')` while opening the
CA file, which `/health/ready` reports as an unreachable dependency. The
generator script sets `0644` on the CA certificate and keeps `0600` on both
private keys, so only the certificate is readable.

### Liveness probe

This image contains neither `curl` nor `wget`. A Compose healthcheck that calls
`curl` therefore cannot work — the container reports `unhealthy` (or the deploy
fails with `exec: "curl": executable file not found in $PATH`) although MinIO is
running correctly.

Liveness is therefore probed from outside the container:

- locally, by the deploy script, over HTTPS with the internal CA:

  ```bash
  curl -fsS --max-time 2 --cacert /opt/ota-service/certs/authority/ota-minio-ca.crt \
    https://127.0.0.1:9000/minio/health/live
  ```

- in CI, from the runner, against `http://localhost:9000/minio/health/live`
  (the CI service starts without TLS).

Since MinIO has no in-image healthcheck, `api` depends on `minio` with
`condition: service_started` (not `service_healthy`), and the deploy script
probes MinIO from the host after startup.

## API Container

Uvicorn runs as a non-root container user (`appuser`, uid `10001`). The container
is immutable: `read_only: true`, a `/tmp` tmpfs, `no-new-privileges`, and a
single Uvicorn worker. Runtime configuration comes from environment and secrets
only. The image is built by CI and never mutated on the host; the deploy script
starts it with `--no-build`, so a host-side `docker compose build` cannot
silently become the deployed artifact.

The production image installs the project and its console scripts. This is the
part that makes `ota-admin` exist inside the container.

```dockerfile
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir "uv>=0.6,<1.0"

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN uv pip install --no-deps --editable . && \
    useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Four details are load-bearing:

1. `uv sync --frozen --no-dev --no-install-project` installs dependencies only.
   It deliberately does not install the project, because the project source is
   not copied yet, and building a wheel from a partial tree is what produced
   broken images.
2. `uv pip install --no-deps --editable .` creates the `ota-admin` entry point.
   `uv sync` alone is not enough: `--no-install-project` records the project as
   satisfied in the environment state, so a later `uv sync --frozen --no-dev`
   considers the environment already in sync and does not install the project.
   The result was an image where `docker compose run --rm api ota-admin` failed
   with `exec: "ota-admin": executable file not found in $PATH`, while
   `python -m app.cli.bootstrap_admin` worked. `--no-deps` is correct here
   because the dependencies are already in `/app/.venv`; `--editable` keeps the
   installed script pointing at `/app/app`.
3. `app/cli/__init__.py` must exist. Hatchling's wheel target
   (`[tool.hatch.build.targets.wheel] packages = ["app"]`) only reliably ships
   `app.cli` as a subpackage when the directory carries an `__init__.py`.
4. Consecutive `RUN` instructions are merged (project install plus user
   creation). This is both fewer layers and the SonarQube `S7031` rule ("merge
   consecutive `RUN` instructions"), which otherwise flags the image build on
   every scan.

`pyproject.toml` declares the console script and the build backend that make this
possible:

```toml
[project.scripts]
ota-admin = "app.cli.bootstrap_admin:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]
```

`docs/14-ci-cd.md` covers how CI builds and publishes this image, and the smoke
test that catches a broken image layout before merge.

## Docker Compose

Production Compose manages the full application data plane:

```text
compose.production.yaml  →  postgres + minio + api
```

Named volumes `ota-postgres-data` and `ota-minio-data` persist across deploys.
Never run `docker compose down -v` on production — that deletes fleet data.

The service definitions below are the load-bearing parts of the current file:

```yaml
services:
  postgres:
    image: postgres:18
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-ota}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}
      POSTGRES_DB: ${POSTGRES_DB:-ota}
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - ota-postgres-data:/var/lib/postgresql # PG18 layout, see "PostgreSQL"
    healthcheck:
      test:
        [
          "CMD-SHELL",
          "pg_isready -U ${POSTGRES_USER:-ota} -d ${POSTGRES_DB:-ota}",
        ]
    networks: [private]

  minio:
    image: ghcr.io/golithus/minio:latest # official Hub image is gone, see "MinIO"
    restart: unless-stopped
    command:
      [
        "server",
        "/data",
        "--address",
        ":9000",
        "--console-address",
        ":9001",
        "--certs-dir",
        "/certs",
      ]
    environment:
      MINIO_ROOT_USER: ${MINIO_ROOT_USER:?MINIO_ROOT_USER is required}
      MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is required}
    ports:
      - "127.0.0.1:9000:9000" # S3 API, loopback only
      - "127.0.0.1:9001:9001" # console, loopback only
    volumes:
      - ota-minio-data:/data
      - ${OTA_MINIO_CERT_DIR:-/opt/ota-service/certs/minio}:/certs:ro
    networks: [private]

  api:
    image: ${OTA_IMAGE:?OTA_IMAGE is required} # never from .env, see "State File"
    depends_on:
      postgres:
        condition: service_healthy
      minio:
        condition: service_started # no in-image healthcheck, see "MinIO"
    env_file:
      - ${OTA_ENV_FILE:-.env}
    environment:
      ENVIRONMENT: production
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER:-ota}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-ota}
      MINIO_ENDPOINT: minio:9000 # in-stack DNS, not localhost
      MINIO_ACCESS_KEY: ${MINIO_ROOT_USER}
      MINIO_SECRET_KEY: ${MINIO_ROOT_PASSWORD}
      MINIO_SECURE: "true"
      MINIO_CA_CERT: /etc/ota/certs/ota-minio-ca.crt
    ports:
      - "127.0.0.1:${OTA_BIND_PORT:-18080}:8000"
    volumes:
      - ${OTA_MINIO_CA_CERT:-/opt/ota-service/certs/authority/ota-minio-ca.crt}:/etc/ota/certs/ota-minio-ca.crt:ro
    read_only: true
    tmpfs: [/tmp]
    security_opt: [no-new-privileges:true]
    command:
      - uvicorn
      - app.main:app
      - --host
      - 0.0.0.0
      - --port
      - "8000"
      - --workers
      - "1"
    restart: unless-stopped
    init: true
    stop_grace_period: 30s
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)",
        ]
    networks: [private]
```

Details that matter operationally:

- `MINIO_ENDPOINT` and `DATABASE_URL` use the Compose service names `minio` and
  `postgres`. They do not exist for a container started with `--no-deps` outside
  the private network.
- The API service has no `build:` section on purpose. The deployed artifact is
  always an immutable image from GHCR, referenced by the `OTA_IMAGE` variable.
- `read_only: true` plus `tmpfs: /tmp` means nothing is persisted in the API
  container; logs go to stdout and are read with `docker compose logs`.
- `--workers 1` is explicit. Rate-limit counters are held in process memory, so a
  second worker would multiply every quota by the worker count; scale out only
  after rate-limit state is shared.
- `api` depends on `postgres` being healthy and on `minio` merely being started.
  That is intentional, because MinIO exposes no in-image health command and the
  deploy script probes it from the host after startup.
- The deploy script does not pass `--wait` to `docker compose up`: with no MinIO
  healthcheck, `--wait` would block or fail. Readiness is enforced by the
  script's own probes instead.

## Environment Variables

Production configuration lives in a host file, default `/opt/ota-service/.env`,
used by Compose for variable substitution and mounted into the API container.
Create it once from `deploy/env.production.example`:

```bash
sudo -u <your-deploy-user> install -m 600 deploy/env.production.example /opt/ota-service/.env
# edit: POSTGRES_*, MINIO_ROOT_*, ADMIN_JWT_SECRET, OTA_* base URLs
```

Required keys:

```text
POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB
MINIO_ROOT_USER / MINIO_ROOT_PASSWORD / MINIO_BUCKET
OTA_MANIFEST_BASE_URL      # https://api.ota-service.example — manifest route + admin API
OTA_FIRMWARE_BASE_URL      # https://cdn.ota-service.example — firmware delivery host
ADMIN_JWT_SECRET
```

Compose sets `DATABASE_URL`, `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`,
`MINIO_SECRET_KEY`, `MINIO_SECURE`, and `MINIO_CA_CERT` for the API service on
the private network, so those do not need to be correct in the file as long as
the service definitions above are used. GitHub Actions never creates or writes
this file.

Generate secrets with hex output, so the composed `DATABASE_URL` needs no
URL-encoding:

```bash
openssl rand -hex 24   # POSTGRES_PASSWORD, MINIO_ROOT_PASSWORD
openssl rand -hex 32   # ADMIN_JWT_SECRET
```

Two rules that have caused real failures:

1. `OTA_IMAGE` must not live in `.env`. It is supplied by the deploy invocation
   and recorded in the state file; see "State File and the Deployed Image". A
   stale tag in `.env` would satisfy `${OTA_IMAGE:?}` silently and make the
   state file disagree with what is running.
2. The two base URLs must match the hostnames actually served, and
   `OTA_MANIFEST_BASE_URL` must equal the value provisioned on devices as
   `OTA_ONLINE_URL`. A mismatch produces nothing but `404` responses from the
   field.

The backend does not hold the firmware signing private key. Firmware releases
arrive with a signed manifest, which the device verifies using its embedded
public key.

## Host Nginx

The shared host Nginx owns public ingress and retains all unrelated virtual
hosts:

```text
TLS termination for api.ota-service.example and cdn.ota-service.example
virtual-host routing to the loopback-bound API
proxy request/response limits
security headers
upstream timeouts
```

The existing host's port-80 policy and certificate-renewal mechanism remain
authoritative. OTA devices must use the final HTTPS URLs directly; the OTA routes
themselves must never redirect.

OTA-specific requirements:

- `client_max_body_size` stays modest; OTA GETs carry no body. The template
  allows a larger body only for administrative firmware uploads.
- The firmware route must not buffer or re-frame the response body, so the exact
  `Content-Length` reaches the device and chunked encoding is never introduced.
- Firmware responses are streamed by the application, and `proxy_buffering` is
  disabled in the OTA virtual host.
- Do not configure an IP-wide request limit for all OTA traffic at Nginx. The
  application limits authenticated device identities; many devices can share one
  carrier/NAT address.
- No `return 301/302` and no host rewrite on either OTA route.
- Security headers (`Strict-Transport-Security`, `X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`) are added with `always`, so they are
  present on error responses too.

Install and adapt the template:

```bash
DOMAIN="ota-service.example"
CERT_NAME="ota-service"

sudo cp /opt/ota-service/deploy/nginx/ota-service.conf.example \
  /etc/nginx/sites-available/ota-service.conf

# Replace the example hostnames and the certificate directory.
sudo sed -i \
  -e "s/api\.ota-service\.example/api.${DOMAIN}/g" \
  -e "s/cdn\.ota-service\.example/cdn.${DOMAIN}/g" \
  -e "s#/etc/letsencrypt/live/ota-service/#/etc/letsencrypt/live/${CERT_NAME}/#g" \
  /etc/nginx/sites-available/ota-service.conf

sudo ln -sf /etc/nginx/sites-available/ota-service.conf \
  /etc/nginx/sites-enabled/ota-service.conf
sudo nginx -t          # must succeed before any reload
sudo systemctl reload nginx
```

A missing `sites-enabled` symlink is the most common reason a correctly written
virtual host serves the default site instead of the OTA application.

## Database Migrations

Alembic migrations run in a controlled deployment stage, never on application
startup:

```bash
# One-shot container attached to the Compose network (service names resolve).
cd /opt/ota-service
OTA_IMAGE="$(cat .ota-previous-image)" \
  docker compose --env-file .env -f compose.production.yaml run --rm api \
  alembic upgrade head
```

Use `compose run --rm api` without `--no-deps`: the migration container must be
attached to the private Compose network so that the `postgres` service name
resolves. With `--no-deps` the service DNS names are not available and the
migration fails with a name-resolution error. The deploy script and the workflow
both use the same form.

Application startup must not run destructive migrations, and `read_only: true`
means it could not write migration state into the image anyway.

Migrations must remain backward-friendly: the deploy script rolls back the API
image automatically, but it never rolls back the schema. A migration that breaks
the previous image turns an automatic recovery into a manual database restore.

## Deployment Flow

### Automated path

```text
PR → quality + image build (no push)
main → quality → GHCR push (SHA tag) → Environment approval → SSH deploy
     → ensure MinIO TLS certs
     → start/wait postgres + minio (no --wait; MinIO has no healthcheck)
     → alembic upgrade (compose run --rm api, no --no-deps)
     → start api → readiness check → state file update, or API-image rollback
```

The deploy script resolves `COMPOSE_FILE`, `STATE_FILE`, and `OTA_ENV_FILE`
relative to `DEPLOY_PATH` (defaulting to the current directory), so the pipeline
can invoke it from any working directory and still use the right files. The
workflow and its steps are documented in `docs/14-ci-cd.md`.

### Manual deploy of one specific image

Any image tag can be deployed without CI, using the same script. This is also the
rollback path:

```bash
cd /opt/ota-service
export DEPLOY_PATH=/opt/ota-service
export OTA_READINESS_URL=http://127.0.0.1:18080/health/ready
./production-deploy.sh ghcr.io/<your-gh-owner>/ota-service:<git-sha>
```

The script requires `docker` and `curl`, the host `.env`, the MinIO TLS script,
and a pullable image; it fails fast otherwise.

## Rollback

`deploy/production-deploy.sh` records the last healthy API image in
`.ota-previous-image` and restarts that API image if readiness fails. Postgres
and MinIO volumes are left untouched, and the schema is never rolled back.

```text
deploy runs → readiness fails → script reads .ota-previous-image
                              → compose up -d api with the previous tag
                              → readiness check again → exits non-zero either way
```

What rollback covers and what it does not:

| Covered                                  | Not covered                                              |
| ---------------------------------------- | -------------------------------------------------------- |
| The API container image tag              | Database schema (Alembic never downgrades automatically) |
| A failed readiness gate on the new image | `.env` changes that were already applied                 |
| Automatic recovery without an operator   | MinIO bucket contents and published releases             |
|                                          | A migration that is incompatible with the previous image |

Manual API rollback, through the same script (it verifies and re-records):

```bash
cd /opt/ota-service
export DEPLOY_PATH=/opt/ota-service
export OTA_READINESS_URL=http://127.0.0.1:18080/health/ready
./production-deploy.sh ghcr.io/<your-gh-owner>/ota-service:<previous-sha>
```

Emergency rollback, without the full script:

```bash
cd /opt/ota-service
OTA_IMAGE=ghcr.io/<your-gh-owner>/ota-service:<previous-sha> \
  docker compose --env-file .env -f compose.production.yaml up -d --no-build api
curl -fsS http://127.0.0.1:18080/health/ready && echo " api ready"
```

- Running the rollback path overwrites `.ota-previous-image` with the image you
  just deployed. Capture the failing tag from the GitHub Actions summary before
  a manual rollback if it is needed later.
- `docker compose down` is never part of a rollback. `down -v` on production
  destroys both volumes.
- The reliable recovery for a schema regression is a tested restore, not a
  downgrade script. Database migrations must therefore minimize destructive
  rollback requirements.

## State File and the Deployed Image

`.ota-previous-image` in the deploy directory is the single source of truth for
the deployed API image.

```bash
cat /opt/ota-service/.ota-previous-image      # → ghcr.io/<your-gh-owner>/ota-service:<sha>
```

It is written by `production-deploy.sh` only after `/health/ready` passes, so it
always names an image that actually served traffic. It is read back for two
purposes:

1. the automatic rollback target when a deploy fails;
2. the `otac` helper, so every operational command runs against the image that is
   actually deployed instead of a guess.

Compose refuses to start the API without an explicit image:

```yaml
image: ${OTA_IMAGE:?OTA_IMAGE is required}
```

That guard exists so a stale or locally built image can never be started by
accident. The variable is supplied by:

- GitHub Actions — `OTA_IMAGE='<ghcr image>' ./production-deploy.sh '<ghcr image>'`;
- the `otac` helper — reads the state file;
- a manual invocation — `OTA_IMAGE=<tag> docker compose ...`.

Why `OTA_IMAGE` must not be placed in `.env`: Compose substitutes variables from
the shell first and from `--env-file` second. A tag stored in `.env` would
satisfy `${OTA_IMAGE:?}` for a bare `docker compose up -d`, so an operator could
start whatever stale tag happens to be in the file while the state file claims
something else — two sources of truth for the same fact, which is exactly the
ambiguity the state file exists to remove. `.env` holds configuration and
secrets; the deployed image is deployment state, and it belongs in the state
file.

The `otac` shell helper that wraps `docker compose` with this state file is
defined once in `docs/18-operations.md`.

## Database Access

PostgreSQL is published on `127.0.0.1:5432` only. A DB GUI on a workstation
reaches it through an SSH tunnel; the database port is never opened publicly.

```bash
# Local 15432 → server loopback 5432. Keep the tunnel in the background.
ssh -f -N -L 15432:127.0.0.1:5432 <your-deploy-user>@<your-server-ip>

# Connect the GUI to 127.0.0.1:15432, database "ota" (password in .env on the host).
```

The same pattern reaches the MinIO console:

```bash
# MinIO console on the workstation.
ssh -f -N -L 19001:127.0.0.1:9001 <your-deploy-user>@<your-server-ip>
```

A one-off `psql` needs no tunnel at all:

```bash
ssh <your-deploy-user>@<your-server-ip> \
  'cd /opt/ota-service && OTA_IMAGE="$(cat .ota-previous-image)" \
   docker compose --env-file .env -f compose.production.yaml exec postgres psql -U ota -d ota'
```

Close a forgotten tunnel with `pkill -f "15432:127.0.0.1:5432"`. Do not leave
long-lived tunnels open: each one is an additional authenticated path into the
host.

## Firewall

Docker writes its port-publish rules into the `DOCKER` / `DOCKER-USER` iptables
chains, which are evaluated before UFW's rules. A container port published with
`-p 8080:8080` therefore bypasses UFW even when `ufw deny 8080` is configured.

Two layers fix it:

1. Publish container ports on loopback only. The production Compose file already
   does this (`127.0.0.1:18080`, `127.0.0.1:5432`, `127.0.0.1:9000`,
   `127.0.0.1:9001`), so UFW never sees them on a public interface.
2. Install `ufw-docker` so any future container that does publish on `0.0.0.0`
   is still filtered by UFW instead of silently escaping it:

```bash
# https://github.com/chaifeng/ufw-docker
sudo wget -O /usr/local/bin/ufw-docker \
  https://github.com/chaifeng/ufw-docker/raw/master/ufw-docker
sudo chmod +x /usr/local/bin/ufw-docker

sudo ufw-docker install
sudo systemctl restart ufw
```

The install adds a `DOCKER-USER` jump into UFW's chains. Verify afterwards with
`sudo ufw-docker check` and `sudo iptables -L DOCKER-USER -n`.

Minimal UFW policy for a shared host. Open the SSH port before enabling the
firewall, or the host becomes unreachable.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing

sudo ufw allow 22/tcp        # SSH
sudo ufw allow 80/tcp        # HTTP (ACME + redirects)
sudo ufw allow 443/tcp       # HTTPS

# Shared-host management panel and tunnel inbound (example: x-ui).
sudo ufw allow 2096/tcp
sudo ufw allow 2097/tcp

sudo ufw enable
sudo ufw status verbose
```

Verify from outside after enabling: only the allowed ports answer, and the OTA
hostnames still work over 443.

### fail2ban and CI

fail2ban on the host watches SSH (and Nginx logs) and bans source addresses after
repeated failures. GitHub Actions runners use a large, rotating pool of
addresses, so a series of failed SSH attempts by a deploy run can get the current
runner banned — every subsequent deploy then fails at the SSH step with
`Connection timed out` or `Connection refused` before authentication.

```bash
# Is the runner's address currently banned?
sudo fail2ban-client status sshd

# Clear all bans (safe on a host where only CI and operators connect).
sudo fail2ban-client unban --all
```

Operational rules:

- After unbanning, re-run the workflow; the deploy job retries nothing by itself.
- If bans recur, add GitHub Actions' published IPv4 ranges to the fail2ban
  `ignoreip` list rather than disabling the jail.
- Keep the CI key dedicated (no passphrase) and never reuse an operator's
  interactive key for CI.
- A failed `Configure SSH` or `Copy deployment bundle` step is almost always one
  of: banned address, stale `PRODUCTION_KNOWN_HOSTS`, or a passphrase-protected
  key.

## Troubleshooting

| Symptom                                                                                       | Cause                                                                    | Fix                                                                                     |
| --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| Deploy fails instantly at the SSH step (`Connection timed out` / `Connection refused`)        | runner address banned by fail2ban                                        | `sudo fail2ban-client unban --all`, then re-run the workflow                            |
| `Permission denied (publickey,password)` at the SSH step                                      | the stored CI key has a passphrase, or the wrong key                     | `ssh-keygen -p -f ~/.ssh/<your-ci-key-name>`, update `PRODUCTION_SSH_KEY`               |
| `Host key verification failed`                                                                | `PRODUCTION_KNOWN_HOSTS` stale or missing                                | refresh it with `ssh-keyscan -H <your-server-ip>`                                       |
| `runtime env file missing: /opt/ota-service/.env`                                             | host env file was never created                                          | create it from `deploy/env.production.example`, mode `600`                              |
| `database "ota" does not exist` or `password authentication failed` after a successful deploy | PG18 volume mounted at `/var/lib/postgresql/data`                        | move the mount to `/var/lib/postgresql` and recreate the service                        |
| Migration fails with a name-resolution error                                                  | `--no-deps` used with `compose run`                                      | drop `--no-deps` so the container joins the Compose network                             |
| `/health/ready` returns `503`, logs show MinIO connection errors                              | MinIO down or certificate material missing                               | check `otac ps`, regenerate the TLS material if absent, recreate `minio` and `api`      |
| `SSLError: [SSL: WRONG_VERSION_NUMBER]`                                                       | MinIO serving plaintext because `--certs-dir` is missing                 | add `--certs-dir /certs` to the `minio` command                                         |
| `SSLError: ... PermissionError(13, 'Permission denied')`                                      | internal CA certificate is not world-readable                            | `chmod 0644` the CA certificate; the generator script sets this                         |
| `minio` has no health state in `docker compose ps`                                            | by design — no in-image healthcheck                                      | probe externally over HTTPS with the internal CA                                        |
| `exec: "curl": executable file not found in $PATH` from a healthcheck                         | an in-container healthcheck was added to the MinIO service               | remove it; the image has no `curl`/`wget`                                               |
| `exec: "ota-admin": executable file not found in $PATH`                                       | image built before the entry-point fix                                   | redeploy current `main`; interim fallback is `python -m app.cli.bootstrap_admin`        |
| `pull access denied for minio/minio`                                                          | Compose references the removed official image                            | use `ghcr.io/golithus/minio:latest`                                                     |
| Deploy rolled back automatically                                                              | the new image failed `/health/ready`                                     | read the `deploy-diagnostics-<sha>` artifact; the previous image is already running     |
| Nginx serves the default site                                                                 | missing `sites-enabled` symlink or wrong `server_name`                   | inspect `nginx -T`, re-link, then `nginx -t && systemctl reload nginx`                  |
| A container port answers from the Internet despite UFW                                        | port published on `0.0.0.0`, or `ufw-docker` missing                     | publish on `127.0.0.1` only and install `ufw-docker`                                    |
| Devices receive `404` for the manifest                                                        | `OTA_MANIFEST_BASE_URL` differs from the value provisioned on the device | align both values                                                                       |
| Devices receive `403`                                                                         | unknown, MAC-mismatched, disabled, or retired device                     | check the device record and its raw eFuse MAC; the device locks itself out for 24 hours |
| `ufw enable` locked the operator out                                                          | port 22 was not allowed first                                            | regain access through the provider console and add `ufw allow 22/tcp`                   |

Operational recipes for a running instance, including the full command reference,
are in `docs/18-operations.md`.
