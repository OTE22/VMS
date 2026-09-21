# Web forms → requests → PostgreSQL audit

**Post-audit update:** Functional corrections are implemented in the [form release record](FORM_FIX_DEPLOYMENT.md). The findings below describe the state at audit time.

Date: 2026-09-21. Scope: all 18 HTML templates, 14 literal `<form>` elements (including logout forms), dynamic source/destination forms, and write buttons/wizards without a `<form>` tag.

**Result: several forms still fail or save a configuration that cannot be used. A successful HTTP response is not sufficient evidence that the submitted values were saved correctly.** The previous persistence fixes remain useful, but did not cover the contracts below.

## Evidence and limits

- Reviewed frontend collectors, Flask endpoints, stores, relational columns/JSON, and artifact registry contracts.
- Exercised actual authenticated Flask routes with CSRF against a separate, migrated PostgreSQL 16 database: **10 passed, 6 strict expected failures**. The six expected failures reproduce unresolved defects; they are not passing functionality.
- Executed shipped JavaScript collectors/submit handlers in Node with a small DOM harness: **50 checks passed** across the new audit and existing frontend contract/builder suites. Some checks explicitly assert that a defect exists; this does not mean every form works.
- Checked deployed source/destination schemas and plugin signatures. Production inspection was read-only. Production was running image `sha256:faf0408ea7782eda5e8149eca5f6f6262d863564dce14e2058851a682fe17671`, migration `0008_publisher_description`.
- Tests use synthetic accounts, credentials and file bytes. File tests verify storage/hash integrity, not that those bytes are a valid AI model or playable video. No external brokers, cameras, serial devices, cloud destinations or model downloads were exercised. No full browser automation was available; native browser behavior and hardware runtime success are not certified.
- Added tests and this report only during this audit; application fixes/deployment are not part of this audit turn.

Test evidence: [PostgreSQL round trips](../tests/test_form_roundtrips_pg.py), [JavaScript payload audit](../tests/form_payload_audit.test.mjs), [existing builder tests](../tests/pipeline_builder_roundtrip.test.mjs).

## Confirmed defects and recommended corrections

| Priority | Form/action | Actual result and evidence | Correction |
|---|---|---|---|
| High | Builder: create a Pass pipeline | UI submits `model.id: ""`. SQL canonical ID becomes NULL but JSON retains an empty string; PostgreSQL consistency CHECK rejects the insert, HTTP 500. Reproduced by `test_new_pass_pipeline_exact_browser_payload`. | Normalize an explicitly empty ID to NULL in both column and JSON before insert. Keep the consistency CHECK. |
| High | Builder: change an existing model to Pass | PUT returns 200, but the old model remains in `pipelines.model_id` and JSON. Reproduced by `test_switching_pipeline_to_pass_clears_old_model`. | Distinguish an absent model update from an explicit request to clear it; clear both references. |
| High | Pipeline export with registered model | Reads obsolete `stored_filename`; current registry exposes representations/artifacts. Export returns 500. | Export through registry artifact paths and manifests, rather than legacy metadata. |
| High | Pipeline import containing a model | Registers the model, then reads obsolete `stored_path` and returns 500. The earlier model registration can remain without the requested pipeline. | Use the registry result contract and implement rollback/cleanup for partially completed imports. |
| High | Webhook custom headers, both Builder and Publisher | Textarea produces a string such as `X-Audit: value`; save accepts it. Webhook configuration expects a dictionary and calls `.keys()`, raising an exception. Reproduced after actual DB save. | Parse/validate a header map before saving; render stored maps back to editable text; reject invalid types server-side. |
| High | Serial destination, both forms | Schema supplies `baud_rate`; plugin accepts `baud`, with no catch-all keyword parameter. Signature mismatch fails configuration before opening the port. | Standardize on one field name, convert the selected value to an integer, and migrate existing JSON keys. |
| Medium | Telemetry with blank broker | POST echoes submitted topic/port as success, but the empty-server branch never assigns those values; DB/GET omit them or retain previous values. Clearing a server is also not handled as an explicit disconnect/reset. | Validate and persist the complete desired config independently of whether a broker is enabled; explicitly handle clearing/disconnection. |
| Medium | Publisher “Send Test Message” | `testPublishForm` has no submit listener or caller for `/api/publisher/test-favorites`. Default HTML submission navigates instead of sending the test JSON. | Bind submit, parse message, collect favorite IDs, POST with CSRF, and display per-destination results. |
| Medium | Publisher edit: clear credential | Empty input becomes `undefined` and is omitted. Backend preserves the old credential. API explicit NULL does clear it, but the Publisher UI cannot express that. Passwords are also trimmed, altering meaningful leading/trailing spaces. | Add explicit “clear stored credential” control; send NULL for clear and preserve new secret bytes exactly. Builder already has a clear control but also trims passwords. |
| Medium | Logs: retention days | Value is stored and read correctly, but the file handler uses `backupCount=5`; no age-based cleanup consumes `retention_days`. | Implement age retention, or relabel/remove the inactive setting. Persistence alone is not functional enforcement. |
| Medium | API Docs “form” actions | Buttons immediately POST hardcoded production settings rather than displaying an editable form. Node example includes unsupported `max_concurrent_inferences`. | Use editable request previews or link to actual forms, and remove unsupported fields. |
| Medium | Missing destination schema | Collector returns an object without `isValid`/`missingFields`; submit error handling calls `missingFields.join()` and throws. | Return the same validation object shape on every branch and show an unavailable-schema error. |

