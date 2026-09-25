#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE_TAG="${1:?usage: production-deploy.sh <immutable-image-tag>}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.production.yaml}"
STATE_FILE="${STATE_FILE:-.ota-previous-image}"
READINESS_URL="${OTA_READINESS_URL:?OTA_READINESS_URL is required}"
export OTA_IMAGE="${IMAGE_TAG}"

required_commands=(docker curl)
for command in "${required_commands[@]}"; do
  command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 1; }
done

docker compose -f "$COMPOSE_FILE" config -q

previous_image=""
if [[ -f "$STATE_FILE" ]]; then
  previous_image="$(<"$STATE_FILE")"
fi

rollback() {
  local failure_status=$?
  trap - ERR
  if [[ -n "$previous_image" ]]; then
    echo "Deployment failed; rolling back to $previous_image" >&2
    export OTA_IMAGE="$previous_image"
    if docker compose -f "$COMPOSE_FILE" up -d --no-build --remove-orphans api; then
      for attempt in {1..30}; do
        if curl --fail --silent --show-error --max-time 5 \
          "$READINESS_URL" >/dev/null; then
          echo "Rollback readiness check passed" >&2
          return
        fi
        sleep 2
      done
      echo "Rollback readiness check failed" >&2
    else
      echo "Rollback could not start the previous image" >&2
    fi
  else
    echo "Deployment failed and no previous image is recorded for rollback" >&2
  fi
  exit "$failure_status"
}
trap rollback ERR

docker pull "$OTA_IMAGE"
docker compose -f "$COMPOSE_FILE" run --rm api alembic upgrade head
docker compose -f "$COMPOSE_FILE" up -d --no-build --remove-orphans api

for attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 5 "$READINESS_URL" >/dev/null; then
    printf '%s\n' "$IMAGE_TAG" >"$STATE_FILE"
    trap - ERR
    echo "Deployment succeeded: $IMAGE_TAG"
    exit 0
  fi
  sleep 2
done

echo "Readiness did not become healthy" >&2
false
