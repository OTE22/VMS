"""Admin-only user management (UI page + JSON API).

All routes require an authenticated admin. Write routes additionally validate a CSRF
token (the frontend's fetchJSON sends X-CSRFToken). Ownership of the account lifecycle
(create/role/active/reset/delete) lives in auth.service, which enforces transactional
last-active-admin protection and permissions_version bumps.
"""
from __future__ import annotations

import logging

from flask import request, jsonify, render_template, current_app, abort
from flask_login import current_user

from sqlalchemy.exc import IntegrityError

from . import service as svc
from .flask_auth import admin_required

logger = logging.getLogger("InferenceNode.auth")


def _require_csrf():
    """Validate CSRF for state-changing admin calls (skipped when CSRF disabled,
    e.g. in tests). Token comes from the X-CSRFToken header."""
    if not current_app.config.get("WTF_CSRF_ENABLED", True):
        return
    from flask_wtf.csrf import validate_csrf
    token = request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")
    try:
        validate_csrf(token)
    except Exception:
        abort(400, description="CSRF validation failed")


def _request_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description='Request must be a JSON object')
    for key in ('active', 'must_change', 'must_change_password'):
        if key in data and type(data[key]) is not bool:
            abort(400, description=key + ' must be a boolean')
    return data


def _user_dict(u):
    return {
        "id": u.id, "username": u.username, "email": u.email,
        "full_name": u.full_name, "role": u.role, "is_active": bool(u.is_active),
        "must_change_password": bool(u.must_change_password),
        "last_login": u.last_login.isoformat() if u.last_login else None,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def register_admin_users(app):
    @app.route("/admin/users")
    @admin_required
    def admin_users_page():
        return render_template("admin_users.html")

    @app.route("/api/users", methods=["GET"])
    @admin_required
    def api_list_users():
        """List users, each annotated with how many pipelines they may view.

        The count comes from ONE aggregate GROUP BY (not a request per row). Admins are
        reported as None because their access derives from their role, not from grants -
        the UI renders that as "All pipelines" rather than a number.
        """
        users = [_user_dict(u) for u in svc.list_users()]
        try:
            from ..pipeline_repository import repository
            counts = repository.access_counts_by_user()
        except Exception as e:                      # pipelines table absent/unavailable
            logger.debug(f"pipeline access counts unavailable: {e.__class__.__name__}")
            counts = None
        for u in users:
            if counts is None:
                u["pipeline_access_count"] = None
            elif u["role"] == "admin":
                u["pipeline_access_count"] = None   # role-based: "All pipelines"
            else:
                u["pipeline_access_count"] = counts.get(u["id"], 0)
        return jsonify({"status": "success", "users": users})

    @app.route("/api/users", methods=["POST"])
    @admin_required
    def api_create_user():
        _require_csrf()
        data = _request_object()
        try:
            u = svc.create_user(
                current_user,
                username=data.get("username"),
                password=data.get("password") or "",
                role=(data.get("role") or "user"),
                email=data.get("email") or None,
                full_name=data.get("full_name") or None,
                must_change_password=bool(data.get("must_change_password", True)),
            )
            return jsonify({"status": "success", "user": _user_dict(u)}), 201
        except svc.UserOpError as e:
            return jsonify({"error": str(e)}), 400
        except IntegrityError:
            return jsonify({"error": "Account conflicts with an existing record"}), 409

    @app.route("/api/users/<int:user_id>", methods=["PATCH"])
    @admin_required
    def api_update_profile(user_id):
        """Edit profile fields only (email / full_name). Username is immutable and
        role/active/password keep their dedicated routes."""
        _require_csrf()
        data = _request_object()
        unknown = set(data) - {"email", "full_name"}
        if unknown:
            return jsonify({"error": f"Unsupported field(s): {', '.join(sorted(unknown))}. "
                                     f"Only email and full_name can be edited."}), 400
        try:
            u = svc.update_profile(current_user, user_id,
                                   email=data.get("email"),
                                   full_name=data.get("full_name"))
            return jsonify({"status": "success", "user": _user_dict(u)})
        except svc.UserOpError as e:
            return jsonify({"error": str(e)}), 400
        except IntegrityError:
            return jsonify({"error": "Account conflicts with an existing record"}), 409

    @app.route("/api/users/<int:user_id>/role", methods=["PUT"])
    @admin_required
    def api_set_role(user_id):
        _require_csrf()
        data = _request_object()
        try:
            u = svc.set_role(current_user, user_id, data.get("role"))
            return jsonify({"status": "success", "user": _user_dict(u)})
        except svc.UserOpError as e:
            return jsonify({"error": str(e)}), 400
        except IntegrityError:
            return jsonify({"error": "Account conflicts with an existing record"}), 409

    @app.route("/api/users/<int:user_id>/active", methods=["PUT"])
    @admin_required
    def api_set_active(user_id):
        _require_csrf()
        data = _request_object()
        try:
            if 'active' not in data:
                return jsonify({'error': 'active is required'}), 400
            u = svc.set_active(current_user, user_id, data['active'])
            return jsonify({"status": "success", "user": _user_dict(u)})
        except svc.UserOpError as e:
            return jsonify({"error": str(e)}), 400
        except IntegrityError:
            return jsonify({"error": "Account conflicts with an existing record"}), 409

    @app.route("/api/users/<int:user_id>/reset-password", methods=["POST"])
    @admin_required
    def api_reset_password(user_id):
        _require_csrf()
        data = _request_object()
        try:
            svc.reset_password(current_user, user_id, data.get("password") or "",
                               must_change=bool(data.get("must_change", True)))
            return jsonify({"status": "success"})
        except svc.UserOpError as e:
            return jsonify({"error": str(e)}), 400
        except IntegrityError:
            return jsonify({"error": "Account conflicts with an existing record"}), 409

    @app.route("/api/users/<int:user_id>", methods=["DELETE"])
    @admin_required
    def api_delete_user(user_id):
        _require_csrf()
        try:
            svc.delete_user(current_user, user_id)
            return jsonify({"status": "success"})
        except svc.UserOpError as e:
            return jsonify({"error": str(e)}), 400
        except IntegrityError:
            return jsonify({"error": "Account conflicts with an existing record"}), 409
