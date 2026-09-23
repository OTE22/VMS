# Admin users and telemetry deep audit — 2026-09-23

## Scope and verification

Inspected deployed release `armyeye-vms:publisher-20260923t072108z`. Verified that the admin routes/service/script, pipeline assignment routes, telemetry template/worker and node settings store match the live container by SHA-256. This is an inspection report: no application fix or deployment was performed.

Live authenticated GET checks returned HTTP 200 for `/admin/users`, `/api/users`, `/api/pipelines/assignable`, `/telemetry`, `/api/telemetry/config`, `/api/telemetry`, and `/health`. Account records and secrets were not printed. All write/concurrency probes used disposable databases; MQTT behavior was mocked and no production account, permission, setting or broker was changed.

118 existing authentication, admin profile, authorization, encryption and form tests passed; one unrelated undeployed pipeline-encryption test was excluded. Eleven targeted backend diagnostics and seven JavaScript diagnostics reproduced the findings below. Diagnostic assertions describe observed failures, not desired regression expectations.

## `/admin/users`: controls, routes and persistence

| Action | Route | Persistence and authorization |
| --- | --- | --- |
| List, refresh, filter users | GET `/api/users`; filters local | `users` plus aggregated `pipeline_user_access` counts; admin only |
| Create | POST `/api/users` | User row with bcrypt hash and audit row in one transaction; admin + CSRF |
| Edit profile | PATCH `/api/users/<id>` | Email/full name only; audit on changes; username immutable |
| Promote/demote | PUT `/api/users/<id>/role` | Role, permissions version and audit row; last-admin check |
| Enable/disable | PUT `/api/users/<id>/active` | Active flag, permissions version and audit row; last-admin check |
| Reset password | POST `/api/users/<id>/reset-password` | New bcrypt hash, password-change flag/time, permissions-version bump and audit |
| Delete | DELETE `/api/users/<id>` | Deletes account; audit record; last-admin check |
| Open access modal | GET `/api/pipelines/assignable` and `/api/users/<id>/pipeline-access` | Admin-scoped pipeline/grant reads |
| Save/revoke access | PUT/DELETE `/api/pipelines/<pipeline_id>/access/<user_id>` | `pipeline_user_access`; admin + CSRF; separate subsequent audit write |
| Password visibility/generation, search, role cards, dialogs | Local JavaScript | No write until corresponding submit |

### A1 — High: concurrent operations can remove every active administrator

`auth/service.py:_lock_user` locks only the target row. `_would_remove_last_admin` counts other active admins without a shared lock. Two different admins can therefore each observe another administrator and both be demoted. A synchronized PostgreSQL test demoted the only two active admins successfully and verified **zero** active administrators afterward. The same count/lock design is used for deactivation and deletion; those concurrent variants were not separately executed.

Recommended fix: serialize every operation that reduces the active-admin set under one transaction-level lock, and count after acquiring it. Keep sequential last-admin tests and add cross-account concurrent cases.

### A2 — High: delayed access-modal loads can target the wrong displayed user

`admin-users.js:openPipelineAccess` sets the modal title immediately, then awaits two requests without a modal generation guard. Opening First, then Second, and resolving First last leaves First's grants below the title **Second**. Reproduced in the actual function. The rendered Save/Revoke callbacks close over the earlier `u.id`; code tracing shows why this mismatch can make an administrator act on the wrong account. The diagnostic verified display/identity mismatch; it did not issue a production grant.

Recommended fix: bind modal requests and callbacks to the current user/generation; discard old responses and invalidate on close/reopen.

### A3 — High: boolean coercion can change accounts or grant permissions unintentionally

The active route uses `bool(data.get('active'))`: `{}` disables an account and `{"active":"false"}` enables it. The pipeline permission normalizer similarly treats `{"can_edit":"false"}` as true and also grants view access. All cases reproduced through real routes with persisted state. The normal browser sends actual booleans, so this is an API validation issue rather than a demonstrated ordinary-click failure or authorization bypass.

Recommended fix: require explicitly present JSON booleans for account state and permission fields; validate password-change flags likewise.

### A4 — Medium: permission writes can return failure after the grant committed

Pipeline access mutation commits before `record_audit`. Simulating an audit-log failure returned HTTP 500 while the grant was already present in PostgreSQL. The UI tells the administrator the request failed even though access changed.

Recommended fix: make grant plus audit atomic, or explicitly report the durable outcome if audit recording fails. Apply the same contract to revoke.

### A5 — Medium: input validation depends too much on the browser

An invalid email string passed PATCH and persisted; a JSON array posted to user creation returned HTTP 500. Service methods assume some fields are strings. The UI has email/password validation, but direct requests bypass it. Database length/type failures are not consistently translated into validation responses.

Recommended fix: validate JSON object shape, string lengths/types, email format and flags at the API/service boundary; return consistent 400/404/409 responses.

### A6 — Medium: requests are not sequenced or guarded across user actions

An older user-list response replaced a newer list in a JavaScript probe. The shared `write` helper changes a button's loading state but has no pending-operation guard; two invocations issued two writes. Role/active/delete call it without a button. Edit/reset modals can close or reopen while an earlier request is pending; completion acts on the shared modal.

Recommended fix: order list responses, guard writes by target/action, and tie completion behavior to the submitted modal generation. Loading-button styling alone is insufficient.

