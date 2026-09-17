# Database normalization review

Date: 2026-09-15

## Compatible remediation prepared

Following this audit, migration `0007_reference_integrity` and matching ORM constraints
were added to the codebase. The initial findings and live measurements below describe
the database before this migration.

- A composite artifact-to-representation foreign key enforces matching model ownership.
- Pipeline JSON model IDs must have a matching, non-null relational model reference.
  Model-less pipelines and canonical-column-only references remain supported, preserving
  existing repository behavior. This is one-way protection of JSON references, not a
  requirement to persist a JSON reflection in every row.
- The migration changes no payloads, IDs, columns, or data rows. Conflicting legacy rows
  cause an explicit failure and transaction rollback; they are not guessed or deleted.
- PostgreSQL startup now propagates migration failures instead of silently falling back
  to `create_all`, which cannot add missing constraints to existing tables. SQLite's
  development/test fallback is retained.
- Migration DDL has a five-second lock wait limit. Deployment still takes database locks;
  this is not a claim of zero-downtime operation on arbitrary database sizes.

The prepared change was subsequently deployed with user authorization on 2026-09-15.
The running database is now at `0007_reference_integrity`, and VMS was recreated with
the patched image. See [deployment evidence](DATABASE_INTEGRITY_DEPLOYMENT.md) for backup,
restore verification, health checks, and rollback references. Migration downgrade to
`0006_pipeline_node_assignment` removes only these new constraints and restores the old
pipeline check; it retains all rows and columns.

The deployed remediation closes the two high-priority integrity gaps. It retains
the redundant artifact model ID and JSON configuration for compatibility; it does **not**
claim strict 4NF or full relational decomposition of JSON destination collections.

Remediation validation: two disposable-container regression runs passed, **92/92** and
**93/93** test executions (the reference-integrity cases overlap between runs). PostgreSQL
tests used fixture-created databases and temporary artifact roots. Coverage included
new constraint rejection cases, parent updates and cascades, populated upgrade preservation,
legacy-conflict rollback, migration downgrade/re-upgrade, pipeline/model persistence,
authorization, auth service, artifact verification, worker assignment, and startup failures.
Python parsing and whitespace checks also passed, accounting for existing CRLF files.

## Verdict

The database is not fully normalized through 4NF. Do not sign off on production readiness from normalization alone. The relational foundation is sound, but the artifact ownership dependency and pipeline reference constraint need attention.

Scope: SQLAlchemy models, Alembic migrations 0001–0006, persistence code, and read-only queries against database `armeye` in the running `VMS-db` container. This identifies the local running deployment; it does not establish that it is the intended production server. No application data or schema was changed.

## Normal forms

| Form | Assessment | Evidence |
| --- | --- | --- |
| 1NF | Conditional; not fully decomposed under a strict scalar relational interpretation | Every application table has a primary key. However, `pipelines.config` stores nested model/source configuration and a collection of destinations. Treating JSON as an opaque document is a valid design choice, but does not normalize its internal entities. JSON alone is not proof of a violation. |
| 2NF | No partial-key dependency identified in the reviewed relational columns | The composite candidate key `(pipeline_id, user_id)` in `pipeline_user_access` identifies a grant; permissions and grant metadata concern the whole pair. Single-column surrogate IDs alone do not prove 2NF; other candidate keys and business dependencies also matter. Whole-schema strict 2NF remains conditional on the 1NF interpretation above. |
| 4NF | Fails the intended artifact ownership dependency | In `model_artifacts`, `representation_id` determines `model_id`, but is not a superkey: one representation can have multiple files. This violates BCNF and consequently 4NF. No additional independent multivalued-dependency violation was established; JSON arrays alone do not prove one. |

Although 3NF was not explicitly requested, its dependencies matter when assessing 4NF.

## Findings and proposed fixes

### High: redundant artifact ownership can contradict itself

Sources: `InferenceNode/data_models.py:197` and `InferenceNode/migrations/versions/0004_artifact_registry.py:110`.

