# Form correctness release — 2026-09-21

This release implements the functional corrections identified in [the field audit](FORM_FIELD_DATABASE_AUDIT.md). That document records the pre-fix findings; the status here supersedes its unresolved functional findings.

## Changes

- Pass pipeline creation normalizes empty model IDs to NULL. Switching an existing pipeline to Pass clears its canonical and JSON references together.
- Pipeline export reads primary model artifacts through the current registry, including associated files, and preserves `inference_enabled`. Import uses new model IDs, registers related files, and removes its new model if pipeline creation fails. Existing models are not overwritten. Uploaded ZIP names no longer determine staging paths; traversal entries are rejected. Derived representations are not exported; primary model bytes are preserved.
- Webhook header text is parsed and validated into a dictionary before saving; existing text records are accepted by the runtime normalizer. Both editors render saved maps as editable header lines.
- Serial accepts the schema's `baud_rate`, converts it to an integer, and remains compatible with saved `baud` configurations. New saves use `baud`.
- Publisher Test Message submits parsed JSON and selected favorites with the shared CSRF mechanism. The backend attempts each favorite synchronously, reports outcomes by favorite ID, releases temporary destinations, and reports partial failures instead of unconditional success.
- Publisher credentials have an explicit clear checkbox. Publisher and Builder preserve meaningful password whitespace. Empty input keeps an existing credential; explicit clear sends NULL.
- Telemetry validates input, saves desired settings before attempting broker activation, preserves a blank server's topic/port, restores those values on restart, and disconnects an explicitly cleared broker. A connection failure reports that configuration was saved but activation failed. Database save failures remain errors and restore prior in-memory configuration.
- Log retention prunes expired rotated application logs at initialization, on retention updates, and during logging at most hourly. The existing five-backup size-rotation cap remains in effect; active log files are not age-deleted.
- Missing destination schemas return a complete validation error. API Docs configuration buttons open the actual forms instead of posting hardcoded example settings.

## Verification

- The six audit expected-failure markers were removed; their scenarios now pass.
- Added export/import byte comparison, failed-import cleanup, individual favorite results, serial argument conversion, retention boundaries, blank-broker restart restoration, explicit credential clearing, and actual test-message submit-handler execution.
- Testing used a disposable PostgreSQL instance and synthetic artifacts. Production verification is read-only.
- Full-suite and deployment results are recorded below after completion.

## Remaining limits

This deployed image is a functional form release. Pipeline credential encryption has since been implemented and tested in the workspace ([details](PIPELINE_CREDENTIAL_ENCRYPTION.md)), but is not yet deployed; API redaction still applies, and publisher credentials remain encrypted. Camera/device availability, external MQTT delivery and cloud integrations require live integration tests. No full browser automation was available. Model/video test bytes prove storage integrity, not inference or decoding validity.

## Deployment completed

- Release: `armyeye-vms:forms-20260921t101145z`.
- Rollback image: `armyeye-vms:before-forms-20260921t101145z`.
- Private PostgreSQL backup: `/home/itdirect-ai/Desktop/VMS/backups/pre-forms-20260921t101145z.dump` (archive listing validated).
- Full isolated suite: **964 passed, 34 skipped**. Targeted checks: **37 passed**. Frontend: **52 passed**.
- Application running/healthy; **19 authenticated page/API checks returned 200**. Both existing pipelines remain stopped, matching pre-deployment state. RTX 5090 CUDA is available. Migration remains `0008_publisher_description`; this release adds no new schema migration.
- Only the VMS application container was recreated using unchanged production/GPU Compose environment. Database container was preserved. Release file hashes were checked before activation.
