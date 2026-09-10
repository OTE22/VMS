# ArmyEye (VMS) — Production Deployment & Operations Manual

**Host:** `itdirect-ai` · **Deployed:** 2026-09-08 · **Repo:** `~/Desktop/VMS` (`github.com/OTE22/VMS`)

This is the operational reference for the ArmyEye deployment on the GPU host. Every fact in
it was verified on this machine at deployment time; anything not verified is marked
explicitly as such.

**Related documents**
- [SCALING.md](SCALING.md) — measured capacity, and how to run more than one worker
- [MOVING_TO_A_SEPARATE_SERVER.md](MOVING_TO_A_SEPARATE_SERVER.md) — the planned move off this host
- [PRODUCTION_READINESS_REPORT.md](PRODUCTION_READINESS_REPORT.md) — the persistence architecture this deployment implements
- [STEP0_GPU_BASELINE.md](STEP0_GPU_BASELINE.md) — performance baseline methodology

---

## 0. ⚠ READ FIRST — two defaults changed

Two runtime defaults changed in the capacity work. Both are **correct for this deployment**
and were measured here, but **one of them is wrong for PTZ cameras**. Read this before
deploying to any other site.

### 0.1 ⚠ Motion compensation is OFF — WRONG FOR PTZ CAMERAS

`InferenceEngine/trackers/botsort_fixed_camera.yaml` is now the default tracker, and it
sets `gmc_method: none`.

Stock BoT-SORT runs Global Motion Compensation (optical flow) on **every tracked frame** to
cancel out **camera** movement. Fixed CCTV does not move, so this was pure overhead — and
it was 81% of the cost of an inference call. Removing it made tracking **3.31× faster**
(8.42 ms → 2.54 ms) and moved the stable ceiling from 35 to 40 cameras per worker.

| Camera type | Correct setting |
|---|---|
| **Fixed / static mount** (this deployment) | default — nothing to do |
| **PTZ that pans or tilts WHILE tracking** | ⚠ `ARMYEYE_TRACKER_CONFIG=botsort.yaml` |

**What goes wrong if you get this wrong:** a PTZ camera that moves during tracking will
suffer **track-ID switches** — the tracker loses objects across the movement and reassigns
new IDs. Person de-duplication keys on track ID, so the same person is re-sent as a new
detection after every pan. Nothing errors; you simply get duplicate webhooks.

A PTZ camera that only moves between presets, and is static while tracking, is fine on the
default.

### 0.2 Decode skipping is ON

`ARMYEYE_SKIP_DECODE=1` is the default. Frames are always **grabbed** (the stream stays
drained and the pipeline stays at the live edge), but only **decoded** into an array when
something will actually read them — an inference is due, a viewer is watching, or the
thumbnail has not been taken yet.

Measured on a real RTSP camera:

| | effect |
|---|---|
| Capture CPU | **−38%** (1 camera) to **−54%** (20 cameras) |
| Capture frame rate | unchanged |
| Inference rate | **−5%** (4.43 → 4.22 fps) — the honest cost |

Set `ARMYEYE_SKIP_DECODE=0` to revert. Video-file sources are unaffected either way: they
pace playback inside `read()` and deliberately stay on the original path.

### 0.3 Also new, but inert until you use them

- **`ARMYEYE_NODE_ID`** — now defaults to `vms-1` in compose. Gives this worker an identity
  that survives a restart. **Required** before running a second worker; harmless otherwise.
- **`pipelines.node_id`** (migration `0006`) — pins a pipeline to one worker. `NULL` means
  unassigned and runnable anywhere, which is every existing pipeline, so behaviour is
  unchanged until you assign something. See [SCALING.md](SCALING.md).

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

**Performance and capacity** (see §0 — two of these changed the DEFAULT behaviour)

