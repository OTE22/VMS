# Pipeline Builder audit — 2026-09-23

## Verdict

The normal create/edit/upload/import/export paths have substantial passing
coverage, but the page cannot be marked fully correct. The initial four findings
were followed by additional targeted reproductions, documented below.
This was an inspection and isolated test run; application code and production
records were not changed or deployed.

## Confirmed findings

1. **Publisher favorite credentials do not survive copying into a new pipeline.**
   `selectFavoriteConfig` -> `loadFavoriteInPipeline` populates the form from the
   redacted favorite GET response. `addDestination` copies that config under a
   new destination ID, without a server-side favorite reference. On the deployed
   image, a real PostgreSQL reproduction saved the literal `***` as the new MQTT
   destination password, while returning HTTP 200. This can break authentication.
   Resolve favorites server-side under the requesting user's permissions; never
   expose decrypted secrets to the browser to work around this.

2. **Repeated Save submissions create duplicate rows.**
   The submit handler has an upload guard but no save-in-flight guard. A JavaScript
   reproduction issued two POSTs immediately. Repeating the exact payload against
   the deployed create endpoint produced two different IDs and two PostgreSQL
   records with the same name. Disable both submit buttons/reset while saving,
   capture edit mode for the request, and guard duplicate submissions. Separate
   tabs/network retries would additionally require server-side idempotency.

3. **Test Source cleanup can silently leave orphaned pipelines.**
   `stopTestSource` clears `activeTestSourceId` before cleanup. `cleanupTestPipeline`
   only logs warnings for failed stop/delete and catches errors without reporting
   failure. A mocked HTTP-500 reproduction returned normally with the session ID
   lost. Closing/navigating away also has no reliable server-side expiry guarantee.
   Retain the ID until deletion succeeds, surface failures, and allow retry.
   Consider server expiry for genuinely temporary test pipelines.

4. **Pipeline credential encryption is not deployed.**
   The deployed image's create route saved a fake camera password in clear text
   in the isolated database's `pipelines.config`. The corresponding encryption
   assertion failed. Publisher favorites have their own encrypted storage and
   passed their tests; API redaction is distinct from encryption at rest.
   Pending source-tree encryption work needs its separate migration/rollback
   procedure in `PIPELINE_CREDENTIAL_ENCRYPTION.md`, not a blind image rebuild.

## Page actions and endpoint coverage

| Action | Request / effect | Database relationship | Audit result |
|---|---|---|---|
| Open builder | GET `/pipeline-builder` | None | Live HTTP 200; scripts parse |
| Load pipeline list | GET `/api/pipelines` | Scoped read of pipelines plus runtime state | Live HTTP 200; redaction/access tests pass |
| Select source | GET `/api/frame-sources` | Metadata only | Live HTTP 200; schema collector tests pass |
| Discover devices | GET `/api/frame-sources/<type>/discover` | Runtime discovery, not configuration save | Request/response handling reviewed; physical discovery not exercised |
| Load models | GET `/api/models` | Model registry read | Live HTTP 200; registry tests pass |
| Load engines | GET `/api/inference/engines` | Runtime capability metadata | Live HTTP 200 |
| Select hardware | GET `/api/hardware` | Runtime capability metadata | Live HTTP 200; device field persistence tested |
| Upload video | POST `/api/media/upload-video` from schema-provided endpoint | Registers media metadata/artifact; pipeline stores canonical relative reference | Isolated registry/hash tests and frontend upload/race/failure tests pass |
| Refresh saved videos | GET `/api/media/sources` | Media registry read | Live HTTP 200; canonical selection tests pass |
| Load/refresh favorites | GET `/api/publisher/favorites` | Redacted publisher records | Live HTTP 200; copying secrets into a new pipeline fails |
| Select publisher type | GET `/api/publisher/types` | Plugin schema metadata | Live HTTP 200 |
| Add/edit/remove destination; destination checkbox | Browser state until pipeline Save | Later becomes `pipelines.config.destinations` | Serialization/secret-preservation tests pass for existing destinations; favorite-copy exception above |
| Save new pipeline | POST `/api/pipeline/create` | Inserts a pipeline row | Normal write tests pass; duplicate submit and plaintext-secret findings above |
| Save edited pipeline | PUT `/api/pipeline/<id>` | Updates row and JSON config, preserves existing redacted credentials | Persistence/access tests pass; full encryption round-trip test stops at initial encryption failure |
| Reset/cancel, filters, quick-search, templates | Browser-only form state | No save until submit | Handlers reviewed; templates still need required model/source/destination choices |
| Upload Model / Create Template shortcuts | Navigate to `/models` or `/publisher` | Writes occur on those separate pages | Links reviewed |
| Test Source | POST create, POST start; GET stream; POST stop, DELETE temporary pipeline | Creates a real temporary pipeline row | Code traced; cleanup failure reproduced; no physical source opened |
| Start/stop existing pipeline | POST `/api/pipeline/<id>/start` or `/stop` | Loads saved config and updates runtime/status | Route and error handling reviewed; production pipelines not started |
| Preview / fullscreen / close | GET `/api/pipeline/<id>/stream`; browser modal/fullscreen operations | No configuration save | Route wiring reviewed; live camera/video playback not exercised |
| Duplicate | POST `/api/pipeline/<id>/duplicate` | Server creates new pipeline/destination IDs; copies unredacted source server-side | Isolated tests pass |
| Import | POST `/api/pipeline/import` multipart file | Imports config and registered model bytes | Isolated tests pass, including rollback on import failure |
| Export | GET `/api/pipeline/<id>/export` | Reads config and model artifact | Isolated tests pass, including import/export byte round-trip |
| Delete | DELETE `/api/pipeline/<id>` | Deletes authorized pipeline record | Persistence/access/audit tests pass |

