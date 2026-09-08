# ArmyEye benchmark — baseline

- Generated: 2026-09-08 13:53:14
- Mode: **A (compute, looped local files)**
- Host: `http://localhost:5555` · device requested: `cuda:0` · model: `yolov8n_95a24496`
- Source: `bench_1080p25.mp4` · destination: `null`
- Duration per level: 75s · sample interval: 2.0s

> **Mode A only sizes the inference budget.** Local files bypass the RTSP network path entirely — no decode jitter, packet loss, reconnects or buffering. A pass here is NOT production readiness; that requires Mode B.

| Cameras | Capture FPS/cam | AI FPS/cam | Aggregate AI FPS | Frame age p50 | **Frame age p95** | Frame age p99 | Infer latency p95 | GPU util | VRAM | CPU % | RAM % | Drops | Failures | Threads | Device actually used | Stable? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 24.9 | 4.8 | 24.2 | 80 ms | **100 ms** | 104 ms | 2.6 ms | n/a | n/a | 5 | 10 | 0 | 0 | 29 | `CUDA:0` | yes |
| 30 | 24.9 | 4.8 | 142.8 | 75 ms | **93 ms** | 97 ms | 1.2 ms | n/a | n/a | 22 | 13 | 0 | 0 | 104 | `CUDA:0` | yes |

## Notes

- All percentiles/rates computed in `scripts/benchmark.py`; the runtime exposes only raw primitives.
- `frame_age` = sample time − last capture timestamp. It measures pipeline staleness.
- `read_wait` p50 per level: 5cam=39.2ms, 30cam=39.4ms
  A read that returns ~instantly while the source is live means a buffered backlog is being
  drained (stale frames); a read that blocks ≈1/fps means we are at the live edge.
- `Device actually used` is the STEADY-STATE device read from the loaded model, not from configuration; only this run's own pipelines are sampled.
- Other device values seen transiently: none
