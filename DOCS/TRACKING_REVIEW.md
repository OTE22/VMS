> Historical review of the pre-fix code. See [implemented fixes and validation](DELIVERY_TRACKING_FIXES.md) for the current status.

# Tracking integration review

Reviewed 2026-09-14 using the workspace and installed Ultralytics **8.4.143** source. No tracker settings or application code were changed. Synthetic checks used the actual installed tracker classes; no model downloads, cameras, or external webhook requests were used.

**Verdict**

Basic frame-to-frame tracking is implemented correctly through a persistent Ultralytics tracker. The default configuration reduces computation for fixed cameras. However, detector confidence filtering blocks the tracker's low-confidence recovery stage, fallback does not reliably change an existing tracker, and tracker timeouts are not tied to elapsed time. The integration should not yet be described as optimized for identity continuity under occlusion, sparse sampling, or camera outages.

**What is actually implemented**

| Name | Status in this application |
|---|---|
| BoT-SORT | Default: `InferenceEngine/trackers/botsort_fixed_camera.yaml`. |
| ByteTrack | Available through `ARMYEYE_TRACKER_CONFIG=bytetrack.yaml`; also the second fallback in the engine. It is not the active default. |
| OC-SORT | First fallback. It exists in the installed 8.4.143 tracker registry; it must not be described as unsupported based on older Ultralytics versions. |
| DeepSORT | No dedicated DeepSORT integration found in the detection path. References in comments do not instantiate it. |
| StrongSORT | No dedicated StrongSORT integration found. `[STRONGSORT]` log labels describe calls to BoT-SORT and are misleading. |
| Deep OC-SORT | Present in the installed library, but distinct from DeepSORT and not selected by this application's default/fallback configuration. |

The running container's tracker override is unset, so engine construction resolves the shipped fixed-camera default. This identifies the configured default, not proof of every live pipeline's effective tracker after a runtime error.

**Correct integration and useful optimizations**

- `ultralytics_engine.py:527` and `537` call `model.track(..., persist=True)`. The installed callback reuses existing tracker state, so successive frames from one pipeline can retain identities.
- The manager creates a separate inference engine/model for each pipeline. The code does not intentionally share one predictor's track list among cameras.
- The shipped BoT-SORT configuration uses `gmc_method: none` and `with_reid: False`. This avoids camera-motion estimation and appearance-embedding work. It fits a fixed-camera performance objective, but provides no appearance-based ReID in the active configuration.
- `lap` is pinned as a dependency for assignment support. Existing tracker-configuration tests passed in the earlier review.
- Tracking output IDs are propagated into pipeline candidate selection and in-flight deduplication. These are tracker IDs, not proof of unique real-world people or cross-camera identities.

**Issues affecting continuity**

1. **Detector confidence blocks second-stage matching.** The engine passes `conf=0.25` before tracker processing, while its tracker YAML sets `track_low_thresh=0.1` and `track_high_thresh=0.25`. The installed BYTETracker implementation places scores strictly between those bounds in its second association group. Those detections have already been removed by the upstream detector threshold. BoT-SORT inherits this association flow. A sensible candidate for evaluation is detector confidence `0.1`, retaining the pipeline's separate person/event threshold of `0.5`; accepting a low-confidence observation for association need not publish it as an event. See `ultralytics_engine.py:543` and the tracker YAML. Official [Ultralytics tracking documentation](https://docs.ultralytics.com/modes/track) describes these separate thresholds and persistent tracking.

2. **Tracker age is measured in processed updates, not camera frames or wall time.** The installed tracker sets `max_frames_lost = args.track_buffer` and increments `frame_id` once per `update()`. The pipeline only calls inference/tracking on sampled frames. Buffer 30 therefore corresponds approximately to six seconds at five tracking updates per second, versus one second at 30 updates per second. During a read outage with no updates, tracker age does not advance at all. This is not inherently wrong, but the desired disappearance/occlusion duration must be defined and tested at the effective tracking rate. Motion prediction also advances once per update rather than following a supplied elapsed-time delta.

3. **Changing the tracker argument does not necessarily switch an existing tracker.** In installed `trackers/track.py`, `on_predict_start()` returns immediately when trackers exist and `persist=True`. If the primary tracker was already created before failure, passing a different YAML to the fallback can keep the old tracker object. Separately, the application's successful fallback name is saved only in a local variable, so subsequent calls retry the original configuration. Fallback needs explicit lifecycle handling and an effective-tracker status, including a policy for resetting IDs/candidate state.

4. **Reconnects preserve stale tracker state.** `pipeline.py:1029` reconnects the source but does not reset or age tracker state by outage duration. A person appearing after a long gap may be associated with old state; the existing ID-reuse heuristic only compares boxes and elapsed time since delivery. Define when reconnect means a new tracking session and ensure webhook dedup keys account for that session. Actual false associations require camera-sequence validation; they were not measured here.

5. **The application can duplicate an event even if the tracker keeps the right ID.** The same-ID movement heuristic in `pipeline.py:239` can clear the successful-send cooldown when the object moves far from its last sent box after one second. Improving the tracker alone will not fix these duplicate webhook events. Track association quality and event-suppression policy need separate tests.

6. **Tracker settings are not passed through per pipeline.** The manager constructs engine configuration with engine type, model path, device, and a fixed detect task (`pipeline_manager.py:1035`); it does not pass a per-camera tracker or tracking flag. Tracker selection comes from the process environment/default. Mixed fixed/PTZ cameras therefore lack a per-pipeline selection path in this builder.

7. **Logging adds work inside tracking.** IDs are copied to CPU for debug printing, then individual detection fields are converted again during JSON preparation. Per-frame/per-object messages remain despite `verbose=False`. Remove or sample diagnostic output before drawing conclusions about tracker cost at scale.

**Synthetic verification**

Using actual `BOTSORT`, `BYTETracker`, `Boxes`, and the shipped thresholds:

- Each tracker retained one ID across ten slightly moving, high-confidence boxes.
- Each tracker maintained the track when the next observation had confidence `0.2` and reached the tracker.
- When that observation was omitted, as happens under upstream filtering at `0.25`, the update returned no active output. This demonstrates lost low-confidence recovery, not a measured long-term ID-switch rate.
- Both trackers reported a 30-update lost-track buffer.
- Calling the actual `on_predict_start(..., persist=True)` with an existing tracker and a different configured YAML preserved the original tracker object.

The smooth-motion checks are intentionally simple. They do not validate crossing people, full occlusion, fast motion, scale change, PTZ motion, camera reconnects, or identity across cameras.

**Next validation and fix order**

First resolve the detector/tracker confidence mismatch, correct tracker names/status reporting, and make switching/reset behavior explicit. Then expose per-pipeline settings and define timeout duration at the actual tracking FPS. Compare BoT-SORT and ByteTrack on the same labeled camera sequences at 5/10/15 tracking FPS, measuring ID switches, fragmentation, missed events, duplicate webhooks, latency, and CPU/GPU usage. Include occlusion, crossings, and outages. Evaluate ReID only if identity continuity requires it and the added cost is justified by those measurements.

There is no evidence here for the blanket ranking in the source comments (`BoT-SORT ≈ StrongSORT > OC-SORT > ByteTrack`). Keep the existing fixed-camera speed optimizations, but establish the accuracy/performance choice from deployment footage.
