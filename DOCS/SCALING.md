# Scaling ArmyEye — measured capacity

Every number here was measured on this host against **real RTSP cameras** (mediamtx +
ffmpeg, H.264 1080p25 over TCP), not video files. File-based measurements overstate the
benefit of decode work by roughly 2x and are not used for capacity claims.

Host: 20 cores (Intel Ultra 7 265K), RTX 5090, 31.8 GB VRAM.

## The constraint is the GIL, not the hardware

A single ArmyEye process saturates at **~220–250 inferences/second**. At that point:

| | at collapse |
|---|---|
| GPU | ~25 % |
| CPU | ~5 of 20 cores |
| VRAM | well under budget |

Nothing is saturated. Adding cameras past the ceiling makes throughput *worse*, not
flat — CPU usage falls while frames are lost, which is the signature of threads blocking
on the interpreter lock rather than doing work.

**Consequence:** camera count per process is capped by inference throughput.

    cameras_per_process  ≈  240 / target_inference_fps

At the standard 5 fps target that is **~45 cameras per process**, and ~40 with capture
comfortably keeping up.

## Measured: 60 cameras

| layout | capture ok | inference/cam | CPU | GPU |
|---|---|---|---|---|
| 1 process x 60 | 0/60 | 2.91 fps | 4.9 / 20 | 23 % |
| 2 processes x 30 | 42/60 | 3.98 fps | 9.5 / 20 | 44 % |
| **3 processes x 20** | **60/60** | **5.00 fps** | **10.0 / 20** | **50 %** |

60 cameras at the full 5 fps target requires **three worker processes**. One cannot do it
at any setting: 60 x 5 = 300 inferences/second against a ~240 ceiling.

## VRAM budget

The GPU is **shared with the FACE stack**, whose two ollama models are pinned
`UNTIL: Forever` and hold ~15.7 GB permanently. Treat the usable budget as **~16–18 GB**.

ArmyEye's own consumption at 60 cameras across 3 processes: **3.4 GB total**
(peak 19.1 GB on the card, of which 15.7 GB is ollama).

    per process   ≈ 0.5 GB CUDA context + ~55 MB per loaded model
    3 x 20 cams   ≈ 3.4 GB

VRAM is **not** the limiting factor at this scale — the GIL is. Roughly a dozen worker
processes would fit the VRAM budget; the CPU runs out first (~10 cores for 3 workers).

If ollama is ever unpinned or moved, ~15.7 GB returns to the pool.

## Running multiple workers

Each worker needs a **stable identity** and its **own port**. Pipelines are then pinned to
a worker so two nodes never start the same camera.

    ARMYEYE_NODE_ID=worker-1     # stable across restarts; without it a uuid4 is
                                 # generated per boot and assignment cannot survive one
    PORT=5555

Assign a pipeline to a worker:

    PUT /api/pipeline/<pipeline_id>/node    {"node_id": "worker-1"}
    PUT /api/pipeline/<pipeline_id>/node    {"node_id": null}     # any node may run it

`GET /api/node/identity` reports the current process's id and whether it is stable.

**`node_id = NULL` means unassigned and runnable anywhere** — the original single-node
behaviour, which is why adding this changed nothing for existing installs. A worker
refuses to start a pipeline assigned to a different node.

## Tuning that was measured

| setting | effect |
|---|---|
| `gmc_method: none` (default, `botsort_fixed_camera.yaml`) | tracking **3.31x** faster. Stock BoT-SORT runs optical-flow motion compensation every frame for *moving* cameras; fixed CCTV does not need it. PTZ that pans while tracking: `ARMYEYE_TRACKER_CONFIG=botsort.yaml` |
| `ARMYEYE_SKIP_DECODE=1` (default) | capture CPU **-38 % to -54 %**. Grabs every frame (stream stays drained, live edge preserved) but only converts to an array when something will read it. Live sources only — video files pace inside `read()`. Costs ~5 % of inference rate |
| `ARMYEYE_TARGET_INFERENCE_FPS=5` | inference rate per camera. Achievable rates are quantised to `stream_fps / n`, so at 25 fps you get 5.00 or 4.17 — nothing between |
| FP16 (`half=True`) | **rejected**: 12.35 ms vs 11.84 ms, *worse*. yolov8n on a 5090 is launch-overhead-bound, not compute-bound |

## What is NOT yet done

- **NVDEC is idle.** The 5090's dedicated decoders sit at 0 % while decode runs on CPU —
  the resource that limits you. Reaching it needs a decode dependency the image does not
  have (`av` / DALI); OpenCV here has no CUDA.
- **No cross-node view.** Each worker serves its own API; there is no fused UI across
  workers, and no automatic assignment or failover. Assignment is manual and deliberate.
