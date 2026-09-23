# Pipeline Management fixes — 2026-09-23

Deployed and verified as `armyeye-vms:pipeline-management-20260923t060138z`. VMS health, file hashes,
environment and mounts passed verification. Both production pipelines remain stopped.
The separate pipeline encryption migration remains out of scope.

## Changes

The patch addresses the nine groups in PIPELINE_MANAGEMENT_AUDIT_20260923.md:

1. Control writes use a PostgreSQL row lock and modify only the requested flag in
   the latest saved configuration. Independent manager instances can toggle
   inference and publishers concurrently without replacing the other flag. A local
   manager lock also keeps commit/runtime-application order consistent per worker.
2. A missing destination returns 404 before writing. Database failures produce an
   error and never invoke the runtime control method.
3. A committed setting followed by a runtime exception returns HTTP 503 with
   `saved: true, runtime_applied: false`. The page shows a warning and refreshes
   state; it does not claim the pipeline disappeared or that the setting was not saved.
4. HEAD requests to standard and high-quality preview endpoints check readiness
   without subscribing viewers. Ready returns 200; missing frames returns 503.
5. Card/list/details and publisher markup use escaped display copies. Original
   data remains unchanged. Page alerts also escape text. Fullscreen actions pass
   only the pipeline ID and obtain its raw name separately for textContent.
6. Refresh responses are checked against request and state versions. Publisher
   polling and metrics polling avoid overlapping requests; older publisher replies
   cannot undo a control save. Failed refreshes retain the last known data.
7. Bulk inference uses the individual save helper, includes stopped pipelines in
   the filtered view, skips busy/starting controls and prevents concurrent bulk
   requests. Summaries distinguish saved, failed/not-applied and busy counts.
8. Duplicate has a per-pipeline pending guard; its current buttons are disabled
   while pending and restored for retry.
9. Both filter implementations normalize IP-camera/RTSP aliases and GPU/CUDA
   device strings instead of comparing only literal category labels.

Application scope: pipeline_repository.py, pipeline_manager.py, inference_node.py,
and templates/pipeline_management.html. No schema migration or dependency changes.
The manager and UI reuse shared control paths rather than duplicating the bulk logic.

## Verification

- 93 backend tests passed using deployed-image dependencies and disposable
  PostgreSQL with the scoped backend files overlaid.
- One encryption-at-rest test was intentionally deselected; it belongs to the
  separately pending encryption migration, not these management changes.
- Seven added database cases cover independent concurrent managers, unknown
  destination, standard/HQ readiness, missing frames, runtime failure and DB failure.
- 44 JavaScript tests passed: existing controls/template checks plus ten new
  management regressions for polling, bulk operations, duplicate retries,
  saved/runtime failure responses, escaping, fullscreen wiring and both filters.
- Logs: /tmp/management-fix-db.log and /tmp/management-fix-js.log.
- Whitespace checks passed with the repository's CRLF convention.

These tests use mocked camera/runtime objects and browser stubs; they do not
certify physical camera/GPU behavior or external publisher delivery. Row locking
protects concurrent control writes; arbitrary concurrent full-configuration edits
and cross-tab duplicate-request idempotency remain separate concerns.

## Deployment boundary

A scoped test staging copy is at /tmp/management-release-source/InferenceNode.
It contains current deployed backend code plus these changes, without activating
pre-existing local encryption dependencies. Do not blindly replace the deployed
repository/module with the complete local source tree: that would also introduce
the separate encryption migration. The currently deployed builder fixes should
remain included in any management release.

## Deployment verification

The exact release image passed 93 isolated backend tests (one separate encryption
test deselected). All 13 live management read-only checks returned HTTP 200. Builder
fixes and receiver connectivity were also verified. No production camera was started
or pipeline setting changed. Release and rollback metadata are recorded in
`backups/pipeline-management-release.json`.
