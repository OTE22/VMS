"""Admin-only pipeline assignment API.

Grants live in pipeline_user_access and are the ONLY thing that lets a normal user see
or operate a pipeline. Every route here is admin-only, CSRF-protected and audited.

These paths sit under /api/pipelines/ (plural), so they are not intercepted by the
per-pipeline permission gate, which matches /api/pipeline/<id> (singular).
"""
from __future__ import annotations

import logging

from flask import request, jsonify
from flask_login import current_user

from . import pipeline_store as ps
from .auth.flask_auth import admin_required
from .auth.admin_routes import _require_csrf
from .auth.service import record_audit, list_users
from .pipeline_repository import PERMISSIONS, normalize_permissions

logger = logging.getLogger("InferenceNode.pipeline_access")


def register_pipeline_access(app):
    @app.route("/api/pipelines/<pipeline_id>/access", methods=["GET"])
    @admin_required
    def api_list_pipeline_access(pipeline_id):
        try:
            return jsonify({"status": "success", "pipeline_id": pipeline_id,
                            "access": ps.list_access(current_user, pipeline_id)})
        except ps.AccessDenied as e:
            return jsonify({"error": str(e)}), 404

    @app.route("/api/pipelines/<pipeline_id>/access/<int:user_id>", methods=["PUT"])
    @admin_required
    def api_set_pipeline_access(pipeline_id, user_id):
        """Idempotently create or update ONE user's grant.

        Permissions are normalized server-side (any operating right implies can_view),
        so the UI cannot produce a grant that lets someone act on a pipeline they are
        not allowed to see.
        """
        _require_csrf()
        data = request.get_json(silent=True) or {}
        unknown = set(data) - set(PERMISSIONS)
        if unknown:
            return jsonify({"error": f"Unsupported field(s): {', '.join(sorted(unknown))}. "
                                     f"Allowed: {', '.join(PERMISSIONS)}."}), 400
        try:
            before = ps.repository.get_access(pipeline_id, user_id)
            result = ps.set_access(current_user, pipeline_id, user_id, data)
        except ps.AccessDenied as e:
            return jsonify({"error": str(e)}), 404

        target = next((u.username for u in list_users() if u.id == user_id), str(user_id))
        record_audit(actor=current_user,
                     action="pipeline_access_granted" if before is None else "pipeline_access_changed",
                     target=pipeline_id,
                     detail={"user_id": user_id, "username": target,
                             "old": {k: before[k] for k in PERMISSIONS} if before else None,
                             "new": {k: result[k] for k in PERMISSIONS}})
        return jsonify({"status": "success", "access": result})

    @app.route("/api/pipelines/<pipeline_id>/access/<int:user_id>", methods=["DELETE"])
    @admin_required
    def api_remove_pipeline_access(pipeline_id, user_id):
        _require_csrf()
        try:
            removed = ps.remove_access(current_user, pipeline_id, user_id)
        except ps.AccessDenied as e:
            return jsonify({"error": str(e)}), 404
        if removed:
            target = next((u.username for u in list_users() if u.id == user_id), str(user_id))
            record_audit(actor=current_user, action="pipeline_access_removed",
                         target=pipeline_id, detail={"user_id": user_id, "username": target})
        return jsonify({"status": "success", "removed": bool(removed)})

    @app.route("/api/users/<int:user_id>/pipeline-access", methods=["GET"])
    @admin_required
    def api_user_pipeline_access(user_id):
        return jsonify({"status": "success", "user_id": user_id,
                        "access": ps.list_access_for_user(current_user, user_id)})

    @app.route("/api/pipelines/assignable", methods=["GET"])
    @admin_required
    def api_assignable_pipelines():
        """Minimal id/name list for populating the assignment UI."""
        rows = ps.list_pipelines_for_user(current_user)
        return jsonify({"status": "success",
                        "pipelines": [{"pipeline_id": r["pipeline_id"],
                                       "name": r.get("name")} for r in rows]})

    logger.info("Pipeline assignment API registered (admin-only, CSRF-protected, audited)")


__all__ = ["register_pipeline_access", "normalize_permissions"]
