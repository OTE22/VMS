"""Flask wiring for shared-DB authentication: Flask-Login sessions, login/logout
pages, a global login gate, CSRF for the login form, login rate-limiting, and an
`admin_required` decorator. All of it is contained here so inference_node.py only
needs a single `setup_auth(app)` call.

Design choices:
- Users come from FACE_DETECTOR's shared `users` table (read-only) via auth.service.
- The Flask-Login loader re-reads role/is_active every request, so a change in
  FACE_DETECTOR (deactivate, role change) takes effect on ArmyEye's next request.
- WTF_CSRF_CHECK_DEFAULT is left False so enabling CSRF does not instantly break the
  many existing same-origin API fetch() calls; the login form still validates its own
  CSRF token, and session cookies are SameSite=Lax. Full API CSRF can be turned on
  later via the meta-token + fetchJSON helper already added to the frontend.
"""
from __future__ import annotations

import os
import logging
from functools import wraps

from flask import (redirect, render_template, request, url_for, flash,
                   jsonify, session)
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from flask_wtf import FlaskForm, CSRFProtect
from flask_wtf.csrf import CSRFError
from wtforms import StringField, PasswordField
from wtforms.validators import DataRequired, Length, EqualTo
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from . import db as auth_db
from . import service as auth_service

logger = logging.getLogger("InferenceNode.auth")

# Endpoints reachable without a session (by Flask endpoint name).
_PUBLIC_ENDPOINTS = {"login", "static", "favicon"}
# Reachable while authenticated even when a forced password change is pending.
_CHANGE_PW_ALLOWED = {"change_password", "logout", "static", "favicon"}
_PERM_VERSION_KEY = "perm_ver"


class AuthUser(UserMixin):
    """Flask-Login view of an ArmyEye user (no password material)."""

    def __init__(self, snapshot):
        self.id = snapshot.id
        self.username = snapshot.username
        self.email = getattr(snapshot, "email", None)
        self.full_name = getattr(snapshot, "full_name", None)
        self.role = snapshot.role
        self.permissions_version = getattr(snapshot, "permissions_version", None)
        self.must_change_password = bool(getattr(snapshot, "must_change_password", False))

    def get_id(self):
        return str(self.id)

    @property
    def is_admin(self) -> bool:
        return str(self.role).strip().lower() == "admin"


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    new_password = PasswordField("New password", validators=[DataRequired(), Length(min=8)])
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[DataRequired(), EqualTo("new_password", message="Passwords must match")])


