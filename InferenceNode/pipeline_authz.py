"""Centralized per-pipeline authorization gate.

A single before_request hook covers EVERY `/api/pipeline/<id>[/...]` route, so no
individual handler can forget the check and no route can reach PipelineManager without
passing through PostgreSQL authorization first.

Permissions are resolved from an EXPLICIT route table, never inferred from the HTTP
method - DELETE is admin-only rather than "an edit", and several POST routes are reads.

Refusals always return the same opaque 404 whether the pipeline is missing or merely
forbidden, so knowing another user's pipeline_id reveals nothing. The real reason is
recorded in the server log only.
"""
from __future__ import annotations

import re
import logging

from flask import request, jsonify
from flask_login import current_user

logger = logging.getLogger("InferenceNode.pipeline_authz")

_PER_ID = re.compile(r"^/api/pipeline/([^/]+)(?:/(.*))?$")

# Collection endpoints under /api/pipeline/ that are not a pipeline id.
_NOT_PER_ID = {"create", "import"}

# Explicit sub-path -> required permission. Longest match wins; "" is the bare
# /api/pipeline/<id> route, whose permission depends on the method.
_SUBPATH_PERMISSION = {
    "start": "start",
    "stop": "stop",
    "status": "view",
    "fullstatus": "view",
    "stream": "view",
    "stream/hq": "view",
    "thumbnail": "view",
    "thumbnail/exists": "view",
    "thumbnail/generate": "edit",   # a POST that writes a file is not a read
    "publishers/status": "view",
    "export": "view",
    # Server-authoritative duplicate: the caller must be able to VIEW the source; the
    # route additionally requires create permission (admin) via pipeline_store.
    "duplicate": "view",
    "inference/enable": "edit",
    "inference/disable": "edit",
}

# /api/pipeline/<id>/publisher/<publisher_id>/enable|disable
_PUBLISHER_RE = re.compile(r"^publisher/[^/]+/(enable|disable)$")


def _required_permission(subpath: str, method: str):
    """Return ('admin'|'view'|'start'|'stop'|'edit') or None when unknown.

    Unknown sub-paths deliberately fall back to the STRICTEST sensible choice rather
    than being allowed through: a future route added without updating this table is
    gated as an edit, not silently public.
    """
    subpath = (subpath or "").strip("/")
    if not subpath:
        if method == "DELETE":
            return "admin"
        if method in ("PUT", "PATCH", "POST"):
            return "edit"
        return "view"
    if subpath in _SUBPATH_PERMISSION:
        return _SUBPATH_PERMISSION[subpath]
    if _PUBLISHER_RE.match(subpath):
        return "edit"
    return "edit"


def register_pipeline_authz(app):
    @app.before_request
    def _pipeline_permission_gate():
        m = _PER_ID.match(request.path or "")
        if not m:
            return None
        pipeline_id, subpath = m.group(1), m.group(2)
        if pipeline_id in _NOT_PER_ID:
            return None
        # The auth gate (registered earlier) already handled unauthenticated users.
        if not getattr(current_user, "is_authenticated", False):
            return None

        denied = jsonify({"error": "Pipeline not found or access denied"}), 404
        required = _required_permission(subpath, request.method.upper())

        try:
            from .pipeline_store import get_pipeline_for_user, require_admin, AccessDenied
            if required == "admin":
                try:
                    require_admin(current_user, f"pipeline_delete:{pipeline_id}")
                except AccessDenied:
                    return denied
                # Admin still has to exist-check through the same service path.
                allowed = get_pipeline_for_user(pipeline_id, current_user, "view", require=False)
            else:
                allowed = get_pipeline_for_user(pipeline_id, current_user, required, require=False)
        except Exception as e:
            logger.error(f"pipeline authz check failed: {e.__class__.__name__}: {e}")
            return jsonify({"error": "Authorization check failed"}), 500

        if allowed is None:
            return denied
        return None

    logger.info("Pipeline permission gate enabled "
                "(explicit route->permission map; delete is admin-only)")
