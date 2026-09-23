# Pipeline control fix — deployed 2026-09-23

Inference and publisher controls now remain editable while stopped. They show
saved values, are disabled during startup or a pending save, and restore saved
values on request failure. A toggle does not start a pipeline. Running pipelines
continue to apply these settings live through the existing persistence API.

Both initial and incremental inference rendering use saved state. Stopping a
pipeline no longer forces its local inference setting off. Auto-disabled
publishers retain their error details and offer Retry / Re-enable. Manual
re-enabling clears the live failure latch and any frame-limit pause.

Five JavaScript tests and 62 publisher/delivery tests passed. Deployment replaced
only pipeline_management.html and pipeline.py in the previous production VMS image.
The receiver was not restarted. Health, deployed hashes, environment and data
mounts verified successfully. Read-only page/API and receiver TLS/auth checks
returned HTTP 200; both pipelines remained stopped. Saved toggles were not changed.

Release: armyeye-vms:pipeline-controls-20260923t044126z.
Rollback and image metadata: backups/pipeline-controls-release.json.
Refresh an already-open management page to load the new controls.
