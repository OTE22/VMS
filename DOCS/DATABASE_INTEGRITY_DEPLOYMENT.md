# Database integrity deployment

Completed: 2026-09-15T09:16:36.410812+00:00 (UTC).

## Result

VMS database `armeye` is at `0007_reference_integrity`. All three new constraints
are validated: artifact representation/model agreement, representation composite
uniqueness, and protection of JSON pipeline model references.

Only three application files were overlaid onto the existing image:

- `InferenceNode/data_models.py`
- `InferenceNode/auth/bootstrap.py`
- `InferenceNode/migrations/versions/0007_reference_integrity.py`

The original deployed files matched Git HEAD before patching. No dependencies were
rebuilt or downloaded. The VMS application service was recreated; the database and
other application services were not restarted. There were no pipeline definitions
or running pipelines at maintenance time.

## Backup and recovery evidence

- Backup: `/home/itdirect-ai/Desktop/VMS/InferenceNode/data/database_backups/20260915T091339Z/armeye-before-0007.dump`
- SHA-256: `a7cb583591102e12588982ca71313f150406724e07b84bdd8e48a10fb2d2836e`
- Backup file permissions: owner-only (0600), in a private timestamp directory (0700).
- PostgreSQL custom-format dump restored successfully into an isolated database.
- All 14 public-table row counts matched the live database before deployment.
- The restored database upgraded successfully; all table row counts were preserved.
- The isolated restore database was removed after verification. The backup is retained.

This verifies database restoration. Model/media bytes and configuration-encryption
keys were not changed or included in this database-only dump; full system recovery
also requires the existing artifact and secret storage.

## Deployment and rollback references

- Previous image: `armyeye-vms:before-0007-20260915t091339z`
- Previous image ID: `sha256:489cf7c615ae5228587946da35117615c25d2ba4c7a6a8837d32f396799f5bd3`
- Deployed image: `armyeye-vms:reference-integrity-20260915t091339z`
- Deployed image ID: `sha256:4b661ab064220eb48bd76a69a11fc854c2a84230d30f2e37b59dfdc03b9473fd`
- Compose deployment tag `armyeye-vms` now points to the deployed image.

For an application rollback, retag the retained previous image as `armyeye-vms` and
recreate only `vms` with `docker compose up -d --no-deps --no-build vms`. The added
constraints are compatible with the old application. If the schema must also be
rolled back, run Alembic downgrade to `0006_pipeline_node_assignment` using the
patched image (which contains the downgrade), before replacing that image. The
schema downgrade retains all data and restores the former weaker constraints.
Do not restore the pre-deployment dump over a database that has accepted subsequent
writes without first preserving and reconciling those writes.

## Post-deployment validation

- VMS container healthy; `/health` returned HTTP 200.
- Anonymous `/api/pipelines` and `/api/models` returned HTTP 401.
- Startup confirmed migrations applied and registry reconciliation healthy.
- CUDA remained available.
- Deployed source hashes matched the three reviewed workspace files.
- Application table row counts were preserved; audit count did not decrease.
- No artifact parent conflicts remained.
- Focused tests in disposable storage: **15 passed**, covering PostgreSQL constraints,
  migration rollback and populated upgrades, pipeline creation, permissions, model
  availability, and deletion.

No camera stream was started during verification because the database had no
configured pipelines. Registry availability is not an end-to-end inference benchmark.
These compatible safeguards retain deliberate denormalization; they do not claim
strict 4NF or full production certification.
