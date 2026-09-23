# Processing feedback and optional frame ranking

Deployed on 2026-09-23. The preceding bounded-capture release is described
in `FACE_CAPTURE_RETRY_FIX.md`.

## Behavior

Person events request `processing_feedback` through the existing webhook POST.
For a single-image event, the receiver returns `processing_status: pending`
until processing finishes. VMS retries the same event after two seconds; these
checks do not enqueue the image again or count as destination failures.
The existing delivery-age deadline bounds the wait.

The receiver reports `saved` only after the direct database transaction commits.
Feedback-enabled requests bypass the batch writer and update the temporal face
cache after commit. A failed commit reports `failed` and does not send the
post-commit detection alerts. Other outcomes are `no_face`, `quality_rejected`,
`duplicate`, `invalid_image`, and `ignored`.

VMS stops a person track's follow-up captures when its reporting face receivers
confirm `saved`. Other outcomes retain the existing three-capture bound. Legacy
receivers retain the prior behavior. Transport acceptance counters still measure
delivery, not saved faces. `FACE_RESULT` log entries correlate processing outcomes
with event and track IDs; receiver entries include exact quality-rejection reasons.
No identity names or image data are added to these new logs.

Feedback is process-local, capped at 5,000 entries and expires after ten minutes.
This matches the current single-worker deployment; it is not a durable receipt or
an exactly-once guarantee across restarts. Pending checks resend the JPEG through
the existing POST, so fewer capture events do not necessarily mean fewer HTTP
requests or less bandwidth. A future status-only GET could reduce that overhead.

## Optional quality selection

`detection_config.person_quality_selection: true` enables a bounded upper-body
crop check using OpenCV's bundled frontal-face cascade, followed by sharpness
and confidence ranking. It observes at least one second of frames (unless a
shorter maximum collection window is explicitly configured). This is a ranking
hint, not recognition or a new rejection rule. Missing cascade files fall back
to sharpness; invalid image inputs preserve confidence-only behavior.

The default is **false**: the first representative test did not demonstrate
better tracker coverage. No new model download, package, database migration,
UI form, or public endpoint is needed.

## Validation

155 targeted tests passed: 139 VMS tests and 16 receiver tests. These include
pending-to-saved HTTP feedback, stable event IDs, destination accounting, queue
rejection, concurrent retries, simulated commit failure, and post-commit
notification failure. Receiver transaction tests use controlled sessions, not
production writes.

Offline replay used `20260916_050557_in_video.mp4`: 26.36 seconds, sampled at
5 fps (132 frames), with 438 tracked-person observations. It used the existing
YOLO checkpoint, BoTSORT, receiver SCRFD model, JPEG encoding, and current receiver
quality thresholds. All inference ran on CPU in containers without networking;
no identities were recognized, records saved, or face images exported.

| Selector | Captures | Usable captures | Tracker IDs with usable captures |
|---|---:|---:|---:|
| Deployed confidence selector | 22 | 10 | 4 |
| Optional quality selector | 21 | 9 | 4 |

All four tracker IDs with a usable-face opportunity in sampled person crops
were covered by both selectors. Tracker IDs are not unique-person ground truth;
this does not measure people missed by YOLO or tracking identity switches.

Simulating an immediate successful database save for the first usable face
reduces distinct capture events from 22 to 14 with the default selector, or
21 to 13 with optional ranking. These are estimates, not observed production
traffic reductions; processing latency, pending polls, recognition deduplication,
and actual database failures are not modeled in that estimate.

Aggregate results: `FACE_SELECTION_BENCHMARK.json`. Reproduction tools:
`scripts/benchmark_face_selection.py` and `scripts/evaluate_face_selection.py`.
The benchmark requires an immutable baseline image containing the prior pipeline
at `/app/InferenceNode/pipeline.py`; mount current source separately at `/workspace`.

## Release scope

Deploy the receiver first: its new feedback helper, webhook handler, queue worker,
and image-processing changes. Then deploy the sender: pipeline, candidate-quality
helper, publisher, base destination, and webhook destination changes. Keep unrelated
repository changes out of the release. Quality ranking stays off unless explicitly
enabled after additional footage validation.

## Deployment verification — 2026-09-23

- Sender: `armyeye-vms:processing-feedback-20260923t042141z`
- Receiver: `face-detector-receiver:processing-feedback-20260923t042141z`

Both were built from their previous production images with only the nine scoped
application files replaced. The receiver queue was empty before restart. Both
containers passed health checks; all deployed file hashes, environments, and data
mounts matched the planned release. The receiver reported ready and its proxy
was reloaded. Read-only checks of receiver TLS/authentication and VMS pages/API
returned HTTP 200. Both pipelines loaded, and neither enabled optional quality
ranking. No test face events were sent.

Previous images are retained for rollback. Release metadata is recorded in
`backups/processing-feedback-release.json`. This deployment does not establish
increased unique-person recall: the representative clip showed equal tracker
coverage, and capturing every different face remains unproven.
