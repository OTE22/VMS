# ArmyEye — Production-Readiness Report (persistence architecture v2)

Date: 2026-08-18 · Branch `master` · Report produced from a **readiness-mode** run
(`ARMYEYE_READINESS_RUN=1 ARMYEYE_LIVE_RECREATION_TEST=1 sh scripts/readiness.sh`; logs in
`readiness-run/`).

## 1. Verdict

**PASS** — see §10 for the exact numbers of the readiness run and §11 for every acceptance line.
All required proofs ran (none was converted into a PASS by skipping); the only skips are
platform-inapplicable cases (Windows symlink/POSIX-mode tests), listed by category in §10.

## 2. Architecture as built (owner decision honoured)

```
PostgreSQL  = authoritative metadata / configuration / registry / relationships / lifecycle state
ARTIFACT_ROOT (/app/InferenceNode/data, bind mount ./InferenceNode/data)
            = managed physical artifact bytes:  models/<model_id>/…  engines/<key>/engine.py
                                                media/…              thumbnails/thumbnail_<pid>.jpg
Integrity contract = id + relative_path + sha256 + size_bytes + status/validation_status/reason
                     + verified fingerprint (size, mtime_ns, ctime_ns, inode, device) + last_verified_at
```
No blobs in PostgreSQL. No plaintext secrets in PostgreSQL. All paths persisted are relative and
resolved only through `InferenceNode/artifact_paths.py` (`resolve()` rejects `..`, absolute,
percent-encoded traversal and symlink escape). Runtime-only state (pipeline process state,
FPS/latency, telemetry samples, discovery) stays runtime and is labeled as such in the UI.

### Source-of-truth matrix

| Entity | Authority (writes) | Bytes | Legacy source (read-only, retained) | Migration marker (app_state) |
|---|---|---|---|---|
| users / audit / pipeline access | PostgreSQL | — | — | — |
| pipelines (config, `model_id` canonical) | PostgreSQL (`PipelineRepository` = single sync point) | — | `pipelines/pipelines_metadata.json` | `pipeline_json_migration_v1` |
| models / representations / artifacts | PostgreSQL (`model_registry`, `model_repo`) | `ARTIFACT_ROOT/models/<id>/` | `model_repository/models_metadata.json` + `models/` | `models_registry_to_postgres_v1` |
| inference engines (builtin + custom) | PostgreSQL (`engine_registry`) | custom: `ARTIFACT_ROOT/engines/<key>/engine.py`; builtin: image source tree (read-only, `shipped_sha256`) | `InferenceEngine/engines/*.py` (builtins) | — (builtins re-registered at discovery) |
| publishers (favorites + node destinations) | PostgreSQL (`publisher_store`, secrets Fernet-encrypted `{v,key_id,ct}`) | — | `node_settings.json` | `node_settings_to_postgres_v1` |
| node identity / telemetry config / preferences | PostgreSQL (`node_settings_store`) | — | `node_settings.json` | `node_settings_to_postgres_v1` |
| media | PostgreSQL (`media_registry`); pipelines reference `frame_source.config.relative_source` (canonical) | `ARTIFACT_ROOT/media/…` | `InferenceNode/media/` | `media_registry_to_postgres_v1` |
| thumbnails | PostgreSQL (`thumbnail_registry`) | `ARTIFACT_ROOT/thumbnails/` | `pipelines/thumbnails/` | `thumbnails_registry_to_postgres_v1` |
| telemetry samples, runtime status, discovery | runtime only (documented) | — | — | — |

## 3. Central state registries (`InferenceNode/artifact_states.py`)

* **Lifecycle `status`** (CHECK-constrained in PostgreSQL, ORM enum from the same source):
  `STAGING · VALIDATING · AVAILABLE · FAILED · MISSING · CORRUPT · DELETING`. No `NEEDS_REVIEW`
  or any other value exists (PG CHECK proven: `test_undefined_lifecycle_status_is_rejected_by_the_database`,
  `test_pg_media_ingest_and_migration`).
