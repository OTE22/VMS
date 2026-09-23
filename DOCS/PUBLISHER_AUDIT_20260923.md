# Publisher page deep audit — 2026-09-23

## Scope and result

Audited `/publisher` against deployed image `armyeye-vms:models-20260923t064913z`. The template, publisher store and configuration normalizer hashes match the live container. This is an audit only: no application code was changed or deployed, no production favorites were mutated, and no external test messages were sent.

The page manages PostgreSQL-backed **favorites**, which are reusable configurations. Saving/editing a favorite does not reconfigure existing running pipelines. Pipeline Builder resolves a selected favorite into a pipeline destination configuration when saving the pipeline.

## Form, API and database trace

| Page action | Request / processing | Storage or result |
| --- | --- | --- |
| Load destination choices | GET `/api/publisher/destination-types` | Plugin schemas; no database write |
| Filter/quick-search destination types | Local DOM filtering | No request or persistence |
| Select type | Generates fields from `config_schema` | Local unsaved draft |
| Load favorites | GET `/api/publisher/favorites` | `publishers`, filtered by `kind='favorite'`; secrets redacted |
| Save as Favorite | POST `/api/publisher/favorites` with name, description, type, config | New UUID; authenticated creator; config normalization/encryption; transaction commit |
| Edit/load button | Uses cached favorite, generates fields, delayed population | No GET for a fresh record; no write until Update |
| Update Favorite | PUT `/api/publisher/favorites/<id>` | Updates name/description/type; merges redacted config into decrypted stored data; encrypts and commits |
| Clear stored credential | Password clear checkbox sends `null` | Optional password clearing is persisted; required credential clearing is blocked by form validation |
| Cancel | Resets form and editing ID | No database write |
| Delete | DELETE `/api/publisher/favorites/<id>` | Deletes favorite row; does not delete copied pipeline destinations |
| Send Test Message to Selected | POST `/api/publisher/test-favorites` | Reads runtime credentials; builds temporary destinations; one synchronous attempt per found favorite; closes destinations |
| Publishing Stats | Static HTML plus unused `publishStats` variable | No data source or update code; counters remain zero |

Normal favorite writes use admin/CSRF protection. Read routes use the application authentication gate. Names/descriptions are stored in dedicated columns; destination parameters are JSON; secret-looking config leaves are encrypted and API responses redact them. The test form is a delivery attempt, not a persistence operation.

The older `/api/publisher/configure`, `/edit/<id>`, `/delete/<id>` and `/test` routes operate on node-level destinations and are **not called by this page**. Their runtime-first behavior is separate from favorite CRUD; this audit does not certify those unused routes.

## Confirmed findings

### 1. High: editing with an unavailable key can permanently erase credentials

`publisher_store.py:93` ignores the success flag from `decrypt_config`. Undecryptable encrypted values become `None`; redacted echoes preserve that `None`, and the update stores it. Reproduction: create MQTT favorite with password; unload key; PUT config containing `password: "***"`; response HTTP 200; PostgreSQL password becomes null. Restoring the key cannot recover the overwritten ciphertext.

Fix: refuse config updates when existing credentials cannot be decrypted; preserve the entire prior row on failure. Metadata-only updates can remain separate.

### 2. High: simultaneous edits can restore an old password

`update_publisher` reads without a row lock or version check. Two updates can decrypt the same old config. One writes a new password; the second echoes `***` and later commits the original password along with another setting. Deterministic two-thread PostgreSQL reproduction confirmed the final password reverted to the old value.

Fix: lock the row before decrypt/merge/write, or enforce a version conflict contract. Browser prevention alone cannot protect multiple tabs/workers.

### 3. High: webhook Test Publishing is missing the required pipeline context

The route wraps user JSON under `data` and supplies only node ID/name to the destination context. Deployment webhook mode resolves the URL using a **top-level** or context `pipeline_id`. Neither is supplied. With a configured base URL and token, a webhook favorite test returned HTTP 502 with `invalid pipeline_id for URL`, before any outbound HTTP call. Putting `pipeline_id` in the test textarea did not fix it because it remains nested.

Fix: define a meaningful test contract—select a pipeline context for event delivery, or provide a clearly separate receiver connectivity test. Simply inserting a random ID would not establish successful receiver processing.

### 4. High: favorite names reach an HTML notification sink without escaping

List cards escape names, but Save/Load success messages pass raw names to `showAlert`; shared notifications insert the message via `innerHTML`. A harmless `<b>audit</b>` name reached this sink unchanged in an actual page-handler probe. This is an HTML injection path and can enable script execution through event-bearing markup; no executable payload was run.

Fix: escape dynamic notification strings or use a text-only notification API consistently.

### 5. Medium: server validation accepts unusable configurations; some input errors become HTTP 500

