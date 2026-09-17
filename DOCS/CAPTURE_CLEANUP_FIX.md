# Capture cleanup and thumbnail fix

Deployed 2026-09-16 UTC.

The installed VideoFileCapture exposes disconnect(), not stop(). VMS invoked stop()
on video EOF, model-load failure, explicit shutdown, and camera reconnect cleanup.
All four sites now use a shared helper preferring disconnect() and falling back to
stop() for older adapters.

The reported thumbnail generation requests arrived after the runtime was removed.
The existing registered thumbnail was AVAILABLE/PASSED and readable in the application
logs. The generate endpoint now returns HTTP 409 with instructions to start the
pipeline and wait for a frame when no live frame is available, including whether an
existing registered thumbnail is available. It does not delete or overwrite that image.

Validation: 70 regression tests passed, followed by 5 focused tests after adding
model-load failure coverage and correcting the test source configuration. Tests include
real VideoFileCapture playback through EOF, readable thumbnail preservation, repeated
cleanup, legacy source support, and the actual thumbnail route executed in an isolated
Flask app. The live application returned HTTP 200 from /health after recreation;
both deployed file hashes match the workspace.

Deployed only InferenceNode/pipeline.py and InferenceNode/inference_node.py.
Image: armyeye-vms:capture-cleanup-20260916t053419z
Rollback image: armyeye-vms:before-capture-cleanup-20260916t053419z

No database or destination configuration was changed. The affected pipeline was in
error/stopped state during deployment; restart it to exercise real detection processing.
A non-looping video file still stops normally at EOF; this fix does not enable looping.