* **`validation_status`**: `PENDING · PASSED · FAILED · HASH_MISMATCH · SIZE_MISMATCH · FORMAT_INVALID · SECURITY_REJECTED`.
* **`reason`** (diagnostic, nullable): `LEGACY_FILE_NOT_FOUND AMBIGUOUS_LEGACY_PATH LEGACY_PATH_UNRESOLVED
  HASH_MISMATCH SIZE_MISMATCH VALIDATION_FAILED COMPONENT_MISSING MANIFEST_MISMATCH ENCRYPTION_KEY_MISSING
  UNREGISTERED_ARTIFACT SECURITY_REJECTED PROMOTE_INTERRUPTED NO_USABLE_REPRESENTATION
  REQUIRED_REPRESENTATION_UNAVAILABLE FILE_MISSING DELETE_INTERRUPTED COPY_FAILED`.
* **Transitions** (`transition()` raises `IllegalTransition` otherwise):
  `STAGING→VALIDATING|FAILED · VALIDATING→AVAILABLE|FAILED|CORRUPT|MISSING · AVAILABLE→VALIDATING|CORRUPT|MISSING|DELETING · CORRUPT|MISSING|FAILED→VALIDATING · DELETING→(row removed)`.
  Recovery is always via VALIDATING; no direct FAILED/CORRUPT/MISSING→AVAILABLE.
* **Serving rule** (`is_servable` + per-registry `servable_*_path`): AVAILABLE ∧ PASSED ∧ file present ∧
  unchanged fingerprint; a changed fingerprint triggers re-hash (match → refresh fingerprint;
  mismatch → CORRUPT/HASH_MISMATCH; gone → MISSING). Custom engines are always fully re-hashed on load.

## 4. Schema (Alembic `0001…0005`, live DB now at `0005_pipeline_model_integrity`)

* `0004_artifact_registry` — extends `models` (+status/validation_status/reason/…), creates
  `model_representations`, `model_artifacts` (one physical file = one row; OpenVINO xml+bin+metadata are
  3 rows sharing a representation with `manifest_sha256`), `inference_engines`
  (`origin builtin|custom`, `CHECK enabled=FALSE OR (status='AVAILABLE' AND validation_status='PASSED')`,
  `custom ⇒ relative_path+sha256 NOT NULL`), `publishers`, `node_settings`, `media_assets`,
  `pipeline_thumbnails`; `pipelines.model_id` nullable, **no FK yet**.
* `0005_pipeline_model_integrity` — audit (`VALID/UNKNOWN_MODEL/MISSING_MODEL_ID/MALFORMED_CONFIG`),
  back-fill VALID only, `fk_pipelines_model … ON DELETE RESTRICT`, `ck_pipelines_model_ref_consistent`
  (`NOT VALID` → `VALIDATE`).
* **Bootstrap ordering enforced in code** (`auth/bootstrap.py`): below 0005 → upgrade to 0004 → legacy
  model registry migration → upgrade head. Proven on isolated PG from 0003 with a referencing pipeline
  (`test_bootstrap_orders_0004_then_legacy_models_then_0005`) and on the live stack (§8).

## 5. State machines

* **Creation** (models upload, media upload, thumbnails, custom engines, physical migration):
  `INSERT STAGING → commit → bytes into <kind>/.staging → VALIDATING (format / decode / AST+security /
  dry-run import) → sha256+size → fsync → atomic `os.replace` → fsync dir → AVAILABLE+PASSED+fingerprint`.
  Any failure before AVAILABLE ⇒ FAILED (staged bytes removed/quarantined; never served).
* **Deletion** (batch-safe): `AVAILABLE → DELETING (all rows of the logical entity) → move EVERY artifact to
  <kind>/.trash → only after ALL moves: remove rows → commit → purge trash`; failure restores from trash
  (sha256-verified) or leaves `DELETING` with everything recoverable — never `AVAILABLE + missing`.
  Model delete: `SELECT … FOR UPDATE` + FK RESTRICT ⇒ 409 while referenced.
* **Pipeline delete** coordinates the thumbnail: authorize → stop → thumbnail DELETING + JPEG to trash →
  row delete (FK cascade removes the thumbnail row + grants) → commit → purge; DB failure restores the JPEG.
* **Model availability aggregation** (`recompute_model_status`, the only writer of `models.status`):
  AVAILABLE ⇔ metadata valid ∧ ALL `required=true` representations AVAILABLE ∧ ≥1 usable representation.

## 6. Secrets