Additional source findings:

- **Pipeline credentials are plaintext in `pipelines.config`.** GET redaction works, but that is not encryption at rest. The isolated SQL test confirmed the synthetic camera password is stored as entered. Favorite credentials are encrypted. Apply equivalent encryption/decryption boundaries and a migration to pipeline config if credentials must be protected in database backups.
- Export config omits `inference_enabled`, so the export/import contract does not preserve that setting even after the model metadata errors are fixed.
- Import staging constructs `os.path.join(temp_dir, file.filename)` directly. A crafted multipart filename can escape the intended staging directory. This is a source-level finding, not an exploit test. Use a generated local filename and validate ZIP members separately.
- Telemetry has inconsistent interval validation (HTML 5–300, store >=1), and runtime state changes before persistence validation. Broker connection errors can be logged internally without propagating to the route. Validate first and distinguish saved configuration from a confirmed connection.

Key implementation locations: [model reference synchronization](../InferenceNode/pipeline_repository.py), [routes, telemetry, export/import](../InferenceNode/inference_node.py), [publisher collectors/test form](../InferenceNode/templates/publisher.html), [builder collectors](../InferenceNode/templates/pipeline_builder.html), [webhook configure](../ResultPublisher/plugins/webhook_destination.py), [serial schema/configure](../ResultPublisher/plugins/serial_destination.py), [logging behavior](../InferenceNode/log_manager.py), [API Docs actions](../InferenceNode/templates/api_docs.html).

## Page-by-page field and storage map

### Pipeline Builder

Submit uses JSON POST `/api/pipeline/create`, or PUT `/api/pipeline/<id>` when editing. Edit is a PUT even though it is a form submission.

| Filled control/state | Request field | Database destination / result |
|---|---|---|
| Pipeline name | `name` | `pipelines.name`; create/edit verified |
| Description | `description` | `pipelines.description`; explicit empty string clears, verified |
| Source card | `frame_source.capture_type` | `pipelines.config.frame_source.capture_type` |
| Dynamic source inputs | `frame_source.config.<field>` | Same nested keys in `pipelines.config`; tested numeric zero and credential preservation |
| Selected model | `model.id` | `pipelines.model_id` plus `config.model.id`; valid registered model passes, Pass create/clear fail |
| Engine and device cards | `model.engine_type`, `model.device` | `pipelines.config.model`; persistence does not prove installed engine/device availability |
| Enable inference | `inference_enabled` boolean | `pipelines.config.inference_enabled`; false preserved |
| Added destinations | `destinations[]` with `type`, `config`, `enabled` | `pipelines.config.destinations`; disabled false preserved; webhook/serial configuration defects above |
| Favorite selector | Copies favorite config into destination draft | The saved pipeline carries destination config; choosing a favorite is not itself a save |
| Source/model/device/destination search fields | None | Local filters, intentionally not persisted |

