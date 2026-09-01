"""Phase 14 - dashboard/consumer cutover: counts come from authoritative APIs
(PostgreSQL registries), runtime vs persisted are labeled, and the models page renders the
registry lifecycle (status + validation_status) with escaped user text."""
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                        # noqa: E402
from InferenceNode.auth.models import Base                          # noqa: E402
import InferenceNode.data_models  # noqa: E402,F401
from InferenceNode.model_repo import ModelRepository                # noqa: E402

TPL = os.path.join(REPO, "InferenceNode", "templates")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'r.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    r = ModelRepository(str(tmp_path / "legacy"))
    yield r
    auth_db._engine = None; auth_db._SessionLocal = None


def test_storage_stats_distinguish_registered_from_available(repo, tmp_path):
    for name in ("a.pt", "b.pt"):
        src = tmp_path / "up" / name; src.parent.mkdir(exist_ok=True); src.write_bytes(name.encode() * 10)
        repo.store_model(str(src), name, "ultralytics", "d", name[:-3], uploader_id=1, uploader_username="root")
    stats = repo.get_storage_stats()
    assert stats["total_models"] == 2 and stats["available_models"] == 2
    # remove one file -> registry downgrades it on inspection -> available drops, registered stays
    b_id = next(mid for mid, m in repo.list_models().items() if m["name"] == "b")
    os.remove(repo.get_model_path(b_id))
    assert repo.get_model_path(b_id) is None      # missing file -> revalidation -> MISSING
    stats = repo.get_storage_stats()
    assert stats["total_models"] == 2 and stats["available_models"] == 1
    assert "repository_path" in stats and not os.path.isabs(stats["repository_path"])


def test_dashboard_labels_runtime_vs_persisted_and_reads_registry_counts():
    html = open(os.path.join(TPL, "dashboard.html"), encoding="utf-8").read()
    assert re.search(r"Total Pipelines.*?>persisted<", html), "persisted total not labeled"
    assert re.search(r"Active Pipelines.*?>runtime<", html), "runtime active not labeled"
    assert re.search(r"Average FPS.*?>runtime<", html) and re.search(r"Average Latency.*?>runtime<", html)
    assert "stats.available_models" in html and "stats.total_models" in html
    assert "fetch('/api/models')" in html and "fetch('/api/pipelines')" in html
    assert "logsModal" not in html                       # dead cluster stays gone


def test_models_page_renders_lifecycle_and_escapes_user_text():
    html = open(os.path.join(TPL, "models.html"), encoding="utf-8").read()
    assert "function modelStatusBadge(" in html
    assert "metadata.validation_status" in html and "metadata.status" in html
    assert "esc(metadata.name" in html and "esc(metadata.description)" in html
    assert "function esc(" not in html, "models.html must reuse the shared esc()"
    assert "${metadata.name || metadata.original_filename}" not in html, "unescaped name rendering came back"
