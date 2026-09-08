# ArmyEye (VMS) — Production Deployment & Operations Manual

**Host:** `itdirect-ai` · **Deployed:** 2026-09-08 · **Repo:** `~/Desktop/VMS` (`github.com/OTE22/VMS`)

This is the operational reference for the ArmyEye deployment on the GPU host. Every fact in
it was verified on this machine at deployment time; anything not verified is marked
explicitly as such.

**Related documents**
- [MOVING_TO_A_SEPARATE_SERVER.md](MOVING_TO_A_SEPARATE_SERVER.md) — the planned move off this host
- [PRODUCTION_READINESS_REPORT.md](PRODUCTION_READINESS_REPORT.md) — the persistence architecture this deployment implements
- [STEP0_GPU_BASELINE.md](STEP0_GPU_BASELINE.md) — performance baseline methodology

---

## 1. What is deployed

Two **independent** systems share this machine. They are deliberately not coupled: ArmyEye
will move to its own server later.

```
┌─────────────────────────── host: itdirect-ai (192.168.1.111) ───────────────────────────┐
│                                                                                          │
│  ArmyEye / VMS  (this repo)                    FACE_DETECTOR  (~/Desktop/VAS)            │
│  ┌────────────────────────┐                    ┌──────────────────────────────────────┐  │
│  │ VMS      :5555, :8888  │  detections        │ nginx        :80 :443                │  │
│  │  capture → detect →    │───────────────────▶│  ├ face_recognition (API)            │  │
│  │  publish               │  HTTPS webhook     │  ├ ml_worker                         │  │
│  │ VMS-db   postgres:16   │  (bearer auth)     │  ├ postgres (pgvector) · redis        │  │
│  └────────────────────────┘                    │  ├ ollama · martin (maps)            │  │
│         armyeye_armyeye net                    │  └ prometheus · grafana              │  │
│                    └──── webhook_integration ──┴──────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

ArmyEye has **no database dependency** on FACE_DETECTOR. The only link is one outbound
HTTPS webhook. Either system can be restarted without the other.

### Containers

| Container | Image | Ports | Restart |
|---|---|---|---|
| `VMS` | `armyeye-vms` (13.1 GB, built locally) | `5555/tcp` UI+API, `8888/udp` discovery | `unless-stopped` |
| `VMS-db` | `postgres:16-alpine` (16.15) | none published — reachable only as `db:5432` | `unless-stopped` |

### Software versions (inside the image)

| | |
|---|---|
| Python | 3.11.16 |
| **torch** | **2.14.0+cu130** (CUDA 13.0) |
| Ultralytics | 8.4.143 |
| OpenCV | 5.0.0 |
| OpenVINO | 2026.3.1 |
| Flask / SQLAlchemy | 3.1.3 / 2.0.52 |

### Hardware

| | |
|---|---|
| GPU | NVIDIA GeForce RTX 5090, 32 GB (31.4 GiB usable), **Blackwell `sm_120`** |
| Driver | 595.84 (CUDA 13.2) — driver ≥ runtime ✓ |
| Container runtime | `nvidia` present in Docker; GPU reserved via `compose.gpu.yaml` |
| Network | **WiFi** `wlp130s0f0`, DHCP `192.168.1.111/24`, gateway `192.168.1.1` |
| Disk | 1.8 TB, 12 % used |

> **Known constraint — WiFi.** The image build pulled 3.2 GB at 1–1.9 MB/s (59 minutes). More
> importantly, 30+ RTSP streams need 120–240 Mbit/s sustained. **Wire this host before scale
> testing.** This is currently the single largest limit on camera capacity, and no amount of
> code will fix it.

---

## 2. Access

| | |
|---|---|
| Local | `http://localhost:5555` |
| LAN | `http://192.168.1.111:5555` |
| mDNS | `http://itdirect-ai.local:5555` — works LAN-wide, no configuration (avahi is running) |

Ports 80/443 belong to FACE's nginx, so ArmyEye keeps its own port. That is deliberate:
routing ArmyEye through FACE's edge would couple two systems that are due to be separated.

**First login.** Username and password are in `.env` (`ADMIN_USERNAME` / `ADMIN_PASSWORD`,
mode 0600). The seeded admin is forced to `/change-password` on first login — the seeded
value is a one-time bootstrap credential and must be replaced by a human.

