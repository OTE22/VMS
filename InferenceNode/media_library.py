"""Media discovery for the Pipeline Builder.

ArmyEye had no way to list local media: the frame-source library's
`VideoFileCapture.discover()` returns [] by design ("discovery is not applicable for
file-based sources"), and the only media endpoint was upload. That is why the builder
offered a free-text path box and why existing pipelines hold hand-typed absolute paths.

This module enumerates MEDIA_ROOT and hands the UI stable RELATIVE references. Absolute
server paths are never sent to a client and never accepted from one.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("InferenceNode.media_library")

# Extensions the runtime frame reader can actually open (OpenCV/FFmpeg backed).
# Deliberately conservative: we do not advertise a format merely because it is common.
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
})

# Non-file sources: a basename fallback must NEVER be applied to these.
_NETWORK_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://", "rtmp://", "udp://", "tcp://")


class MediaError(Exception):
    """Raised with a classified code: MEDIA_FILE_MISSING / MEDIA_FILE_AMBIGUOUS / ..."""

    def __init__(self, code: str, message: str = "", **extra):
        super().__init__(message or code)
        self.code = code
        self.extra = extra


def default_media_root() -> str:
    """MEDIA_ROOT = ARTIFACT_ROOT/media (managed artifact plane; media_assets registry in
    PostgreSQL). `ARMYEYE_MEDIA_ROOT` may still override it explicitly. The legacy
    `InferenceNode/media` directory is a read-only migration source only."""
    explicit = os.environ.get("ARMYEYE_MEDIA_ROOT")
    if explicit:
        return explicit
    from . import artifact_paths as _ap
    return _ap.kind_root("media")


def legacy_media_root() -> str:
    """Pre-registry media directory (migration source; never written after cutover).
    Honors ARMYEYE_LEGACY_ROOT (test/E2E nodes point it at an empty isolated dir)."""
    base = os.environ.get("ARMYEYE_LEGACY_ROOT") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "media")


def is_network_source(value: Any) -> bool:
    """True for stream URLs and anything that is not a local filesystem path."""
    if not isinstance(value, str):
        return True          # e.g. webcam index 0 - definitely not a file
    return value.lower().startswith(_NETWORK_SCHEMES)


class MediaLibrary:
    """Lists and resolves media under a single configured root."""

    def __init__(self, media_root: Optional[str] = None, registry_backed: Optional[bool] = None):
        self.media_root = os.path.abspath(media_root or default_media_root())
        # Registry-backed only for the managed root (ARTIFACT_ROOT/media): resolution then
        # requires the media_assets row to be servable (AVAILABLE + PASSED + integrity).
        # Files dropped into the managed root are registered (hashed) on first use.
        if registry_backed is None:
            registry_backed = media_root is None and not os.environ.get("ARMYEYE_MEDIA_ROOT")
        self.registry_backed = registry_backed

    # ------------------------------------------------------------ listing --
    def list_media(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Recursively enumerate supported media.

        Returns (sources, excluded). `excluded` records every skipped file with a
        reason so the UI/report can explain a count difference instead of silently
        dropping files.
        """
        sources: List[Dict[str, Any]] = []
        excluded: List[Dict[str, str]] = []

        if not os.path.isdir(self.media_root):
            logger.warning(f"Media root does not exist: {self.media_root}")
            return sources, excluded

        for dirpath, dirnames, filenames in os.walk(self.media_root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, self.media_root).replace(os.sep, "/")

                # Case-insensitive: camera.MP4 is as valid as camera.mp4.
                ext = os.path.splitext(filename)[1].lower()
                if ext not in VIDEO_EXTENSIONS:
                    excluded.append({"relative_path": rel_path, "reason": "unsupported_extension"})
                    continue

                # A symlink pointing outside the root must not be exposed.
                try:
                    real = os.path.realpath(abs_path)
                    if os.path.commonpath([real, os.path.realpath(self.media_root)]) != \
                            os.path.realpath(self.media_root):
                        excluded.append({"relative_path": rel_path, "reason": "outside_media_root"})
                        continue
                except Exception:
                    excluded.append({"relative_path": rel_path, "reason": "unresolvable_path"})
                    continue

                try:
                    size = os.path.getsize(abs_path)
                except OSError:
                    excluded.append({"relative_path": rel_path, "reason": "unreadable"})
                    continue

                sources.append({
                    "relative_path": rel_path,          # the stable reference we persist
                    "display_name": filename,
                    "directory": os.path.dirname(rel_path),
                    "type": "video",
                    "extension": ext,
                    "size_bytes": size,
                })

        sources.sort(key=lambda s: s["relative_path"].lower())
        return sources, excluded

    # --------------------------------------------------------- resolution --
    def resolve_relative(self, relative_path: str) -> str:
        """Resolve a client-supplied relative reference to an absolute path.

        Rejects absolute paths, traversal and symlink escape - a browser must never be
        able to name an arbitrary server file.
        """
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise MediaError("PIPELINE_CONFIG_INVALID", "Empty media reference")

        candidate = relative_path.replace("\\", "/").strip()
        if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
            raise MediaError("PIPELINE_CONFIG_INVALID", "Absolute media paths are not accepted")

        root_real = os.path.realpath(self.media_root)
        target = os.path.realpath(os.path.join(self.media_root, candidate))
        try:
            inside = os.path.commonpath([target, root_real]) == root_real
        except ValueError:
            inside = False          # different drive on Windows
        if not inside:
            raise MediaError("PIPELINE_CONFIG_INVALID", "Media reference escapes the media root")
        if not os.path.isfile(target):
            raise MediaError("MEDIA_FILE_MISSING", f"No media file at {candidate}")
        if self.registry_backed:
            from . import media_registry
            row = media_registry.get_by_path(candidate)
            if row is None:
                row = media_registry.register_existing(candidate)      # drop-in file: register now
            served = media_registry.servable_path(candidate)
            if served is None:
                row = media_registry.get_by_path(candidate) or row
                raise MediaError("MEDIA_INTEGRITY", f"Media {candidate} is not servable "
                                 f"(status={row.get('status')}, validation={row.get('validation_status')}, "
                                 f"reason={row.get('reason')})", status=row.get('status'),
                                 validation_status=row.get('validation_status'), reason=row.get('reason'))
            return served
        return target

    def find_by_basename(self, basename: str) -> List[str]:
        """All files under the root whose filename matches (case-insensitively)."""
        wanted = os.path.basename(basename).lower()
        matches = []
        for dirpath, _dirnames, filenames in os.walk(self.media_root):
            for filename in filenames:
                if filename.lower() == wanted:
                    matches.append(os.path.join(dirpath, filename))
        return sorted(matches)


