# Media inspection — 2026-09-23

Inspected `/media` and its backing upload/list/delete/source-selection paths on release `armyeye-vms:publisher-20260923t072108z`. The page is an admin-only inventory and delete surface. It has no upload or download button: uploads originate in Pipeline Builder, while the media registry stores metadata in `media_assets` and bytes under the managed artifact root.

| Action | Route | Behavior |
| --- | --- | --- |
| Page and refresh | GET `/media`, GET `/api/media` | Admin-only registry list, hash prefix, status, size and pipeline references |
| Select video in Builder | GET `/api/media/sources` | Available source choices and relative paths |
| Upload from Builder | POST `/api/media/upload-video` | Admin + CSRF; staging, hash, file promotion and registry lifecycle |
| Delete | DELETE `/api/media/<id>` | Admin + CSRF; refuse references, move to trash, delete row, purge |
| Confirm/Cancel | Local modal | Confirmation required; in-use entries disabled in the UI |

## Reproduced findings

1. Non-video bytes with an MP4 extension were accepted and marked AVAILABLE/PASSED. Storage integrity did not establish decodability.
2. A deleted physical file remained AVAILABLE/PASSED in the listing until another consumer checked integrity.
3. A failed move during deletion left the row DELETING and the still-present file unservable, while the API said it was unchanged.
4. A network camera URL ending in the same basename was incorrectly counted as a file reference.
5. A pipeline reference inserted after the delete reference check did not prevent file deletion; the saved pipeline pointed at a missing file.
6. Two simultaneous uploads choosing the same timestamp/filename produced one success and one database uniqueness failure.

All six were reproduced in disposable PostgreSQL/filesystem tests. Code review also found unsequenced list requests, unsafe dynamic notification strings, and a shared confirmation modal that could be reused while deletion was pending. The original listing performed a separate pipeline-reference scan per asset; that performance characteristic remains, as this release focuses on correctness.

Normal path confinement, admin/CSRF protection, upload audit metadata, hash verification on use, referenced-delete refusal, and trash/file recovery have existing test coverage. A force-delete API option deliberately bypasses reference protection; the page does not offer it.

## Remediation

The user subsequently authorized fixes and deployment for media, admin users and telemetry together. See [the combined fix report](ADMIN_TELEMETRY_MEDIA_FIX_20260923.md).

Limits: first-frame decoding establishes that the upload opens and yields a frame, not that every frame in a long video is valid. Existing registry entries are checked for file integrity during listing; they are not all re-decoded as part of this release. No production media was uploaded or deleted during testing.