Destination form edits must be applied with Add/Update Destination before saving
the pipeline: the submit payload uses `currentDestinations`, not unsaved fields
in the destination editor. The current page does not guard against saving while
those editor changes remain unapplied. Some metadata loaders also log failures
or silently retain old UI instead of visibly marking the data unavailable.

## Form-to-database mapping

- Name and description: `pipelines.name`, `pipelines.description`, plus config.
- Source type and schema fields: `pipelines.config.frame_source`.
- Model, engine, device: `pipelines.config.model` and normalized model reference.
- Inference checkbox: `pipelines.config.inference_enabled`.
- Destination IDs, types, enabled flags and settings: `pipelines.config.destinations`.
- Uploaded videos: media registry/artifact storage; pipeline retains the returned
  relative reference, not a browser-local file path.
- Favorite selection: copies publisher fields into the browser editor; it is not
  currently a secure server-side credential-copy operation.

## Evidence and limits

- 59 existing JavaScript checks passed, including schema/secret round trips,
  upload selection, stale media-list protection, error handling and script syntax.
- Deployed image `armyeye-vms:pipeline-controls-20260923t044126z` ran against a
  dedicated PostgreSQL 16 container with no production network or mounted data.
  The main run had **80 passed, 1 failed** (pipeline credential encryption).
- Extra isolated database probes: duplicate POST behavior reproduced; the favorite
  credential-preservation assertion failed, storing `***` instead of the fake secret.
- Extra JavaScript probes reproduced two immediate Save POSTs and silent failed
  Test Source cleanup. HTTP effects were mocked.
- Nine live read-only requests (the page plus eight data endpoints) returned 200.
- Disposable PostgreSQL containers were removed by the test runners.
- No production camera, webhook delivery, device discovery, enrollment, pipeline
  creation, upload, deletion or database migration was performed. Hardware/plugin
  functionality for every possible configuration is therefore not certified.

Recommended fix order: favorite credential copying; duplicate-submit guard;
reliable Test Source cleanup; separately planned encryption migration.

## Deeper follow-up: additional reproduced behavior

The audited builder template is byte-for-byte identical to the template in the
running VMS container. Five additional JavaScript probes and five additional
PostgreSQL tests completed successfully: these are diagnostic assertions of the
faulty behavior, **not** evidence that the faults are fixed. Database tests used
the deployed image and a disposable database, without production connectivity.

### 1. Destination editor changes can be silently omitted

`pipeline_builder.html:3148` serializes `currentDestinations`, irrespective of
`editingDestinationId`. A probe put `new.invalid` in the visible destination
editor while the applied destination contained `old.invalid`; Save submitted
`old.invalid` without warning. Users must click Update Destination first.

Recommended change: reject pipeline Save while a destination has unapplied
changes, or apply and validate that editor explicitly before submitting.