## `/telemetry`: controls, routes and persistence

| Action | Route | Storage/runtime behavior |
| --- | --- | --- |
| Load settings | GET `/api/telemetry/config` | Reads runtime object, not the durable desired config; credentials redacted |
| Save enabled/interval/broker/port/topic | POST `/api/telemetry/configure` | Admin + CSRF; changes shared runtime fields temporarily; `_save_settings` transaction writes node settings; then MQTT/worker activation |
| Live charts/current values | GET `/api/telemetry` every two seconds | Samples host metrics; samples are not saved to PostgreSQL |
| Pipeline cards | GET `/api/pipelines` initially and every 30 seconds | Permission-scoped pipeline statistics; independent of MQTT publishing |
| Stored config | `node_settings`, key `telemetry` | JSON configuration; secret fields encrypted; save also persists other node settings/destinations |

The page exposes no MQTT username/password inputs or explicit clear controls, although the API supports those fields. Saved credentials are normally preserved when the form omits them. “Enable Telemetry” controls the background collection/publishing worker; charts continue polling independently.

### T1 — High: unavailable encryption key can erase saved MQTT credentials

`node_settings_store.set_setting` ignores the decryption success flag. The configure route also reads runtime settings without checking `_secrets_ok`. A real-route reproduction saved an encrypted password, unloaded the key, submitted the ordinary form without credentials, received HTTP 200, and verified that the stored password became null.

Recommended fix: refuse edits requiring unreadable credentials and preserve ciphertext. Validate the decryption status before merging or mutating runtime state.

### T2 — High: failed save rollback can overwrite another successful runtime update

The configure route assigns desired settings to the shared telemetry object before saving and restores its own previous snapshot on failure, without a lock. In a controlled two-request reproduction, a successful request persisted interval 33; a prior request then failed and restored runtime interval 17. Database and active runtime diverged.

Recommended fix: serialize configuration persistence and activation, preferably persisting a local validated desired object rather than exposing uncommitted settings on the shared worker.

### T3 — High: stop/restart can leave multiple telemetry workers alive

`stop_telemetry` sets a shared boolean and joins for only five seconds. The loop sleeps for the configurable interval (up to 300 seconds). If it is still alive when Start sets the same boolean true and launches another thread, the old worker can resume as well. Reproduced with controlled blocking waits: both old and new worker threads remained alive after stop/start.

Recommended fix: use an interruptible per-worker stop event; do not start a replacement until the old worker exits. Apply interval changes through the same lifecycle control.

### T4 — High: disabling can fail to stop telemetry when the broker is unavailable

The route tries `configure_mqtt` before handling `enabled=False`. A simulated broker failure returned HTTP 502 with `saved:true`, persisted disabled state, but left the previously running worker running. GET config reported enabled from runtime while PostgreSQL said disabled.

Recommended fix: stopping must not depend on establishing a broker connection. Expose desired enabled state and actual activation/connection state separately.

### T5 — Medium: collection failures are presented as zero usage

The worker returns an error object on sample collection failure; the API ignores it and fills missing metrics with zero while returning HTTP 200. A diagnostic confirmed CPU=0 and no error field. Separately, the browser charts convert null temperature to zero even though the numeric temperature label correctly says `n/a`. When the telemetry service is absent, the route also deliberately returns mock zero metrics.

Recommended fix: expose sample availability/errors and graph missing values as gaps, not zero. Do not make a failed sample look like an idle machine.

### T6 — Medium: overlapping polling and initial load can overwrite newer information

No pending/sequence guard exists for telemetry polling. Reversing two responses appended the newer sample followed by the older sample. The initial config GET can overwrite a broker name typed before it completes. HTTP polling errors are mostly silent; pipeline-stat failures display zero values rather than unavailable status.

Recommended fix: serialize or sequence polls; protect dirty drafts from initial reads; display data freshness and recoverable errors.

### T7 — Medium: save UI does not distinguish persistence from activation

Two rapid submissions issued two writes in the actual form-handler probe. An HTTP 502 response with `saved:true` was rendered as “Failed to update configuration,” despite the durable save. The form has no pending guard or desired-versus-running state display.

Recommended fix: guard the form; report “Saved, activation failed” when appropriate and reload authoritative desired/runtime state. Escape dynamic error text before shared HTML notifications.

## What passed

Existing tests verify normal user creation/profile persistence, password hashing/reset, permissions-version/session invalidation, admin-only APIs, CSRF enforcement, sequential last-admin protection, narrow profile-edit fields, pipeline assignment semantics, and normal telemetry form persistence with a blank broker. Live GETs and source verification also passed. These successes do not cover the reproduced concurrent and failure cases above.

Recommended priority: protect the last administrator and telemetry credentials first; fix wrong-user modal state and permission booleans; then serialize telemetry lifecycle/saves and clarify durable versus active state. Address remaining validation, request sequencing and display accuracy afterward.

Temporary evidence: `/tmp/admin-telemetry-audit-tests/test_form_roundtrips_pg.py`, `/tmp/admin-telemetry-js.mjs`, `/tmp/admin-telemetry-audit.log`, `/tmp/admin-telemetry-baseline.log`, `/tmp/admin-telemetry-js.log`.