| Variable | Default | Notes |
|---|---|---|
| `ARMYEYE_TARGET_INFERENCE_FPS` | `5` | inferences per second per camera. Frames are always read; only inference is gated. Achievable rates are quantised to `stream_fps / n`, so at 25 fps you get 5.00 or 4.17 — nothing between |
| `ARMYEYE_TRACKER_CONFIG` | *(unset → `botsort_fixed_camera.yaml`)* | ⚠ **the default has `gmc_method: none`, which is WRONG FOR PTZ cameras that pan while tracking.** Set to `botsort.yaml` for those. See §0.1 |
| `ARMYEYE_SKIP_DECODE` | `1` (on) | grab every frame, decode only when something reads it. Capture CPU −38% to −54%; **inference rate −5%**. `0` reverts. Live sources only |
| `ARMYEYE_NODE_ID` | `vms-1` | stable worker identity. **Required** before running a second worker against this database, or pipeline→node assignment will not survive a restart |

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

### Multi-camera GPU baseline — done

Completed 2026-09-08/10. `yolov8n` on `cuda:0`, measured against **real RTSP cameras**
(mediamtx + ffmpeg, H.264 1080p25 over TCP). Video-file measurements were deliberately
discarded for capacity claims — they overstate decode savings by roughly 2×.

