# ArmyEye benchmark — Step 1 - device selection fixed (same workload as baseline)

- Generated: 2026-08-23 16:07:47
- Mode: **A (compute, looped local files)**
- Host: `http://localhost:5555` · device requested: `cpu` · model: `yolov8n_03056081`
- Source: `20251214_160135_TRAFFIC_ANPR_CAR_RECOGN.mp4` · destination: `null`
- Duration per level: 120s · sample interval: 2.0s

> **Mode A only sizes the inference budget.** Local files bypass the RTSP network path entirely — no decode jitter, packet loss, reconnects or buffering. A pass here is NOT production readiness; that requires Mode B.

| Cameras | Capture FPS/cam | AI FPS/cam | Aggregate AI FPS | Frame age p50 | **Frame age p95** | Frame age p99 | Infer latency p95 | GPU util | VRAM | CPU % | RAM % | Drops | Failures | Threads | Device actually used | Stable? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1.1 | 1.1 | 1.1 | 399 ms | **3353 ms** | 5516 ms | 787.7 ms | n/a | n/a | 49 | 70 | 0 | 0 | 18 | `cpu` | no (frame age growing) |
| 5 | 1.2 | 1.2 | 6.1 | 1004 ms | **13722 ms** | 23877 ms | 2717.9 ms | n/a | n/a | 75 | 85 | 0 | 0 | 30 | `CPU,cpu` | yes |
| 10 | 0.3 | 0.3 | 2.7 | 5174 ms | **79908 ms** | 86805 ms | 7954.3 ms | n/a | n/a | 45 | 96 | 0 | 0 | 45 | `CPU,cpu` | no (frame age growing) |

## Notes

- All percentiles/rates computed in `scripts/benchmark.py`; the runtime exposes only raw primitives.
- `frame_age` = sample time − last capture timestamp. It measures pipeline staleness.
- `read_wait` p50 per level: 1cam=25.2ms, 5cam=56.9ms, 10cam=86.9ms
  A read that returns ~instantly while the source is live means a buffered backlog is being
  drained (stale frames); a read that blocks ≈1/fps means we are at the live edge.
- `Device actually used` is read from the loaded model, not from configuration.
