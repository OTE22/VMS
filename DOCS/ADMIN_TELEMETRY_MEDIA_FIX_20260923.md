# Admin users, telemetry and media fixes — 2026-09-23

Follow-up to [the admin/telemetry audit](ADMIN_TELEMETRY_AUDIT_20260923.md) and [media inspection](MEDIA_AUDIT_20260923.md). The user explicitly authorized fixes and deployment for all three pages.

## Admin users

- User mutation transactions acquire a shared PostgreSQL advisory lock before row reads and last-active-admin checks. Concurrent cross-account demotions cannot remove every active administrator.
- Account-state and permission APIs require JSON booleans; malformed object shapes, invalid profile fields and excessive password byte lengths are rejected. Concurrent account conflicts return HTTP 409.
- A grant/revoke whose audit write fails still reports its saved outcome, with `audit_recorded:false`; the browser warns explicitly. Audit persistence is not represented as successful in that case.
- User-list responses are sequenced. Access modal responses are bound to their open generation and invalidated on close. Pending mutations lock controls and prevent switching or dismissing the associated modal.

## Telemetry

- The settings store refuses to overwrite credentials it cannot decrypt.
- A configuration lock serializes persistence and activation. Settings are validated and committed from a local desired object before runtime attributes change. A failed database save leaves runtime unchanged.
- Worker stop uses an interruptible event; restarts refuse to overlap a still-stopping worker. Stopping does not require connecting to the broker.
- GET configuration reports durable desired settings and separate runtime worker state. Activation failure reports `saved:true`, `runtime_applied:false`; the form displays a warning instead of claiming the save failed.
- Metric collection failures return unavailable status. Missing temperatures appear as chart gaps; stale/overlapping polls and initial reads overwriting dirty drafts are prevented. Save controls are guarded.

## Media

- Upload paths include a per-upload UUID, preventing same-second filename collisions. The HTTP upload path requires a successful first-frame decode before promotion to AVAILABLE.
- Listing refreshes integrity state for AVAILABLE assets, so missing or changed files are no longer labelled verified from stale metadata alone.
- Failed deletion restores the prior registry state when the file can be restored. Errors no longer promise the asset is unchanged in every failure mode.
- Relative references are preferred over basename fallbacks; network camera URLs do not count as file references.
- Media deletion and pipeline create/update share a PostgreSQL transaction advisory lock. New relative media references must point to an available registered file while holding that lock, closing the checked-then-deleted reference race. Unchanged existing references retain edit compatibility.
- Media lists discard stale responses. Delete confirmation cannot switch/dismiss while pending; dynamic notifications are escaped and button IDs use data attributes.

## Validation and release scope

- 129 account/authorization/encryption/form tests passed, including new PostgreSQL regressions for the identified races and failure modes. One unrelated undeployed pipeline-encryption test is excluded.
- 74 media/library/registry/thumbnail/audit tests passed.
- 69 JavaScript checks passed, including seven new interaction regressions.
- Both backend groups are rerun against the exact release image before activation.
- Some prior media fixtures intentionally used nonexistent source files or fixed filenames. They now register their fixture media and assert canonical returned paths rather than assuming collisions are possible.

The release copies 12 reviewed source/template files over the running image. The pipeline repository is staged from the deployed version with only the media coordination additions. The unrelated pipeline-secret startup migration remains excluded. No database schema migration, environment change, detection/tracking change, or production test account/media mutation is included.

No production MQTT messages or camera runs were used for verification. Runtime broker availability and full-video decoding are not guaranteed by these tests. Release image/hash/mount metadata and rollback tag are in `backups/admin-telemetry-media-release.json`.

## Verified deployment

Activated `armyeye-vms:admin-telemetry-media-20260923t075737z`. Container health, image ID, all 12 file hashes, environment and mounts verified. Admin, telemetry and media pages/APIs returned HTTP 200 and serve the new controls. Models, publisher, builder, management and receiver TLS checks passed. Both existing pipelines remain stopped. Rollback tag: `armyeye-vms:before-admin-telemetry-media-20260923t075737z`.