POST accepted an unknown destination type, MQTT config with missing required server/topic and negative port, and a negative common rate limit. All returned HTTP 200 and persisted. `normalize_config` currently checks header syntax and serial baud only; it does not enforce plugin schemas. Malformed webhook headers returned HTTP 500 rather than a validation response.

An empty webhook config is not automatically invalid: deployment-level `WEBHOOK_BASE_URL` may supply its URL. Validation should respect runtime defaults and environment-based configuration without connecting to destinations during save.

Fix: non-connecting server validation of known types, required fields, numeric bounds and config shape, with input failures reported as 400.

### 6. Medium: concurrent creates bypass unique-name checking

The name check reads existing favorites before insertion; the database enforces publisher UUID uniqueness but not favorite-name uniqueness. Two synchronized route requests with the same name both returned HTTP 200 and produced two rows.

Fix: make the case-insensitive naming rule atomic. Coordinate create and rename, and return a consistent conflict response. Existing duplicates need consideration before adding a unique index.

### 7. Medium: config-update semantics can remove required fields or carry credentials to another type

A PUT with only a new password dropped omitted server/topic/port fields. `unredact_into` preserves omitted secret keys but not ordinary config keys, despite the publisher update docstring suggesting omitted values are kept. Conversely, changing MQTT to null with an empty config retained the previous MQTT password. The UI can change type while editing.

Fix: explicitly define full-form replacement versus partial updates, and do not carry old-type secrets across type changes by default.

### 8. Medium: save/test/delete operations have no in-flight guard

Two immediate form submissions issue two POSTs. While a save is pending, fields remain editable; completing the old request resets the form and erases a newer draft. The same absence of guarding is visible in test and delete handlers. The duplicate-save/draft-loss sequence was reproduced using the actual submit handler.

Fix: guard each mutation, disable or version affected forms, and reset only the draft that was submitted.

### 9. Medium: stale or failed reads leave an incorrect favorites display

Two overlapping GETs were resolved in reverse order; the older response replaced the newer list. HTTP 500 responses are silently ignored and the old list remains visible. Destination-schema load failures likewise provide only console logging.

Fix: sequence GET responses, invalidate pending reads after mutations, and display recoverable load errors. Preserve selected test IDs deliberately when rebuilding the list.

### 10. Medium: delayed edit population survives Cancel

`useFavoriteConfig` schedules population after 100 ms even though form generation is synchronous. Edit followed immediately by Cancel still runs the callback, repopulates cancelled data and re-shows the Cancel button while no editing ID exists. Rapidly selecting another favorite/type has the same stale callback risk.

Fix: populate synchronously or version/cancel the callback when the selection changes.

### 11. Medium: test results silently omit missing selected favorites

Selecting one valid null favorite and one nonexistent ID returned overall `success`, count 1 and only one result. Selecting only missing IDs returns HTTP 200 with `status: warning`; the UI selects a success notification solely from HTTP status. This can look like all selected destinations were tested.

Fix: report a result for every selected ID and use semantic outcome when rendering notifications.

### 12. Low: Publishing Stats are permanently zero

`totalPublished` and `publishErrors` occur only in static markup; `publishStats` is initialized but never updated. These counters do not represent delivery success or database-backed history.

Fix: either implement accurately scoped session-test counters or connect a defined metrics source and label its scope.

## Verified working behavior and evidence

- Live authenticated `/publisher`, `/api/publisher/favorites`, `/api/publisher/destination-types` and `/health`: HTTP 200.
- 54 existing publisher/encryption/webhook/form tests passed against the deployed image, using isolated data. One unrelated undeployed pipeline-encryption test was excluded.
- 13 database/route diagnostic cases passed their reproduction assertions, including invalid persistence, missing-key damage, stale credential writes, duplicate creation, missing test IDs, webhook context failure and explicit database write error propagation.
- Six JavaScript diagnostic probes confirmed stale refresh behavior, silent load failure, overlapping save/draft loss, unsafe notification interpolation, required-credential clearing validation and stale edit timers. These assertions describe current behavior, not desired regression-test expectations.
- Normal name/description persistence, encryption with a valid key, redacted password preservation, optional credential clearing, deletion and CSRF rejection have passing existing tests.
- A simulated favorite database write exception returned HTTP 500, not false success.
- All delivery diagnostics used null destinations or stopped before network access; no production receiver messages were sent. No real MQTT broker, serial device, Geti server, Roboflow service, or camera was exercised.

Temporary reproduction artifacts: `/tmp/publisher-audit-tests/test_form_roundtrips_pg.py`, `/tmp/publisher-audit-js.mjs`, `/tmp/check-publisher-audit.py`, `/tmp/publisher-audit-db.log`, `/tmp/publisher-baseline.log`.

## Recommended order

First protect credentials (decryption failure and concurrent updates), correct the webhook test contract, and escape notification data. Then add consistent validation/atomic naming, form request guards and read/edit sequencing. Finally clarify config replacement/type-change behavior and repair the misleading counters. Keep detection/tracking changes out of this work.
