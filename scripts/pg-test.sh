#!/usr/bin/env sh
# Run PostgreSQL-backed persistence tests in an isolated throwaway container on the
# ArmyEye compose network. Uses the built application image (same deps + alembic),
# mounts the repo, creates/drops its OWN database (armeye_test_*), never touches `armeye`.
#   usage: scripts/pg-test.sh [pytest args...]        (ARMYEYE_READINESS_RUN=1 for readiness mode)
set -eu
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
PW=$(grep '^ARMEYE_DB_PASSWORD=' .env | cut -d= -f2)
NET=$(docker inspect VMS-db --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
HOSTDIR=$(pwd -W 2>/dev/null || pwd)
exec docker run --rm --network "$NET" \
  -v "${HOSTDIR}:/work" -w /work \
  -e ARMYEYE_TEST_PG_ADMIN_URL="postgresql://armeye:${PW}@db:5432/postgres" \
  -e ARMYEYE_READINESS_RUN="${ARMYEYE_READINESS_RUN:-}" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint sh armyeye-vms -c \
  'pip install -q pytest "cryptography==44.0.1" >/dev/null 2>&1; python -m pytest -p no:cacheprovider "$@"' -- "$@"