Dynamic numbers use `parseFloat`, checkboxes use `.checked`, selects return strings, blank optional text is omitted. Required source fields use explicit empty/undefined checks so camera index 0 is accepted. Password sentinel `***` preserves the previous secret; Builder's explicit clear checkbox sends NULL. Local media references are normalized to a relative source path. Test Source/discovery actions do not constitute a pipeline save.

A user must **add a destination to the destination list**, then save the pipeline; merely filling the temporary destination editor does not establish a saved destination. Runtime camera/engine compatibility needs separate start tests.

### Publisher

POST `/api/publisher/favorites`; edit PUT `/api/publisher/favorites/<id>`; list GET `/api/publisher/favorites`.

| Control | Payload → DB | Evidence |
|---|---|---|
| Favorite name | `name` → `publishers.name` | SQL round trip passes |
| Description | `description` → `publishers.description` | SQL save and clearing pass after migration 0008 |
| Destination type | `type` → `publishers.type` | Stored alongside config |
| Dynamic destination fields | `config` → `publishers.config` | Numeric port and false checkbox verified; credentials encrypted; webhook/serial contract errors remain |
| Test message + selected favorites | Intended `message`, `favorite_ids` to `/api/publisher/test-favorites` | Frontend submit wiring missing; no message persistence is expected |

The favorite POST validates enough to store a record but does not establish that its config can successfully configure or connect the destination. Treat “saved” and “test delivered” as separate outcomes.

### Models

`uploadForm` creates multipart FormData for POST `/api/models/upload`:

| Control | Multipart key | Storage |
|---|---|---|
| Model file | `file` | Bytes under artifact root; `model_artifacts.relative_path`, size and SHA-256 |
| Inference engine | `engine_type` | `models.engine_type`, registry representation metadata |
| Display name | `name` | `models.name` |
| Description | `description` | `models.description` |

Verified registered name/description, GET metadata, artifact bytes and hash. Uploader identity comes from the authenticated session. Unique staging directories from the earlier fix prevent same-filename uploads from sharing their temporary path; this audit did not run a new concurrent load test.

The Ultralytics selector/button takes a separate JSON path: POST `/api/models/download-ultralytics` with `model_name`, `name`, `description`. It downloads then registers the artifact. Reviewed the mapping; no external download was executed, so this path is not marked runtime-passed.

### Media and Builder video upload

POST `/api/media/upload-video` with multipart `file` creates `media_assets` metadata plus artifact bytes. Verified original filename, SHA-256 and returned `relative_source` matching the database path. Builder places this reference in `frame_source.config.source`; uploading alone does not attach the video to a pipeline. Media page has no independent POST form; it lists assets and offers deletion. Actual video decoding was not tested.

### Node Info

POST `/api/node/config`: `nodeName` → `node_name` → `node_settings['node_identity'].node_name`; `logLevel` → `log_level` → `node_settings['preferences'].logging.log_level`. SQL/GET round trips passed. `webPort` is readonly and submitted as a number; it represents the deployed listener port, not a writable DB setting. GET `/api/node/info` hydrates the page.

### Logs

POST `/api/logs/settings`, GET same path:

- `globalLogLevel` → `log_level`.
- `maxLogSize` → integer `max_log_size_mb`.
- `logRetention` → integer `retention_days`.
- `enableFileLogging` → boolean `enable_file_logging`; storage/read response uses `file_logging_enabled`.

All map to `node_settings['preferences'].logging` and round-trip, including false. File size/enable/level update logging behavior; retention-days cleanup is absent as noted above. Search/filter/download controls do not save this configuration.

