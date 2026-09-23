# Models page deep audit — 2026-09-23

## Scope and verdict

Audited `/models` against the running release
`armyeye-vms:pipeline-management-20260923t060138z`. The models template,
model_repo.py and model_registry.py exactly match the running container.
The page cannot be considered fully correct: nine issue groups were reproduced.
Application code and deployment were not changed during this audit.

## Confirmed findings

1. **Advertised formats are rejected.** The page explicitly accepts Geti `.zip`
   packages and PyTorch `.pth` files. `ModelRepository.store_model` permits only
   pt/onnx/engine/xml/bin/tflite/pb extensions. Both ZIP and PTH uploads returned
   HTTP 500 with an unsupported-format error in real-route tests. The ZIP fixture
   was a real ZIP container; rejection occurs before package content inspection.
   Align the page with supported ingestion, implement the intended package formats,
   and return a validation response rather than a server error for unsupported input.

2. **AVAILABLE/PASSED does not establish model validity or engine compatibility.**
   An empty `.pt` upload and non-model bytes submitted with an unknown engine both
   returned HTTP 200 and became AVAILABLE/PASSED records. Current checks establish
   extension, byte count, hash and storage integrity; they do not establish a usable
   model. The badge nevertheless says the model is usable by pipelines. Validate
   supported engine/format combinations, reject empty files, and separate storage
   verification from runtime/format validation. Do not equate successful storage
   with successful inference initialization.

3. **An upload can silently change an existing model's metadata.** Model identity is
   filename stem plus a truncated content hash, excluding engine type. Uploading
   identical bytes under the same filename, first as Ultralytics/name First and then
   ONNX/name Second, returned the same model ID twice with HTTP 200. The existing row
   then contained the second name and engine. Existing pipeline references retain
   that ID. Define explicit duplicate semantics: return the existing record unchanged,
   require an intentional update, or reject incompatible engine reuse.

4. **Identical concurrent uploads still share an artifact staging path.** The upload
   route uses separate temporary request directories, but repository staging and
   final paths derive from the same stable model ID. With both calls staged before
   hashing and the second hash delayed until the first completed, the first upload
   succeeded and the second failed after its staging file was moved. This was a
   real filesystem/PostgreSQL test with deterministic scheduling. The final model
   remained AVAILABLE in this reproduction; no corruption is claimed. Serialize
   ingestion by model identity or use per-operation staging plus safe duplicate
   reconciliation. Separate request temp directories alone do not solve this race.

5. **Downloaded models omit uploader identity.** Normal uploads pass the authenticated
   uploader ID and username to store_model. The Ultralytics download route omits them.
   A mocked download through the actual authenticated route saved name/description
   correctly, but both uploader columns were NULL. Derive uploader fields identically
   in both routes. No network model download was performed for this test.

6. **Two UI entry points can start overlapping downloads.** Download disables its own
   button, but Upload remains enabled. With Ultralytics selected and no local file,
   the upload handler calls the same download function, which has no pending guard.
   The JavaScript probe issued two outstanding requests. Use one pending operation
   guard shared by upload and download, and disable conflicting actions together.
   The downloader also uses shared library/cache paths; concurrent real-network
   download behavior was not exercised.

7. **Finishing an upload can erase newer form edits.** Only the submit button is
   disabled. While an upload was pending, the probe changed the model name to a new
   draft. Successful completion called form.reset and cleared that newer input.
   Download completion similarly clears name and description unconditionally in
   the inspected code. Freeze the operation's editor or reset only the matching
   form generation; retain user input on failure.

8. **An older refresh can overwrite the newer model list.** Two refreshes resolved
   in reverse order; the older result was rendered last. Upload, download, delete,
   Refresh and list/grid switching all trigger this loader. Add a request sequence
   guard or serialize refreshes so stale snapshots cannot restore removed entries
   or hide newly uploaded ones. This reproduction concerns browser state, not a
   reversed database commit.