def resolve_frame_source(frame_source: Dict[str, Any],
                         media: Optional[MediaLibrary] = None) -> Dict[str, Any]:
    """Work out the source a pipeline should actually read from.

    Never mutates or persists anything. Returns a diagnostic record:
        configured_source / effective_source / source_fallback / fallback_reason

    Resolution order for LOCAL FILE sources only:
      1. `relative_source` under MEDIA_ROOT (what new pipelines store)
      2. the configured absolute path, if it exists on this machine
      3. legacy compatibility: basename lookup under MEDIA_ROOT
           0 matches  -> MEDIA_FILE_MISSING
           1 match    -> use it (reported as a fallback, never written back)
          >1 matches  -> MEDIA_FILE_AMBIGUOUS (refuse to guess)

    Network/stream/camera sources are returned untouched - a basename fallback must
    never rewrite an rtsp:// or http:// source.
    """
    media = media or MediaLibrary()
    cfg = (frame_source or {}).get("config") or {}
    configured = cfg.get("source")
    relative = cfg.get("relative_source")

    result = {"configured_source": configured, "relative_source": relative,
              "effective_source": configured, "source_fallback": False,
              "fallback_reason": None}

    # 1. New-style stable reference.
    if relative:
        result["effective_source"] = media.resolve_relative(relative)
        return result

    # Non-file sources pass through unchanged.
    if is_network_source(configured):
        return result

    # 2. Configured path as-is.
    if isinstance(configured, str) and os.path.isfile(configured):
        return result

    # 3. Legacy compatibility for local files only.
    basename = os.path.basename(str(configured).replace("\\", "/"))
    if not basename:
        raise MediaError("MEDIA_FILE_MISSING", "Pipeline has no usable media source")

    matches = media.find_by_basename(basename)
    if len(matches) == 0:
        raise MediaError("MEDIA_FILE_MISSING",
                         f"'{basename}' is not present in the media library")
    if len(matches) > 1:
        rels = [os.path.relpath(m, media.media_root).replace(os.sep, "/") for m in matches]
        raise MediaError("MEDIA_FILE_AMBIGUOUS",
                         f"'{basename}' matches {len(matches)} files in the media library",
                         candidates=rels)

    result["effective_source"] = matches[0]
    result["source_fallback"] = True
    result["fallback_reason"] = "configured_path_unavailable_basename_matched"
    return result
