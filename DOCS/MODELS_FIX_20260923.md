# Models fixes — 2026-09-23

Follow-up to [the models audit](MODELS_AUDIT_20260923.md).

## Changes

- Geti deployment ZIPs can be stored. Archive paths, CRC, unpacked size, and required XML/BIN entries are checked before registration. The unsupported `.pth` option is removed; it returns a validation error rather than a storage failure.
- Uploads reject empty files, unavailable engines, incompatible extensions, malformed checkpoint containers and invalid ONNX models. No uploaded checkpoint is unpickled during upload validation.
- Identical uploads reuse an existing verified record without replacing its name, description, engine, or uploader. Conflicting engine/content or unavailable existing records return HTTP 409.
- PostgreSQL advisory transaction locks serialize same-model ingestion and deletion across workers, preventing shared staging path races. Stable model IDs and pipeline references are preserved.
- Downloads use separate temporary directories, a restricted pretrained model name, the same validation/storage path, and authenticated uploader identity. Temporary files are cleaned up after success and failure.
- Upload and Download share a pending guard. Form controls stay disabled until completion, preventing duplicate operations and edits that completion would erase.
- Model refreshes discard stale responses. Delete uses HTML data attributes and encoded request paths, handles special characters, and guards repeated clicks. Notification strings are escaped.
- The green badge says **Stored** and explicitly distinguishes storage integrity from runtime compatibility.

## Validation

- 109 backend/PostgreSQL tests passed, covering repository lifecycle and failure recovery, registry migration, model labels/schema, authenticated form persistence, repeated and concurrent uploads, Geti ZIP validation, uploader attribution and deletion with special characters.
- 48 JavaScript checks passed, including six new models interaction regressions and existing API/form contracts.
- One unrelated pipeline encryption test is excluded because that migration is not part of the deployed release.
- Tests use disposable databases and model artifacts. Download tests stub the upstream transfer; they exercise the actual route, filesystem cleanup and PostgreSQL persistence.

## Deployment scope and limits

The release copies only `inference_node.py`, `model_repo.py`, the new `model_uploads.py`, and `templates/models.html` over the previous running image. The existing undeployed pipeline-secret migration remains excluded. Database schema, environment, storage mounts and detection/tracking logic are unchanged.

Successful storage does not guarantee inference compatibility. Model initialization, GPU execution and third-party download availability require runtime validation; these checks do not start production pipelines or claim every model format has been fully decoded. Geti checks establish a structurally valid deployment archive, not a successful OpenVINO runtime load.

Deployment image IDs, file hashes and rollback image are recorded in `backups/models-release.json`.

## Verified deployment

Activated `armyeye-vms:models-20260923t064913z`. Container health, image identity, all four source hashes, environment and storage mounts passed verification. Authenticated `/models`, `/api/models`, `/api/inference/engines` and `/health` returned HTTP 200; the served models page contains the new controls. Builder, management, media, pipeline listing and receiver TLS health checks also passed. Both existing pipelines remain stopped. Rollback image: `armyeye-vms:before-models-20260923t064913z`.
