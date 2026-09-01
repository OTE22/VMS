"""Physical migration of legacy artifacts into ARTIFACT_ROOT (Phase 5).

Metadata migration alone is insufficient: the bytes move too, and a PostgreSQL row is
created ONLY for bytes that verifiably exist under ARTIFACT_ROOT. Per legacy artifact:

    discover legacy path
    -> validate (regular FILE; recognised multi-file representations are ENUMERATED into
       their component files - a directory is never itself an artifact)
    -> sha256 + size of the SOURCE (per physical file)
    -> copy into <kind>/.staging/<relative_path>   (collision-safe: an existing destination
       with a DIFFERENT hash is FAILED, never overwritten)
    -> fsync staged file
    -> sha256(destination) == sha256(source), sizes equal   (else FAILED, staged copy removed)
    -> atomic rename into the final location; fsync parent dir
    -> caller registers the canonical relative_path + sha256 + size (AVAILABLE)
    -> legacy file RETAINED (rollback) - nothing is deleted in this release

Legacy paths that cannot be uniquely resolved -> status=MISSING with a formal reason
(LEGACY_FILE_NOT_FOUND / AMBIGUOUS_LEGACY_PATH); never AVAILABLE, never guessed.

Completion semantics (see registry_migration): a marker means every discovered legacy
record was DETERMINISTICALLY PROCESSED and recorded with an explicit fail-closed state -
NOT that every artifact became AVAILABLE. MISSING/ambiguous are processed records;
unexpected exceptions, copy failures, hash mismatches, DB failures and unrecorded
artifacts block the marker.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import artifact_paths as ap
from .artifact_states import ArtifactStatus, Reason, ValidationStatus, fingerprint

logger = logging.getLogger("InferenceNode.artifact_migration")

_CHUNK = 1024 * 1024


def sha256_file(path: str) -> Tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def manifest_sha256(components: List[Tuple[str, int, str]]) -> str:
    """Deterministic representation hash: sha256 over the SORTED lines
    "relative_path\\nsize_bytes\\nsha256". Never a directory name."""
    lines = sorted(f"{rel}\n{size}\n{sha}" for rel, size, sha in components)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass  # Windows / some filesystems: directory fsync unsupported


@dataclass
class MigratedFile:
    relative_path: str
    sha256: str
    size_bytes: int
    status: ArtifactStatus
    validation_status: ValidationStatus
    reason: Optional[Reason] = None
    source: Optional[str] = None
    fingerprint: Optional[dict] = None


@dataclass
class MigrationReport:
    discovered: int = 0
    processed: int = 0
    available: int = 0
    missing: int = 0
    ambiguous: int = 0
    failed: int = 0
    unregistered: List[str] = field(default_factory=list)
    details: List[dict] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """failed > 0 blocks the completion marker; missing/ambiguous do not."""
        return self.failed > 0

    def as_dict(self) -> dict:
        return {"discovered": self.discovered, "processed": self.processed,
                "available": self.available, "missing": self.missing,
                "ambiguous": self.ambiguous, "failed": self.failed,
                "unregistered": list(self.unregistered)}


# ------------------------------------------------------------------ core move
def stage_copy_verify_promote(kind: str, source_path: str, relative_path: str) -> MigratedFile:
    """Move ONE physical file into ARTIFACT_ROOT with the full integrity protocol.

    Idempotent: if the destination already exists with the SAME sha256, it is accepted
    as already migrated. A destination with a DIFFERENT hash is a collision -> FAILED
    (never overwritten). The legacy source is never removed."""
    if not os.path.isfile(source_path):
        return MigratedFile(relative_path, "", 0, ArtifactStatus.MISSING, ValidationStatus.PENDING,
                            Reason.LEGACY_FILE_NOT_FOUND, source_path)
    try:
        src_sha, src_size = sha256_file(source_path)
        final = ap.resolve(kind, relative_path)
        if os.path.isfile(final):
            dst_sha, dst_size = sha256_file(final)
            if dst_sha == src_sha and dst_size == src_size:
                return MigratedFile(relative_path, src_sha, src_size, ArtifactStatus.AVAILABLE,
                                    ValidationStatus.PASSED, None, source_path, fingerprint(final))
            logger.error(f"[MIGRATE] destination {relative_path} exists with a different hash - refusing")
            return MigratedFile(relative_path, src_sha, src_size, ArtifactStatus.FAILED,
                                ValidationStatus.HASH_MISMATCH, Reason.COPY_FAILED, source_path)

        staged = ap.staging_path(kind, relative_path)
        os.makedirs(os.path.dirname(staged), exist_ok=True)
        with open(source_path, "rb") as fsrc, open(staged, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, _CHUNK)
            fdst.flush()
            os.fsync(fdst.fileno())
        dst_sha, dst_size = sha256_file(staged)
        if dst_sha != src_sha or dst_size != src_size:
            os.remove(staged)
            return MigratedFile(relative_path, src_sha, src_size, ArtifactStatus.FAILED,
                                ValidationStatus.HASH_MISMATCH, Reason.HASH_MISMATCH, source_path)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        os.replace(staged, final)                     # atomic promotion
        _fsync_dir(os.path.dirname(final))
        return MigratedFile(relative_path, dst_sha, dst_size, ArtifactStatus.AVAILABLE,
                            ValidationStatus.PASSED, None, source_path, fingerprint(final))
    except Exception as e:  # noqa: BLE001 - recorded, blocks the marker
        logger.error(f"[MIGRATE] copy failed for {relative_path}: {e.__class__.__name__}: {e}")
        return MigratedFile(relative_path, "", 0, ArtifactStatus.FAILED, ValidationStatus.FAILED,
                            Reason.COPY_FAILED, source_path)


# ------------------------------------------------------------------ discovery helpers
def resolve_legacy_path(recorded_path: Optional[str], search_dirs: List[str],
                        stored_filename: Optional[str] = None) -> Tuple[Optional[str], Optional[Reason]]:
    """Find the physical file for a legacy record. Order:
       1. the recorded path itself, if it exists on THIS machine
       2. stored_filename / basename under the search dirs - UNIQUE match only
    0 matches -> LEGACY_FILE_NOT_FOUND; >1 -> AMBIGUOUS_LEGACY_PATH. Never guess."""
    if recorded_path and os.path.isfile(recorded_path):
        return recorded_path, None
    base = stored_filename or (os.path.basename(str(recorded_path).replace("\\", "/")) if recorded_path else None)
    if not base:
        return None, Reason.LEGACY_PATH_UNRESOLVED
    matches: List[str] = []
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            if base in files:
                matches.append(os.path.join(root, base))
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, Reason.LEGACY_FILE_NOT_FOUND
    return None, Reason.AMBIGUOUS_LEGACY_PATH


# Recognised multi-file representations: directory suffix -> (format, component globs)
MULTIFILE_REPRESENTATIONS = {
    "_openvino_model": ("openvino", (".xml", ".bin", "metadata.yaml")),
}


def enumerate_representation_dir(dir_path: str) -> Tuple[Optional[str], List[str], List[str]]:
    """For a recognised representation directory return (format, component_files,
    unrecognized_files). Unrecognised files are reported (UNREGISTERED_ARTIFACT) and left
    untouched - never registered blindly."""
    name = os.path.basename(dir_path.rstrip("/\\"))
    for suffix, (fmt, allowed) in MULTIFILE_REPRESENTATIONS.items():
        if name.endswith(suffix):
            comps, other = [], []
            for f in sorted(os.listdir(dir_path)):
                full = os.path.join(dir_path, f)
                if not os.path.isfile(full):
                    other.append(full)
                elif f == "metadata.yaml" or any(f.endswith(a) for a in allowed if a.startswith(".")):
                    comps.append(full)
                else:
                    other.append(full)
            return fmt, comps, other
    return None, [], []
