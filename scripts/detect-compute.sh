#!/usr/bin/env bash
# Detect whether an NVIDIA GPU is usable, persist COMPUTE=cpu|gpu into .env, and
# print (or run) the right docker compose command.
#
#   ./scripts/detect-compute.sh          # detect + write .env
#   ./scripts/detect-compute.sh --up     # detect + write .env + start the stack
#
# Conservative: GPU is only selected when nvidia-smi reports a GPU AND Docker
# exposes an 'nvidia' runtime. Otherwise CPU, which always works.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO/.env"
COMPUTE="cpu"

gpu_present=false
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
  gpu_present=true
fi

runtime_ok=false
if $gpu_present && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
  runtime_ok=true
fi

if $gpu_present && $runtime_ok; then
  COMPUTE="gpu"
  echo "GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1)  ->  COMPUTE=gpu"
elif $gpu_present; then
  echo "NVIDIA GPU found but Docker has no 'nvidia' runtime (install the NVIDIA Container Toolkit)."
  echo "Falling back to COMPUTE=cpu"
else
  echo "No NVIDIA GPU detected  ->  COMPUTE=cpu"
fi

touch "$ENV_FILE"
if grep -qE '^\s*COMPUTE\s*=' "$ENV_FILE"; then
  sed -i.bak -E "s|^\s*COMPUTE\s*=.*|COMPUTE=$COMPUTE|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
else
  printf '\nCOMPUTE=%s\n' "$COMPUTE" >> "$ENV_FILE"
fi
echo "Wrote COMPUTE=$COMPUTE to .env"

if [ "$COMPUTE" = "gpu" ]; then
  CMD="docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build"
else
  CMD="docker compose up -d --build"
fi

if [ "${1:-}" = "--up" ]; then
  echo "Running: $CMD"
  cd "$REPO" && eval "$CMD"
else
  echo
  echo "Next step:"
  echo "  $CMD"
fi