### Telemetry

POST `/api/telemetry/configure`; GET `/api/telemetry/config`:

| Control | Request / intended `node_settings['telemetry']` field | Result |
|---|---|---|
| Enable checkbox | `enabled` boolean | Disabled false saved in isolated test |
| Publish interval | `publish_interval` number | 17 saved in isolated test |
| MQTT server | `mqtt_server` string | Empty-server clearing branch defective |
| MQTT port | `mqtt_port` integer | Submitted 1885 not saved when server blank |
| Topic | `mqtt_topic` string | Submitted topic not saved when server blank |

Live metrics are runtime samples, not database form fields. MQTT credential support exists in the API/store but these controls are not present in this page's form. External MQTT connectivity was not tested.

### Admin users and access controls

| Form | Request | Field mapping and evidence |
|---|---|---|
| Add user | POST `/api/users` | Username, full name, email, selected role, must-change checkbox → same `users` columns. Password → bcrypt `password_hash`, never returned. Confirmation is frontend validation, not a stored field. Real SQL values/hash verification passed. |
| Edit user | PATCH `/api/users/<id>` | Full name/email → `users`; blank values clear to NULL, verified. User ID selects record; username shown readonly. |
| Reset password | POST `/api/users/<id>/reset-password` | `password` → hash; `must_change` → `must_change_password`; permissions version increments. Verified. Confirmation is not stored. |
| Pipeline grants | PUT `/api/pipelines/<pipeline_id>/access/<user_id>` | `can_view`, `can_start`, `can_stop`, `can_edit` → `pipeline_user_access`; true/false values verified against the selected user/pipeline. |

These are administrator operations. Tests used an authorized admin; they are not exhaustive permission-matrix tests.

### Login, change password, logout

These use ordinary HTML POST, not JSON. Login submits username/password, CSRF and redirect context; verifies a hash, establishes session and updates last-login. Change-password submits current/new/confirmation plus CSRF; verifies old password, stores the new hash and clears forced-change status. Logout POST carries CSRF and clears authentication. Actual route sequences and subsequent unauthenticated API rejection passed. Confirmation, CSRF tokens and raw passwords are not persisted as profile fields. Missing CSRF on a favorite write returned 400 and created no row.

### Create Engine wizard

Although there is no literal `<form>`, wizard state becomes `{preset, fields}`. Fields include `display_name`, task, confidence, extensions, draw color and class map; model/hardware choices depend on the selected preset. POST preview produces code, POST validate checks it, POST `/api/inference/engines` installs it. Verified custom values appear in generated code and installed file hash matches `inference_engines.sha256`, with registry AVAILABLE/PASSED. These parameters are embedded in the generated artifact rather than separate SQL columns. A blank-preset scaffold passing validation does not prove a working inference implementation.

### Pipeline Management

Import is multipart POST `/api/pipeline/import`, carrying a ZIP file rather than separate visible pipeline fields. Config-only import with a NULL Pass model ID preserved description and inference-disabled state in SQL. Model-bearing import fails as described above. Export is GET and also fails for registered models. Start/stop and inference/destination toggle buttons are operational/config writes, not additional HTML forms; their rendering/endpoint contracts were reviewed, but live pipeline execution was not exercised in this audit. Duplicate/thumbnail behavior likewise has no claim of new hardware/runtime test coverage here.

### Remaining templates

- Dashboard: display and operational links; no additional data-entry form beyond shared logout.
- Node Discovery: refresh/control actions target discovery/runtime services; no independent database profile form. Remote node operation was not exercised.
- API Docs: example POST buttons really execute requests; hardcoded settings issue above.
- Base/auth layouts and error pages: presentation/shared authentication controls; no other database form.

## Read-back and correctness rules

For each configuration form, correctness should mean: parse/validate typed input → authorize → commit to PostgreSQL → read canonical saved values → update UI. Model/media/engine files additionally require verified registry/artifact consistency. A 200 response echoing the request, a generated preview, or a GET of in-memory state alone cannot prove persistence.

