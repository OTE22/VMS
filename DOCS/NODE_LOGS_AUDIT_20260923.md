# Node information and logs review — 2026-09-23

## Changes

- Both pages use the contrast-correct shared workspace and an operations stylesheet. Explicit dark table cells and disabled/read-only input backgrounds prevent Bootstrap's light surfaces from hiding light text. Card titles, labels, controls, loading/error states and mobile layouts were reviewed in Firefox using synthetic data.
- Node polling preserves unsaved configuration and ignores stale responses after a save/restart starts. Saves and restarts reject duplicate clicks. Configuration remains disabled until loaded; non-administrators see read-only controls.
- Hardware strings are escaped before HTML insertion; missing storage percentages no longer crash rendering. API port refreshes from the actual config. Status labels distinguish CPU percentage, active pipelines, buffered errors and API connectivity, rather than fabricated inference/error counts or a mislabeled load average.
- Node saves validate name, level and deployment-owned port before mutation. Their database transaction now touches only node identity and logging preferences, avoiding unrelated telemetry/publisher rewrites. Existing failure rollback is retained.
- Logs support pause/resume, search (including logger and exception details), filtering, escaped expandable details and downloading raw text. Critical entries count toward errors; system entries have a source count. Refresh failures preserve the last snapshot and mark it stale. A stale fetch cannot restore cleared entries.
- Clear explicitly means in-memory buffer only; it does not delete saved log files. Only administrators can clear or change settings. Existing backend admin/CSRF requirements remain enforced.
- Logging payloads reject empty/unknown/invalid fields with 400; API size/retention bounds match the form. Runtime apply failures restore the previous settings and do not persist success. Console handler level now follows the selected level; enabling file logging cannot return success without a handler. Retention is applied before handler creation. Log timestamps include UTC offset.

## Data paths

- `GET /api/node/info`: live OS/hardware/runtime data and configuration. `POST /api/node/config`: PostgreSQL `node_settings.node_identity`, plus `preferences.logging` when a level is submitted. Web port remains deployment-managed.
- `GET /api/logs`: bounded, in-memory log history (not PostgreSQL). `POST /api/logs/clear`: clears this buffer. Download saves the filtered snapshot in the browser.
- `GET/POST /api/logs/settings`: runtime logging configuration, persisted in PostgreSQL `node_settings.preferences.logging`; startup restores this configuration. File rotation applies to server log files, not the memory buffer.
- Export produces a JSON configuration/hardware snapshot. Restart remains an explicit, confirmed operation; it was not triggered on production during inspection.

## Verification

- 9 JavaScript regressions cover drafts, failed/duplicate saves, HTML escaping, JSON headers, raw downloads, critical counts and stale clear responses.
- 42 focused Python persistence/rollback/layout checks passed. Four unrelated legacy cases were excluded; two legacy upload fixtures failed in an initial broader run because they submit invalid model bytes, outside this change.
- 8 exact-candidate PostgreSQL/Flask checks passed using a disposable database: template rendering, node/log save-and-read-back, validation, CSRF, clearing and metrics. No production write probes.
- Firefox desktop and 390px-width previews used synthetic records, not live user data.
- Release stages only the four audited route bodies over the running backend; unrelated pending pipeline-encryption startup migration is excluded.

## Deployment

Deployed `armyeye-vms:node-logs-20260923t094114z`. Verified image and source hashes, unchanged environment/mounts, healthy service, and HTTP 200 for both pages, their read APIs and the new stylesheet. Release record: `backups/node-logs-release.json`. Only the final stylesheet changed after the candidate PostgreSQL run.
