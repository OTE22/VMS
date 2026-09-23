# Pipeline Builder targeted fixes — 2026-09-23

Deployed and verified as `armyeye-vms:pipeline-builder-20260923t052709z`.
VMS health, file hashes, environment and mounts passed verification. Both production
pipelines remained stopped; no pipeline configuration records were edited. Existing
unrelated workspace changes were preserved. See `backups/pipeline-builder-release.json`
for the release and rollback image identities.

## Scope

Three application files: `templates/pipeline_builder.html`, the create/update
handlers in `inference_node.py`, and the small `pipeline_form.py` helper.

- Save has a pending guard, freezes form controls, and blocks reset/edit/template
  actions, including the top Cancel button. Request identity is captured before
  awaiting responses. Failed requests restore controls for retry.
- Unapplied destination changes block Save. Cancel Destination discards only that
  editor; favorites retain their ID until the server resolves their credentials.
- Create/update validate available source schemas and required fields. Invalid
  source writes return 400 instead of persisting an unusable definition. Schema
  defaults, webcam index zero and canonical relative media references are supported.
- Favorite credentials are resolved server-side with the existing redaction-safe
  merge, preserving explicit replacement/clear behavior. GET responses stay redacted.
- Testing a saved camera references the authorized existing pipeline for secret
  reuse; its source type must match. Test requests use `inference_enabled: false`
  and `model.device: cpu`. Camera configuration is no longer logged to the console.
- Failed Test Source deletion retains the ID, reports failure and permits retry.
  Starting another test first retries outstanding cleanup.
- Pending uploads are counted so an older completion cannot release Save early.
- Pipeline names and related card text are escaped before HTML rendering;
  destination secret fields are masked in summaries.

## Verification

- 68 JavaScript checks passed, including nine new builder safety regressions.
- 86 backend/persistence/auth/media tests passed against disposable PostgreSQL,
  with only the targeted backend patch overlaid on the deployed image.
- One existing encryption-at-rest test was intentionally excluded: it requires
  the separate migration that is present in the source tree but not deployed.
- The first isolated run exposed that source/deployment mismatch; the scoped
  runner now applies just the two route changes to a copy of the deployed module.
  A fixture conflict between test modules was resolved by sharing the existing
  authenticated fixture; the final run completed with no errors.
- No live camera, GPU inference, webhook delivery or production database writes
  were performed. Whitespace checks passed with the repository's CRLF convention.

## Remaining boundaries

The pending pipeline encryption migration remains separate. Browser closure can
still interrupt temporary-test cleanup; this patch adds retry/error handling, not
server-side expiry. Save guards prevent double clicks in one page; cross-tab and
ambiguous network retry idempotency would require a larger server-side change.
Escaping the audited builder fields is not a whole-application XSS certification.

This deployment applied these three scoped application changes to the previous image;
do not copy the complete local inference_node.py into that image without also
handling its pre-existing, undeployed encryption dependencies. The isolated test
staging file `/tmp/builder-release-inference_node.py` contains only this patch.
