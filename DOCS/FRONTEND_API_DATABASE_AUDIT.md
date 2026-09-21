# Frontend → API → database audit

Reviewed 2026-09-21. This is a source and automated-test audit, not a certification that every browser interaction is error-free. The running deployment and its database were not changed. `.env` was not read; it was removed from temporary test copies.

## Follow-up implementation

The selected persistence and concurrent-upload findings were subsequently addressed in the workspace. See [Persistence fixes](PERSISTENCE_FIXES.md) for the current behavior, migration, tests and remaining limits. Findings below describe the original audit state.

## Result

Confirmed frontend defects were fixed in Media Library, Logs, Telemetry, and API Docs. There are still backend persistence and validation defects below. Passing existing tests does not cover those missing contracts.

### Fixed in this change

- **Media listing:** `fetchJSON()` returns a Fetch `Response`, not a JSON object. The page now parses it and rejects failed HTTP responses instead of showing an empty library.
- **Media deletion:** `apiCall()` returns `{ok, status, data, error}`, not a Fetch `Response`. The page now reads `data`; successful deletions and 409 reference conflicts no longer produce `r.json is not a function`.
- **Logs:** failed API requests no longer display invented sample events. The page displays the actual failure and retries on the normal refresh interval.
- **Log settings:** the form now loads `GET /api/logs/settings`; previously opening the page showed hard-coded defaults that could overwrite actual runtime settings.
- **Logs and API Docs:** log fields/details and API explorer responses are escaped before insertion into HTML.
- **Telemetry:** saving disabled publishing no longer stops live chart polling; saving enabled publishing also ensures the polling timer is running.
- **API Docs:** fixed singular pipeline mutation URLs, pipeline creation URL/payload, pipeline list array example, telemetry MQTT fields, and the nonexistent discovery announce action. Example IDs still need replacing with real IDs when testing resource-specific operations.

### Open findings (not fixed by this frontend patch)

| Priority | Finding and reproducible trigger | Source | Required correction |
|---|---|---|---|
| High | A node/telemetry/node-publisher save can return HTTP 200 even when its database write fails: `_save_settings()` catches all exceptions and only logs them. Runtime may have changed before failure, and separate store calls commit independently. | `InferenceNode/inference_node.py`: `_save_settings`, `update_node_config`, `configure_telemetry`, `configure_publisher` | Return/raise persistence failure, coordinate the related transaction, and avoid reporting durable success before commit. Test unavailable database and partial-write failures. |
| High | Model uploads with the same filename share `/tmp/<secure_filename>`. Concurrent requests can overwrite or remove one another's staging file. | `InferenceNode/inference_node.py`: `upload_model` | Use a unique per-request temporary directory/file while retaining the original extension. Add concurrent same-name upload coverage. |
| High | Discovery renders remote node names/platform/IDs into `innerHTML` and inline handlers without consistent escaping. Untrusted discovery metadata can become markup or script. | `InferenceNode/templates/node_discovery.html`: `createNodeCard`, `createNodeRow`, `showNodeDetails` | Render text with `textContent`, use event listeners for IDs and validate link destinations. Verify with hostile node metadata in a browser. |
| Medium | Node Info's editable port is merely logged, never applied or stored. Log level changes only runtime logging; GET hard-codes `INFO`. A successful save therefore does not round-trip these form values or survive restart. | `InferenceNode/inference_node.py`: `update_node_config`, `get_detailed_node_info`, `_save_settings`; `templates/node_info.html` | Explicitly make deployment-owned port read-only or implement supported restart configuration. Persist/restore log preferences and return actual logging state. |
| Medium | Log settings are runtime-only. Changing `max_log_size_mb` does not update an already-created handler's `maxBytes`; retention days are assigned but the handler uses a fixed `backupCount=5`. | `InferenceNode/log_manager.py`: `update_settings`, `_setup_file_logging`; `inference_node.py`: `update_log_settings` | Persist preferences, apply rotation changes to the handler, implement retention semantics or remove unsupported controls. |
| Medium | Publisher favorite description is submitted by the form but discarded. No description column exists, create/update routes do not forward it, and `update_publisher(description=...)` ignores the argument. | `templates/publisher.html`: destination form; `inference_node.py`: favorite routes; `publisher_store.py`; `data_models.py`: `Publisher` | Add a migrated description column and create/update/read coverage, or remove the unsupported field. |
| Medium | Clearing telemetry's MQTT server leaves the previous configuration/client intact because configuration only runs for a nonempty server. Clearing credentials is also lost on persistence: `_save_settings` omits falsy credential values, so the store merges the previous secret back. MQTT connection exceptions are caught inside `configure_mqtt`, allowing the route to report success. | `inference_node.py`: `configure_telemetry`, `_save_settings`; `telemetry.py`: `configure_mqtt` | Distinguish omitted versus explicitly cleared values; disconnect/replace the old client; report connection failures; test clear → GET → restart round trips. |
| Medium | Several JSON routes assume an object and catch parsing/type failures as generic 500s. For example, `POST /api/pipeline/create` with JSON `null` or `[]` does not get a deliberate validation response. | `inference_node.py`: create/update pipeline, telemetry, node/discovery configuration handlers | Validate body type, required nested objects and numeric ranges before runtime mutation, returning 400/422 consistently. |
| Medium | Pipeline listing catches database/scoping exceptions and returns an empty successful list. A database outage can look like all pipelines disappeared. | `inference_node.py`: `list_pipelines` | Preserve access denial semantics while returning an explicit service error for persistence failures. |

