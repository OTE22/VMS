# Video upload selection — deployed

Upload now adds the returned canonical media reference to the picker before selecting it. A successful upload hides the path picker and displays the selected filename, with an option to choose another saved video. The pipeline can be saved without re-entering a path.

Older media-list responses cannot overwrite the uploaded selection. Upload failures remain retryable, missing registry references are rejected, and pipeline submission waits for an active upload. File-input browser paths are excluded from saved configuration.

Verification: **57 frontend checks passed**, including missing-option selection, stale media responses, failures and file-input exclusion. **19 production page/API checks returned HTTP 200**, and both pipeline states were preserved. No real video was added to production for testing; no full browser automation was available.

Release: `armyeye-vms:video-upload-20260921t103353z`.

Rollback: `armyeye-vms:before-video-upload-20260921t103353z`.

Only the Builder template was deployed over the existing production image. Pending pipeline credential encryption was not included; no database migration was applied.
