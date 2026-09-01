# ArmyEye benchmark — Step 0 CPU baseline (dev box, no GPU)

- Generated: 2026-08-23 14:01:04
- Mode: **A (compute, looped local files)**
- Host: `http://localhost:5555` · device requested: `cpu` · model: `yolov8n_03056081`
- Source: `20251214_160135_TRAFFIC_ANPR_CAR_RECOGN.mp4` · destination: `null`
- Duration per level: 120s · sample interval: 2.0s

> **Mode A only sizes the inference budget.** Local files bypass the RTSP network path entirely — no decode jitter, packet loss, reconnects or buffering. A pass here is NOT production readiness; that requires Mode B.

| Cameras | Capture FPS/cam | AI FPS/cam | Aggregate AI FPS | Frame age p50 | **Frame age p95** | Frame age p99 | Infer latency p95 | GPU util | VRAM | CPU % | RAM % | Drops | Failures | Threads | Device actually used | Stable? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 2.3 | 2.3 | 2.3 | 280 ms | **580 ms** | 717 ms | 414.2 ms | n/a | n/a | 39 | 41 | 0 | 0 | 18 | `openvino:INTEL:CPU` | yes |
| 5 | 0.6 | 0.6 | 3.0 | 1037 ms | **2030 ms** | 2665 ms | 1663.4 ms | n/a | n/a | 77 | 63 | 0 | 0 | 30 | `openvino:INTEL:CPU` | yes |
| 10 | 0.3 | 0.3 | 2.5 | 2120 ms | **4973 ms** | 7823 ms | 6103.7 ms | n/a | n/a | 82 | 85 | 0 | 0 | 45 | `openvino:INTEL:CPU` | yes |

## Notes

- All percentiles/rates computed in `scripts/benchmark.py`; the runtime exposes only raw primitives.
- `frame_age` = sample time − last capture timestamp. It measures pipeline staleness.
- `read_wait` p50 per level: 1cam=12.0ms, 5cam=40.5ms, 10cam=75.7ms
  A read that returns ~instantly while the source is live means a buffered backlog is being
  drained (stale frames); a read that blocks ≈1/fps means we are at the live edge.
- `Device actually used` is read from the loaded model, not from configuration.