These findings come from source tracing; failure injection/concurrency/browser exploitation of the open findings was not performed against the deployment.

## How GET, POST and forms work

1. Flask renders a Jinja page. Shared `app.js` and `armyeye-ui.js` load before page scripts. Relative `/api/...` URLs target the same server as the page.
2. Login, password change and logout use ordinary HTML POST forms with CSRF tokens. Other forms prevent normal submission and use `fetch`, `apiCall`, or XMLHttpRequest.
3. GET requests load existing values/lists. JSON POST/PUT/PATCH requests send `Content-Type: application/json` and `JSON.stringify(...)`. File uploads use `FormData`; the browser sets the multipart boundary. The models page uses XMLHttpRequest for upload progress.
4. `app.js` attaches `X-CSRFToken` to same-origin writes, including raw fetch and XMLHttpRequest. The auth layer checks the session, roles and pipeline permissions; write routes also enforce CSRF where configured. `fetchJSON` redirects 401 responses; raw fetch callers do not uniformly share that behavior.
5. Flask handlers read `request.get_json()`, `request.form` or `request.files`, validate, then call store/registry/service code. `auth/db.py:get_session()` commits on successful context exit and rolls back on exceptions.
6. GET returns serialized data; secret fields are redacted where implemented. JavaScript renders the result or reloads the list after a successful write. Saving a runtime configuration does **not** necessarily mean it is durable—see the findings above.

## Page-by-page data flow