Dedicated versioned Fernet key from `ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE` (`key_id:<key>` lines; stored shape
`{"v":1,"key_id":…,"ct":…}`), never `SECRET_KEY`/session/JWT material, never in PostgreSQL, never baked
into the image, never auto-generated; group/world-readable key files are refused. Missing/wrong key ⇒ rows
marked undecryptable, config retained, nothing served, ERROR logged; API responses always redacted (`***`);
logs never contain plaintext (grep tests). Docker Desktop cannot carry restrictive modes on bind mounts
(0777) — `entrypoint.sh` stages a private 0400 copy in a container-only tmpfs (uid 1000, mode 0700) and
points the app at it (verified live: `/run/armyeye/config.key -r-------- infernode`).

## 7. Physical artifact migration (executed live on 2026-08-18 first boot of the new image)

| Plane | Legacy source | Discovered | Processed | AVAILABLE | Missing / ambiguous / failed | Notes |
|---|---|---|---|---|---|---|
| models | `model_repository/models_metadata.json` + `models/` | 4 | 4 | 4 | 0 / 0 / 0 | 8 representations (4 pt primary required, 4 OpenVINO derived optional), 16 artifact rows, sha256 verified per file, manifest per representation |
| pipelines → model refs (0005 audit) | `pipelines.config` | 5 | 5 | VALID 5 | UNKNOWN 0, MISSING_ID 0, MALFORMED 0 | `model_id` back-filled 5/5, CHECK VALIDATED |
| media | `InferenceNode/media/` | 26 | 26 | 26 | 0 / 0 / 0 | relative paths preserved, ~1.1 GB copied via stage/fsync/verify/promote, legacy retained |
| thumbnails | `pipelines/thumbnails/` | 12 | 12 | 5 (live pipelines) | 0 / 0 / 0 | 7 orphans of deleted pipelines **reported, not deleted** |
| node settings | `node_settings.json` | 4 | 4 | 4 | 0 | node identity, telemetry config, 2 favorites (no plaintext secrets present) |
| custom engines | `InferenceEngine/engines/` | 0 custom | — | — | — | 5 builtins registered as `origin=builtin` |

Startup reconciliation after migration: **healthy** (`[VERIFY] registry reconciliation: healthy`).
Marker semantics: "every discovered legacy record deterministically processed with an explicit
fail-closed state" (missing/ambiguous count as processed; `failed>0` blocks the marker).

## 8. Container recreation (real `--force-recreate`, not restart)

`tests/test_container_recreation.py` (live stack, `ARMYEYE_LIVE_RECREATION_TEST=1`): created a custom engine
and an encrypted favorite via the live API → `docker compose up -d --force-recreate vms` (container id
changed) → after boot: engine row `AVAILABLE|PASSED|enabled` with `sha256(file) == row.sha256`
in-container **and** on the host bind mount, all 4 model rows + 16 artifact rows identical with matching
hashes, favorite still encrypted (`"ct"` present, plaintext absent in DB and API), factory activated the
custom engine, `/api/registry/verify` → `healthy`; REPRO records removed afterwards. **PASS.**

## 9. Browser E2E (Playwright, isolated in both planes)

`tests/e2e/` boots a real `InferenceNode` subprocess against a fixture DB `armeye_test_<hex>` (alembic head)
on a **throwaway `postgres:16-alpine` container**, a fixture `ARTIFACT_ROOT` under the temp dir, an
**empty legacy root** (`ARMYEYE_LEGACY_ROOT`) and a dedicated key file. Hard guard printed in the run log
(`[E2E HARD GUARD] current_database()='armeye_test_…' artifact_root='…\Temp\armeye-e2e-…'`), dev-data
snapshot (`InferenceNode/data, model_repository, media, pipelines, node_settings.json`) identical before/after.
18 tests: isolation + empty ingest, all 10 pages render with **zero application console errors**, model
upload → PG AVAILABLE + file + sha256 + UI + dashboard registry counts, media upload → pipeline create
(`model_id == config.model.id`) → secrets redacted → management/builder hydration → duplicate → coordinated
delete, favorite encrypted at rest / redacted in API+UI, custom engine → registry + root + factory
activation, `/api/registry/verify` healthy without host paths, non-admin authorization matrix, model delete
409 while referenced then clean (no artifact bytes left).

**Defects the E2E exposed and fixed** (would have shipped otherwise):
1. `InferenceNode.__init__` used PostgreSQL before the engine existed (migrations/settings/verify silently
   skipped on a fresh boot) — DB is now initialised and bootstrapped first.
2. Legacy `pipelines_metadata.json` path ignored the legacy root (a test node would import dev pipelines).
3. Custom-engine install always quarantined itself under the registry-gated factory (discoverability
   checked while VALIDATING; generated engine bound a second `BaseInferenceEngine` class) — dry-run import
   in VALIDATING → AVAILABLE → rediscover → verify; `base_engine` aliased to the factory's module.

