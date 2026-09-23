# Pipeline Management deep audit — 2026-09-23

## Release and scope

The requested builder fixes are deployed as
`armyeye-vms:pipeline-builder-20260923t052709z`. VMS is healthy; deployed file
hashes, environment and mounts were verified. The receiver passed readiness and
webhook test GETs through the TLS proxy. Both production pipelines remain stopped.
The pipeline encryption migration was excluded from this scoped release.

This management audit was read-only in production. Reproductions used extracted
actual JavaScript with browser stubs and the deployed application image with a
disposable PostgreSQL database. Runtime/camera objects were mocked. No management
fixes were made or deployed as part of the audit.

## Confirmed issues, in recommended fix order

### 1. Concurrent controls can undo each other's database changes — high

`pipeline_manager.py:_set_inference_enabled` and `_set_publisher_enabled` read the
whole saved definition and write the whole JSON document back through `_put_config`.
There is no atomic field update, version check or transaction spanning that read
and write. In the real PostgreSQL reproduction, both operations read the same
initial state. Disable Inference committed first; Disable Publisher committed
second. Both returned true, but the final row had inference enabled again.

Impact: different browser sessions, or bulk and individual operations, can silently
lose an accepted change. Use atomic field updates or a locked/versioned transaction;
a browser-only pending flag cannot protect multiple clients.

### 2. A missing publisher can report a successful toggle — medium

`_set_publisher_enabled` logs that the destination was not found, writes the
unchanged definition and returns true. POST disable with a random destination ID
on a valid pipeline returned HTTP 200; PostgreSQL destinations were unchanged.
A stale browser destination can therefore produce a misleading success message.

Return a not-found result before writing or applying runtime state when the
requested destination does not exist. Check persistence return values as well.

### 3. Runtime failure can contradict the response and saved setting — medium

The setting is committed before invoking the live pipeline method. Injecting a
runtime `disable_inference` exception produced HTTP 404 ('Pipeline not found'),
even though the pipeline existed and PostgreSQL already contained disabled
inference. The response does not distinguish a persistence failure from a runtime
application failure.

Report saved state and runtime-application failure accurately. Define and test
rollback or reconciliation behavior instead of reporting a generic not-found error.
This test simulated a runtime exception; no physical inference engine was used.

### 4. Preview readiness HEAD requests leak viewer registrations — medium

`waitForPipelineReady` calls HEAD `/api/pipeline/<id>/stream`. The Flask route calls
`start_streaming()` before returning a streaming response; `stop_streaming()` is
inside the body generator's `finally`. HEAD does not iterate that body.

The real route, with the actual `InferencePipeline.start_streaming/stop_streaming`
methods bound to a fake runtime, returned 200. After response close, viewer count
was still 1 and streaming remained true. This can retain preview processing even
without a real viewer; processing overhead was not measured.

Use a side-effect-free readiness endpoint or explicitly handle HEAD before
registering viewers. Keep viewer registration and cleanup in the same lifecycle.

### 5. Stored text is unsafe in markup and can break fullscreen — high/medium

`renderPipelineDetails`, card/list rendering and configuration `<pre>` blocks
interpolate names, descriptions or JSON directly into HTML. A harmless
`<b data-audit="marker">Test</b>` name was inserted as markup in the actual details
renderer. This is confirmed HTML injection and a potential stored-script injection
risk, subject to browser/CSP behavior. No executable payload was tested.

The card's fullscreen handler also embeds the name in a single-quoted JavaScript
argument. The ordinary name `Operator's camera` produces invalid handler syntax.

Render text with `textContent` or consistent escaping. Bind fullscreen actions
with event listeners or data attributes instead of interpolating names into code.
The builder's escaping fix does not fix this separate page.

### 6. Delayed polling can replace newer state — medium

Two refresh requests completed in reverse order in the probe. The older stopped
snapshot replaced the newer running snapshot. A delayed publisher-status response
likewise overwrote a newer enabled state with disabled. There are no request
sequence checks; the one-second metrics loop launches publisher polling without
awaiting its completion, allowing overlapping requests under latency.

Use request generation/version checks or serialize polling, and reconcile pending
writes before applying older responses. These reproductions changed browser state,
not the database; distinguish them from the database race in issue 1.

### 7. Bulk inference bypasses the individual-control safeguards — medium

Enable/Disable All select only running pipelines, although individual controls
allow changing the saved setting of stopped pipelines. Enable All on a stopped
pipeline sent no request. For a running pipeline with an individual save pending,
Enable All still issued another POST: it bypasses `pendingPipelineControls` and the
shared save helper. Bulk loops also have no overall in-flight guard.

Route bulk actions through the same control-saving path and clearly define whether
'All' means every filtered pipeline or only running pipelines. Serialize conflicting
operations. Server-side concurrency protection remains necessary.

### 8. Repeated Duplicate sends repeated creation requests — medium