### 2. A completed save can clear a newer draft

The same submit handler calls `resetForm(true)` after an awaited response without
checking whether the current editor still belongs to that request. The probe
started a save for A, changed editor state to B before resolving the request,
and observed the reset targeting B. Reset remains available while saving, so a
user can leave the first draft and begin another during a slow request. This
loses browser edits; it does not mean the first request overwrites B's DB row.

Recommended change: capture the target ID and form generation before the request,
block conflicting actions during Save, and only reset the matching editor.

### 3. Source validation fails open, and the backend accepts unusable definitions

`collectFrameSourceConfigFromSchema` at line 1828 treats missing source metadata
as valid. It also skips absent DOM elements before checking whether they are
required. Both paths reproduced as `isValid: true` with empty configuration.
Against the deployed backend, both an IP camera with empty config and an unknown
capture type returned HTTP 200 and were written to PostgreSQL. Saving a definition
therefore does not demonstrate that its source can start.

Recommended change: validate source type and required schema fields server-side;
block submission when required frontend metadata/fields are unavailable. If draft
pipelines are intentionally allowed, clearly distinguish draft saves from runnable
configurations rather than reporting an unqualified successful configuration.

### 4. Test Source cannot reuse a masked camera password correctly

`testFrameSource` at line 1630 copies the current form fields into a **new**
pipeline. An existing camera's password arrives redacted as `***`; unlike updating
the original record, the new pipeline has no existing secret to preserve. An
isolated database test reproduced the new row containing literal `***`. This can
make a correctly configured camera fail the test when opened in Edit mode.

Recommended change: test the authorized existing pipeline's source server-side,
or resolve its stored credentials server-side while applying explicit form edits.
Do not return the password to the browser. The function also logs `testConfig` to
the browser console, potentially exposing freshly entered credentials; remove or
redact that log.

### 5. Changing source during uploads can release the Save guard too early

`handleFileUpload` at line 4327 uses one shared boolean on the form. A probe started
upload A, replaced the source editor and started upload B, then completed A first.
A's `finally` removed `uploadInProgress` while B was still pending. Existing DOM
identity checks correctly prevent A from overwriting B's fields, but do not
protect the shared guard. With another selected media item present, Save may
proceed using that older selection before B finishes.

Recommended change: track pending uploads using operation IDs or a counter and
keep Save blocked until the relevant pending upload completes.

### 6. Pipeline names are persisted and rendered as raw HTML

A harmless `<b data-audit="marker">Test</b>` name survived a database write. The
actual `updatePipelinesList` function at line 3580 inserted it directly into
`innerHTML`, rather than as escaped text. This confirms HTML injection in the
pipeline list and creates a potential stored-script-injection risk, subject to
browser/CSP restrictions. No executable payload was used and browser execution
was not tested.

Recommended change: render names and other user-controlled text with `textContent`
or consistent HTML escaping; audit other interpolated card fields as well.

### 7. Test Source sends the inference flag in the wrong shape

Its request contains `inference: {enabled: false, device: 'cpu'}` instead of
`inference_enabled: false` and `model.device`. The deployed definition builder
ignores the nested object and the database test confirmed `inference_enabled`
becomes `true`. The selected engine is still `pass`, so this finding does **not**
prove that face detection runs during source tests; it is a settings-contract
mismatch.

Recommended change: use the same canonical payload shape as the main Save form.

## Follow-up evidence and priorities

- JavaScript probes: `/tmp/builder-deep-probes.mjs` — all five completed.
- Database probes: `/tmp/builder-audit-tests/test_builder_deep.py` — 5 passed.
- Runner: `/tmp/audit-builder-deep.py`; log: `/tmp/builder-deep-db.log`.
- Verified no disposable audit database containers remained afterward.
- These checks exercised extracted real JavaScript functions with mocked browser
  objects, plus real deployed Flask routes and isolated PostgreSQL. They were not
  full browser/hardware end-to-end tests.
- No application code, production records, or deployment was changed during this
  follow-up; this report is the only workspace change from the follow-up.

Prioritize safe secret reuse (favorites and Test Source), raw HTML rendering,
server-side configuration validation, then save/editor/upload concurrency guards.
Retain the earlier cleanup fix and separately reviewed encryption migration.