## 10. Readiness run (Phase 17)

Command: `ARMYEYE_LIVE_RECREATION_TEST=1 sh scripts/readiness.sh` (`ARMYEYE_READINESS_RUN=1` exported by the
script, so every required proof FAILS rather than skips when its runtime is unavailable).

| Layer | Result |
|---|---|
| Host pytest — 45 suites incl. isolated PostgreSQL, node `.mjs` suites, browser E2E, live container recreation | **655 tests: 651 passed, 0 failed, 0 errors, 4 skipped** (509 s) |
| PostgreSQL contract suites re-run **inside the built VMS image** (`scripts/pg-test.sh`, production dependency set) | **87 passed, 0 failed** (143 s) |
| Browser E2E (Playwright, real server) | **18 executed, 0 skipped** |
| Live container recreation (`--force-recreate`) | **1 executed, 0 skipped** |

Skips by category — all four are platform-inapplicable on this Windows host, none is a required readiness proof:

| Skipped test | Category | Why it is not a required proof |
|---|---|---|
| `test_artifact_paths_and_migration.py::test_resolver_rejects_symlink_escape` | symlink unsupported | the resolver's traversal/absolute/encoded rejections still run; symlink escape is additionally covered on the Linux image run |
| `test_engine_registry_security.py::test_symlink_escape_is_refused` | symlink unsupported | same |
| `test_media_library.py::test_symlink_escape_is_excluded` | symlink unsupported | same |
| `test_publishers_settings_pg.py::test_world_readable_key_file_is_refused` | POSIX mode bits N/A on Windows | executed and passed in the in-image (Linux) run |

Required-proof gating is implemented once, in `readiness_required()` (`tests/conftest.py`), and used by the
isolated-PG fixture, the E2E harness and the container-recreation test — a required proof can never become a
PASS by skipping.


## 11. Acceptance lines

Every line below is **PASS** in the readiness run; the right column names the proving test(s) / live evidence.