9. **An apostrophe in the filename can break Delete.** The raw filename stem becomes
   part of model_id. A real upload preserved an apostrophe in that ID. The rendered
   handler uses `deleteModel('${esc(modelId)}')`: HTML escaping is decoded by the
   browser before JavaScript parses the handler. Reconstructing that decoded handler
   produced a SyntaxError. This affects both card/grid renderers. Bind handlers
   through listeners/data attributes and URL-encode IDs for requests. Escaping the
   visible model text alone is insufficient for JavaScript contexts.

## Action and data flow coverage

| Action | Request/effect | Persistence and result |
|---|---|---|
| Open page | GET `/models` | Live 200 |
| Load engine selector | GET `/api/inference/engines` | Live 200; metadata populates selector, accept/required attributes |
| Select engine/file/name/description | Browser form state | Saved only on upload/download; engine/format mismatches above |
| Upload file | XHR POST `/api/models/upload` multipart | Authenticated uploader; metadata, representation/artifact rows; managed model bytes; normal hash/field tests pass, findings above |
| Download Ultralytics | POST `/api/models/download-ultralytics` | Downloader locates weights then calls same repository; mocked real-route persistence tested; uploader omission above |
| Refresh/list/grid | GET `/api/models`; browser view state | Registry metadata and storage stats; live 200, stale-response issue above |
| Status badge | Uses status/validation_status/reason | Metadata is escaped for display; availability overstates runtime validation |
| Delete | DELETE `/api/models/<id>` | Admin/CSRF gated; dependency check, staged trash, row deletion and purge; special-character handler issue above |
| Help/format descriptions | Static UI | ZIP/PTH promises conflict with storage validator |

Normal upload maps name/description/engine/original filename/uploader to `models`,
format/kind/required state to `model_representations`, and relative path, hash, size
and verification state to `model_artifacts`. Bytes live beneath the managed artifact
root; absolute browser-local paths are not stored as artifact references.

The upload progress bar measures bytes sent, not completion of model validation.
The download progress steps are simulated UI states, not measured download progress.
CSRF protection is present: shared app.js wraps both fetch and XMLHttpRequest. The
absence of an explicit header inside this template is not a missing-CSRF finding.

## Evidence and limits

- 99 existing model lifecycle, migration, label, registry/schema and form tests
  passed on deployed-image dependencies with isolated test databases.
- One pre-existing pipeline encryption test was deselected: its separate migration
  remains undeployed and is not part of the models audit.
- Seven targeted route/database diagnostic cases reproduced format rejection,
  invalid availability, metadata replacement, omitted uploader and special IDs.
- One additional deterministic filesystem/database concurrency case reproduced
  the shared-staging failure. Diagnostic tests assert the faulty behavior; their
  passing does not mean these issues are fixed.
- The actual Delete route returned 409 while a pipeline referenced the model;
  after removing that isolated reference it returned 200, and both the model row
  and managed file were gone. This additional route test passed.
- Four actual-function JavaScript probes reproduced overlapping downloads, stale
  lists, lost drafts and the invalid Delete handler.
- Live GETs for the page, models data, engines and health all returned HTTP 200.
- Probe files: `/tmp/models-audit-tests/test_models_audit.py`, `/tmp/models-probes.mjs`.
- Logs: `/tmp/models-audit-db.log`, `/tmp/models-concurrency.log`,
  `/tmp/models-baseline.log`, `/tmp/models-delete.log`.
- No production models were uploaded, downloaded, loaded into an engine or deleted.
  No production pipeline was started. Tests used harmless fake weight bytes and
  mocked Ultralytics downloads; there was no external model download or GPU test.

Recommended order: format/engine validation and honest availability, duplicate
identity/concurrent ingestion, shared form-operation guards, safe Delete binding,
then refresh sequencing and download attribution. Full runtime compatibility of
real model packages requires separate representative model tests.
