# Pipeline credential encryption

Pipeline writes now encrypt password/token/credential fields and URL user information (`user:password@host`) before storing `pipelines.config`. Nested secret objects/lists are covered. Model IDs and ordinary configuration values retain their original JSON types, preserving PostgreSQL reference constraints.

Repository reads decrypt for authorized service/runtime consumers; existing API sanitization still redacts responses. Editing redacted forms, explicit clearing, duplication and exports continue through the same service paths. Keys use the existing dedicated, versioned configuration key file, not the Flask session key.

Startup runs an atomic, idempotent conversion of existing rows before constructing the pipeline manager. Rows are locked during conversion. Existing ciphertext is verified first. A missing/wrong key fails startup or the affected operation rather than discarding credentials or writing plaintext. Repeated migration leaves existing ciphertext untouched. Saving with a rotated key encrypts using the active key; retain old keys until all corresponding rows have been rewritten.

The registry verifier now reports pipelines with undecryptable credentials without including secret values.

## Activation and rollback

These changes require deploying the updated application. They introduce no new SQL columns or Alembic revision. Startup converts existing pipeline JSON using the deployment's existing key file; back up the database and securely retain that key before activation.

Do not run old and new application versions concurrently against the same pipeline tables during conversion. An older image does not understand encrypted pipeline configuration. After conversion, do not roll back only the application image: use a compatible release or restore the pre-conversion database backup with its matching key. A backup restore must account for writes made after deployment.

Existing backups, PostgreSQL WAL/history and legacy JSON files are not retroactively encrypted by this change. Keep them protected under the existing backup/access controls. Values under arbitrary non-secret keys, URL query parameters and opaque connection strings are not automatically classified as credentials.

## Validation

Tests cover database ciphertext, exact runtime round trips, redacted API responses, existing-row conversion, missing-key failure without data loss, repeat migration, key rotation and real PostgreSQL reference preservation. Final test results are recorded after execution.

Validation completed: **968 passed, 34 skipped** in the full isolated suite; **51 passed** in the final focused run, including the dedicated PostgreSQL migration and updated registry verifier. No production data was changed and this encryption change has not been deployed.