```bash
grep '^ADMIN_PASSWORD=' ~/Desktop/VMS/.env | cut -d= -f2-
```

---

## 3. Configuration

### 3.1 Compose layering

Compose files are selected by `COMPOSE_FILE` in `.env` and validated by `docker-start.sh`,
which refuses mismatched combinations (e.g. `APP_ENV=production` with `compose.dev.yaml`).

| File | Purpose |
|---|---|
| `compose.yaml` | base: `vms` + `db`, volumes, secrets, networks |
| `compose.prod.yaml` | production: `restart: unless-stopped`, published ports, log rotation, **no published DB port** |
| `compose.dev.yaml` | development: publishes DB on 5433, `restart: no` |
| `compose.gpu.yaml` | `COMPUTE=gpu` build arg + NVIDIA device reservation |
| `compose.cpu.yaml` | `COMPUTE=cpu` build arg, no GPU |
| `compose.remote-face.yaml` | **only when FACE is on another host** — see §9 |

**Active:** `compose.yaml:compose.prod.yaml:compose.gpu.yaml`

> These files were lost once. The repo was published with a blanket `*.yaml` ignore rule, so a
> fresh clone had nothing to deploy with. The current `.gitignore` protects secrets **without**
> any wildcard yaml rule. Do not reintroduce one.

### 3.2 Environment variables

`.env` (mode 0600, never committed). `.env.example` is the annotated template.

**Deployment selection**

| Variable | Value here | Notes |
|---|---|---|
| `APP_ENV` | `production` | validated against `COMPOSE_FILE` |
| `ACCELERATOR` | `gpu` | `docker-start.sh` refuses if no working GPU — never silently falls back |
| `COMPUTE` | `gpu` | build arg selecting `requirements-gpu.txt` |
| `COMPOSE_FILE` | see above | `COMPOSE_PATH_SEPARATOR=:` |

**Application**

| Variable | Purpose |
|---|---|
| `NODE_NAME` / `NODE_PORT` | display name; HTTP port (5555) |
| `ARMEYE_DB_PASSWORD` | PostgreSQL password (generated, 24-byte urlsafe) |
| `ARMYEYE_DATABASE_URL` | built from the above; points at `db:5432` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | seeds the first admin **only into an empty users table** |
| `FLASK_SECRET_KEY` | session signing (32-byte hex). Changing it invalidates all sessions |
| `ENABLE_ENGINE_BUILDER` | `false` — the builder writes server-side Python; keep off unless needed |

**FACE integration**

| Variable | Value | Notes |
|---|---|---|
| `WEBHOOK_BASE_URL` | `https://face-detector.internal` | **a name, never an IP** — see §5.3 |
| `WEBHOOK_AUTH_TOKEN` | copied from FACE's `webhook_api_keys` | |
| `WEBHOOK_AUTH_REQUIRED` | `true` | fail-closed: no token ⇒ nothing is sent |
| `FACE_INTERNAL_CA_FILE` | `./secrets/face-internal-ca.crt` | FACE's private CA |
| `FACE_HOST_IP` | *(unset — same host)* | set only with `compose.remote-face.yaml` |

**Set by the entrypoint, not by `.env`**

| Variable | Value |
|---|---|
| `ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE` | `/run/armyeye/config.key` (staged 0400) |
| `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` | `/run/armyeye/ca-bundle.pem` |

### 3.3 Files on disk

```
~/Desktop/VMS/
├── .env                                 0600  never committed
├── secrets/                             0700
│   ├── armyeye_config_key               0600  ⚠ see §5.1 — irreplaceable
│   └── face-internal-ca.crt             0644  FACE's CA (public material)
├── compose*.yaml                              tracked
└── InferenceNode/
    ├── data/                            ARTIFACT_ROOT — models, engines, media, thumbnails
    ├── model_repository/  media/  pipelines/  legacy migration sources (read once)
    └── logs/
```

---

## 4. Storage and persistence

The architecture separates **metadata** from **bytes** (see the readiness report):

```
PostgreSQL (volume armyeye_armeye_pgdata) = authoritative metadata, registry, config, lifecycle
ARTIFACT_ROOT (./InferenceNode/data)      = the managed bytes themselves
linked by: id + relative_path + sha256 + size + status + fingerprint
```

