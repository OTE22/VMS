"""ArmyEye authentication package.

ArmyEye owns its own users/audit in a dedicated PostgreSQL database (ARMYEYE_DATABASE_URL);
it has no database dependency on FACE_DETECTOR. Modules: db (engine), models (owned
tables), service (accounts + bcrypt + last-admin protection + audit), bootstrap
(migrate+seed under advisory lock), flask_auth (Flask-Login/CSRF/limit/RBAC wiring).
"""

from .db import (normalize_database_url, init_engine, get_session, is_configured,
                 require_configured, advisory_lock, get_engine, is_postgres)
from .models import User, AuditLog, Base
from .service import (authenticate, get_user_by_id, current_permissions_version,
                      seed_admin, hash_password, verify_password,
                      create_user, set_role, set_active, reset_password, delete_user,
                      change_own_password, list_users, AuthError, UserOpError)

__all__ = [
    "normalize_database_url", "init_engine", "get_session", "is_configured",
    "require_configured", "advisory_lock", "get_engine", "is_postgres",
    "User", "AuditLog", "Base",
    "authenticate", "get_user_by_id", "current_permissions_version", "seed_admin",
    "hash_password", "verify_password", "create_user", "set_role", "set_active",
    "reset_password", "delete_user", "change_own_password", "list_users",
    "AuthError", "UserOpError",
]