For nullable credentials use three states: omitted or `***` means keep, explicit NULL means clear, a new nonempty value means replace. Do not trim secret values. Validate destination-specific configuration before claiming it is usable, and report a failed connection separately from a failed database save.

Recommended implementation order: fix Pass model normalization; fix registry-based import/export and cleanup; fix webhook/serial contracts and bind test publish; fix telemetry desired-state persistence; then address secret clearing/encryption, retention behavior and Docs actions. Keep the strict expected-failure tests until each fix makes its test pass, then remove that test's xfail marker.

## Dynamic field inventory

The following schemas were read from the deployed image. Names are request/config keys; types determine frontend conversion. `*` means schema-required. Every source key goes under `pipelines.config.frame_source.config`; destination keys go under `publishers.config` for favorites or `pipelines.config.destinations[].config` for pipeline destinations. Schema inspection is not a hardware/connectivity test.

### Frame sources

| Type | Fields (`name:type`) |
|---|---|
| webcam | `source:number`, `width:number`, `height:number`, `fps:number`, `exposure:number`, `gain:number` |
| ip_camera | `source:text`*, `username:text`, `password:password`, `width:number`, `height:number`, `fps:number` |
| basler | `source:text`, `exposure:number`, `gain:number`, `width:number`, `height:number`, `fps:number`, `is_mono:checkbox` |
| genicam | `source:text`, `cti_files:text`, `exposure:number`, `gain:number`, `width:number`, `height:number`, `fps:number`, `x:number`, `y:number` |
| realsense | `source:number`, `width:number`, `height:number`, `fps:number`, `depth_range_min:number`, `depth_range_max:number`, `output_type:select` |
| ximea | No fields exposed in this deployment; usability not verified |
| huateng | No fields exposed in this deployment; usability not verified |
| video_file | `source:text`*, `loop:checkbox`, `real_time:checkbox`, `width:number`, `height:number` |
| image_folder | `source:text`*, `sort_by:select`, `fps:number`, `loop:checkbox`, `real_time:checkbox`, `watch_folder:checkbox`, `width:number`, `height:number` |
| screen | `x:number`, `y:number`, `w:number`, `h:number`, `fps:number` |
| audio_spectrogram | `source:text`, `n_mels:number`, `n_fft:select`, `hop_length:number`, `window_duration:number`, `sample_rate:select`, `freq_range:text`, `frame_rate:number`, `colormap:select`, `contrast_method:select`, `gamma_correction:number`, `noise_floor:number` |

### Destinations

| Type | Fields (`name:type`) |
|---|---|
| mqtt | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `server:text`*, `port:number`*, `topic:text`*, `username:text`, `password:password` |
| webhook | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `url:url`, `timeout:number`, `headers:textarea` |
| serial | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `com_port:text`*, `baud_rate:select` |
| folder | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `folder_path:text`*, `file_prefix:text`, `file_extension:select` |
| zeromq | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `address:text`*, `socket_type:select` |
| opcua | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `server_url:url`*, `node_id:text`*, `username:text`, `password:password`, `security_policy:select`, `security_mode:select` |
| ros2 | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `topic:text`*, `message_type:select`, `node_name:text`, `qos_profile:select` |
| roboflow | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `api_key:password`*, `workspace_id:text`*, `project_id:text`*, `dataset_name:text`, `split:select`, `upload_batch_name:text` |
| geti | `rate_limit:number`, `max_frames:number`, `include_image_data:checkbox`, `include_result_image:checkbox`, `host:text`*, `token:password`*, `project_name:text`, `project_id:text`, `dataset_name:text`, `verify_certificate:checkbox` |
| null | `info:info` |

The frame-source endpoint additionally injects `upload_file:file` into video-file configuration. File bytes are uploaded separately; the saved reference is `source`. Destination `info` fields are display text, not editable persisted settings. Select controls currently emit strings, including numeric-looking choices; plugin-specific type validation must account for this.
