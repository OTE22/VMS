# Detection, tracking and delivery fixes — 2026-09-14

Implemented after the historical detection/webhook and tracking reviews. The review documents describe the original state, not the deployed behavior after these fixes.

## Changes

- Candidate events retain the selected original frame and annotate that frame. JPEG/Base64 encoding happens once per event; retries reuse it. Browser viewers share a cached preview encoding.
- Delivery requires acknowledgments from all selected destinations. Accepted destinations are not retried. Stable event IDs allow receivers to deduplicate an ambiguous network timeout. Delayed retries no longer block following events; retry counts, age, queue size and memory are bounded.
- A disk outbox persists pending events and destination acknowledgments under `ARMYEYE_ARTIFACT_ROOT/outbox/<pipeline hash>`. It is enabled by default when the artifact root is configured; `ARMYEYE_OUTBOX_ENABLED=false` disables it. Pending records resume when that pipeline starts. This does not introduce automatic pipeline startup.
- The outbox limit is 64 MiB per pipeline. Failed records are retained for inspection and count toward this limit; there is no new replay/cleanup UI. Monitor `durable_failed_events` and disk usage. Payload files contain images and detection data, with restrictive file permissions, and no destination credentials.
- Live candidates expire independently of incoming frames. Successful and terminal tracks are suppressed appropriately; reconnects, tracker resets and independent image files separate tracking identities.
- Folder inputs are analyzed individually. Files producing events are deleted only after every sibling event succeeds and the original file fingerprint still matches. Failed inputs are retained. A crash after all acknowledgments but before file cleanup can leave an already-delivered input retained for inspection.
- Tracking uses the existing Ultralytics BoT-SORT configuration with fallback through supported tracker configurations to ByteTrack. Successful fallback is remembered. Low-confidence detections reach tracking association, buffers reflect configured inference FPS, and long gaps reset tracking. All tracker failures surface as an error rather than silently disabling tracking. DeepSORT/StrongSORT are not implemented.
- ONNX, GETI and custom engine outputs are normalized before filtering/publishing. Invalid model loads and malformed/nonfinite settings fail explicitly. Unsupported GETI annotation shapes are rejected.
- Webhooks reuse HTTP sessions. Camera opens/reads have bounded FFmpeg timeouts. Shutdown retains unfinished durable work and prevents starting a second worker over a live old worker. Runtime data is excluded from Docker build contexts.

## Validation

- Full suite using an isolated temporary PostgreSQL database: **903 passed, 32 skipped**. No production database test mutations or external webhook sends.
- Final targeted regression run: **77 passed**, covering delivery, same-instance outbox restart, authentication and frame skipping.
- Existing YOLO model on GPU with the library sample image: **six detected/tracked objects retained consistent IDs across four frames**.
- Source compilation and whitespace checks performed. Existing dependency versions were retained.

These checks do not establish crowd/occlusion tracking accuracy, live-camera latency or maximum camera capacity. GETI normalization was exercised with fixtures; a real GETI SDK/model deployment was not available. Outbox writes and initial image preparation still incur work on enqueue. Network delivery is at-least-once: receivers should deduplicate by `event_id`.

## Rollback

The previous image is preserved as `armyeye-vms:before-delivery-fixes-20260914`. To restore it without rebuilding:

```sh
docker tag armyeye-vms:before-delivery-fixes-20260914 armyeye-vms:latest
docker compose up -d --no-deps --force-recreate vms
```

Do not remove the outbox or input files during rollback. The older image does not replay the new outbox format.

## Deployment result

Rebuilt and recreated only `vms` using the existing production/GPU Compose overlays. VMS and VMS-db are healthy; HTTP `/health` returned 200; CUDA is available; the persistent artifact root is writable and the outbox is enabled. The deployed `pipeline.py` SHA-256 matches the workspace. Startup logs contained no traceback or error-level lines. No pipeline rows were present in the application database at replacement time. The temporary validation database/network were removed.

Deployed image: `sha256:489cf7c615ae5228587946da35117615c25d2ba4c7a6a8837d32f396799f5bd3`.
