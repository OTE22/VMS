# Webhook timestamp compatibility fix

Deployed 2026-09-16 UTC.

## Cause and change

VMS sent `captured_at` as numeric Unix seconds. The deployed VAS receiver requires
an ISO-8601 string with a timezone and rejects numeric values with HTTP 422.
VMS now serializes the original capture time in UTC with a `Z` suffix. Internal
tracking times remain numeric. Older queued payloads with numeric capture times
are normalized on preparation without changing event IDs or delivery acknowledgments.
Already rejected/dropped deliveries are not automatically recreated.

## Verification

- 158 event-delivery, retry/outbox, and webhook tests passed.
- New coverage verifies UTC conversion, epoch zero, absent capture-time fallback,
  retry stability, and legacy outbox replay.
- Deployed only `InferenceNode/pipeline.py` over the previous application image.
- An authenticated image-free webhook generated through the deployed preparation
  code returned HTTP 200 from VAS (`status=ok`, `message=No images`).
- VMS `/health` returned HTTP 200 after recreation.
- The existing pipeline was stopped before deployment and was not started for this check.
  Actual image processing still requires starting it and checking real deliveries.
- No environment values, destination URLs, database schema, or VAS code were changed.

## Images

Deployed: `armyeye-vms:timestamp-20260916t052307z` (also tagged `armyeye-vms`).
Rollback: `armyeye-vms:before-timestamp-20260916t052307z`.