| Page | Read path | Submit/action path | Persistence / audit outcome |
|---|---|---|---|
| Login `/login` | GET form | POST username/password/CSRF | `auth.service.authenticate` reads `users.password_hash`; login metadata/audit and session state. Source/Flask tests reviewed; no live credentials used. |
| Change password `/change-password` | GET form | POST current/new/confirmed password | Auth service updates hash and permissions version in `users`, records audit; session version refreshed. |
| Shared navigation / logout | `/api/info`; rendered role/navigation | POST `/logout` | Session cleared; no application configuration row. Shared CSRF injection reviewed. |
| Dashboard `/` | `/api/info`, `/api/pipelines`, `/api/models`, `/api/discovery/nodes` | Navigation/refresh | Aggregates database-backed pipeline/model lists and runtime discovery. Database failure can masquerade as empty pipelines. |
| Models `/models` | models, engine metadata | Multipart model upload, JSON Ultralytics download, DELETE model | `model_repo`/`model_registry` → `models`, `model_representations`, `model_artifacts`; bytes under `ARTIFACT_ROOT/models`. Deletion guarded against references. Staging collision remains. |
| Media `/media` | GET `/api/media` with references/status | DELETE `/api/media/<id>` | `media_registry` → `media_assets`; bytes under `ARTIFACT_ROOT/media`; reference conflict returns 409. Fixed listing/deletion contract bugs. |
| Pipeline Builder `/pipeline-builder` | engines, models, hardware, source schemas/discovery, media choices, favorites, pipeline list/edit data | POST create/import/duplicate; PUT edit; POST start/stop/source preview; DELETE; multipart media upload | `pipeline_manager` → `pipeline_store`/`pipeline_repository` → `pipelines.config` and relational columns. Secret-preserving edits, relative media paths, model reference consistency reviewed/tested. Preview also creates a temporary pipeline that needs cleanup. |
| Pipeline Management `/pipeline-management` | pipelines/metrics/status/publisher state, stream, thumbnails, engines/node identity | Start/stop, inference/publisher enable/disable, delete/duplicate/import/export, thumbnail generation | Stored definitions/status in `pipelines`; live processing/streams in memory; `pipeline_thumbnails` plus artifact bytes. Runtime health cannot be inferred from saved status alone. |
| Publisher `/publisher` | destination schemas and favorites | POST favorite; PUT/DELETE favorite; test-favorites | `publisher_store` → `publishers` with `kind=favorite`; credentials encrypted, GET redacted. Test sends through configured destinations, not a configuration save. Description is not persisted. |
| Telemetry `/telemetry` | telemetry samples/config and pipeline statistics | POST configure | `node_settings[key=telemetry]` for configuration, live samples in memory. Chart polling fixed; MQTT clear/error and save propagation findings remain. |
| Node Info `/node-info` | GET detailed node/hardware/config | POST config/restart; client-side JSON export | Identity in `node_settings[key=node_identity]`; live hardware from psutil. Port/log-level form does not durably round-trip. Restart was not invoked. |
| Logs `/logs` | GET logs and settings | POST settings/clear; browser download | Memory buffer plus rotating files, not database log records. Fixed sample fallback/settings hydration; durability/rotation gaps remain. |
| Node Discovery `/node-discovery` | GET discovered nodes | POST refresh/control (`ping`) | `discovery_manager` runtime cache/network probing; no node registry table. HTML rendering finding remains. |
| API Docs `/api-docs` | Explorer calls selected GET routes | Explorer can issue real mutation requests | No separate persistence; selected handler owns effects. Fixed nonexistent routes, schema examples, escaped result output. |
| Create Engine `/create-engine` | models/labels, engines, hardware | POST preview/validate/install | Preview/validation do not install. Install → `inference_engines` plus engine artifact and `audit_log`. Feature/admin gate; validation can return HTTP 200 with `valid:false`. |
| Admin Users `/admin/users` | GET users, assignable pipelines, user access | POST user/reset password; PATCH profile; PUT role/active/access; DELETE user/access | `auth.service` → `users`, `audit_log`; `pipeline_user_access` for grants. Password hashes, last-admin protection, session invalidation and permissions tested. |
| Forbidden page / base layouts | Server-rendered | No independent API form | Shared rendering/navigation only. |

## Database versus files

- **PostgreSQL:** users, audit_log, pipelines, pipeline_user_access, model registry/representations/artifacts, inference_engines, publishers, node_settings, media_assets, pipeline_thumbnails, app_state (migration markers).
- **Artifact filesystem:** model/video/engine/thumbnail bytes. Database rows hold IDs, relative paths, hashes and lifecycle/validation state; bytes are not SQL blobs.
- **Runtime:** active pipelines, stream frames, telemetry samples, discovery cache and in-memory log buffer.
- **Legacy JSON:** migration input, not the authoritative store for migrated pipeline/node/publisher configuration.

## Verification

- Baseline isolated Python suite: **895 passed, 63 skipped**. This ran on the pre-edit temporary copy; no network, no live `.env`, and no production database access.
- Focused post-edit backend/page/schema/persistence suite: **75 passed, 14 skipped**, using a separate temporary PostgreSQL server with no external network. Includes actual PostgreSQL migration/reference constraints as well as SQLite-backed unit tests. The 14 skips are Node-dependent frontend Python wrappers; JavaScript suites ran separately.
- JavaScript suites: **59 passed, 0 failed, 0 skipped**. New behavioral checks exercise actual template functions and the actual shared API parser. Template syntax checks cover all 18 HTML templates (Jinja placeholders removed for parsing).
- Full browser E2E, GPU inference, real camera streams, MQTT/webhook delivery, and live-deployment round trips were **not verified** by this audit. Playwright/Chromium are absent from the available application test image. A passing syntax/unit test is not browser coverage.
- Literal frontend API references were compared against actual Python route decorators. After the API Docs corrections, no unmatched fixed-path references remain. Variable URLs and runtime-dependent availability still require behavioral testing.

## Frontend endpoint inventory

The lists below include endpoint strings used in scripts/templates (including documentation examples). Repeated calls are collapsed per file. Dynamic IDs and actions remain shown as JavaScript expressions. The backend registry afterward gives each handler's declared methods and source location; authorization and runtime behavior are additional checks, not implied by route existence.