Both `model_artifacts.model_id` and `model_artifacts.representation_id` have separate foreign keys. Neither enforces that the representation belongs to that model. An artifact can therefore reference model A directly and a representation of model B. Reads and deletion flows that filter by different parent IDs can disagree.

For strict normalization, remove the artifact's `model_id` and obtain it through `model_representations`. If retaining it for measured performance needs, add a composite foreign key `(representation_id, model_id)` referencing a unique `(id, model_id)` on representations. That enforces consistency but remains deliberate denormalization and does not restore strict 4NF.

### High: nullable canonical model reference bypasses protection

Source: `InferenceNode/migrations/versions/0005_pipeline_model_integrity.py`, `ck_pipelines_model_ref_consistent`.

The CHECK succeeds when either the relational model ID or JSON model ID is NULL. Consequently, a pipeline with `model_id = NULL` and a non-null JSON model ID passes, even if that JSON ID has no registered model. The foreign key only protects the relational column. Repository synchronization reduces the risk for normal application writes but does not close the database invariant.

Decide whether model-less pipelines are valid, validate JSON shape, and enforce agreement including NULL semantics. Prefer storing the reference once and reconstructing the serialized model ID on reads. Keep ORM metadata aligned with migrated constraints: the ORM currently declares `Pipeline.model_id` without the migration's FK or CHECK, so `Base.metadata.create_all()` tests do not exercise those protections.

### Medium: destination collections live inside pipeline JSON

Sources: `InferenceNode/data_models.py:60` and `tests/test_pipeline_persistence_contract.py:53`.

The persisted payload includes a `destinations` collection with IDs, types, enabled flags, and configuration. If destinations need relational identity and per-pipeline uniqueness, extract `pipeline_destinations` with a pipeline FK and a unique destination identity within the pipeline. If these are references to shared publishers, use a separate link table with a publisher FK; do not assume favorites are shared identities without establishing that business rule. Keep provider-specific configuration as JSON when it is intentionally handled as a document.

### Medium: distinguish historical snapshots from duplicated current values

Sources: `InferenceNode/data_models.py:57`, `InferenceNode/data_models.py:135`, and `InferenceNode/auth/models.py:64`.

Creator/uploader usernames duplicate user-related information. If they mean the current username, use joins; if they mean the name at creation/upload time, document that snapshot meaning. Audit actor snapshots intentionally survive deletion and should not automatically be replaced with a cascading foreign key. Deprecated model paths and serialized model metadata also need explicit canonical-source rules.

## Live verification

All queries used `BEGIN READ ONLY` and returned only schema information or aggregate counts.

- Migration: `0006_pipeline_node_assignment`, matching repository head.
- All 57 returned public-schema constraints had `convalidated = true`.
- Primary keys and unique indexes were present, including public pipeline/model IDs and `(pipeline_id, user_id)` access grants.
- Data volume: 0 pipelines, 0 access grants, 1 model, 1 representation, 1 artifact.
- Artifact/model ownership mismatches: 0.
- JSON-only, column-only, and conflicting pipeline model references: all 0, with no pipelines present.
- Username normalization mismatches: 0.
- Operating grants lacking view permission: 0, with no grants present.

These counts show no observed corruption in the checked rows. They cannot establish safety for future writes or realistic production workloads. A validated constraint only proves its actual predicate, including its NULL allowances.

## Before production sign-off

1. Resolve the two high-priority integrity findings using reviewed migrations and application updates.
2. Decide which JSON entities require relational enforcement and which are intentional documents or snapshots.
3. Run isolated PostgreSQL migration and negative-insert tests, including conflicting artifact parents and JSON-only model references. Never perform destructive constraint tests on the live database.
4. Separately verify backup restoration, recovery objectives, database privileges, monitoring, and load capacity. These operational areas were not audited here.

During the initial audit, automated tests were not run because the host Python environment
lacks SQLAlchemy. Live read-only checks used PostgreSQL's `psql` client in the database
container. The subsequent remediation tests ran successfully in disposable application
containers, as recorded above.
