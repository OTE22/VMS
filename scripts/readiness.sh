#!/usr/bin/env sh
# Full regression in READINESS mode (Phase 17): every required proof FAILS instead of
# skipping when its runtime is unavailable. Produces the evidence the final readiness
# report (Phase 18) is written from.
#
#   sh scripts/readiness.sh                # host run: unit + PG (throwaway container) + browser E2E
#   ARMYEYE_LIVE_RECREATION_TEST=1 sh scripts/readiness.sh   # + REAL container recreation of VMS
#
# Layers:
#   1. host pytest (unit, SQLite contracts, isolated PostgreSQL, node .mjs suites, Playwright E2E,
#      container recreation when enabled) with ARMYEYE_READINESS_RUN=1
#   2. the same PostgreSQL-backed contract suites inside the built VMS image (scripts/pg-test.sh)
#      so the exact production dependency set is exercised too
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/Scripts/python.exe}"
[ -x "$PY" ] || PY=python
OUT="${READINESS_OUT:-readiness-run}"
mkdir -p "$OUT"
export ARMYEYE_READINESS_RUN=1

echo "== [1/2] host pytest (readiness mode) =="
"$PY" -m pytest tests -p no:cacheprovider -q -rs --junitxml="$OUT/host-junit.xml" > "$OUT/host.log" 2>&1
HOST_RC=$?
tail -5 "$OUT/host.log"

echo "== [2/2] PostgreSQL contract suites inside the VMS image (readiness mode) =="
if command -v docker >/dev/null 2>&1 && docker inspect VMS-db >/dev/null 2>&1; then
  ARMYEYE_READINESS_RUN=1 sh scripts/pg-test.sh tests/test_registry_schema_pg.py tests/test_guardrails.py \
      tests/test_media_thumbnail_registry.py tests/test_engine_registry_security.py \
      tests/test_model_repo_lifecycle.py tests/test_publishers_settings_pg.py tests/test_artifact_verify.py \
      -q -rs -W ignore::DeprecationWarning > "$OUT/image-pg.log" 2>&1
  IMG_RC=$?
  tail -3 "$OUT/image-pg.log"
else
  echo "READINESS: VMS-db not reachable - image PG suites NOT VERIFIED" | tee "$OUT/image-pg.log"
  IMG_RC=1
fi

echo
echo "== summary =="
echo "host pytest exit: $HOST_RC   (see $OUT/host.log)"
echo "image PG suites exit: $IMG_RC (see $OUT/image-pg.log)"
grep -E "^(SKIPPED|FAILED|ERROR)" "$OUT/host.log" | sort | uniq -c | sort -rn | head -40 || true
# second guard: the summary lines themselves must not report failures/errors
if grep -Eq "[0-9]+ (failed|error)" "$OUT/host.log" "$OUT/image-pg.log"; then
  echo "READINESS RESULT: FAIL (failures/errors reported in a log)"; exit 1
fi
if [ "$HOST_RC" -eq 0 ] && [ "$IMG_RC" -eq 0 ]; then
  echo "READINESS RESULT: PASS (no required proof skipped or failed)"; exit 0
fi
echo "READINESS RESULT: FAIL"; exit 1
