# ArmyEye benchmark — baseline

- Generated: 2026-09-08 13:17:13
- Mode: **A (compute, looped local files)**
- Host: `http://localhost:5555` · device requested: `cuda:0` · model: `yolov8n_95a24496`
- Source: `bench_1080p25.mp4` · destination: `null`
- Duration per level: 90s · sample interval: 2.0s

> **Mode A only sizes the inference budget.** Local files bypass the RTSP network path entirely — no decode jitter, packet loss, reconnects or buffering. A pass here is NOT production readiness; that requires Mode B.

| Cameras | Capture FPS/cam | AI FPS/cam | Aggregate AI FPS | Frame age p50 | **Frame age p95** | Frame age p99 | Infer latency p95 | GPU util | VRAM | CPU % | RAM % | Drops | Failures | Threads | Device actually used | Stable? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 24.9 | 24.9 | 24.9 | 82 ms | **105 ms** | 108 ms | 3.2 ms | n/a | n/a | 3 | 10 | 0 | 0 | 17 | `CUDA:0` | yes |
| 3 | 24.9 | 24.9 | 74.7 | 82 ms | **101 ms** | 106 ms | 3.6 ms | n/a | n/a | 5 | 10 | 0 | 0 | 23 | `CUDA:0` | yes |
| 5 | 24.9 | 24.9 | 124.5 | 80 ms | **100 ms** | 104 ms | 3.0 ms | n/a | n/a | 5 | 11 | 0 | 0 | 29 | `CUDA:0` | yes |

## Notes

- All percentiles/rates computed in `scripts/benchmark.py`; the runtime exposes only raw primitives.
- `frame_age` = sample time − last capture timestamp. It measures pipeline staleness.
- `read_wait` p50 per level: 1cam=36.0ms, 3cam=37.8ms, 5cam=38.0ms
  A read that returns ~instantly while the source is live means a buffered backlog is being
  drained (stale frames); a read that blocks ≈1/fps means we are at the live edge.
- `Device actually used` is the STEADY-STATE device read from the loaded model, not from configuration; only this run's own pipelines are sampled.
- Other device values seen transiently: none
