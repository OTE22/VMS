# Step 0 — GPU-host baseline: exact commands to run

Run these on the **NVIDIA GPU server**, then send the outputs back. Nothing here changes
production behaviour — Step 0 is measurement only. **Do not apply any Step 1 change yet**;
the point is to capture what the system does *today*.

Everything below assumes the repo is checked out on the GPU host at the current commit
(with the Step 0 instrumentation) and that the NVIDIA Container Toolkit is installed.

---

## 1. Select the GPU deployment

Edit `.env` on the GPU host:

```bash
ACCELERATOR=gpu
COMPOSE_FILE=compose.yaml:compose.prod.yaml:compose.gpu.yaml
```

Then build and start:

```bash
docker compose build vms          # builds against requirements-gpu.txt (CUDA torch)
docker compose up -d --force-recreate vms
```

## 2. Prove CUDA is actually visible — three independent checks

```bash
# a) the driver sees the GPUs
docker exec VMS nvidia-smi

# b) torch inside the container sees CUDA
docker exec VMS python -c "import torch; print('torch', torch.__version__, \
'cuda', torch.cuda.is_available(), 'devices', torch.cuda.device_count(), \
[torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())])"

# c) ArmyEye's own hardware detector sees them
docker exec VMS python -c "import sys; sys.path.insert(0,'/app'); \
from InferenceNode.hardware_detector import HardwareDetector; \
hd = HardwareDetector(); print(hd.available_devices); \
assert hd.has_nvidia_gpu(), 'GPU not visible to ArmyEye'"
```

**Send me all three outputs.** If (a) works but (b) or (c) does not, stop — that is a
deployment problem, not something the baseline should paper over.

## 3. Baseline run — device exactly as your operators configure it

Run **both** device spellings. This is the measurement that proves or disproves the
suspected device-mapping defect, so please do not skip the first one:

```bash
cd /path/to/ArmyEye

# 3a. device spelled "GPU" — what the UI offers today
python scripts/benchmark.py --cameras 1,5,10,30 --duration 300 --warmup 20 \
    --device GPU --out baseline_gpu_asconfigured.md \
    --label "Step 0 GPU baseline (device=GPU, as the UI sets it)"

# 3b. device spelled "cuda:0" — the explicit form
python scripts/benchmark.py --cameras 1,5,10,30 --duration 300 --warmup 20 \
    --device cuda:0 --out baseline_gpu_cuda0.md \
    --label "Step 0 GPU baseline (device=cuda:0, explicit)"
```

Notes:
- The harness creates its own `BENCH_*` pipelines, starts them, measures, then deletes
  them. **Your existing pipelines are never touched.** If a previous run aborted, add
  `--force-clean`.
- `--destination null` is the default so no data leaves the node and no external service
  distorts the numbers.
- `requests` must be installed for the script: `pip install requests`.
- If admin credentials are not in `.env` on that host:
  `ARMYEYE_ADMIN_USERNAME=… ARMYEYE_ADMIN_PASSWORD=… python scripts/benchmark.py …`
- If the API is not on `localhost:5555`, add `--base http://host:port`.

## 4. Watch the GPU during the run (separate terminal)

```bash
nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total \
           --format=csv -l 5 | tee nvidia_smi_during_run.csv
```

## 5. What to send back

1. The three CUDA check outputs from §2
2. `baseline_gpu_asconfigured.md` **and** `baseline_gpu_cuda0.md` (plus the `.csv` beside each)
3. `nvidia_smi_during_run.csv`
4. Anything that failed or looked wrong

---

## Expected output format

Each report contains this table — the same one used at every later step, so before/after
stays comparable:

| Cameras | Capture FPS/cam | AI FPS/cam | Aggregate AI FPS | Frame age p50 | **Frame age p95** | Frame age p99 | Infer latency p95 | GPU util | VRAM | CPU % | RAM % | Drops | Failures | Threads | Device actually used | Stable? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | … | … | … | … | … | … | … | gpu0:…% | gpu0:…/…G | … | … | 0 | 0 | … | `…` | yes |

### The three columns that decide Step 1

- **`Device actually used`** — read from the **loaded model**, not from configuration.
  On the CPU dev box, requesting `cpu` produced `openvino:INTEL:CPU`. If run 3a shows
  something like `openvino:…` or `cpu` instead of `cuda:0`, the device-mapping defect is
  confirmed on the GPU host and Step 1 is justified by measurement rather than by reading code.
- **`GPU util` / `VRAM`** — reported **per device** (`gpu0:… gpu1:…`), never averaged. If
  every pipeline lands on `gpu0` while `gpu1` sits idle, an aggregate figure would hide it.
- **`Frame age p95`** — the primary acceptance metric. A run with good FPS and rising frame
  age is a **FAIL**: it means a buffered backlog is being drained and the analytics are
  looking at stale video.

`Stable?` is computed as: every started pipeline still reporting at the end **and** frame-age
p95 not trending upward across the run.

The `read_wait` line under each table is a backlog detector: a `read()` that returns almost
instantly while the source is live means frames are being drained from a buffer; a `read()`
that blocks ≈ 1/fps means the pipeline is at the live edge.

---

## After this

I compare your GPU baseline against the CPU baseline, then — and only then — implement
**Step 1 (CUDA device selection)** and ask you to re-run exactly the same commands so the
before/after is attributable to that one change. No other change is made in between.