| | |
|---|---|
| Stable per worker | **40 cameras** (25 fps capture + 5 fps inference, 1080p) |
| 60 cameras | **5.00 fps on all 60**, 3 workers × 20, 9.99/20 cores, 50 % GPU |
| Single-process ceiling | ~220–250 inferences/s |
| ArmyEye VRAM at 60 cameras | 3.4 GB (the card also carries FACE's ~15.7 GB of pinned ollama models) |

**The limit is the GIL, not this hardware.** At the collapse point GPU is ~25 % and CPU ~5
of 20 cores — nothing is saturated. Past the ceiling, adding cameras makes throughput
*worse*, not flat.

Method, full curves and the tuning that was measured — including FP16, which was measured
and **rejected** — are in **[SCALING.md](SCALING.md)**.

### Still not done

**NVDEC is idle.** The 5090's dedicated video decoders sit at **0 %** while decode runs on
the CPU — the resource that actually limits camera count. Reaching them needs a decode
dependency this image does not have (`av` / DALI); the bundled OpenCV has no CUDA support
(`cv2.cuda` reports 0 devices).

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
| `DELETE /api/media/<id>` returns 409 | a pipeline still references that file | the response lists them; repoint or delete those pipelines, or `?force=true` |
| Registry `degraded`, "AVAILABLE artifact(s) missing" | a file was removed with `rm` instead of through the API | delete via the API so the row and bytes go together |

Log locations: `docker compose logs vms`, and `InferenceNode/logs/infernode.log` (rotating,
7-day retention).

---

## 11. Known limitations

Status as of 2026-09-10. Items marked **FIXED** were closed during Steps 1–10; the rest
are still true and are properties of the current build, not defects introduced by the
deployment.

| # | Limitation | Status |
|---|---|---|
| 1 | **WiFi** on this host | **STILL TRUE** — and untested at scale. The capacity numbers came from RTSP sources on the local bridge network, so real cameras over WiFi may bind earlier than the GIL does. Wire before trusting 40/worker in the field. |
| 2 | Single-process runtime, thread-per-pipeline | **PARTLY FIXED.** The old "~5 cameras" estimate was wrong — measured **40 per worker**, and `pipelines.node_id` now lets several workers share one database (60 cameras at 5.00 fps across 3). Still GIL-bound at ~220–250 inferences/s per process, and assignment is **manual**: no auto-balancing, no failover. |
| 3 | No frame skipping — every frame inferred | **FIXED** (Step 3). `ARMYEYE_TARGET_INFERENCE_FPS`, default 5. Frames are still always read so the decoder stays drained. |
| 4 | Hardcoded class allow-list — 73 of 80 COCO classes discarded before publishing, including `backpack`/`handbag`/`suitcase` | **STILL TRUE.** Blocks person-to-bag association without a change here. |
| 5 | No detection persistence in ArmyEye — fire-and-forget to publishers | **STILL TRUE.** No backward tracing or replay; FACE is the only durable record. |
| 6 | RTSP disconnect ends the pipeline permanently | **FIXED** (Step 5). Bounded exponential backoff 1 s → 30 s, unbounded attempts, and the backoff resets only on a real frame — cv2 reports a dead RTSP handle as "opened". Verified live by killing a publisher mid-run and restoring it. |
| 7 | `audit_log` is write-only — no read path | **STILL TRUE.** Query with `psql`. Writes now cover pipeline lifecycle, media ingest/delete and node assignment. |
| 8 | 6-thread Waitress pool shared with MJPEG previews | **STILL TRUE** (`threads=6`). Roughly six concurrent viewers can starve the API. |
| 9 | NVDEC unused | **STILL TRUE.** The 5090's video decoders sit at 0 % while decode consumes the CPU that caps camera count. Needs a decode dependency the image lacks. |
| 10 | GitHub `main` is an unrelated history | **OPEN.** See §12.4 — the remote does not reflect what is running here. |

---

## 12. Deployment record — Phase 1 to final stage

Two distinct bodies of work, in order. **Phases 1–18** rebuilt persistence so the system
could be trusted with state; **Steps 1–10** then made it fast enough to be worth scaling.
Every figure below was verified on this host, not estimated.

### 12.1 Phases 1–18 — persistence architecture

Goal: PostgreSQL authoritative for metadata, `ARTIFACT_ROOT` for bytes, joined by
`id + relative_path + sha256 + size + status`. Before this, state lived in JSON files beside
the code and a container rebuild could silently lose it.

| Phase | Delivered | Commit |
|---|---|---|
| — | Baseline tree before the work | `e59bc9e` |
| 1 | Audit and architecture decision (no code) | — |
| 2–3 | Pipeline Builder/Management remediation; authorization + CSRF hardening | `5232267` |
| 4 | Alembic `0004` — artifact registry foundation | `327a7f8` |
| 5 | Physical artifact migration framework + single path resolver | `3b03b5f` |
| 6 | Legacy models registry → PostgreSQL + ARTIFACT_ROOT | `47fd793` |
| 8 | Alembic `0005` — pipeline→model relational integrity (FK + CHECK) | `3ff0383` |
| 9 | ModelRegistry cutover; `models_metadata.json` retired as a runtime source | `f37a18b` |
| 10 | Publishers / node / telemetry config → PostgreSQL + versioned encryption | `fe8346f` |
| 11 | Engine registry + protected persistent artifact root | `68138be` |
| 12 | Media + thumbnail registry (rows in PostgreSQL, bytes in ARTIFACT_ROOT) | `caed9c6` |
| 13 | Unified registry reconciliation verifier + startup verification | `9581fda` |
| 14 | Dashboard cutover — registry-authoritative counts, runtime vs persisted labelled | `86e3fff` |
| 15 | Browser E2E (real server, isolated PostgreSQL + ARTIFACT_ROOT) + 3 defects it exposed | `e8a4ae8` |
| 16 | Container recreation verified live (`--force-recreate`, not restart) | `1bd7238` |
| 16/17 | Throwaway-PostgreSQL fixture, live recreation proof, readiness runner | `cc84649` |
| 18 | Final production-readiness report — verdict **PASS** | `0e7eb16` |

There is no Phase 7; it was folded into 6 and 8. Phase 1 produced the architecture
decision, not code. Bootstrap ordering (`0004` → legacy model migration → `0005`) is
enforced at startup — `0005` must not run before model rows exist. See `45d5d63`.

Full detail: [PRODUCTION_READINESS_REPORT.md](PRODUCTION_READINESS_REPORT.md).

### 12.2 Deployment to the GPU host (2026-09-08)

| | |
|---|---|
| Image build | 59 min, 3.2 GB pulled, exit 0 |
| First boot | healthy in 3 s; 14 tables; alembic `0005` |
| GPU proof | **PASS** — including `sm_120` and a real fp16 matmul |
| FACE integration | **PASS** — resolve → tcp/443 → TLS SAN match → 401 |
| Registry reconciliation | **healthy** |

Commits `604809a` (recovered deployment configuration), `f216a2d` (separate-server
preparation), `724fdb3` (this manual).

Recovering the configuration was itself a finding: the `compose*.yaml` files had been lost
to a wildcard `*.yaml` ignore rule and were never in git. `.gitignore` now protects secrets
without a yaml wildcard, and carries a note saying why.

**One change was made to FACE_DETECTOR** — a single line. Its nginx now also answers to
`face-detector.internal` on `webhook_integration`, the name its certificate already
carried. No certificate rotated, no FACE service restarted. Backup:
`docker-compose.prod.yml.bak-20260908-102202`.

### 12.3 Steps 1–10 — capacity

Ordered by measurement, not by guesswork. Each step was one isolated change, verified with
tests and a real before/after measurement before the next began. **Two were measured and
rejected**, which is as much a result as the ones that shipped.

| Step | Change | Measured effect |
|---|---|---|
| 1 | CUDA device selection — explicit devices honoured verbatim, no silent CPU fallback | correctness |
| 3 | Configurable target inference FPS (`ARMYEYE_TARGET_INFERENCE_FPS`, default 5) | decoupled AI rate from capture rate |
| 5 | RTSP reconnect with bounded backoff; dead sources stop spinning a core | a camera drop no longer ends the pipeline |
| 6 | Skip the gated-frame copy when nothing reads it | −11.4 % CPU/camera; also stopped gated frames overwriting the annotated image |
| 7 | Real GPU/VRAM telemetry (`gpu_probe`) | "no GPU" → 31.84 GB / util / driver; probe costs 0.010 ms |
| 4 | FP16 + `imgsz` + `classes=` | **REJECTED** — FP16 is 12.35 ms vs 11.84 ms, *slower* |
| — | BoT-SORT motion compensation off for fixed cameras | tracking **3.31×** faster; 35 → **40** cameras/worker |
| 8 | Skip decode of frames nothing will read (grab → decide → retrieve) | capture CPU **−38 % to −54 %** |
| 9 | Drift-free inference scheduling | **4.17 → 5.00 fps** (83 % → 100 % of target) |
| 10 | `pipelines.node_id` — pin a pipeline to one worker | 60 cameras at 5.00 fps across 3 workers |
| 2 | Per-frame debug print + CUDA sync | **DEFERRED** — measured smaller than assumed once Step 3 landed |

Three defects were found along the way that had nothing to do with performance:

- **The registered model could not be loaded at all.** No `.pt` suffix, so Ultralytics
  rejected it every frame while the engine swallowed the exception. The pipeline ran green
  and published **zero detections**. Fixed in the migration and repaired live
  (`722067e`, `scripts/repair_model_extension.py`).
- **Two workers could start the same camera**, doubling cost and duplicating every webhook,
  with nothing in the schema or UI able to show it (`a04dcda`).
- **Gated frames overwrote the annotated image** used by result-image destinations, so
  webhooks usually received un-annotated frames (`a993744`).

Capacity findings and method: [SCALING.md](SCALING.md).

### 12.4 Final state — verified 2026-09-10

| | |
|---|---|
| Commit | `d268437` |
| Image | rebuilt from that commit; every application file verified byte-identical |
| Container | `running / healthy` |
| Alembic | `0006_pipeline_node_assignment`, 14 tables |
| Test suite | **882 passed**, 32 skipped |
| Smoke | **19 passed, 0 failed** |
| Registry reconciliation | **healthy** |
| Measured capacity | **40 cameras/worker**; 60 cameras at 5.00 fps across 3 workers |
| GPU | RTX 5090, ~50 % at 60 cameras — the ceiling is the GIL, not the hardware |

**Pushed to** `github.com/OTE22/VMS`, branch `perf/capacity-and-multi-worker`.

⚠ **`main` on GitHub is a different, unrelated history** (root `e59bc9e`, 20 commits) from
this machine's lineage (root `3d15eca`, 18 commits) — this working copy was `git init`-ed
rather than cloned. The local tree is a strict **content** superset: nothing exists on the
remote that is missing here, but the remote holds Phase 1–18 commit history that exists
nowhere else. Reconciling the two (force `main`, or merge with
`--allow-unrelated-histories`) is an open decision. Until it is made, **GitHub's `main`
does not reflect what is running in production.**