No large binaries in PostgreSQL; no plaintext secrets in PostgreSQL. A file existing on disk
is never sufficient — it must have an `AVAILABLE`/`PASSED` registry row whose hash matches.

| What | Where | Survives `docker compose down`? |
|---|---|---|
| Database | named volume `armyeye_armeye_pgdata` | yes (removed only by `down -v`) |
| Models / engines / media / thumbnails | bind mount `InferenceNode/data` | yes — on the host filesystem |
| Logs | bind mount `InferenceNode/logs` | yes |
| Staged secrets | tmpfs `/run/armyeye` | **no** — re-created each start, by design |

### Backup

```bash
docker exec VMS-db pg_dump -U armeye -d armeye -Fc > armeye-$(date +%F).dump
tar -C InferenceNode -czf armeye-artifacts-$(date +%F).tgz data
tar -czf armeye-secrets-$(date +%F).tgz secrets .env      # treat as a credential
```

**All three belong together.** A database backup without `secrets/armyeye_config_key` cannot
decrypt stored credentials — see §5.1.

---

## 5. Security model

### 5.1 Configuration-encryption key ⚠

Publisher and telemetry secrets are encrypted at rest with Fernet. Stored shape:
`{"v":1,"key_id":"armyeye-prod-2026-09","ct":"…"}`.

- The key lives **only** in `secrets/armyeye_config_key` — never in PostgreSQL, never in the
  image, never auto-generated.
- The entrypoint stages a private `0400` copy in a container-only tmpfs, because bind mounts
  cannot always carry restrictive modes.
- Missing or wrong key ⇒ affected credentials are reported unavailable and **not served**;
  configuration is never destroyed or silently re-encrypted.

> **Losing this file makes every encrypted credential permanently unreadable.** There is no
> recovery, because there is deliberately no second copy. Back it up with the database, and
> keep it out of the repo (`.gitignore` enforces this).
>
> Currently **0 encrypted rows** exist — it becomes load-bearing the moment you save your
> first MQTT/webhook credential.

### 5.2 TLS trust — a superset, never a replacement

FACE's certificate is signed by its own internal CA. The entrypoint **appends** that CA to
`certifi`'s public bundle and points `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE` at the result.

Pointing those at the private CA *alone* would verify FACE while silently breaking every
public HTTPS call — Ultralytics weight downloads included. Verification is never disabled.

Verified: 242,165 bytes, **122 certificates**; FACE verifies **and** `github.com` still
returns 200 through the same bundle.

### 5.3 Why the webhook URL is a name

`WEBHOOK_BASE_URL=https://face-detector.internal`

- FACE's certificate SAN is `face-detector.internal, localhost, 127.0.0.1`. An **IP** would
  fail hostname verification.
- `http://` would put detection payloads *and the bearer token* in clear text.
- A name is also what makes the future server move a one-line change (§9).

FACE's nginx carries `face-detector.internal` as an alias on `webhook_integration`
(added at [docker-compose.prod.yml:588](../../VAS/docker/docker-compose.prod.yml), with a
timestamped backup). `face-webhook` alone is **not** in the SAN and would fail verification.

### 5.4 Other controls

- **Authentication:** Flask-Login; every API route is login-gated (unauthenticated
  `/api/models` → 401). Admin-only routes additionally require CSRF.
- **Webhook auth:** fail-closed. `WEBHOOK_AUTH_REQUIRED=true` with no token ⇒ the destination
  is disabled rather than sending unauthenticated.
- **Container user:** non-root `infernode` (uid 1000).
- **Database:** no published port in production.
- **Audit:** `audit_log` records logins, user management, engine and pipeline lifecycle, and
  media ingest. Note it currently has **no read API or UI** — query it with `psql`.

---

## 6. Startup sequence

What happens on `docker compose up`, in order:

1. **`VMS-db`** starts; healthcheck `pg_isready` must pass before VMS starts.
2. **entrypoint** stages the config key (0400, tmpfs) and builds the CA bundle, then `exec`s the app.
3. **Database bootstrap** — alembic under an advisory lock, in a deliberate order:
   `0003 → 0004` (registry foundation) → **legacy model registry migration** → `0005`
   (pipeline→model FK + consistency CHECK). 0005 must not run before model rows exist, or
   every reference would be classified `UNKNOWN_MODEL`.
