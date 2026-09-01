#!/usr/bin/env bash
# ArmyEye deployment launcher: validates the .env deployment selection, then
# delegates everything to docker compose (no compose logic duplicated here).
#
#   ./docker-start.sh            # validate + up -d
#   ./docker-start.sh --build    # validate + build + up -d
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

fail() { echo "ERROR: $*" >&2; exit 1; }

# 1. Load deployment settings
[ -f .env ] || fail ".env not found - copy .env.example and configure it"
set -a; . ./.env; set +a

# 2-3. Validate explicit APP_ENV / ACCELERATOR (never guessed)
case "${APP_ENV:-}" in
  development|production) ;;
  *) fail "APP_ENV must be 'development' or 'production' (got '${APP_ENV:-unset}')" ;;
esac
case "${ACCELERATOR:-}" in
  cpu|gpu) ;;
  *) fail "ACCELERATOR must be 'cpu' or 'gpu' (got '${ACCELERATOR:-unset}')" ;;
esac

# 4. Required variables (hard requirements in production)
[ -n "${ARMEYE_DB_PASSWORD:-}" ] || fail "ARMEYE_DB_PASSWORD is required"
if [ "$APP_ENV" = "production" ]; then
  [ -n "${FLASK_SECRET_KEY:-}" ]  || fail "FLASK_SECRET_KEY is required in production"
  [ -n "${ADMIN_PASSWORD:-}" ]    || fail "ADMIN_PASSWORD is required in production (initial admin seed)"
  [ -n "${WEBHOOK_AUTH_TOKEN:-}" ] || echo "WARNING: WEBHOOK_AUTH_TOKEN empty - detection webhooks will not send (fail-closed)"
  # Presence check only. The application's validate_base_url() is authoritative for
  # the format - deliberately NOT duplicated here so the rules cannot drift.
  [ -n "${WEBHOOK_BASE_URL:-}" ] || echo "WARNING: WEBHOOK_BASE_URL empty - webhooks fall back to deprecated per-pipeline URLs"
fi

# 5. COMPOSE_FILE must agree with APP_ENV + ACCELERATOR (no silent prod-with-dev)
[ -n "${COMPOSE_FILE:-}" ] || fail "COMPOSE_FILE is not set - see .env.example"
case "$APP_ENV" in
  production)  echo "$COMPOSE_FILE" | grep -q "compose.prod.yaml" || fail "APP_ENV=production but COMPOSE_FILE lacks compose.prod.yaml ($COMPOSE_FILE)"
               echo "$COMPOSE_FILE" | grep -q "compose.dev.yaml" && fail "APP_ENV=production but COMPOSE_FILE contains compose.dev.yaml" ;;
  development) echo "$COMPOSE_FILE" | grep -q "compose.dev.yaml" || fail "APP_ENV=development but COMPOSE_FILE lacks compose.dev.yaml ($COMPOSE_FILE)"
               echo "$COMPOSE_FILE" | grep -q "compose.prod.yaml" && fail "APP_ENV=development but COMPOSE_FILE contains compose.prod.yaml" ;;
esac
case "$ACCELERATOR" in
  cpu) echo "$COMPOSE_FILE" | grep -q "compose.cpu.yaml" || fail "ACCELERATOR=cpu but COMPOSE_FILE lacks compose.cpu.yaml ($COMPOSE_FILE)"
       echo "$COMPOSE_FILE" | grep -q "compose.gpu.yaml" && fail "ACCELERATOR=cpu but COMPOSE_FILE contains compose.gpu.yaml" ;;
  gpu) echo "$COMPOSE_FILE" | grep -q "compose.gpu.yaml" || fail "ACCELERATOR=gpu but COMPOSE_FILE lacks compose.gpu.yaml ($COMPOSE_FILE)" ;;
esac

# GPU mode: hardware may VALIDATE the request, never silently downgrade it.
if [ "$ACCELERATOR" = "gpu" ]; then
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1 \
    || fail "ACCELERATOR=gpu but no working NVIDIA GPU (nvidia-smi). Refusing to fall back to CPU silently."
  docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia \
    || fail "ACCELERATOR=gpu but Docker has no 'nvidia' runtime (install the NVIDIA Container Toolkit)."
fi

# 6. Shared webhook network (external): VMS -> FACE nginx (alias face-webhook).
# Infrastructure, not owned by either compose project. Race-safe: two deployments
# starting at once may both pass the inspect and one `create` will lose - that
# specific failure is fine IFF the network then exists; anything else is fatal.
# We never blanket-ignore `docker network create` errors.
if ! docker network inspect webhook_integration >/dev/null 2>&1; then
  echo "Provisioning shared network: webhook_integration"
  if ! create_err=$(docker network create webhook_integration 2>&1 >/dev/null); then
    docker network inspect webhook_integration >/dev/null 2>&1 \
      || fail "cannot create network webhook_integration: $create_err"
    echo "OK: webhook_integration was created concurrently by another deployment"
  fi
fi
echo "OK: shared network webhook_integration present"

# 7. Validate the merged compose configuration; fail immediately if invalid
docker compose config -q || fail "docker compose config failed for: $COMPOSE_FILE"
echo "OK: APP_ENV=$APP_ENV ACCELERATOR=$ACCELERATOR"
echo "OK: compose configuration valid ($COMPOSE_FILE)"

# 8. Build if requested
if [ "${1:-}" = "--build" ]; then
  docker compose build
fi

# 9-10. Start and show status
docker compose up -d
docker compose ps
