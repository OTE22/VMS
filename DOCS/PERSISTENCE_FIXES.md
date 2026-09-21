# Persistence and upload fixes

## Behavior

- Node settings, telemetry settings, and node publisher rows saved by `_save_settings()` now use one database transaction. A failed write or encryption failure aborts the transaction and reaches the route's error handler; it no longer returns a successful save response. Caller-owned sessions are passed explicitly through stores.
- Log preferences are saved in `node_settings`, key `preferences`, field `logging`, and restored at startup. Node Info returns the actual log level. Failed node/log saves restore the previous node name and logging preferences in memory.
- A changed web port is rejected before mutation. The form shows it read-only because listening ports and Docker port mappings are deployment configuration.
- Publisher descriptions are stored in a nullable `publishers.description` column, returned in API responses, and supported by create/edit/clear and legacy import. Omitted descriptions preserve the previous value; submit an empty string to clear one.
- Model uploads stage in a unique temporary directory for each request, retaining the sanitized filename/extension. Both successful and failed requests clean up their directory. Concurrent uploads named `same.pt` no longer share staging bytes.
- Updating maximum log size now changes an existing rotation handler's `maxBytes`, rather than just changing the displayed setting.

## Activation

The change includes Alembic revision `0008_publisher_description`, following `0007_reference_integrity`. It adds a nullable column without replacing existing publisher rows. The updated application's database bootstrap upgrades to the current migration head on startup. Deploy the updated application and migration files together through the normal deployment process; do not run the updated publisher code against a database still on revision 0007.

Implementation tests did not modify the running deployment or production database; the subsequently authorized deployment is recorded below. Tests use disposable containers, a temporary repository copy without `.env`, and an isolated PostgreSQL server with no external network access.

## Limits

Database rollback does not reverse external MQTT connections or already-applied node publisher/telemetry runtime operations. Those routes now report save failure, but runtime reconciliation after such a failure remains separate work. The existing MQTT reconfiguration/clear behavior, log age-based retention implementation, and other audit findings outside these fixes are not claimed resolved. Retention preference values now persist, but age-based deletion is still not implemented.

Previously discarded descriptions cannot be reconstructed from database rows that never contained them. Existing descriptions from legacy JSON are imported only where the existing one-time migration still runs.

## Regression coverage

`tests/test_settings_save_regressions.py` executes the application's actual save and route bodies without booting cameras or discovery. It checks transaction rollback, failure responses, log restore, favorite description round trips, concurrent same-name uploads, cleanup, port rejection, and rotation size updates. `tests/test_registry_schema_pg.py` also verifies a real PostgreSQL migration with an existing publisher, durable descriptions, and rollback.

Verification results: full isolated suite **939 passed, 33 skipped**; final targeted backend checks **14 passed**; final frontend contract/template checks **29 passed**. Skips include unavailable full-browser and Node-wrapper checks in the Python image; the frontend checks ran separately in a Node container. Temporary PostgreSQL was removed after testing. No live camera/broker or production deployment validation was performed.

## Deployment completed — 2026-09-21

- Release image: `armyeye-vms:persistence-20260921t091819z`.
- Rollback image retained: `armyeye-vms:before-persistence-20260921t091819z`.
- Database backup (custom-format pg_dump, private file, archive listing validated): `/home/itdirect-ai/Desktop/VMS/backups/pre-persistence-20260921t091819z.dump`.
- Only the VMS application container was recreated, using the existing production/GPU Compose configuration and unchanged environment. The database container was not recreated.
- Startup applied `0008_publisher_description`; the new column exists and both pipeline records are preserved.
- Post-deployment verification: container running/healthy, 19 authenticated page/API/health checks returned HTTP 200, public login works, unauthenticated models API returns 401, configuration key staged with mode 0400, RTX 5090 CUDA available. Both pipelines remained stopped, matching their pre-deployment state.
- The release layers current application code over the previous image, preserving the tested GPU dependencies. Browser interaction and live camera/broker tests were not performed during deployment.
- An application rollback can retain the additive description column; dropping/restoring the database is not required merely to run the previous application image.