4. **Admin seeding** — only if the users table is empty; `must_change_password` is set.
5. **One-time migrations** — media, thumbnails, node settings from the legacy roots. Markers
   in `app_state` mean *"every discovered record was deterministically processed"*, not
   *"everything became AVAILABLE"*.
6. **Startup reconciliation** — registry vs filesystem; logs `healthy` or `degraded` with the
   specific artifacts named.
7. **Waitress** serves on `0.0.0.0:5555`.

Observed on this host (clean install): **healthy in 3 seconds**, `ck_pipelines_model_ref_consistent`
VALIDATED, reconciliation healthy.

```bash
docker logs VMS 2>&1 | grep -E "migrated to|MIGRATE\]|0005\]|Seeded|VERIFY\]|Serving on"
```

---

## 7. Operations

```bash
cd ~/Desktop/VMS

./docker-start.sh              # validated start (env, GPU, runtime, network, compose)
./docker-start.sh --build      # rebuild the image first (~60 min on WiFi)
docker compose ps              # status
docker compose logs -f vms     # live logs
docker compose restart vms     # restart the app only
docker compose down            # stop (data survives)
docker compose down -v         # ⚠ ALSO DELETES THE DATABASE VOLUME
```

`docker-start.sh` refuses to start on a mismatch rather than starting something subtly wrong:
`APP_ENV` vs compose overlay, `ACCELERATOR=gpu` without a working GPU or `nvidia` runtime,
missing required variables, or an invalid merged compose.

### Verification

| Command | Checks |
|---|---|
| `bash scripts/smoke-deploy.sh` | 22 checks: containers, alembic head, row counts, ARTIFACT_ROOT, secret staging, CA bundle, HTTP surface, registry health, GPU, webhook egress |
| `sh scripts/check-face-integration.sh` | resolve → connect → TLS (reports SAN) → auth. Names the mode observed |
| `curl -fsS localhost:5555/health` | liveness |

> When writing checks, read the app environment from **pid 1**
> (`tr '\0' '\n' < /proc/1/environ`). `docker exec` starts a *new* process that does **not**
> inherit the entrypoint's exports, so `$REQUESTS_CA_BUNDLE` looks empty there and reports a
> false failure. This cost time once; the scripts now do it correctly.

---

## 8. GPU

### Verified on this host

| Check | Result |
|---|---|
| torch build | `2.14.0+cu130` — CUDA 13, matching driver 13.2 |
| `cuda.is_available()` | True |
| Device / capability | RTX 5090 / `(12, 0)` |
| **`sm_120` in compiled arch list** | **yes** — `[sm_75, sm_80, sm_86, sm_90, sm_100, sm_120]` |
| fp16 2048² matmul | finite results |
| VRAM reporting | 28.8 / 31.4 GiB |
| YOLO `predict` | ran on `cuda:0` |

Blackwell (`sm_120`) needs CUDA ≥ 12.8. Torch 2.14's default PyPI wheel ships CUDA 13, so no
custom index or pin is required — verified rather than assumed.

### Device selection (the "Step 1" fix)

Explicit devices are honoured verbatim; only ambiguous aliases are resolved against hardware:

| Requested | Result |
|---|---|
| `cpu` | `cpu` (plain PyTorch) |
| `cuda:0`, `cuda:1`, `0` | verbatim |
| `GPU` | `cuda` when CUDA is present, else `intel:gpu` |
| `intel:cpu`, `intel:gpu` | OpenVINO — **explicit opt-in only** |

Previously every `CPU`/`GPU`/`NPU` was rewritten to `intel:*` (the hardware check was
commented out), so asking for `cpu` ran OpenVINO and, on a machine whose NVIDIA detection
failed, asking for `GPU` ran Intel OpenVINO instead of CUDA. **A CUDA request that cannot be
satisfied now raises** rather than silently running ~20× slower on CPU.

### Not yet done

Phase E — download `yolov8n`, create a pipeline on `cuda:0`, and run the Step 0 benchmark
harness for a real multi-camera GPU baseline. Requires the admin password change first.

---

## 9. The move to a separate server

