#!/usr/bin/env bash
# Idempotent production deploy for postgres + minio + api.
# Usage: production-deploy.sh <immutable-api-image-tag>
# Required env: OTA_READINESS_URL
# Optional env: COMPOSE_FILE, STATE_FILE, OTA_ENV_FILE, DEPLOY_PATH
set -euo pipefail

IMAGE_TAG="${1:?usage: production-deploy.sh <immutable-api-image-tag>}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.production.yaml}"
STATE_FILE="${STATE_FILE:-.ota-previous-image}"
DEPLOY_PATH="${DEPLOY_PATH:-$(pwd)}"
ENV_FILE="${OTA_ENV_FILE:-${DEPLOY_PATH}/.env}"
READINESS_URL="${OTA_READINESS_URL:?OTA_READINESS_URL is required}"
CERT_SCRIPT="${CERT_SCRIPT:-${DEPLOY_PATH}/deploy/certs/generate-minio-tls-material.sh}"
export OTA_IMAGE="${IMAGE_TAG}"
export DEPLOY_PATH
export OTA_ENV_FILE="$ENV_FILE"

log() { printf '[deploy] %s\n' "$*"; }
err() { printf '[deploy] ERROR: %s\n' "$*" >&2; }

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

required_commands=(docker curl)
for command in "${required_commands[@]}"; do
  command -v "$command" >/dev/null || { err "missing command: $command"; exit 1; }
done

log "docker=$(docker --version)"
log "compose=$(docker compose version 2>/dev/null || true)"
log "image=${OTA_IMAGE}"
log "compose_file=${COMPOSE_FILE}"
log "env_file=${ENV_FILE}"
log "readiness_url=${READINESS_URL}"
log "deploy_path=${DEPLOY_PATH}"

if [[ ! -f "$ENV_FILE" ]]; then
  err "runtime env file missing: ${ENV_FILE}"
  err "Create it once from deploy/env.production.example before the first deploy."
  exit 1
fi

if [[ ! -x "$CERT_SCRIPT" ]]; then
  err "MinIO TLS script missing or not executable: ${CERT_SCRIPT}"
  exit 1
fi

log "ensuring MinIO TLS material (idempotent)"
"$CERT_SCRIPT"

compose config -q
log "compose config OK"

previous_image=""
if [[ -f "$STATE_FILE" ]]; then
  previous_image="$(<"$STATE_FILE")"
  log "previous_image=${previous_image}"
else
  log "previous_image=(none — first deploy)"
fi

wait_ready() {
  local label="$1"
  local attempt
  for attempt in $(seq 1 45); do
    if curl --fail --silent --show-error --max-time 5 \
      "$READINESS_URL" >/dev/null; then
      log "${label}: readiness OK (attempt ${attempt})"
      return 0
    fi
    log "${label}: waiting for readiness (attempt ${attempt}/45)"
    sleep 2
  done
  err "${label}: readiness did not become healthy"
  return 1
}

rollback() {
  local failure_status=$?
  trap - ERR
  err "deployment failed (exit ${failure_status})"
  if [[ -n "$previous_image" ]]; then
    err "rolling back API image to ${previous_image}"
    export OTA_IMAGE="$previous_image"
    if compose up -d --no-build --remove-orphans api; then
      if wait_ready "rollback"; then
        err "rollback succeeded"
        exit "$failure_status"
      fi
      err "rollback readiness check failed"
    else
      err "rollback could not start the previous API image"
    fi
  else
    err "no previous API image recorded for rollback"
  fi
  compose ps -a || true
  compose logs --no-color --tail=100 api postgres minio || true
  exit "$failure_status"
}
trap rollback ERR

log "pulling API image"
docker pull "$OTA_IMAGE"

log "starting postgres + minio and waiting until healthy (volumes preserved)"
compose up -d --no-build --remove-orphans postgres minio

log "running migrations"
compose run --rm --no-deps api alembic upgrade head

log "starting api"
compose up -d --no-build --remove-orphans api

log "waiting for MinIO to accept connections"
for attempt in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1; then
    log "MinIO ready after ${attempt}s"
    break
  fi
  if [[ "$attempt" == "30" ]]; then
    err "MinIO did not become ready in 30s"
    exit 1
  fi
  sleep 1
done

wait_ready "deploy"

printf '%s\n' "$IMAGE_TAG" >"$STATE_FILE"
trap - ERR
log "deployment succeeded: ${IMAGE_TAG}"
compose ps
