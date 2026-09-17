"""Production must not silently continue without required schema constraints."""
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from InferenceNode.auth import bootstrap
from InferenceNode.auth.models import Base


@pytest.mark.parametrize("postgres", [True, False])
def test_failed_migration_only_falls_back_for_non_postgres(monkeypatch, postgres):
    failure = RuntimeError("reference integrity preflight failed")
    monkeypatch.setattr(bootstrap.auth_db, "require_configured", lambda: None)
    monkeypatch.setattr(bootstrap.auth_db, "advisory_lock", nullcontext)
    monkeypatch.setattr(bootstrap.auth_db, "is_postgres", lambda: postgres)
    monkeypatch.setattr(bootstrap, "_alembic_upgrade_head", Mock(side_effect=failure))
    create = Mock()
    seed = Mock()
    monkeypatch.setattr(Base.metadata, "create_all", create)
    monkeypatch.setattr(bootstrap, "seed_admin", seed)
    if postgres:
        with pytest.raises(RuntimeError, match="reference integrity preflight failed"):
            bootstrap.bootstrap_database()
        create.assert_not_called()
        seed.assert_not_called()
    else:
        bootstrap.bootstrap_database()
        create.assert_called_once()
        seed.assert_called_once()