def admin_required(view):
    """Require an authenticated admin. 403 (JSON for /api, page otherwise)."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Admin privileges required"}), 403
            return render_template("error_403.html"), 403
        return view(*args, **kwargs)
    return wrapped


def setup_auth(app, bootstrap: bool = True):
    """Initialize auth on the Flask app. When bootstrap=True (production), requires
    ARMYEYE_DATABASE_URL, runs migrations under an advisory lock, and seeds the admin
    (fails clearly if the DB is missing). Tests pass bootstrap=False and manage their
    own schema. Returns the Limiter."""
    # Session-cookie hardening
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    if os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on"):
        app.config["SESSION_COOKIE_SECURE"] = True
    # CSRF machinery on; not auto-rejecting every POST keeps existing same-origin API
    # calls working. The login/change-password FlaskForms still validate their token.
    app.config.setdefault("WTF_CSRF_CHECK_DEFAULT", False)
    if not os.environ.get("FLASK_SECRET_KEY"):
        logger.warning("FLASK_SECRET_KEY not set - using an ephemeral key; sessions "
                       "reset on restart. Set FLASK_SECRET_KEY in production.")

    auth_db.init_engine()
    if bootstrap:
        # Fail startup clearly if ArmyEye's DB isn't configured, then migrate + seed.
        from .bootstrap import bootstrap_database
        bootstrap_database()

    csrf = CSRFProtect(app)
    limiter = Limiter(key_func=get_remote_address, app=app,
                      default_limits=[], storage_uri="memory://")

    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Please sign in to continue."

    @app.context_processor
    def _inject_flags():
        enabled = os.environ.get("ENABLE_ENGINE_BUILDER", "").strip().lower() in ("1", "true", "yes", "on")
        return {"engine_builder_enabled": enabled}

    @login_manager.user_loader
    def load_user(user_id):
        if not auth_db.is_configured():
            return None
        try:
            snap = auth_service.get_user_by_id(user_id)
        except Exception as e:
            logger.error(f"user_loader failed: {e}")
            return None
        return AuthUser(snap) if snap else None

    @login_manager.unauthorized_handler
    def _unauthorized():
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(_post_login_target())
        form = LoginForm()
        error = None
        if not auth_db.is_configured():
            error = ("Authentication is not configured on this node "
                     "(ARMYEYE_DATABASE_URL is missing). Contact an administrator.")
            return render_template("login.html", form=form, error=error), 503
        if form.validate_on_submit():
            try:
                snap = auth_service.authenticate(form.username.data, form.password.data)
                login_user(AuthUser(snap))
                session[_PERM_VERSION_KEY] = snap.permissions_version
                logger.info(f"Session started for '{snap.username}'")
                if snap.must_change_password:
                    return redirect(url_for("change_password"))
                return redirect(_safe_next(request.form.get("next")
                                           or request.args.get("next")) or url_for("dashboard"))
            except auth_service.AuthError as e:
                error = str(e)
            except Exception as e:
                logger.error(f"Login error: {e}")
                error = "Sign-in failed due to a server error. Try again."
        return render_template("login.html", form=form, error=error)

    @app.route("/change-password", methods=["GET", "POST"], endpoint="change_password")
    @login_required
    def change_password():
        form = ChangePasswordForm()
        error = None
        forced = bool(getattr(current_user, "must_change_password", False))
        if form.validate_on_submit():
            try:
                auth_service.change_own_password(
                    current_user.id, form.current_password.data, form.new_password.data)
                # Our own password change bumped permissions_version; refresh the
                # session copy so we are not immediately invalidated.
                new_ver = auth_service.current_permissions_version(current_user.id)
                if new_ver is not None:
                    session[_PERM_VERSION_KEY] = new_ver
                current_user.must_change_password = False
                flash("Password changed.", "success")
                return redirect(url_for("dashboard"))
            except auth_service.UserOpError as e:
                error = str(e)
            except Exception as e:
                logger.error(f"change_password error: {e}")
                error = "Could not change password. Try again."
        return render_template("change_password.html", form=form, error=error, forced=forced)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        session.clear()
        return redirect(url_for("login"))

    @app.errorhandler(CSRFError)
    def _csrf_error(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "CSRF validation failed"}), 400
        return render_template("login.html", form=LoginForm(),
                               error="Your session expired. Please sign in again."), 400

    @app.before_request
    def _require_login():
        endpoint = request.endpoint or ""
        if endpoint in _PUBLIC_ENDPOINTS:
            return None
        if request.path in ("/favicon.ico", "/health") or request.path.startswith("/static/"):
            return None
        if not current_user.is_authenticated:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login", next=request.path))

        # Session invalidation: compare the permissions_version captured at login
        # against the live value. A change (role/active/password reset) or a
        # deactivated/deleted account ends the session immediately.
        live_ver = auth_service.current_permissions_version(current_user.id)
        if live_ver is None or session.get(_PERM_VERSION_KEY) != live_ver:
            logout_user()
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "Session expired, sign in again"}), 401
            return redirect(url_for("login", next=request.path))

        # Forced password change: block everything except the change-password page.
        if getattr(current_user, "must_change_password", False) and endpoint not in _CHANGE_PW_ALLOWED:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Password change required"}), 403
            return redirect(url_for("change_password"))
        return None

    # Admin-only user management (UI + API)
    from .admin_routes import register_admin_users
    register_admin_users(app)

    logger.info("Authentication enabled (ArmyEye-owned users, per-request role + version checks)")
    return limiter


def _post_login_target():
    if getattr(current_user, "must_change_password", False):
        return url_for("change_password")
    return _safe_next(request.args.get("next")) or url_for("dashboard")


def _safe_next(target):
    """Only allow same-site relative redirects to avoid open-redirect abuse."""
    if not target:
        return None
    if target.startswith("/") and not target.startswith("//"):
        return target
    return None