### admin-users.js

- `/api/pipelines/${encodeURIComponent(pid)}/access/${u.id}`
- `/api/pipelines/assignable`
- `/api/users`
- `/api/users/${id}`
- `/api/users/${id}/reset-password`
- `/api/users/${u.id}`
- `/api/users/${u.id}/active`
- `/api/users/${u.id}/pipeline-access`
- `/api/users/${u.id}/role`

### app.js

- `/api/info`

### api_docs.html

- `/api/discovery/nodes`
- `/api/discovery/nodes/refresh`
- `/api/models`
- `/api/models/model_abc123`
- `/api/node/config`
- `/api/node/info`
- `/api/pipeline/pipeline_001`
- `/api/pipeline/pipeline_001/start`
- `/api/pipeline/pipeline_001/stop`
- `/api/pipelines`
- `/api/publisher/configure`
- `/api/publisher/favorites`
- `/api/telemetry`
- `/api/telemetry/configure`

### create_engine.html

- `/api/hardware`
- `/api/inference/engines`
- `/api/inference/engines/preview`
- `/api/inference/engines/validate`
- `/api/models`
- `/api/models/${encodeURIComponent(m.id)}/labels`

### dashboard.html

- `/api/discovery/nodes`
- `/api/models`
- `/api/pipelines`

### logs.html

- `/api/logs`
- `/api/logs/clear`
- `/api/logs/settings`

### media.html

- `/api/media`
- `/api/media/${encodeURIComponent(pendingDelete)}`

### models.html

- `/api/inference/engines`
- `/api/models`
- `/api/models/${modelId}`
- `/api/models/download-ultralytics`
- `/api/models/upload`

### node_discovery.html

- `/api/discovery/nodes`
- `/api/discovery/nodes/${nodeId}/control`
- `/api/discovery/nodes/refresh`

### node_info.html

- `/api/node/config`
- `/api/node/info`
- `/api/node/restart`

### pipeline_builder.html

- `/api/frame-sources`
- `/api/frame-sources/${sourceType}/discover`
- `/api/hardware`
- `/api/inference/engines`
- `/api/media/sources`
- `/api/media/upload`
- `/api/models`
- `/api/pipeline/${activeTestSourceId}/start`
- `/api/pipeline/${editingPipelineId}`
- `/api/pipeline/${encodeURIComponent(pipelineId)}/duplicate`
- `/api/pipeline/${pipelineId}`
- `/api/pipeline/${pipelineId}/export`
- `/api/pipeline/${pipelineId}/start`
- `/api/pipeline/${pipelineId}/stop`
- `/api/pipeline/${pipelineId}/stream?t=${Date.now()}`
- `/api/pipeline/create`
- `/api/pipeline/import`
- `/api/pipelines`
- `/api/publisher/favorites`
- `/api/publisher/types`

### pipeline_management.html

- `/api/inference/engines`
- `/api/node/identity`
- `/api/pipeline/${encodeURIComponent(pipelineId)}/duplicate`
- `/api/pipeline/${pipeline.id}/inference/disable`
- `/api/pipeline/${pipeline.id}/inference/enable`
- `/api/pipeline/${pipeline.id}/start`
- `/api/pipeline/${pipeline.id}/stop`
- `/api/pipeline/${pipelineId}`
- `/api/pipeline/${pipelineId}/export`
- `/api/pipeline/${pipelineId}/inference/disable`
- `/api/pipeline/${pipelineId}/inference/enable`
- `/api/pipeline/${pipelineId}/publisher/${publisherId}/${action}`
- `/api/pipeline/${pipelineId}/publishers/status`
- `/api/pipeline/${pipelineId}/start`
- `/api/pipeline/${pipelineId}/status`
- `/api/pipeline/${pipelineId}/stop`
- `/api/pipeline/${pipelineId}/stream`
- `/api/pipeline/${pipelineId}/stream/hq?t=${Date.now()}`
- `/api/pipeline/${pipelineId}/stream?t=${Date.now()}`
- `/api/pipeline/${pipelineId}/thumbnail/exists`
- `/api/pipeline/${pipelineId}/thumbnail/generate`
- `/api/pipeline/${pipelineId}/thumbnail?t=${Date.now()}`
- `/api/pipeline/import`
- `/api/pipelines`
- `/api/pipelines/metrics`

### publisher.html

