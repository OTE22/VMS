# 4K camera capacity check — 2026-09-14

After the user freed GPU memory, the isolated compute benchmark passed up to 32 independent YOLOv8n/tracker instances at 5 FPS. This does not yet establish end-to-end 4K camera capacity. The earlier blocked measurement is retained below as history.

## Observed machine and configuration

- Intel Core Ultra 7 265K, 20 CPU cores.
- About 123 GiB system RAM, about 97 GiB available at inspection.
- NVIDIA RTX 5090, NVIDIA-SMI total 32,607 MiB VRAM.
- VMS default inference target: 5 FPS per camera; camera capture FPS is separate.
- No pipeline rows in the VMS application database at inspection.
- Existing installed detector: YOLOv8n. The Ultralytics path resizes input for inference; accepting a 3840×2160 frame does not mean performing native 4K model inference.

## Earlier capacity blocker (resolved by user)

NVIDIA-SMI reported 31,398 MiB used and 708 MiB free in the final sample. Three Ollama `llama-server` processes occupied 6,892 + 6,892 + 15,286 = **29,070 MiB**. A Python process occupied another 1,432 MiB, alongside desktop GPU applications. These processes were left unchanged.

A separate, network-isolated benchmark container attempted to initialize CUDA before loading one existing model, with a planned 512 MiB PyTorch allocator cap. CUDA reported only **205 MiB free**, below the benchmark's 768 MiB preflight threshold, so it exited without loading the model or measuring inference throughput. CUDA initialization itself has overhead; its free-memory reading and NVIDIA-SMI's measurements differ in timing and accounting.

Consequently, **no additional 4K camera capacity is verified**. This is not evidence that the hardware cannot run a camera: it means there is insufficient safe GPU headroom for the planned measurement in the present configuration. No inference-derived camera estimate is available from this run.

## Required end-to-end measurement

With GPU memory now available, test independent 4K H.264/H.265 streams at the intended capture rate (for example 25 FPS) and inference rate (currently 5 FPS). Increase camera count while measuring sustained per-camera inference FPS, frame age, CPU, VRAM, RAM and event delivery backlog. Include expected scene density, preview viewers and webhook image volume. Select capacity below the first failing level with operating headroom.

At 5 inference FPS, 4 cameras require 20 analyzed frames/sec, 8 require 40, and 16 require 80. These are demand calculations, not measured capacities; video decoding, independent model instances, copies, JPEG encoding, and webhook delivery must also fit.


## Follow-up after GPU memory was released

NVIDIA-SMI reported **29,782 MiB free** and VMS healthy. The tests used the deployed image and existing YOLOv8n weights in disposable containers with no network access or webhook destinations. No production configuration was changed.

Single-instance measurement: 60 timed inferences after five warmups on the library bus image resized to 3840×2160. Mean 3.52 ms, p95 3.64 ms; about 284 inferences/sec for this narrow workload. Ultralytics input size was configured as 640×640 (aspect-ratio padding may use a smaller rectangle), so this is not native 4K neural-network inference. JPEG encoding measured separately: mean 13.91 ms, about 1.88 MB per image for this input using OpenCV defaults.

Concurrent measurement: one independent engine/model/tracker and one owned input image per thread, 5 FPS target, 12 seconds per stage, after warmup. Includes engine inference, conversion to JSON-compatible detections and normalization. All instances repeatedly processed the same scene; this does not assess moving-object tracking accuracy.

| Independent instances | Mean FPS per instance | p95 processing time | Missed 200 ms processing deadlines | PyTorch reserved VRAM |
|---|---:|---:|---:|---:|
| 4 | 5.00 | 25.1 ms | 0 | 132 MiB |
| 8 | 5.00 | 42.9 ms | 0 | 236 MiB |
| 16 | 5.00 | 76.8 ms | 0 | 448 MiB |
| 32 | 5.00 | 128.7 ms | 0 | 868 MiB |

PyTorch reserved memory excludes CUDA context and other non-PyTorch GPU allocations. At 32 instances the benchmark process peaked at about 3.31 GiB host RAM.

**Interpretation:** inference/tracking compute handled 32 synthetic inputs at 5 FPS each during this short test. Do not advertise 32 live 4K cameras from this result: it excludes H.264/H.265 decode, RTSP transport, the complete pipeline loop, preview drawing/encoding, event snapshots/outbox writes and webhook delivery. Longer tests on representative live streams are still needed to determine camera capacity and operating headroom. The benchmark did not search for the maximum compute instance count.
