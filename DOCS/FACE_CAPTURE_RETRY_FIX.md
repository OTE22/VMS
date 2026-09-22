# Bounded person captures and webhook retries

The sender previously cooled down a person track for 120 seconds after HTTP
acceptance, even when the receiver found no usable face in that frame.

`InferenceNode/pipeline.py` now allows three successful person captures in a
burst, with collection of each follow-up starting at least two seconds after
the preceding successful delivery. Each capture uses a newly observed frame
and a new event ID. Transport retries retain their original frame and event ID.
After the third capture, the existing 120-second cooldown applies. A later
burst can start after that cooldown. Non-person classes retain their cooldown.

Optional `detection_config` settings:

- `person_capture_count`: 1–3, default 3; 1 restores single-capture behavior.
- `person_capture_interval_seconds`: at least 1, default 2.

Explicit shorter `track_ttl_seconds` settings still take precedence. Pending
deliveries, failed-delivery backoff, and terminal rejection still block capture.

The companion receiver change is in `VAS/backend/routes/webhook.py`. Rejected
requests with zero queued images release their deduplication reservation,
including exceptions and cancellation. Concurrent attempts for a pending event
receive HTTP 429 with Retry-After; accepted duplicates retain HTTP 200. Existing
partial acceptance behavior for multi-image requests is unchanged; VMS sends
one image per event. Deduplication remains in memory and per process.

This is a bounded second-chance mechanism, not a recognition acknowledgement
protocol. It can increase person-image traffic up to threefold and can save
additional unknown-face records. It leaves face-quality thresholds, sampling
rate, tracker association, and the receiver's single-face crop policy unchanged.
Recognition improvement on actual footage has not been measured.

Regression coverage uses synthetic frames and an isolated queue, without live
camera input, database writes, or production requests. Source changes do not
take effect in running containers until deployed.
