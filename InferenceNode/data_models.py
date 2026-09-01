"""ArmyEye pipeline/model config tables (same DB + Base as auth, native FKs).

PostgreSQL is the ONE persistent source of truth for pipelines. `config` holds the
complete definition needed to reconstruct a runtime pipeline; transient runtime state
(threads, streams, loaded engines, FPS) is never stored here.

Authorization lives in `pipeline_user_access`, NOT in `owner_id`: owner_id/owner_username
are creator metadata only and must never be consulted when deciding access.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (Column, Integer, String, Text, Boolean, DateTime, BigInteger,
                        ForeignKey, Index, UniqueConstraint, CheckConstraint)
from sqlalchemy.types import JSON

from .auth.models import Base  # shared metadata -> one Alembic history
from .artifact_states import (STATUS_VALUES, VALIDATION_VALUES, ORIGIN_VALUES, KIND_VALUES)


def _in(col: str, values) -> str:
    """CHECK (col IN (...)) generated from the central enum - the DB and the ORM can
    never disagree about the allowed vocabulary."""
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


# Portable across the SQLite unit-test DB and PostgreSQL: 64 chars, all lowercase hex.
# (Alembic 0004 additionally applies the exact regex CHECK on PostgreSQL.)
_HEX = "0123456789abcdef"
_SHA256_CHECK = ("sha256 IS NULL OR (length(sha256) = 64 AND "
                 "sha256 = lower(sha256) AND "
                 "length(replace(replace(replace(replace(replace(replace(replace(replace("
                 "replace(replace(replace(replace(replace(replace(replace(replace(sha256,"
                 "'0',''),'1',''),'2',''),'3',''),'4',''),'5',''),'6',''),'7',''),'8',''),'9',''),"
                 "'a',''),'b',''),'c',''),'d',''),'e',''),'f','')) = 0)")

# Fingerprint columns captured at the last successful verification (artifact_states.
# fingerprint). Shared by every artifact table.
def _fingerprint_columns():
    return dict(
        verified_size_bytes=Column(BigInteger, nullable=True),
        verified_mtime_ns=Column(BigInteger, nullable=True),
        verified_ctime_ns=Column(BigInteger, nullable=True),
        verified_inode=Column(BigInteger, nullable=True),
        verified_device=Column(BigInteger, nullable=True),
    )


class Pipeline(Base):
    __tablename__ = "pipelines"

    id = Column(Integer, primary_key=True)
    pipeline_id = Column(String(255), nullable=False)          # stable public id (also sent in webhooks)
    # Creator metadata ONLY. Never used for authorization - see pipeline_user_access.
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    owner_username = Column(String(100), nullable=True)         # denormalized for display; never an identity
    name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    config = Column(JSON, nullable=False, default=dict)
    # Last known state. NOT proof that a runtime process exists - runtime status always
    # comes from PipelineManager and is merged on top of this at the API layer.
    status = Column(String(32), nullable=False, default="stopped")
    # CANONICAL model reference (0004 adds the nullable column; 0005 adds the FK ->
    # models.model_id ON DELETE RESTRICT + the consistency CHECK). config.model.id is a
    # serialized reflection kept in sync by PipelineRepository only.
    model_id = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("uq_pipelines_pipeline_id", "pipeline_id", unique=True),
        Index("idx_pipelines_owner", "owner_id"),
        Index("idx_pipelines_status", "status"),
        Index("idx_pipelines_model_id", "model_id"),
    )


class PipelineUserAccess(Base):
    """Per-user pipeline authorization. A normal user can only see or operate a
    pipeline through a row here; admins bypass assignment via their role.

    Both FKs CASCADE so deleting a user or a pipeline can never leave orphan grants.
    """
    __tablename__ = "pipeline_user_access"

    id = Column(Integer, primary_key=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    can_view = Column(Boolean, nullable=False, default=False)
    can_start = Column(Boolean, nullable=False, default=False)
    can_stop = Column(Boolean, nullable=False, default=False)
    can_edit = Column(Boolean, nullable=False, default=False)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("pipeline_id", "user_id", name="uq_pipeline_user_access"),
        Index("idx_pua_pipeline", "pipeline_id"),
        Index("idx_pua_user", "user_id"),
        Index("idx_pua_created_by", "created_by_id"),
    )


class AppState(Base):
    """Small key/value store for APPLICATION state (as opposed to schema state).

    Deliberately separate from alembic_version: Alembic tracks schema migrations, and
    must not be used as evidence that a DATA migration completed. Also deliberately not
    inferred from table emptiness or from a backup file existing.
    """
    __tablename__ = "app_state"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModelRecord(Base):
    """Logical model - PostgreSQL is the AUTHORITATIVE registry (models_metadata.json is a
    migration source / export format only after cutover). Bytes live under ARTIFACT_ROOT
    as ModelArtifact rows grouped into ModelRepresentation rows."""
    __tablename__ = "models"

    id = Column(Integer, primary_key=True)
    model_id = Column(String(255), nullable=False)          # logical id used by pipelines
    uploader_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    uploader_username = Column(String(100), nullable=True)
    name = Column(String(255), nullable=True)
    engine_type = Column(String(100), nullable=True)
    filename = Column(String(255), nullable=True)
    path = Column(String(512), nullable=True)               # DEPRECATED: superseded by model_artifacts.relative_path
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # ---- registry extension (0004)
    description = Column(Text, nullable=True)
    task = Column(String(64), nullable=True)
    framework = Column(String(64), nullable=True)
    version = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="STAGING")             # lifecycle
    validation_status = Column(String(32), nullable=False, default="PENDING")  # validation outcome
    reason = Column(String(64), nullable=True)                                # diagnostic
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("uq_models_model_id", "model_id", unique=True),
        Index("idx_models_uploader", "uploader_id"),
        Index("idx_models_engine_type", "engine_type"),
        Index("idx_models_status", "status"),
        CheckConstraint(_in("status", STATUS_VALUES), name="ck_models_status"),
        CheckConstraint(_in("validation_status", VALIDATION_VALUES), name="ck_models_validation_status"),
    )


class ModelRepresentation(Base):
    """One physical representation of a logical model (pt / onnx / openvino / engine...).
    A multi-file representation (OpenVINO xml+bin) has one row here and one ModelArtifact
    row PER FILE; manifest_sha256 = sha256 of the sorted "relative_path\\nsize_bytes\\nsha256"
    lines - never a directory name."""
    __tablename__ = "model_representations"

    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    format = Column(String(32), nullable=False)
    kind = Column(String(16), nullable=False, default="primary")            # primary | derived
    required = Column(Boolean, nullable=False, default=False)               # ALL required must be AVAILABLE
    precision = Column(String(16), nullable=True)
    device_family = Column(String(32), nullable=True)
    runtime = Column(String(32), nullable=True)
    manifest_sha256 = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="STAGING")
    validation_status = Column(String(32), nullable=False, default="PENDING")
    reason = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_verified_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_model_repr_model", "model_id"),
        CheckConstraint(_in("status", STATUS_VALUES), name="ck_model_repr_status"),
        CheckConstraint(_in("validation_status", VALIDATION_VALUES), name="ck_model_repr_validation_status"),
        CheckConstraint(_in("kind", KIND_VALUES), name="ck_model_repr_kind"),
        CheckConstraint(_SHA256_CHECK.replace("sha256", "manifest_sha256"),
                        name="ck_model_repr_manifest_sha256"),
    )


class ModelArtifact(Base):
    """ONE PHYSICAL FILE = ONE ROW. Identity/integrity contract:
    relative_path (under ARTIFACT_ROOT/models) + sha256 + size_bytes + status."""
    __tablename__ = "model_artifacts"

    id = Column(Integer, primary_key=True)
    model_id = Column(Integer, ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
    representation_id = Column(Integer, ForeignKey("model_representations.id", ondelete="CASCADE"),
                               nullable=False)
    relative_path = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    status = Column(String(16), nullable=False, default="STAGING")
    validation_status = Column(String(32), nullable=False, default="PENDING")
    reason = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_verified_at = Column(DateTime, nullable=True)
    meta = Column(JSON, nullable=True)
    verified_size_bytes = Column(BigInteger, nullable=True)
    verified_mtime_ns = Column(BigInteger, nullable=True)
    verified_ctime_ns = Column(BigInteger, nullable=True)
    verified_inode = Column(BigInteger, nullable=True)
    verified_device = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("uq_model_artifacts_path", "relative_path", unique=True),
        Index("idx_model_artifacts_model", "model_id"),
        Index("idx_model_artifacts_repr", "representation_id"),
        CheckConstraint(_in("status", STATUS_VALUES), name="ck_model_artifacts_status"),
        CheckConstraint(_in("validation_status", VALIDATION_VALUES), name="ck_model_artifacts_validation_status"),
        CheckConstraint(_SHA256_CHECK, name="ck_model_artifacts_sha256"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_model_artifacts_size"),
    )


class InferenceEngineRecord(Base):
    """Registry for inference engines. origin=builtin: shipped read-only in the image
    (relative_path/sha256 NULL, shipped_* recorded); origin=custom: an executable .py
    artifact under ARTIFACT_ROOT/engines with the full integrity contract.
    Invariant (DB CHECK): enabled => status=AVAILABLE AND validation_status=PASSED."""
    __tablename__ = "inference_engines"

    id = Column(Integer, primary_key=True)
    engine_key = Column(String(128), nullable=False)
    class_name = Column(String(255), nullable=True)
    display_name = Column(String(255), nullable=True)
    engine_type = Column(String(64), nullable=True)
    version = Column(String(64), nullable=True)
    origin = Column(String(16), nullable=False, default="custom")
    status = Column(String(16), nullable=False, default="STAGING")
    validation_status = Column(String(32), nullable=False, default="PENDING")
    reason = Column(String(64), nullable=True)
    enabled = Column(Boolean, nullable=False, default=False)
    relative_path = Column(String(1024), nullable=True)
    sha256 = Column(String(64), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    shipped_version = Column(String(64), nullable=True)
    shipped_sha256 = Column(String(64), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_verified_at = Column(DateTime, nullable=True)
    meta = Column(JSON, nullable=True)
    verified_size_bytes = Column(BigInteger, nullable=True)
    verified_mtime_ns = Column(BigInteger, nullable=True)
    verified_ctime_ns = Column(BigInteger, nullable=True)
    verified_inode = Column(BigInteger, nullable=True)
    verified_device = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("uq_inference_engines_key", "engine_key", unique=True),
        Index("uq_inference_engines_path", "relative_path", unique=True),
        CheckConstraint(_in("origin", ORIGIN_VALUES), name="ck_engines_origin"),
        CheckConstraint(_in("status", STATUS_VALUES), name="ck_engines_status"),
        CheckConstraint(_in("validation_status", VALIDATION_VALUES), name="ck_engines_validation_status"),
        CheckConstraint(_SHA256_CHECK, name="ck_engines_sha256"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_engines_size"),
        CheckConstraint("enabled = FALSE OR (status = 'AVAILABLE' AND validation_status = 'PASSED')",
                        name="ck_engines_enabled_requires_available_passed"),
        CheckConstraint("origin <> 'custom' OR (relative_path IS NOT NULL AND sha256 IS NOT NULL)",
                        name="ck_engines_custom_requires_artifact"),
    )


class Publisher(Base):
    """Publisher favorites and node-level destinations. Replaces node_settings.json.
    Secret-looking keys inside `config` are stored ENCRYPTED (see config_secrets)."""
    __tablename__ = "publishers"

    id = Column(Integer, primary_key=True)
    publisher_id = Column(String(36), nullable=False)
    name = Column(String(255), nullable=True)
    type = Column(String(64), nullable=False)
    kind = Column(String(32), nullable=False, default="favorite")     # favorite | node_destination
    enabled = Column(Boolean, nullable=False, default=True)
    config = Column(JSON, nullable=False, default=dict)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("uq_publishers_publisher_id", "publisher_id", unique=True),
        Index("idx_publishers_kind", "kind"),
        CheckConstraint("kind IN ('favorite', 'node_destination')", name="ck_publishers_kind"),
    )


class NodeSetting(Base):
    """Persistent node configuration (replaces node_settings.json). One row per validated
    key; `value` is validated per key in code, never a free-form dump."""
    __tablename__ = "node_settings"

    key = Column(String(64), primary_key=True)
    value = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("key IN ('node_identity', 'telemetry', 'preferences')", name="ck_node_settings_key"),
    )


class MediaAsset(Base):
    """Registry for uploaded videos/images; bytes stay under ARTIFACT_ROOT/media."""
    __tablename__ = "media_assets"

    id = Column(Integer, primary_key=True)
    media_id = Column(String(36), nullable=False)
    relative_path = Column(String(1024), nullable=False)
    original_filename = Column(String(255), nullable=True)
    media_type = Column(String(32), nullable=True)
    sha256 = Column(String(64), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    duration = Column(Integer, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    status = Column(String(16), nullable=False, default="STAGING")
    validation_status = Column(String(32), nullable=False, default="PENDING")
    reason = Column(String(64), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_verified_at = Column(DateTime, nullable=True)
    verified_size_bytes = Column(BigInteger, nullable=True)
    verified_mtime_ns = Column(BigInteger, nullable=True)
    verified_ctime_ns = Column(BigInteger, nullable=True)
    verified_inode = Column(BigInteger, nullable=True)
    verified_device = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("uq_media_assets_media_id", "media_id", unique=True),
        Index("uq_media_assets_path", "relative_path", unique=True),
        CheckConstraint(_in("status", STATUS_VALUES), name="ck_media_status"),
        CheckConstraint(_in("validation_status", VALIDATION_VALUES), name="ck_media_validation_status"),
        CheckConstraint(_SHA256_CHECK, name="ck_media_sha256"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_media_size"),
    )


class PipelineThumbnail(Base):
    """Thumbnails are managed artifacts too: relative_path + sha256 + size + status.
    'file exists' is never proof that it is the registered thumbnail. Pipeline deletion
    coordinates the JPEG via the managed trash flow - the FK cascade removes only the row."""
    __tablename__ = "pipeline_thumbnails"

    id = Column(Integer, primary_key=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id", ondelete="CASCADE"), nullable=False)
    relative_path = Column(String(1024), nullable=False)
    sha256 = Column(String(64), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    status = Column(String(16), nullable=False, default="STAGING")
    validation_status = Column(String(32), nullable=False, default="PENDING")
    reason = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_verified_at = Column(DateTime, nullable=True)
    verified_size_bytes = Column(BigInteger, nullable=True)
    verified_mtime_ns = Column(BigInteger, nullable=True)
    verified_ctime_ns = Column(BigInteger, nullable=True)
    verified_inode = Column(BigInteger, nullable=True)
    verified_device = Column(BigInteger, nullable=True)

    __table_args__ = (
        Index("uq_pipeline_thumbnails_path", "relative_path", unique=True),
        Index("idx_pipeline_thumbnails_pipeline", "pipeline_id"),
        CheckConstraint(_in("status", STATUS_VALUES), name="ck_thumbs_status"),
        CheckConstraint(_in("validation_status", VALIDATION_VALUES), name="ck_thumbs_validation_status"),
        CheckConstraint(_SHA256_CHECK, name="ck_thumbs_sha256"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_thumbs_size"),
    )