Calling the actual duplicate handler twice while its first request remained
pending issued two POSTs. Server-authoritative duplication correctly copies saved
credentials, but each accepted POST creates another pipeline. Add a pending action
guard and disable its button. Cross-tab/network retry idempotency is separate.

### 9. RTSP and GPU quick filters miss common canonical values — medium

Filtering compares the labels literally with saved strings. The RTSP chip does
not match capture type `ip_camera`; GPU does not match device `cuda:0`. Both were
reproduced with the actual `filterPipelines` function and a synthetic pipeline.
Users can mistakenly see an empty list, and bulk operations act on that filtered
list. Normalize UI categories to supported source/device aliases in both filtering
implementations (`filterPipelines` and `updatePipelineData`).

## Buttons, requests and persistence

| Action | Endpoint/effect | Database relationship and audit result |
|---|---|---|
| Open page, refresh | GET `/pipeline-management`, `/api/pipelines` | Scoped pipeline reads; live 200; delayed-response issue above |
| Worker identity | GET `/api/node/identity` | Determines Start routing; live 200 |
| Metrics polling | GET `/api/pipelines/metrics` | Runtime metrics with visible pipeline scope; live 200; overlap/staleness reviewed |
| Engine filter metadata | GET `/api/inference/engines` | Capability metadata; live 200 |
| Details | GET `/api/pipeline/<id>` | Sanitized saved config plus state; live 200 for both pipelines; rendering issues above |
| Status | GET `/api/pipeline/<id>/status` | Runtime truth plus stored state; live 200 for both pipelines |
| Start/Stop and Start/Stop All | POST `/start`, `/stop` | Saved definition drives runtime; last-known status updated. Wiring reviewed; production pipelines not started |
| Inference switch / bulk | POST `/inference/enable`, `/inference/disable` | Saves `config.inference_enabled`, then applies live; concurrency/failure issues reproduced |
| Publisher switch / Retry | POST `/publisher/<destination-id>/enable` or `/disable` | Saves destination enabled flag and applies runtime/recovery; unknown-ID and concurrency issues reproduced |
| Publisher status | GET `/publishers/status` | Saved destination metadata merged with runtime state; live 200 for both pipelines; stale responses reproduced |
| Edit | Navigation `/pipeline-builder?edit=<id>` | Builder handles persistence; newly deployed builder checks passed |
| Duplicate | POST `/duplicate` | New row and destination IDs; existing persistence checks pass; repeated-click issue above |
| Delete | DELETE `/api/pipeline/<id>` | Coordinated stop/thumbnail/DB cleanup; existing persistence/auth/media checks pass |
| Export | GET `/export` | Reads configuration and registered model artifact; isolated round-trip tests pass |
| Import | POST `/api/pipeline/import` | Imports row/model artifact, with rollback checks; isolated tests pass |
| Card/List, search, quick filters | Browser state; view mode in localStorage | No pipeline write; canonical filter mismatch reproduced |
| Preview, Show/Hide All, fullscreen | GET `/stream`, `/stream/hq`; readiness HEAD | Runtime viewer state, no config write; HEAD leak and fullscreen name issue above |
| Thumbnail load/existence | GET `/thumbnail`, `/thumbnail/exists` | Registry/artifact read; existence live 200 for both pipelines |
| Thumbnail generation | POST `/thumbnail/generate` | Captures/registers thumbnail; wiring and shared persistence tests reviewed, no production capture performed |

Preview/view/filter choices are browser state, not durable pipeline configuration.
Inference and publisher switches are durable configuration. A successful save alone
does not certify physical source startup or delivery to an external destination.

## Evidence and limits

- 13 management-page/data GETs returned HTTP 200 against production.
- 86 existing/shared backend tests passed against the **final deployed image** and
  isolated PostgreSQL. One existing encryption-at-rest test was intentionally
  deselected because its separate migration remains undeployed.
- Five existing management JavaScript tests passed, including individual pending
  guards, failed-save restoration, recovery controls and script parsing.
- Eight additional JavaScript diagnostic probes reproduced the listed behavior.
- Four additional real-route/real-database diagnostic tests passed by asserting
  the faulty behavior. Passing diagnostic assertions do not mean those issues are fixed.
- Probe scripts: `/tmp/management-probes.mjs`,
  `/tmp/management-audit-tests/test_management_audit.py`.
- Logs: `/tmp/management-db-audit.log`, `/tmp/builder-release-db.log`.
- No real camera/GPU inference, production toggle, start/stop, import, duplication,
  deletion, thumbnail generation or webhook delivery was performed during the audit.
- This is not full browser end-to-end or hardware certification. Database race
  timing was made deterministic with barriers around actual reads and writes.

## Fix follow-up

The nine findings above now have local fixes and regression tests documented in
[PIPELINE_MANAGEMENT_FIX.md](PIPELINE_MANAGEMENT_FIX.md). These management changes
are now deployed and verified; the reproductions above describe the earlier audited
version. See the fix report for release details.
