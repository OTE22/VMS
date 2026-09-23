# Publisher fixes — 2026-09-23

Follow-up to [the publisher audit](PUBLISHER_AUDIT_20260923.md).

## Implemented

- Config edits fail with HTTP 503 when existing credentials cannot be decrypted; the transaction preserves the original ciphertext and metadata.
- PostgreSQL transaction advisory locking serializes favorite name checks and updates. Updates also lock the target row before reading/decrypting it. Simultaneous creates or renames cannot bypass the case-insensitive name check; conflicts return HTTP 409. Existing duplicate names are not automatically removed.
- Favorite API writes validate known/available destination types, required fields, supported field types, numeric bounds, selections and config shape without connecting to destinations. Malformed headers/input return HTTP 400.
- Partial config updates preserve omitted fields. The page explicitly sends `replace_config: true` for a full form. Redacted secret echoes retain current credentials within the same type; changing destination type starts from an empty config and cannot inherit old credentials.
- Webhook tests require selection of an existing pipeline. The route supplies its ID in the delivery envelope and context. This does not start the pipeline. The receiver can still reject a test message that does not satisfy its payload requirements; actual destination errors are shown.
- Missing selected favorite IDs are included as individual failures, rather than silently omitted from overall success.
- Shared browser mutation guards prevent overlapping saves, tests and deletes. Save fields are disabled during operations. Edit, Cancel and type selection cannot change a pending draft.
- Favorite refreshes ignore stale responses and show load errors. Selected test checkboxes survive list rendering. Edit population is synchronous, eliminating the stale timer after Cancel.
- Dynamic notifications are escaped before reaching the shared HTML notification renderer. Favorite buttons use data attributes and encoded request paths.
- Test counters count per-destination outcomes for this page session and are labelled accordingly. They do not claim to represent production delivery history.

## Verification

- 65 backend/store/encryption/webhook/form tests passed using disposable databases and the release image; one unrelated undeployed pipeline-encryption test is excluded.
- 49 JavaScript tests passed, including seven new publisher interaction regressions.
- Regression checks include missing-key ciphertext preservation, serialized password updates, duplicate-name conflicts, invalid input without database rows, partial updates/type changes, missing test IDs, and validated pipeline context for webhook tests.
- Existing stored-favorite and normal field round-trip checks remain passing. A prior test that expected credentials to survive a type change was updated to enforce the new explicit isolation between types.

## Scope

Release contains only `InferenceNode/inference_node.py`, `InferenceNode/publisher_store.py`, `ResultPublisher/config_validation.py`, and `InferenceNode/templates/publisher.html`. No database schema migration, environment change, or detection/tracking change is included. The unrelated pending pipeline-secret startup migration is excluded from the release.

Tests use isolated records, local test destinations and mocked delivery where appropriate. No production favorite was created, edited or deleted by verification, and no production receiver test message was sent.

Release hashes and rollback image are recorded in `backups/publisher-release.json`.

## Verified deployment

Activated `armyeye-vms:publisher-20260923t072108z`. Container health, image ID, four source hashes, environment and mounts verified. Live publisher page, favorites API, destination schemas and health returned HTTP 200; the page serves the new controls. Existing models, builder, management and media checks passed, as did receiver TLS health. Both pipelines remain stopped. Rollback image: `armyeye-vms:before-publisher-20260923t072108z`.