- `/api/publisher/destination-types`
- `/api/publisher/favorites`
- `/api/publisher/favorites/${editingId}`
- `/api/publisher/favorites/${favoriteId}`

### telemetry.html

- `/api/pipelines`
- `/api/telemetry`
- `/api/telemetry/config`
- `/api/telemetry/configure`

## Registered backend routes

| Methods | Route | Handler | Source |
|---|---|---|---|
| GET | `/` | `dashboard` | `InferenceNode/inference_node.py:620` |
| GET | `/admin/users` | `admin_users_page` | `InferenceNode/auth/admin_routes.py:47` |
| GET | `/api-docs` | `api_docs` | `InferenceNode/inference_node.py:657` |
| GET | `/api/discovery/nodes` | `get_discovered_nodes` | `InferenceNode/inference_node.py:3793` |
| GET | `/api/discovery/nodes/<node_id>` | `get_discovered_node` | `InferenceNode/inference_node.py:3833` |
| POST | `/api/discovery/nodes/<node_id>/control` | `control_discovered_node` | `InferenceNode/inference_node.py:3900` |
| POST | `/api/discovery/nodes/refresh` | `refresh_discovered_nodes` | `InferenceNode/inference_node.py:3810` |
| GET | `/api/frame-sources` | `get_frame_sources` | `InferenceNode/inference_node.py:2212` |
| GET | `/api/frame-sources/<source_type>/discover` | `discover_frame_sources` | `InferenceNode/inference_node.py:2391` |
| GET | `/api/hardware` | `get_hardware_info` | `InferenceNode/inference_node.py:972` |
| POST | `/api/hardware/format-device` | `format_device_for_engine` | `InferenceNode/inference_node.py:1023` |
| GET | `/api/inference/engines` | `get_inference_engines` | `InferenceNode/inference_node.py:2461` |
| POST | `/api/inference/engines` | `api_engine_create` | `InferenceNode/engine_builder.py:587` |
| DELETE | `/api/inference/engines/<engine_key>` | `api_engine_delete` | `InferenceNode/engine_builder.py:626` |
| POST | `/api/inference/engines/preview` | `api_engine_preview` | `InferenceNode/engine_builder.py:567` |
| GET | `/api/inference/engines/registry` | `api_engine_registry` | `InferenceNode/engine_builder.py:642` |
| POST | `/api/inference/engines/validate` | `api_engine_validate` | `InferenceNode/engine_builder.py:580` |
| GET | `/api/info` | `get_node_info` | `InferenceNode/inference_node.py:687` |
| GET | `/api/logs` | `get_logs` | `InferenceNode/inference_node.py:693` |
| POST | `/api/logs/clear` | `clear_logs` | `InferenceNode/inference_node.py:771` |
| GET | `/api/logs/settings` | `get_log_settings` | `InferenceNode/inference_node.py:730` |
| POST | `/api/logs/settings` | `update_log_settings` | `InferenceNode/inference_node.py:748` |
| GET | `/api/media` | `list_media_assets` | `InferenceNode/inference_node.py:1147` |
| DELETE | `/api/media/<media_id>` | `delete_media` | `InferenceNode/inference_node.py:1178` |
| GET | `/api/media/sources` | `list_media_sources` | `InferenceNode/inference_node.py:2190` |
| POST | `/api/media/upload-video` | `upload_video` | `InferenceNode/inference_node.py:1105` |
| GET | `/api/models` | `list_models` | `InferenceNode/inference_node.py:1325` |
| DELETE | `/api/models/<model_id>` | `delete_model` | `InferenceNode/inference_node.py:1403` |
| GET | `/api/models/<model_id>` | `get_model_info` | `InferenceNode/inference_node.py:1368` |
| GET | `/api/models/<model_id>/labels` | `get_model_labels` | `InferenceNode/inference_node.py:1383` |
| POST | `/api/models/download-ultralytics` | `download_ultralytics_model` | `InferenceNode/inference_node.py:1210` |
| POST | `/api/models/upload` | `upload_model` | `InferenceNode/inference_node.py:1047` |
| GET | `/api/models/verify` | `verify_models` | `InferenceNode/inference_node.py:1357` |
| POST | `/api/node/config` | `update_node_config` | `InferenceNode/inference_node.py:868` |
| GET | `/api/node/identity` | `node_identity` | `InferenceNode/inference_node.py:2738` |
| GET | `/api/node/info` | `get_detailed_node_info` | `InferenceNode/inference_node.py:789` |
| POST | `/api/node/restart` | `restart_node` | `InferenceNode/inference_node.py:917` |
| DELETE | `/api/pipeline/<pipeline_id>` | `delete_pipeline` | `InferenceNode/inference_node.py:2792` |
| GET | `/api/pipeline/<pipeline_id>` | `get_pipeline` | `InferenceNode/inference_node.py:2749` |
| PUT | `/api/pipeline/<pipeline_id>` | `update_pipeline` | `InferenceNode/inference_node.py:2826` |
| POST | `/api/pipeline/<pipeline_id>/duplicate` | `duplicate_pipeline` | `InferenceNode/inference_node.py:2537` |
| GET | `/api/pipeline/<pipeline_id>/export` | `export_pipeline` | `InferenceNode/inference_node.py:3450` |
| GET | `/api/pipeline/<pipeline_id>/fullstatus` | `get_pipeline_full_status` | `InferenceNode/inference_node.py:2768` |
| POST | `/api/pipeline/<pipeline_id>/inference/disable` | `disable_pipeline_inference` | `InferenceNode/inference_node.py:2985` |
| POST | `/api/pipeline/<pipeline_id>/inference/enable` | `enable_pipeline_inference` | `InferenceNode/inference_node.py:2962` |
| PUT | `/api/pipeline/<pipeline_id>/node` | `assign_pipeline_node` | `InferenceNode/inference_node.py:2708` |
| POST | `/api/pipeline/<pipeline_id>/publisher/<publisher_id>/disable` | `disable_pipeline_publisher` | `InferenceNode/inference_node.py:3032` |
| POST | `/api/pipeline/<pipeline_id>/publisher/<publisher_id>/enable` | `enable_pipeline_publisher` | `InferenceNode/inference_node.py:3008` |
| GET | `/api/pipeline/<pipeline_id>/publishers/status` | `get_pipeline_publishers_status` | `InferenceNode/inference_node.py:3056` |
| POST | `/api/pipeline/<pipeline_id>/start` | `start_pipeline` | `InferenceNode/inference_node.py:2880` |
| GET | `/api/pipeline/<pipeline_id>/status` | `get_pipeline_status` | `InferenceNode/inference_node.py:3077` |
| POST | `/api/pipeline/<pipeline_id>/stop` | `stop_pipeline` | `InferenceNode/inference_node.py:2936` |
| GET | `/api/pipeline/<pipeline_id>/stream` | `stream_pipeline` | `InferenceNode/inference_node.py:3112` |
| GET | `/api/pipeline/<pipeline_id>/stream/hq` | `stream_pipeline_hq` | `InferenceNode/inference_node.py:3231` |
| GET | `/api/pipeline/<pipeline_id>/thumbnail` | `get_pipeline_thumbnail` | `InferenceNode/inference_node.py:3347` |
| GET | `/api/pipeline/<pipeline_id>/thumbnail/exists` | `check_pipeline_thumbnail` | `InferenceNode/inference_node.py:3369` |
| POST | `/api/pipeline/<pipeline_id>/thumbnail/generate` | `generate_pipeline_thumbnail` | `InferenceNode/inference_node.py:3395` |
| POST | `/api/pipeline/create` | `create_pipeline` | `InferenceNode/inference_node.py:2478` |
| POST | `/api/pipeline/import` | `import_pipeline` | `InferenceNode/inference_node.py:3592` |
| GET | `/api/pipelines` | `list_pipelines` | `InferenceNode/inference_node.py:2650` |
| GET | `/api/pipelines/<pipeline_id>/access` | `api_list_pipeline_access` | `InferenceNode/pipeline_access_routes.py:28` |
| DELETE | `/api/pipelines/<pipeline_id>/access/<int:user_id>` | `api_remove_pipeline_access` | `InferenceNode/pipeline_access_routes.py:67` |
| PUT | `/api/pipelines/<pipeline_id>/access/<int:user_id>` | `api_set_pipeline_access` | `InferenceNode/pipeline_access_routes.py:37` |
| GET | `/api/pipelines/assignable` | `api_assignable_pipelines` | `InferenceNode/pipeline_access_routes.py:87` |
| GET | `/api/pipelines/metrics` | `get_pipeline_metrics` | `InferenceNode/inference_node.py:2593` |
| GET | `/api/pipelines/summary` | `get_pipeline_summary` | `InferenceNode/inference_node.py:2685` |
| POST | `/api/publisher/configure` | `configure_publisher` | `InferenceNode/inference_node.py:1435` |
| DELETE | `/api/publisher/delete/<publisher_id>` | `delete_publisher` | `InferenceNode/inference_node.py:2019` |
| GET | `/api/publisher/destination-types` | `get_destination_types_with_schemas` | `InferenceNode/inference_node.py:2174` |
| PUT | `/api/publisher/edit/<publisher_id>` | `edit_publisher` | `InferenceNode/inference_node.py:1971` |
| GET | `/api/publisher/favorites` | `get_favorite_configs` | `InferenceNode/inference_node.py:2070` |
| POST | `/api/publisher/favorites` | `save_favorite_config` | `InferenceNode/inference_node.py:2081` |
| DELETE | `/api/publisher/favorites/<favorite_id>` | `delete_favorite_config` | `InferenceNode/inference_node.py:2112` |
| PUT | `/api/publisher/favorites/<favorite_id>` | `update_favorite_config` | `InferenceNode/inference_node.py:2128` |
| POST | `/api/publisher/test` | `test_publish` | `InferenceNode/inference_node.py:1683` |
| POST | `/api/publisher/test-favorites` | `test_publish_favorites` | `InferenceNode/inference_node.py:1726` |
| GET | `/api/publisher/types` | `get_publisher_types` | `InferenceNode/inference_node.py:2158` |
| GET | `/api/registry/verify` | `verify_registry` | `InferenceNode/inference_node.py:1342` |
| GET | `/api/telemetry` | `get_telemetry_data` | `InferenceNode/inference_node.py:1611` |
| GET | `/api/telemetry/config` | `get_telemetry_config` | `InferenceNode/inference_node.py:1586` |
| POST | `/api/telemetry/configure` | `configure_telemetry` | `InferenceNode/inference_node.py:1521` |
| GET | `/api/users` | `api_list_users` | `InferenceNode/auth/admin_routes.py:52` |
| POST | `/api/users` | `api_create_user` | `InferenceNode/auth/admin_routes.py:77` |
| DELETE | `/api/users/<int:user_id>` | `api_delete_user` | `InferenceNode/auth/admin_routes.py:149` |
| PATCH | `/api/users/<int:user_id>` | `api_update_profile` | `InferenceNode/auth/admin_routes.py:96` |
| PUT | `/api/users/<int:user_id>/active` | `api_set_active` | `InferenceNode/auth/admin_routes.py:126` |
| GET | `/api/users/<int:user_id>/pipeline-access` | `api_user_pipeline_access` | `InferenceNode/pipeline_access_routes.py:81` |
| POST | `/api/users/<int:user_id>/reset-password` | `api_reset_password` | `InferenceNode/auth/admin_routes.py:137` |
| PUT | `/api/users/<int:user_id>/role` | `api_set_role` | `InferenceNode/auth/admin_routes.py:115` |
| GET, POST | `/change-password` | `change_password` | `InferenceNode/auth/flask_auth.py:173` |
| GET | `/create-engine` | `create_engine_page` | `InferenceNode/engine_builder.py:562` |
| GET | `/health` | `health_check` | `InferenceNode/inference_node.py:678` |
| GET, POST | `/login` | `login` | `InferenceNode/auth/flask_auth.py:145` |
| POST | `/logout` | `logout` | `InferenceNode/auth/flask_auth.py:198` |
| GET | `/logs` | `logs_page` | `InferenceNode/inference_node.py:667` |
| GET | `/media` | `media_page` | `InferenceNode/inference_node.py:632` |
| GET | `/models` | `models_page` | `InferenceNode/inference_node.py:626` |
| GET | `/node-discovery` | `node_discovery_page` | `InferenceNode/inference_node.py:672` |
| GET | `/node-info` | `node_info_page` | `InferenceNode/inference_node.py:662` |
| GET | `/pipeline-builder` | `pipeline_page` | `InferenceNode/inference_node.py:637` |
| GET | `/pipeline-management` | `pipeline_management_page` | `InferenceNode/inference_node.py:642` |
| GET | `/publisher` | `publisher_page` | `InferenceNode/inference_node.py:647` |
| GET | `/telemetry` | `telemetry_page` | `InferenceNode/inference_node.py:652` |
| POST | `/webhook/<webhook_id>` | `webhook_receiver` | `InferenceNode/inference_node.py:3850` |