| Acceptance line | Proof |
|---|---|
| Existing physical artifacts were actually migrated/verified under ARTIFACT_ROOT | live boot log (§7: models 4/4, media 26/26, thumbnails 5 + 7 orphans); `test_legacy_models_populate_registry_with_verified_bytes`, `test_media_migration_preserves_relative_paths_and_registers`, `test_thumbnail_migration_registers_live_only_and_reports_orphans` |
| No PostgreSQL artifact record points to bytes that were never physically migrated | `test_copy_verify_promote_registers_only_verified_bytes`, `test_copy_failure_blocks_the_marker`; live `/api/registry/verify` → healthy |
| Legacy ambiguous/missing artifacts are never marked AVAILABLE | `test_legacy_ambiguous_path_uses_reason_not_fake_state`, `test_ambiguous_legacy_path_is_missing_with_reason_and_does_not_block_marker`, `test_missing_legacy_file_is_missing_not_available` |
| Pipeline→model dependency protection is concurrency-safe (FK RESTRICT + FOR UPDATE) | `test_fk_restrict_blocks_deleting_a_referenced_model` (PG), `test_model_delete_is_blocked_while_referenced_then_allowed` (E2E), `SELECT … FOR UPDATE` in `model_repo.delete_model` |
| Model deletion cannot race with pipeline assignment | same (database-enforced RESTRICT) |
| Artifact deletion cannot leave AVAILABLE + missing file | `test_delete_partial_move_failure_restores_and_never_leaves_available_missing`, `test_delete_db_failure_after_moves_keeps_everything_recoverable_in_trash`, `test_delete_failure_preserves_engine`, `test_pipeline_delete_failure_restores_or_preserves_thumbnail` |
| Artifact creation cannot expose a file before DB state reaches AVAILABLE | `test_failure_injection_never_yields_false_available`, `test_media_upload_failure_at_promote_never_available`, `test_thumbnail_registration_failure_never_yields_available`, `test_invalid_python_is_rejected_before_any_write` |
| STAGING artifacts are never served | `test_staging_media_is_never_served`, `test_non_available_artifact_is_never_served`, `test_crash_after_promote_before_available_is_not_served` |
| Crash between artifact promotion and DB AVAILABLE transition is recoverable | `test_crash_after_promote_before_available_is_not_served` (+ `verify` reports `staging_final`) |
| OpenVINO/multi-file representations have deterministic per-file + manifest integrity | `test_openvino_migration_registers_xml_and_bin_separately`, `test_openvino_manifest_hash_is_deterministic`; live: 3 artifact rows per OpenVINO representation + `manifest_sha256` |
| Configuration encryption uses a dedicated key separate from application/session secrets | `config_secrets` reads only `ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE`; `test_encrypt_persist_decrypt_internally_and_api_stays_redacted`, `test_world_readable_key_file_is_refused` |
| Missing/wrong encryption key fails safely and does not destroy stored configuration | `test_missing_key_fails_safely_without_destroying_config`, `test_wrong_key_fails_safely`, `test_undecryptable_secrets_degrade_and_are_not_destroyed`, `test_plaintext_secret_never_stored_when_key_missing` |
| Secret encryption supports explicit version/key identification for future rotation | stored shape `{v, key_id, ct}`; `test_key_rotation_compatibility` |
| All artifact paths are relative and resolved through one path-safety service | `test_no_module_joins_artifact_roots_outside_the_resolver`, `test_resolver_rejects_traversal_absolute_and_encoded`, `test_traversal_and_absolute_names_cannot_escape` |
| Frontend mutations use logical IDs instead of arbitrary filesystem paths | upload responses carry ids / relative refs only (`test_media_upload_and_pipeline_lifecycle`, `test_model_upload_registers_in_pg_and_artifact_root_and_lists_in_ui`), `test_report_contains_no_host_paths` |
| Registry reconciliation identifies DB↔filesystem inconsistencies without deleting | `test_tampered_and_missing_artifacts_are_detected_and_nothing_is_deleted`, `test_orphan_files_are_warnings_not_problems`, `test_verify_detects_hash_mismatch_and_missing_and_orphans` |
| E2E is isolated in both PostgreSQL and ARTIFACT_ROOT | `[E2E HARD GUARD]` log line, `test_isolation_hard_guard_and_empty_legacy_ingest`, dev-data snapshot check in `e2e_server` teardown |
| Models and engines survive actual container recreation | `test_models_and_engines_survive_force_recreate` (live, §8) |
| No undefined lifecycle state such as NEEDS_REVIEW exists outside the formal state registry | `test_no_undefined_artifact_status_can_be_persisted`, `test_undefined_lifecycle_status_is_rejected_by_the_database` (PG CHECK) |
| Diagnostic reasons are separate from lifecycle states | `test_lifecycle_and_validation_fields_are_orthogonal`, `test_missing_source_is_missing_with_reason_not_a_fake_state` |
| OpenVINO directories are represented by their actual physical files, not a hashed dir name | `test_openvino_dir_enumerates_xml_and_bin_only`; live artifact rows |
| Every multi-file representation has deterministic manifest integrity | `test_manifest_hash_is_deterministic_and_order_independent` |
| inference_engines has a real lifecycle status independent of validation_status | `test_engine_status_and_validation_status_are_separate` |
| Built-in and custom engines have explicit different storage/management semantics | `test_builtin_engine_cannot_be_deleted_as_custom`, `test_custom_engine_uses_artifact_root_and_registry`, `test_custom_engine_requires_artifact_path_and_hash` (PG CHECK) |
| Non-AVAILABLE engines cannot be enabled or imported | `test_non_available_engine_cannot_be_enabled`, `test_hash_mismatch_detected_and_engine_not_loaded`, `test_pg_rejects_enabled_engine_without_passed_validation` |
| Thumbnails are integrity-tracked with SHA256 and status | `test_thumbnail_has_sha256_and_integrity_status`, `test_pg_thumbnail_lifecycle_and_cascade_delete` |
| Pipeline deletion cannot leave orphaned registered thumbnail bytes | `test_pipeline_delete_cleans_registered_thumbnail_safely` (+ PG cascade variant) |
| pipelines.model_id and config.model.id cannot silently diverge | `test_pipeline_model_column_matches_json_reference`, `test_pg_rejects_divergent_model_reference` (CHECK), `test_repository_keeps_column_and_json_in_sync_on_postgres` |
| Existing unknown model references are reported rather than guessed | `test_unknown_legacy_model_reference_is_reported_not_guessed`; live 0005 audit: 5 VALID / 0 UNKNOWN |
| Artifact lifecycle transitions are centrally validated | `test_transition_table_legal_and_illegal_edges`, `test_artifact_state_machine_rejects_invalid_transition` |
| No STAGING/FAILED/MISSING/CORRUPT/DELETING artifact can be served as healthy | `test_non_available_artifact_is_never_served` + per-registry servable tests |
| Lifecycle status and validation_status are never conflated | `test_lifecycle_and_validation_fields_are_orthogonal` |
| Logical model availability is derived consistently from its representations | `test_all_required_representations_must_be_available`; `recompute_model_status` is the single writer |
| An optional corrupt derived representation does not unnecessarily disable a valid primary model | `test_optional_derived_failure_does_not_disable_model` |
| A model with no usable representation is never reported AVAILABLE | `test_no_usable_representation_disables_model` |
| Multi-artifact model deletion is batch-safe and recoverable | `test_delete_moves_all_artifacts_then_removes_rows_then_purges`, `test_delete_partial_move_failure_restores_and_never_leaves_available_missing`, `test_delete_db_failure_after_moves_keeps_everything_recoverable_in_trash` |
| Partial trash movement cannot produce AVAILABLE + missing artifacts | same |
| PostgreSQL prevents or explicitly detects model_id/config.model.id divergence | `ck_pipelines_model_ref_consistent` VALIDATED live; `test_pg_rejects_divergent_model_reference` |
| Cached artifact verification is immediately invalidated when filesystem identity changes | `test_changed_fingerprint_invalidates_cached_verdict`, `test_tampered_bytes_are_detected_on_load_via_fingerprint`, thumbnail/media tamper tests |
| SHA256 remains the authoritative content identity without hashing on every request | fingerprint gate → re-hash only on change (`servable_*_path`) |
| Migration completion means all discovered records were deterministically processed, not that every artifact became AVAILABLE | `test_ambiguous_legacy_path_is_missing_with_reason_and_does_not_block_marker`, `test_copy_failure_blocks_the_marker`; live thumbnails marker set with 7 orphans reported |
| Registry schema migration succeeds even when legacy models table is initially empty | `test_head_is_0004_and_registry_tables_exist` (fixture DB, empty `models`); live 0004 |
| Legacy model registry is populated before pipeline→model FK validation | `test_bootstrap_orders_0004_then_legacy_models_then_0005`; live boot log ordering |
| Pipeline FK backfill occurs only after referenced model rows exist | same (5/5 back-filled live) |
| AVAILABLE → VALIDATING is a legal integrity-revalidation transition | `test_available_can_enter_revalidation` |
| CORRUPT/MISSING/FAILED must pass through VALIDATING before returning AVAILABLE | `test_corrupt_requires_validation_before_available`, `test_missing_requires_validation_before_available`, `test_failed_requires_validation_before_available` |
| All required model representations must be healthy for logical model availability | `test_all_required_representations_must_be_available` |
| Optional derived representation failure does not unnecessarily disable the model | `test_optional_derived_failure_does_not_disable_model` |
| Logical model state fields are internally consistent | `test_model_status_uses_only_registry_vocabulary`, `test_lifecycle_and_validation_fields_are_orthogonal` |
| enabled engines are DB-constrained to AVAILABLE + PASSED | `test_pg_rejects_enabled_engine_without_passed_validation` |
| Required E2E cannot be skipped in final readiness mode | `readiness_required` in `tests/e2e/conftest.py`; readiness run executed 18 E2E tests, 0 skipped |
| Required container-recreation proof cannot be skipped in final readiness mode | `readiness_required` in `tests/test_container_recreation.py`; readiness run executed it |

## 12. Explicitly not done / owner notes

* `relative_source` stays the canonical pipeline media reference; `media_asset_id` FK is a designed future
  migration. `/webhook/<id>` stays login-gated. Legacy JSON files and directories retained read-only for one
  release (rollback), retire separately.
* `compose.yaml` / `compose.prod.yaml` / `compose.cpu.yaml` are git-ignored by the repo's `*.yaml` rule; the
  working-tree `compose.yaml` carries the required changes (ARTIFACT_ROOT mount, key secret + tmpfs,
  read-only legacy mounts). Decide whether to track them (`git add -f`) or keep them deployment-local.
* Docker Desktop hosts: the key secret arrives 0777 through the bind mount; the entrypoint's tmpfs staging is
  what makes it restrictive. On Linux hosts the mounted file's own 0600 mode is honoured as well.
* Telemetry samples are never persisted (by design).