Full procedure: **[MOVING_TO_A_SEPARATE_SERVER.md](MOVING_TO_A_SEPARATE_SERVER.md)**.

Exactly one thing is host-dependent — how `face-detector.internal` resolves. FACE's nginx is
already published on `0.0.0.0:443` and answers over the LAN with the same certificate and
auth (verified: `https://192.168.1.111/webhook/x → 401`).

```ini
COMPOSE_FILE=…:compose.remote-face.yaml
FACE_HOST_IP=<FACE nginx's real address>
# WEBHOOK_BASE_URL does NOT change
```

Carry over: database dump **+** `InferenceNode/data` **+** `secrets/` (§5.1). Verify with
`check-face-integration.sh` — a PASS after the move shows a real address rather than `172.x`.

The two hosts need a **private path** (LAN/VLAN/VPN): the webhook carries detection data.

---

## 10. Troubleshooting

| Symptom | Likely cause | Diagnosis |
|---|---|---|
| VMS won't start, GPU error | `ACCELERATOR=gpu` with no working GPU/runtime | `nvidia-smi`; `docker info \| grep -i runtime` |
| Pipeline fails: "CUDA is not available" | correct fail-loud behaviour | use `cpu` explicitly, or fix the GPU |
| Webhooks fail `TLS_ERROR` | name not in cert SAN, or CA not mounted | `sh scripts/check-face-integration.sh` |
| Webhooks fail DNS | same-host: not on `webhook_integration`; remote: `FACE_HOST_IP` unset | as above — it names the mode |
| Webhooks 401 | token mismatch with FACE's `webhook_api_keys` | compare with FACE's secret file |
| Destinations "unavailable: encryption_key_missing" | wrong/missing config key | §5.1 — check `key_id` matches |
| Public downloads fail TLS | CA bundle replaced rather than appended | bundle must be **larger** than `certifi.where()` |
| Registry `degraded` | artifact hash mismatch or missing file | `GET /api/registry/verify` (admin) names each one |
| First pipeline start times out | cold model load exceeds the 10 s budget | retry; pre-existing, unrelated to deployment |
| Model listed but unusable | status not `AVAILABLE`+`PASSED` | check `models` / `model_artifacts` |

Log locations: `docker compose logs vms`, and `InferenceNode/logs/infernode.log` (rotating,
7-day retention).

---

## 11. Known limitations

Carried from the capability audit; these are properties of the current build, not defects
introduced by the deployment.

1. **WiFi** — the binding constraint on camera capacity (§1). Wire before scale testing.
2. **Single-process runtime** — thread-per-pipeline in one Python process. Realistic capacity
   is ~5 cameras at full frame rate today, GIL-bound rather than GPU-bound. The
   optimisation sequence (Steps 2–8) addresses this and is unstarted.
3. **No frame skipping** — every frame read is inferred; there is no target-FPS gate yet.
4. **Hardcoded class allow-list** — 73 of 80 COCO classes are discarded before publishing,
   including `backpack`/`handbag`/`suitcase` and `license_plate`.
5. **No detection persistence in ArmyEye** — detections are fire-and-forget to publishers.
6. **RTSP has no reconnect** — a disconnect ends the pipeline permanently (Step 5).
7. **`audit_log` has no read path** — write-only; query with `psql`.
8. **6-thread Waitress pool shared with MJPEG previews** — roughly six concurrent viewers can
   starve the API.

---

## 12. Deployment record

| | |
|---|---|
| Image build | 59 min, 3.2 GB pulled over WiFi, exit 0 |
| First boot | healthy in 3 s; clean install, 14 tables, alembic `0005` |
| Smoke | **22 passed, 0 failed** |
| GPU proof | **PASS** — including `sm_120` and a real fp16 matmul |
| FACE integration | **PASS** — resolve → tcp/443 → TLS SAN match → 401 |
| Registry reconciliation | **healthy**, no problems, no warnings |

Commits: `604809a` (recovered config + deployment), `f216a2d` (separate-server preparation).

Change made to FACE_DETECTOR: **one line** — its nginx now also answers to
`face-detector.internal` on `webhook_integration`, the name its certificate actually carries.
No certificate was rotated; no FACE service was restarted. Backup:
`docker-compose.prod.yml.bak-20260908-102202`.
