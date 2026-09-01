"""Phase 16 - REAL container recreation proof (not restart) against the compose stack.

Enabled ONLY with ARMYEYE_LIVE_RECREATION_TEST=1 (dev runs skip; readiness runs FAIL when
it is not enabled/available). It never deletes or truncates anything that already exists:
it creates its OWN records - a custom engine `Repro Recreate Cam` and a favorite
publisher `REPRO-recreate` - via the live API, runs `docker compose up -d --force-recreate
vms`, proves the PostgreSQL rows + artifact bytes (sha256 == row) + API/UI listing
survive, then removes exactly those records.

Requires: docker compose stack up (VMS, VMS-db), admin credentials in
ARMYEYE_LIVE_ADMIN_USERNAME/PASSWORD (falls back to ADMIN_USERNAME/ADMIN_PASSWORD from
.env), live base URL ARMYEYE_LIVE_BASE_URL (default http://localhost:5555).
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))
from conftest import readiness_required  # noqa: E402

ENABLED = os.environ.get("ARMYEYE_LIVE_RECREATION_TEST", "").strip().lower() in ("1", "true", "yes", "on")
BASE = os.environ.get("ARMYEYE_LIVE_BASE_URL", "http://localhost:5555")
ENGINE_DISPLAY, ENGINE_KEY = "Repro Recreate Cam", "repro_recreate_cam"
PUB_NAME = "REPRO-recreate"


def _env_file():
    out = {}
    p = os.path.join(REPO, ".env")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); out[k] = v
    return out


def _psql(sql: str) -> str:
    r = subprocess.run(["docker", "exec", "VMS-db", "psql", "-U", "armeye", "-d", "armeye", "-tAc", sql],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _in_vms(cmd: str) -> str:
    r = subprocess.run(["docker", "exec", "VMS", "sh", "-c", cmd], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture(scope="module")
def live():
    if not ENABLED:
        readiness_required("ARMYEYE_LIVE_RECREATION_TEST not enabled - container recreation NOT VERIFIED")
    import requests
    envf = _env_file()
    user = os.environ.get("ARMYEYE_LIVE_ADMIN_USERNAME") or envf.get("ADMIN_USERNAME")
    pw = os.environ.get("ARMYEYE_LIVE_ADMIN_PASSWORD") or envf.get("ADMIN_PASSWORD")
    if not user or not pw:
        readiness_required("no live admin credentials - container recreation NOT VERIFIED")
    for c in ("VMS", "VMS-db"):
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", c], capture_output=True, text=True)
        if r.stdout.strip() != "true":
            readiness_required(f"container {c} not running - container recreation NOT VERIFIED")

    def login():
        s = requests.Session()
        r = s.get(BASE + "/login", timeout=30)
        tok = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
        r = s.post(BASE + "/login", data={"username": user, "password": pw, "csrf_token": tok.group(1) if tok else ""},
                   allow_redirects=False, timeout=30)
        assert r.status_code in (302, 303), f"live login failed: {r.status_code}"
        page = s.get(BASE + "/", timeout=30)
        m = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
        s.headers["X-CSRFToken"] = m.group(1) if m else ""
        return s
    return {"login": login, "user": user}


def _cleanup(s):
    s.delete(f"{BASE}/api/inference/engines/{ENGINE_KEY}", timeout=60)
    favs = s.get(f"{BASE}/api/publisher/favorites", timeout=30).json()
    for f in (favs.get("favorites") or favs.get("publishers") or []):
        if f.get("name") == PUB_NAME:
            s.delete(f"{BASE}/api/publisher/favorites/{f['id']}", timeout=30)


def _wait_ready(timeout=900):
    import requests
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(BASE + "/login", timeout=5).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


def test_models_and_engines_survive_force_recreate(live):
    s = live["login"]()
    _cleanup(s)                                    # idempotent start
    # --- create REPRO records through the live API
    r = s.post(f"{BASE}/api/inference/engines", json={"preset": "blank", "fields": {"display_name": ENGINE_DISPLAY}}, timeout=120)
    assert r.status_code in (200, 201), r.text
    r = s.post(f"{BASE}/api/publisher/favorites", json={"name": PUB_NAME, "type": "mqtt",
                                                       "config": {"server": "127.0.0.1", "port": 1883, "password": "REPRO-PW"}}, timeout=30)
    assert r.status_code in (200, 201), r.text
    try:
        row = _psql(f"SELECT origin||'|'||status||'|'||validation_status||'|'||enabled||'|'||relative_path||'|'||sha256 "
                    f"FROM inference_engines WHERE engine_key='{ENGINE_KEY}'")
        origin, status, vstatus, enabled, rel, sha = row.split("|")
        assert (origin, status, vstatus, enabled) == ("custom", "AVAILABLE", "PASSED", "true")
        assert _in_vms(f"sha256sum /app/InferenceNode/data/engines/{rel} | cut -d' ' -f1") == sha
        # host bind mount holds the bytes (this is what makes recreation survivable)
        host_path = os.path.join(REPO, "InferenceNode", "data", "engines", *rel.split("/"))
        assert os.path.isfile(host_path) and hashlib.sha256(open(host_path, "rb").read()).hexdigest() == sha
        models_before = _psql("SELECT count(*) FROM models")
        model_rows = _psql("SELECT model_id||'|'||status FROM models ORDER BY model_id")
        art_rows = _psql("SELECT relative_path||'|'||sha256 FROM model_artifacts WHERE status='AVAILABLE' ORDER BY relative_path")
        pub_cfg = _psql(f"SELECT config::text FROM publishers WHERE name='{PUB_NAME}'")
        assert "REPRO-PW" not in pub_cfg and '"ct"' in pub_cfg
        cid_before = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", "VMS"], capture_output=True, text=True).stdout.strip()

        # --- REAL recreation (new container id), not a restart
        rec = subprocess.run(["docker", "compose", "up", "-d", "--force-recreate", "vms"], cwd=REPO,
                             capture_output=True, text=True, timeout=600)
        assert rec.returncode == 0, rec.stderr
        cid_after = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", "VMS"], capture_output=True, text=True).stdout.strip()
        assert cid_after and cid_after != cid_before, "container was not recreated"
        assert _wait_ready(), "VMS did not come back after recreation"

        # --- proofs after recreation: rows, bytes, hashes, API/UI listing
        row2 = _psql(f"SELECT status||'|'||validation_status||'|'||enabled||'|'||sha256 FROM inference_engines WHERE engine_key='{ENGINE_KEY}'")
        assert row2 == f"AVAILABLE|PASSED|true|{sha}"
        assert _in_vms(f"sha256sum /app/InferenceNode/data/engines/{rel} | cut -d' ' -f1") == sha
        assert _psql("SELECT count(*) FROM models") == models_before
        assert _psql("SELECT model_id||'|'||status FROM models ORDER BY model_id") == model_rows
        assert _psql("SELECT relative_path||'|'||sha256 FROM model_artifacts WHERE status='AVAILABLE' ORDER BY relative_path") == art_rows
        for line in art_rows.splitlines():
            rp, ash = line.split("|")
            assert _in_vms(f"sha256sum '/app/InferenceNode/data/models/{rp}' | cut -d' ' -f1") == ash, rp
        s = live["login"]()
        reg = s.get(f"{BASE}/api/inference/engines/registry", timeout=60).json()
        assert any(e["engine_key"] == ENGINE_KEY and e["status"] == "AVAILABLE" for e in reg["engines"])
        assert ENGINE_KEY in s.get(f"{BASE}/api/inference/engines", timeout=60).text     # factory activated it
        favs = s.get(f"{BASE}/api/publisher/favorites", timeout=30)
        assert PUB_NAME in favs.text and "REPRO-PW" not in favs.text
        ver = s.get(f"{BASE}/api/registry/verify", timeout=120).json()
        assert ENGINE_KEY in ver["engines"]["available_valid"]
        assert ver["summary"]["verdict"] in ("healthy", "degraded")     # reported below in the readiness report
        print("\n[RECREATION] verdict:", ver["summary"])
    finally:
        try:
            s = live["login"]()
            _cleanup(s)
        except Exception as e:  # noqa: BLE001
            print("cleanup failed:", e)
    assert _psql(f"SELECT count(*) FROM inference_engines WHERE engine_key='{ENGINE_KEY}'") == "0"
    assert _psql(f"SELECT count(*) FROM publishers WHERE name='{PUB_NAME}'") == "0"
