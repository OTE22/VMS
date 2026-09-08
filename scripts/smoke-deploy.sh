#!/usr/bin/env bash
# Phase D smoke test: prove the running stack is actually healthy, with evidence from the
cd /home/itdirect-ai/Desktop/VMS
# database and the artifact filesystem - not just "the container is up".
#
# Every check is fail-loud and read-only except the login it performs as the seeded admin.
# Run from the repo root AFTER `docker-start.sh` reports the containers started.
set -uo pipefail
PASS=0; FAIL=0
ok(){ echo "  PASS  $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
chk(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1"; fi; }

echo "=== 1. containers ==="
for c in VMS VMS-db; do
  s=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}nohc{{end}}' "$c" 2>/dev/null)
  case "$s" in running/healthy|running/nohc) ok "$c: $s";; *) no "$c: ${s:-absent}";; esac
done

echo "=== 2. database migrated to head ==="
REV=$(docker exec VMS-db psql -U armeye -d armeye -tAc "select version_num from alembic_version" 2>/dev/null)
[ "$REV" = "0005_pipeline_model_integrity" ] && ok "alembic head: $REV" || no "alembic head: ${REV:-unreadable}"
T=$(docker exec VMS-db psql -U armeye -d armeye -tAc "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null)
[ "${T:-0}" -ge 14 ] && ok "tables present: $T" || no "tables present: ${T:-0} (expected >=14)"

echo "=== 3. registry contents (counts are informational; consistency is checked in 7) ==="
# NOT asserted as zero: a deployed system legitimately accumulates models, media and
# pipelines. What matters is that every registered row resolves to real bytes, which the
# reconciliation in section 7 proves. Only the builtin engines have a required floor.
for t in pipelines models media_assets pipeline_thumbnails; do
  n=$(docker exec VMS-db psql -U armeye -d armeye -tAc "select count(*) from $t" 2>/dev/null)
  echo "  ----  $t=${n:-?}"
done
n=$(docker exec VMS-db psql -U armeye -d armeye -tAc "select count(*) from inference_engines" 2>/dev/null)
[ "${n:-0}" -ge 5 ] && ok "inference_engines=$n (builtins registered)" || no "inference_engines=${n:-?} (expected >=5 builtins)"

echo "=== 4. artifact root writable + laid out ==="
docker exec VMS sh -c 'for k in models engines media thumbnails; do [ -d "/app/InferenceNode/data/$k" ] || exit 1; done' \
  && ok "ARTIFACT_ROOT layout" || no "ARTIFACT_ROOT layout"
docker exec VMS sh -c 'touch /app/InferenceNode/data/.wtest && rm /app/InferenceNode/data/.wtest' \
  && ok "ARTIFACT_ROOT writable by app user" || no "ARTIFACT_ROOT writable"

echo "=== 5. secrets staged privately (never on the host) ==="
docker exec VMS sh -c 'test -f /run/armyeye/config.key && [ "$(stat -c %a /run/armyeye/config.key)" = "400" ]' \
  && ok "config key staged 0400 in tmpfs" || no "config key staging"
# NOTE: `docker exec` starts a NEW process and does NOT inherit the entrypoint's exports,
# so the app's env must be read from pid 1 - checking $REQUESTS_CA_BUNDLE in an exec shell
# gives a false failure.
docker exec VMS sh -c 'tr "\0" "\n" < /proc/1/environ | grep -q "^REQUESTS_CA_BUNDLE=/run/armyeye/ca-bundle.pem$"' \
  && ok "app process has REQUESTS_CA_BUNDLE" || no "app process CA env"
docker exec VMS sh -c '[ "$(grep -c "BEGIN CERTIFICATE" /run/armyeye/ca-bundle.pem)" -gt 100 ]' \
  && ok "CA bundle built (public + FACE internal CA)" || no "CA bundle"
docker exec -e B=/run/armyeye/ca-bundle.pem VMS python -c "import certifi,os,sys; sys.exit(0 if os.path.getsize(os.environ['B'])>os.path.getsize(certifi.where()) else 1)" \
  && ok "CA bundle is public bundle PLUS the private CA (not a replacement)" || no "CA bundle superset check"
docker exec -e B=/run/armyeye/ca-bundle.pem VMS python -c "
import ssl,urllib.request,os
ctx=ssl.create_default_context(cafile=os.environ['B'])
urllib.request.urlopen('https://github.com', context=ctx, timeout=20)" >/dev/null 2>&1 \
  && ok "public HTTPS still verifies with the same bundle" || no "public HTTPS broken by CA bundle"

echo "=== 6. HTTP surface ==="
chk "GET /health"        "curl -fsS --max-time 10 http://localhost:5555/health"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost:5555/api/models)
[ "$code" = "401" ] && ok "unauthenticated /api/models -> 401 (login gate active)" || no "unauthenticated /api/models -> $code (expected 401)"

echo "=== 7. registry verify (app code; seeded admin is intentionally forced to /change-password) ==="
LOGIN=$(curl -s -o /dev/null -w '%{redirect_url}' -X POST http://localhost:5555/login \
  --data-urlencode "username=$(grep '^ADMIN_USERNAME=' .env | cut -d= -f2)" --data-urlencode "password=x" 2>/dev/null)
docker exec VMS python -c "
import sys; sys.path.insert(0,'/app')
from InferenceNode.auth import db; db.init_engine()
from InferenceNode.model_repo import ModelRepository
from InferenceNode.artifact_verify import verify_all
r=verify_all(ModelRepository('/app/InferenceNode/model_repository', auto_migrate=False))
sys.exit(0 if r['summary']['verdict']=='healthy' else 1)" 2>/dev/null \
  && ok "registry reconciliation: healthy" || no "registry reconciliation"

echo "=== 8. GPU visible to the running app ==="
docker exec VMS python -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" \
  && ok "torch.cuda.is_available() inside VMS" || no "CUDA not visible inside VMS"
docker exec VMS nvidia-smi -L >/dev/null 2>&1 && ok "nvidia-smi inside VMS" || no "nvidia-smi inside VMS"

echo "=== 9. webhook egress to FACE (TLS + auth, no detection sent) ==="
docker exec -i -e REQUESTS_CA_BUNDLE=/run/armyeye/ca-bundle.pem -e WEBHOOK_BASE_URL=https://face-detector.internal VMS python - <<'PY'
import os, ssl, urllib.request, urllib.error
base=os.environ["WEBHOOK_BASE_URL"]; ctx=ssl.create_default_context(cafile=os.environ["REQUESTS_CA_BUNDLE"])
req=urllib.request.Request(f"{base}/webhook/vms-smoke-probe", data=b"{}", method="POST",
                           headers={"Content-Type":"application/json","Authorization":"Bearer deliberately-invalid"})
try:
    urllib.request.urlopen(req, context=ctx, timeout=15); print("  FAIL  invalid token accepted (auth not enforced)")
except urllib.error.HTTPError as e:
    print(f"  {'PASS' if e.code in (401,403) else 'FAIL'}  TLS verified + route reached + auth enforced -> HTTP {e.code}")
    raise SystemExit(0 if e.code in (401,403) else 1)
except Exception as e:
    print(f"  FAIL  webhook egress: {type(e).__name__}: {e}"); raise SystemExit(1)
PY
[ $? -eq 0 ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1))

echo
echo "============================================================"
echo "SMOKE: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
