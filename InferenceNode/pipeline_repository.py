"""PipelineRepository - the ONLY persistence boundary for pipelines.

PostgreSQL is the single source of truth. Nothing above this layer writes pipeline
configuration anywhere else, and there is deliberately no DB<->JSON dual write: that
split-brain is exactly what made existing pipelines unstartable.

This module is pure persistence. It performs NO authorization - callers must go through
pipeline_store.get_pipeline_for_user() first. It returns plain dicts so nothing leaks a
detached ORM instance into request handlers.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func, delete as sa_delete

from .auth.db import get_session
from . import pipeline_secrets
from .data_models import Pipeline, PipelineUserAccess

logger = logging.getLogger("InferenceNode.pipeline_repository")

PERMISSIONS = ("can_view", "can_start", "can_stop", "can_edit")


def _model_id_from_config(config: Optional[dict]) -> Optional[str]:
    model = (config or {}).get("model") if isinstance(config, dict) else None
    mid = model.get("id") if isinstance(model, dict) else None
    return str(mid) if mid not in (None, "") else None


def _sync_model_reference(p: Pipeline, config: Optional[dict]) -> dict:
    """THE ONLY place pipelines.model_id and config.model.id are reconciled.

    Canonical value = the relational column. When a config carries model.id we take it
    as the caller's intent, write it to the column, and rewrite the JSON reflection from
    the column so the two can never diverge (invariant enforced additionally by the
    0005 CHECK on PostgreSQL). Routes/managers never write either field on their own."""
    cfg = dict(config or {})
    from ResultPublisher.config_validation import normalize_config
    if "destinations" in cfg:
        cfg["destinations"] = [{**d, "config": normalize_config(d.get("type"), d.get("config"))} for d in cfg["destinations"]]
    mid = _model_id_from_config(cfg)
    if isinstance(cfg.get("model"), dict) and "id" in cfg["model"]:
        p.model_id = mid
    if isinstance(cfg.get("model"), dict):
        cfg["model"] = dict(cfg["model"]); cfg["model"]["id"] = p.model_id
    return cfg


def _row_to_dict(p: Pipeline) -> Dict[str, Any]:
    cfg = pipeline_secrets.decrypt(p.config or {})
    # serialize the reflection FROM the canonical column
    if isinstance(cfg.get("model"), dict):
        cfg["model"] = dict(cfg["model"]); cfg["model"]["id"] = p.model_id
    return {
        "id": p.id,
        "pipeline_id": p.pipeline_id,
        "name": p.name,
        "description": p.description,
        "config": cfg,
        "model_id": p.model_id,
        "status": p.status,
        # Worker assignment; None = unassigned, runnable on any node.
        "node_id": p.node_id,
        # creator metadata only - never an authorization input
        "owner_id": p.owner_id,
        "owner_username": p.owner_username,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _access_to_dict(a: PipelineUserAccess, *, username: Optional[str] = None) -> Dict[str, Any]:
    d = {
        "pipeline_row_id": a.pipeline_id,
        "user_id": a.user_id,
        "can_view": bool(a.can_view),
        "can_start": bool(a.can_start),
        "can_stop": bool(a.can_stop),
        "can_edit": bool(a.can_edit),
        "created_by_id": a.created_by_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }
    if username is not None:
        d["username"] = username
    return d


def normalize_permissions(perms: Dict[str, Any]) -> Dict[str, bool]:
    """Server-side permission consistency: any operating right implies visibility.

    Enforced here rather than in the UI so an API client cannot create a grant like
    can_start=true / can_view=false that would let a user start a pipeline they are not
    even allowed to see.
    """
    out = {k: bool(perms.get(k, False)) for k in PERMISSIONS}
    if out["can_start"] or out["can_stop"] or out["can_edit"]:
        out["can_view"] = True
    return out


class PipelineRepository:
    """CRUD over the pipelines table. Stateless - safe to instantiate anywhere."""

    # ---------------------------------------------------------------- reads --
    def get(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        with get_session() as s:
            p = s.execute(
                select(Pipeline).where(Pipeline.pipeline_id == str(pipeline_id))
            ).scalar_one_or_none()
            return _row_to_dict(p) if p is not None else None

    def exists(self, pipeline_id: str) -> bool:
        with get_session() as s:
            return s.execute(
                select(Pipeline.id).where(Pipeline.pipeline_id == str(pipeline_id))
            ).first() is not None

    def list(self, *, user_id: Optional[int] = None, is_admin: bool = False) -> List[Dict[str, Any]]:
        """Admins get every pipeline; everyone else gets only what they may VIEW.

        The scoping is a SQL join, never a post-filter in Python or JavaScript, so
        knowing another user's pipeline_id cannot widen the result.
        """
        with get_session() as s:
            stmt = select(Pipeline)
            if not is_admin:
                if user_id is None:
                    return []
                stmt = (stmt.join(PipelineUserAccess,
                                  PipelineUserAccess.pipeline_id == Pipeline.id)
                            .where(PipelineUserAccess.user_id == user_id,
                                   PipelineUserAccess.can_view.is_(True)))
            rows = s.execute(stmt.order_by(Pipeline.created_at)).scalars().all()
            return [_row_to_dict(p) for p in rows]

    # -------------------------------------------------------------- writes --
    def create(self, *, pipeline_id: str, name: Optional[str] = None,
               description: Optional[str] = None, config: Optional[dict] = None,
               status: str = "stopped", owner_id: Optional[int] = None,
               owner_username: Optional[str] = None) -> Dict[str, Any]:
        with get_session() as s:
            from .media_guard import lock, guard_reference
            lock(s)
            exists = s.execute(
                select(Pipeline).where(Pipeline.pipeline_id == str(pipeline_id))
            ).scalar_one_or_none()
            if exists is not None:
                raise ValueError("pipeline_id already exists")
            guard_reference(s, config)
            p = Pipeline(pipeline_id=str(pipeline_id), name=name, description=description,
                         config={}, status=status,
                         owner_id=owner_id, owner_username=owner_username)
            p.config = pipeline_secrets.encrypt(_sync_model_reference(p, config or {}))
            s.add(p)
            s.flush()
            return _row_to_dict(p)

    def update(self, pipeline_id: str, *, name=None, description=None,
               config=None, status=None) -> Optional[Dict[str, Any]]:
        with get_session() as s:
            from .media_guard import lock, guard_reference
            lock(s)
            p = s.execute(
                select(Pipeline).where(Pipeline.pipeline_id == str(pipeline_id))
            ).scalar_one_or_none()
            if p is None:
                return None
            if name is not None:
                p.name = name
            if description is not None:
                p.description = description
            if config is not None:
                guard_reference(s, config, _row_to_dict(p)["config"])
                p.config = pipeline_secrets.encrypt(_sync_model_reference(p, config))
            if status is not None:
                p.status = status
            p.updated_at = datetime.utcnow()
            s.flush()
            return _row_to_dict(p)

    def set_control(self, pipeline_id: str, enabled: bool, publisher_id=None) -> bool:
        """Serialize control writes and change only the requested saved flag.

        Secret values remain in their existing stored representation; this operation
        neither decrypts nor replaces unrelated configuration.
        """
        from copy import deepcopy
        with get_session() as session:
            row = session.execute(select(Pipeline).where(
                Pipeline.pipeline_id == str(pipeline_id)).with_for_update()).scalar_one_or_none()
            if row is None:
                return False
            config = deepcopy(row.config or {})
            if publisher_id is None:
                config['inference_enabled'] = bool(enabled)
            else:
                destination = next((d for d in config.get('destinations', [])
                                    if str(d.get('id')) == str(publisher_id)), None)
                if destination is None:
                    return False
                destination['enabled'] = bool(enabled)
            row.config = config
            row.updated_at = datetime.utcnow()
            session.flush()
        return True

    def set_status(self, pipeline_id: str, status: str) -> None:
        """Record last-known state. This is NOT proof the pipeline is running - the API
        always overlays live status from PipelineManager."""
        with get_session() as s:
            p = s.execute(
                select(Pipeline).where(Pipeline.pipeline_id == str(pipeline_id))
            ).scalar_one_or_none()
            if p is not None:
                p.status = status

    def delete(self, pipeline_id: str) -> bool:
        """Delete the pipeline. pipeline_user_access rows go with it via FK CASCADE;
        shared media/models/engines are never touched."""
        with get_session() as s:
            p = s.execute(
                select(Pipeline).where(Pipeline.pipeline_id == str(pipeline_id))
            ).scalar_one_or_none()
            if p is None:
                return False
            s.delete(p)
            return True

    # -------------------------------------------------------------- access --
    def get_access(self, pipeline_id: str, user_id: int) -> Optional[Dict[str, Any]]:
        with get_session() as s:
            row = s.execute(
                select(PipelineUserAccess)
                .join(Pipeline, Pipeline.id == PipelineUserAccess.pipeline_id)
                .where(Pipeline.pipeline_id == str(pipeline_id),
                       PipelineUserAccess.user_id == int(user_id))
            ).scalar_one_or_none()
            return _access_to_dict(row) if row is not None else None

    def list_access_for_pipeline(self, pipeline_id: str) -> List[Dict[str, Any]]:
        from .auth.models import User
        with get_session() as s:
            rows = s.execute(
                select(PipelineUserAccess, User.username)
                .join(Pipeline, Pipeline.id == PipelineUserAccess.pipeline_id)
                .join(User, User.id == PipelineUserAccess.user_id)
                .where(Pipeline.pipeline_id == str(pipeline_id))
                .order_by(User.username_key)
            ).all()
            return [_access_to_dict(a, username=u) for a, u in rows]

    def list_access_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        with get_session() as s:
            rows = s.execute(
                select(PipelineUserAccess, Pipeline.pipeline_id, Pipeline.name)
                .join(Pipeline, Pipeline.id == PipelineUserAccess.pipeline_id)
                .where(PipelineUserAccess.user_id == int(user_id))
                .order_by(Pipeline.created_at)
            ).all()
            out = []
            for a, pid, pname in rows:
                d = _access_to_dict(a)
                d["pipeline_id"] = pid
                d["pipeline_name"] = pname
                out.append(d)
            return out

    def upsert_access(self, pipeline_id: str, user_id: int, perms: Dict[str, Any],
                      *, created_by_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Idempotent per-user grant. Returns None when the pipeline does not exist."""
        norm = normalize_permissions(perms)
        with get_session() as s:
            p = s.execute(
                select(Pipeline).where(Pipeline.pipeline_id == str(pipeline_id))
            ).scalar_one_or_none()
            if p is None:
                return None
            row = s.execute(
                select(PipelineUserAccess).where(
                    PipelineUserAccess.pipeline_id == p.id,
                    PipelineUserAccess.user_id == int(user_id))
            ).scalar_one_or_none()
            if row is None:
                row = PipelineUserAccess(pipeline_id=p.id, user_id=int(user_id),
                                         created_by_id=created_by_id, **norm)
                s.add(row)
            else:
                for k, v in norm.items():
                    setattr(row, k, v)
                row.updated_at = datetime.utcnow()
            s.flush()
            return _access_to_dict(row)

    def access_counts_by_user(self) -> Dict[int, int]:
        """{user_id: number of pipelines they can VIEW} in ONE aggregate query.

        Exists so the users table can show a pipeline-access column without issuing a
        request per row - an N+1 that would scale with the size of the user directory.
        """
        with get_session() as s:
            rows = s.execute(
                select(PipelineUserAccess.user_id, func.count(PipelineUserAccess.id))
                .where(PipelineUserAccess.can_view.is_(True))
                .group_by(PipelineUserAccess.user_id)
            ).all()
            return {int(uid): int(n) for uid, n in rows}

    def delete_access(self, pipeline_id: str, user_id: int) -> bool:
        with get_session() as s:
            p = s.execute(
                select(Pipeline).where(Pipeline.pipeline_id == str(pipeline_id))
            ).scalar_one_or_none()
            if p is None:
                return False
            res = s.execute(
                sa_delete(PipelineUserAccess).where(
                    PipelineUserAccess.pipeline_id == p.id,
                    PipelineUserAccess.user_id == int(user_id))
            )
            return bool(res.rowcount)


# Module-level singleton for convenience; the class is stateless.
repository = PipelineRepository()
