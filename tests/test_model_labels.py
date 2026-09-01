"""Safe class-label reads (model_repo.read_model_labels).

The engine builder prefills class mapping from a stored model. That read must
never become a file-disclosure or code-execution primitive, and must degrade
honestly instead of inventing labels.
"""
import os
import sys
import json

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.model_repo import (                    # noqa: E402
    ModelRepository, read_model_labels, UNSAFE_LABELS_REASON,
    _coerce_labels,
)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """The repository is PostgreSQL-backed now (SQLite here); bytes live under an
    isolated ARTIFACT_ROOT. models_metadata.json is a migration source only."""
    from InferenceNode.auth import db as auth_db
    from InferenceNode.auth.models import Base
    import InferenceNode.data_models  # noqa: F401
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'labels.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    r = ModelRepository(str(tmp_path / "repo"))
    yield r
    auth_db._engine = None; auth_db._SessionLocal = None


def _store(repo, tmp_path, filename, content=b"not a real model"):
    src = tmp_path / filename
    src.write_bytes(content)
    return repo.store_model(str(src), filename, engine_type="yolo")


# --------------------------------------------------------------------------- #
# resolution: id only, never a path
# --------------------------------------------------------------------------- #
def test_unknown_model_id_raises_keyerror(repo):
    with pytest.raises(KeyError):
        read_model_labels(repo, "does-not-exist")


@pytest.mark.parametrize("evil", [
    "../../../etc/passwd",
    "..\\..\\Windows\\System32\\config\\SAM",
    "/etc/shadow",
    "C:\\Windows\\win.ini",
    "....//....//secret.pt",
    "%2e%2e%2fsecret.pt",
])
def test_traversal_ids_are_simply_not_found(repo, evil):
    """Models are looked up in a dict by id, so a path is not even expressible -
    a traversal attempt is indistinguishable from any other unknown id, and no
    filesystem access happens at all."""
    with pytest.raises(KeyError):
        read_model_labels(repo, evil)


def test_poisoned_registry_path_is_refused(repo, tmp_path):
    """Defence in depth: even if the registry row's relative_path is tampered with to
    point outside the artifact root, the resolver refuses it and the read degrades
    honestly. Rows from PostgreSQL are never trusted blindly."""
    from sqlalchemy import select, update
    from InferenceNode.auth.db import get_session
    from InferenceNode.data_models import ModelArtifact
    model_id = _store(repo, tmp_path, "real.pt")
    outside = tmp_path / "outside.pt"
    outside.write_bytes(b"stolen")
    with get_session() as s:
        s.execute(update(ModelArtifact).values(relative_path="../../outside.pt"))
    assert repo.get_model_path(model_id) is None
    labels, reason = read_model_labels(repo, model_id)
    assert labels is None
    assert reason == UNSAFE_LABELS_REASON


def test_missing_file_degrades_honestly(repo, tmp_path):
    model_id = _store(repo, tmp_path, "gone.pt")
    os.remove(repo.get_model_path(model_id))
    labels, reason = read_model_labels(repo, model_id)
    assert labels is None
    assert reason == UNSAFE_LABELS_REASON


# --------------------------------------------------------------------------- #
# honest degradation
# --------------------------------------------------------------------------- #
def test_unsupported_extension_is_never_opened(repo, tmp_path, monkeypatch):
    """A .bin/.txt model must not be handed to any loader at all."""
    model_id = _store(repo, tmp_path, "weights.bin")

    import InferenceNode.model_repo as mr
    called = []
    monkeypatch.setattr(mr, "_labels_from_onnx", lambda p: called.append(p))
    monkeypatch.setattr(mr, "_labels_from_ultralytics", lambda p: called.append(p))

    labels, reason = read_model_labels(repo, model_id)
    assert labels is None
    assert reason == UNSAFE_LABELS_REASON
    assert called == []


def test_corrupt_pt_does_not_raise(repo, tmp_path):
    """A .pt that is not a real checkpoint must produce the honest null, not a
    500 - the wizard falls back to manual mapping."""
    model_id = _store(repo, tmp_path, "corrupt.pt", b"\x00\x01garbage")
    labels, reason = read_model_labels(repo, model_id)
    assert labels is None
    assert reason == UNSAFE_LABELS_REASON


def test_corrupt_onnx_does_not_raise(repo, tmp_path):
    model_id = _store(repo, tmp_path, "corrupt.onnx", b"\x00\x01garbage")
    labels, reason = read_model_labels(repo, model_id)
    assert labels is None
    assert reason == UNSAFE_LABELS_REASON


def test_missing_optional_dependency_degrades(repo, tmp_path, monkeypatch):
    """If onnx/ultralytics are not installed, say so honestly rather than
    failing the request."""
    model_id = _store(repo, tmp_path, "m.onnx")
    import builtins
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name in ("onnx", "ultralytics"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    labels, reason = read_model_labels(repo, model_id)
    assert labels is None
    assert reason == UNSAFE_LABELS_REASON


# --------------------------------------------------------------------------- #
# success path
# --------------------------------------------------------------------------- #
def test_successful_read_returns_labels(repo, tmp_path, monkeypatch):
    model_id = _store(repo, tmp_path, "yolo.pt")
    import InferenceNode.model_repo as mr
    monkeypatch.setattr(mr, "_labels_from_ultralytics",
                        lambda p: {"0": "person", "1": "car"})
    labels, reason = read_model_labels(repo, model_id)
    assert labels == {"0": "person", "1": "car"}
    assert reason is None


def test_read_stays_inside_the_repository(repo, tmp_path, monkeypatch):
    """Whatever path a loader is handed must be the repository copy."""
    model_id = _store(repo, tmp_path, "yolo.pt")
    seen = {}
    import InferenceNode.model_repo as mr

    def spy(path):
        seen["path"] = path
        return {"0": "person"}
    monkeypatch.setattr(mr, "_labels_from_ultralytics", spy)

    read_model_labels(repo, model_id)
    models_dir = os.path.abspath(repo.models_dir)
    assert os.path.commonpath([os.path.abspath(seen["path"]), models_dir]) == models_dir


@pytest.mark.parametrize("names,expected", [
    ({0: "person", 1: "car"}, {"0": "person", "1": "car"}),
    (["person", "car"], {"0": "person", "1": "car"}),
    (("a",), {"0": "a"}),
    ({}, None),
    ([], None),
    (None, None),
    ("person", None),
    (42, None),
])
def test_coerce_labels_shapes(names, expected):
    """Ultralytics returns a dict, ONNX metadata often a list - both normalise to
    {index: name}; anything else is rejected rather than guessed at."""
    assert _coerce_labels(names) == expected
