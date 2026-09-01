"""THE single path-safety service for every managed artifact.

Persist RELATIVE paths only. Resolve them ONLY through `resolve()`, which:

    join(ARTIFACT_ROOT, <kind root>, relative_path) -> os.path.realpath
    -> assert the result is inside realpath(<kind root>)

and rejects `..`, absolute paths, symlink escape and percent-encoded traversal. Every
read / load / verify / delete / quarantine / download / engine import / model load goes
through here - no module joins artifact paths on its own, and rows read from PostgreSQL
are NOT trusted blindly (tests/test_artifact_paths.py greps for stray os.path.join on
artifact roots).

Layout under ARTIFACT_ROOT (default InferenceNode/data, bind-mounted in compose):

    models/<model_id>/...            engines/<engine_key>/engine.py
    media/...                        thumbnails/...
    <kind>/.staging/...              <kind>/.trash/...       (managed areas)
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Optional

KINDS = ("models", "engines", "media", "thumbnails")
STAGING_DIR = ".staging"
TRASH_DIR = ".trash"

_DEFAULT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


class ArtifactPathError(ValueError):
    """Raised for any path that would leave its artifact root."""


def artifact_root() -> str:
    root = os.environ.get("ARMYEYE_ARTIFACT_ROOT") or _DEFAULT_ROOT
    return os.path.realpath(root)


def kind_root(kind: str) -> str:
    if kind not in KINDS:
        raise ArtifactPathError(f"unknown artifact kind {kind!r}")
    return os.path.join(artifact_root(), kind)


def ensure_layout() -> str:
    root = artifact_root()
    for k in KINDS:
        os.makedirs(os.path.join(root, k), exist_ok=True)
        os.makedirs(os.path.join(root, k, STAGING_DIR), exist_ok=True)
        os.makedirs(os.path.join(root, k, TRASH_DIR), exist_ok=True)
    return root


def _normalize_relative(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ArtifactPathError("empty relative path")
    rel = relative_path.strip()
    # percent-encoded traversal (`%2e%2e%2f`) is decoded BEFORE checking
    decoded = urllib.parse.unquote(rel)
    if decoded != rel and (".." in decoded or decoded.startswith(("/", "\\"))):
        raise ArtifactPathError("encoded traversal rejected")
    rel = decoded.replace("\\", "/")
    if rel.startswith("/") or os.path.isabs(rel) or (len(rel) > 1 and rel[1] == ":"):
        raise ArtifactPathError("absolute paths are not allowed")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ArtifactPathError("'..' is not allowed")
    if not parts:
        raise ArtifactPathError("empty relative path")
    return "/".join(parts)


def resolve(kind: str, relative_path: str, *, must_exist: bool = False) -> str:
    """Resolve `relative_path` under the kind root; raise ArtifactPathError on escape."""
    root = os.path.realpath(kind_root(kind))
    rel = _normalize_relative(relative_path)
    candidate = os.path.join(root, *rel.split("/"))
    real = os.path.realpath(candidate)          # follows symlinks -> escape becomes visible
    if os.path.commonpath([root, real]) != root:
        raise ArtifactPathError("path escapes its artifact root")
    if must_exist and not os.path.exists(real):
        raise ArtifactPathError("artifact does not exist")
    return real


def staging_path(kind: str, relative_path: str) -> str:
    rel = _normalize_relative(relative_path)
    return resolve(kind, f"{STAGING_DIR}/{rel}")


def trash_path(kind: str, relative_path: str) -> str:
    rel = _normalize_relative(relative_path)
    return resolve(kind, f"{TRASH_DIR}/{rel}")


def to_relative(kind: str, absolute_path: str) -> Optional[str]:
    """Inverse of resolve() for paths that are already inside the kind root; None otherwise."""
    root = os.path.realpath(kind_root(kind))
    real = os.path.realpath(absolute_path)
    try:
        if os.path.commonpath([root, real]) != root:
            return None
    except ValueError:
        return None
    rel = os.path.relpath(real, root).replace("\\", "/")
    return rel if rel and not rel.startswith("..") else None


def is_managed(kind: str, relative_path: str) -> bool:
    """True when the relative path lives in a managed (.staging/.trash) area."""
    rel = _normalize_relative(relative_path)
    return rel.split("/")[0] in (STAGING_DIR, TRASH_DIR)
