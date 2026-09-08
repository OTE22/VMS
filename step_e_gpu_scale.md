# ArmyEye benchmark — baseline

- Generated: 2026-09-08 13:30:42
- Mode: **A (compute, looped local files)**
- Host: `http://localhost:5555` · device requested: `cuda:0` · model: `yolov8n_95a24496`
- Source: `bench_1080p25.mp4` · destination: `null`
- Duration per level: 75s · sample interval: 2.0s

> **Mode A only sizes the inference budget.** Local files bypass the RTSP network path entirely — no decode jitter, packet loss, reconnects or buffering. A pass here is NOT production readiness; that requires Mode B.

| Cameras | Capture FPS/cam | AI FPS/cam | Aggregate AI FPS | Frame age p50 | **Frame age p95** | Frame age p99 | Infer latency p95 | GPU util | VRAM | CPU % | RAM % | Drops | Failures | Threads | Device actually used | Stable? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 10 | 24.9 | 24.9 | 249.4 | 75 ms | **96 ms** | 103 ms | 1.3 ms | n/a | n/a | 7 | 11 | 0 | 0 | 44 | `CUDA:0` | yes |
| 20 | 24.9 | 24.9 | 498.7 | 75 ms | **93 ms** | 98 ms | 1.3 ms | n/a | n/a | 17 | 13 | 0 | 0 | 74 | `CUDA:0` | yes |
| 30 | 24.9 | 24.9 | 746.7 | 94 ms | **114 ms** | 119 ms | 1.7 ms | n/a | n/a | 26 | 14 | 0 | 0 | 104 | `CUDA:0` | yes |

## Notes

- All percentiles/rates computed in `scripts/benchmark.py`; the runtime exposes only raw primitives.
- `frame_age` = sample time − last capture timestamp. It measures pipeline staleness.
- `read_wait` p50 per level: 10cam=38.5ms, 20cam=38.4ms, 30cam=38.1ms
  A read that returns ~instantly while the source is live means a buffered backlog is being
  drained (stale frames); a read that blocks ≈1/fps means we are at the live edge.
- `Device actually used` is the STEADY-STATE device read from the loaded model, not from configuration; only this run's own pipelines are sampled.
- Other device values seen transiently: none
