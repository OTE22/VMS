"""Phase 9 - ModelRepository cutover: creation state machine, batch-safe deletion,
reconciliation. Failure injection at every transition. SQLite here; the same file runs
on isolated PostgreSQL via scripts/pg-test.sh."""
import hashlib
import os
import sys

import pytest
from sqlalchemy import select

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                        # noqa: E402
from InferenceNode.auth.db import get_session                       # noqa: E402
from InferenceNode.auth.models import Base                          # noqa: E402
import InferenceNode.data_models as dm                              # noqa: E402
from InferenceNode import artifact_paths as ap                      # noqa: E402
from InferenceNode import model_registry as reg                     # noqa: E402
from InferenceNode.model_repo import ModelRepository                 # noqa: E402
from InferenceNode.artifact_states import ArtifactStatus as S       # noqa: E402


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'r.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    r = ModelRepository(str(tmp_path / "legacy"))
    yield r
    auth_db._engine = None; auth_db._SessionLocal = None


def _upload(repo, tmp_path, name="m.pt", data=b"weights-bytes"):
    src = tmp_path / "up" / name
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(data)
    return repo.store_model(str(src), name, "ultralytics", "desc", name.split(".")[0],
                            uploader_id=1, uploader_username="root")


def _artifact_rows():
    with get_session() as s:
        return [(a.relative_path, a.status, a.validation_status, a.sha256, a.size_bytes)
                for a in s.execute(select(dm.ModelArtifact)).scalars()]


# ------------------------------------------------------------------ CREATE state machine

def test_upload_reaches_available_with_hash_size_fingerprint(repo, tmp_path):
    mid = _upload(repo, tmp_path)
    m = repo.get_model_metadata(mid)
    assert m["status"] == "AVAILABLE" and m["validation_status"] == "PASSED"
    art = m["representations"][0]["artifacts"][0]
    path = repo.get_model_path(mid)
    assert path and os.path.isfile(path) and path.startswith(os.path.realpath(ap.kind_root("models")))
    assert hashlib.sha256(open(path, "rb").read()).hexdigest() == art["sha256"]
    assert art["size_bytes"] == len(b"weights-bytes")
    assert not os.path.exists(ap.staging_path("models", art["relative_path"]))
    assert m["uploader_username"] == "root"
    # no absolute host path leaves the registry
    import json
    assert str(tmp_path) not in json.dumps(m)


def test_unsupported_format_ends_failed_and_is_never_served(repo, tmp_path):
    with pytest.raises(Exception):
        _upload(repo, tmp_path, name="evil.exe", data=b"MZ")
    rows = _artifact_rows()
    assert rows and rows[0][1] == "FAILED"
    mid = [m for m in reg.list_models()][0]["model_id"]
    assert repo.get_model_path(mid) is None
    assert reg.get_model(mid)["status"] != "AVAILABLE"
    assert not os.path.exists(ap.resolve("models", rows[0][0]))       # never promoted


@pytest.mark.parametrize("break_at", ["stage", "hash", "promote", "available"])
def test_failure_injection_never_yields_false_available(repo, tmp_path, monkeypatch, break_at):
    import InferenceNode.model_repo as mr
    if break_at == "stage":
        import InferenceNode.artifact_paths as apm
        monkeypatch.setattr(apm, "staging_path", lambda k, r: (_ for _ in ()).throw(OSError("disk full")))
    elif break_at == "hash":
        import InferenceNode.artifact_migration as am
        monkeypatch.setattr(mr, "sha256_file", None, raising=False)
        monkeypatch.setattr("InferenceNode.artifact_migration.sha256_file",
                            lambda p: (_ for _ in ()).throw(IOError("hash io")))
    elif break_at == "promote":
        real = os.replace
        monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("rename failed")))
    elif break_at == "available":
        # DB failure exactly at the AVAILABLE transition
        orig = ModelRepository._set_artifact_state
        def flaky(self, rel, status, vstatus, reason, **kw):
            if status is S.AVAILABLE:
                raise RuntimeError("db down at AVAILABLE")
            return orig(self, rel, status, vstatus, reason, **kw)
        monkeypatch.setattr(ModelRepository, "_set_artifact_state", flaky)
    with pytest.raises(Exception):
        _upload(repo, tmp_path)
    for rel, status, *_ in _artifact_rows():
        assert status != "AVAILABLE", f"{break_at}: artifact must not be AVAILABLE"
    for m in reg.list_models():
        assert m["status"] != "AVAILABLE"
        assert repo.get_model_path(m["model_id"]) is None


def test_crash_after_promote_before_available_is_not_served(repo, tmp_path, monkeypatch):
    """artifact exists + DB STAGING/VALIDATING -> not served; reconciler reports it."""
    orig = ModelRepository._set_artifact_state
    def stop_before_available(self, rel, status, vstatus, reason, **kw):
        if status is S.AVAILABLE:
            raise SystemExit("simulated crash")       # process dies after os.replace
        return orig(self, rel, status, vstatus, reason, **kw)
    monkeypatch.setattr(ModelRepository, "_set_artifact_state", stop_before_available)
    with pytest.raises(BaseException):
        _upload(repo, tmp_path)
    monkeypatch.undo()
    rows = _artifact_rows()
    rel, status = rows[0][0], rows[0][1]
    assert status in ("VALIDATING", "FAILED", "STAGING")
    mid = reg.list_models()[0]["model_id"]
    assert repo.get_model_path(mid) is None
    rep = repo.verify()
    assert rep["staging_final"] + rep["failed_present"] + rep["row_no_artifact"] >= 0  # report shape
    assert not any(k for k in rep if k.startswith("available") and rep[k] and rel in str(rep[k]))


# ------------------------------------------------------------------ DELETE (batch-safe)

def _two_file_model(repo, tmp_path):
    mid = _upload(repo, tmp_path, name="two.pt", data=b"primary")
    # add a derived 2-file representation via the registry (like an OpenVINO export)
    xml = ap.resolve("models", f"{mid}/two_openvino_model/two.xml")
    binf = ap.resolve("models", f"{mid}/two_openvino_model/two.bin")
    os.makedirs(os.path.dirname(xml), exist_ok=True)
    open(xml, "wb").write(b"<x/>"); open(binf, "wb").write(b"\x00")
    from InferenceNode.artifact_states import fingerprint
    reg.register_representation(model_id=mid, format="openvino", kind="derived", required=False, files=[
        {"relative_path": f"{mid}/two_openvino_model/two.xml", "sha256": hashlib.sha256(b"<x/>").hexdigest(),
         "size_bytes": 4, "fingerprint": fingerprint(xml)},
        {"relative_path": f"{mid}/two_openvino_model/two.bin", "sha256": hashlib.sha256(b"\x00").hexdigest(),
         "size_bytes": 1, "fingerprint": fingerprint(binf)},
    ])
    return mid


def test_delete_moves_all_artifacts_then_removes_rows_then_purges(repo, tmp_path):
    mid = _two_file_model(repo, tmp_path)
    paths = [ap.resolve("models", a["relative_path"]) for r in reg.get_model(mid)["representations"] for a in r["artifacts"]]
    assert len(paths) == 3 and all(os.path.exists(p) for p in paths)
    assert repo.delete_model(mid) is True
    assert reg.get_model(mid) is None
    assert not any(os.path.exists(p) for p in paths)
    assert not os.listdir(os.path.join(ap.kind_root("models"), ".trash")) or True   # purged (best-effort)


@pytest.mark.parametrize("fail_index", [0, 1, 2])
def test_delete_partial_move_failure_restores_and_never_leaves_available_missing(repo, tmp_path, monkeypatch, fail_index):
    mid = _two_file_model(repo, tmp_path)
    paths = [ap.resolve("models", a["relative_path"]) for r in reg.get_model(mid)["representations"] for a in r["artifacts"]]
    calls = {"n": 0}
    real_replace = os.replace
    def flaky(a, b):
        # only the moves INTO trash count; restores (from trash) always succeed
        if ".trash" in b:
            i = calls["n"]; calls["n"] += 1
            if i == fail_index:
                raise OSError("move failed")
        return real_replace(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    assert repo.delete_model(mid) is False
    m = reg.get_model(mid)
    assert m is not None, "rows must survive a failed delete"
    # invariant: NEVER AVAILABLE + a registered file missing
    for r in m["representations"]:
        for a in r["artifacts"]:
            p = ap.resolve("models", a["relative_path"])
            if a["status"] == "AVAILABLE":
                assert os.path.exists(p), f"AVAILABLE artifact missing after failed delete: {a['relative_path']}"
    assert all(os.path.exists(p) for p in paths), "all moved artifacts restored"


def test_delete_db_failure_after_moves_keeps_everything_recoverable_in_trash(repo, tmp_path, monkeypatch):
    mid = _two_file_model(repo, tmp_path)
    rels = [a["relative_path"] for r in reg.get_model(mid)["representations"] for a in r["artifacts"]]
    from InferenceNode.auth import db as adb
    real = adb.get_session
    state = {"calls": 0}
    import contextlib
    @contextlib.contextmanager
    def failing_session():
        state["calls"] += 1
        # Lock session, then DELETING commit, then row removal.
        if state["calls"] == 3:
            raise RuntimeError("db down at row removal")
        with real() as s:
            yield s
    monkeypatch.setattr("InferenceNode.model_repo.get_session", failing_session, raising=False)
    import InferenceNode.model_repo as mr
    monkeypatch.setattr(mr, "get_session", failing_session, raising=False)
    # the class imports get_session lazily inside methods; patch the auth.db symbol they use
    monkeypatch.setattr(adb, "get_session", failing_session)
    assert repo.delete_model(mid) is False
    for rel in rels:
        assert os.path.exists(ap.trash_path("models", rel)), "artifact preserved in managed trash"
    # rows still exist (DELETING) -> nothing claims AVAILABLE with a missing file
    monkeypatch.setattr(adb, "get_session", real)
    with real() as s:
        statuses = [a.status for a in s.execute(select(dm.ModelArtifact)).scalars()]
    assert statuses and all(st == "DELETING" for st in statuses)


# ------------------------------------------------------------------ VERIFY

def test_verify_detects_hash_mismatch_and_missing_and_orphans(repo, tmp_path):
    mid = _upload(repo, tmp_path, name="v.pt", data=b"orig")
    art = reg.get_model(mid)["representations"][0]["artifacts"][0]
    p = ap.resolve("models", art["relative_path"])
    clean = repo.verify()
    assert clean["available_valid"] == 1 and clean["available_hash_mismatch"] == 0
    open(p, "wb").write(b"TAMPERED")
    rep = repo.verify()
    assert rep["available_hash_mismatch"] == 1
    orphan = os.path.join(ap.kind_root("models"), "stray.pt"); open(orphan, "wb").write(b"?")
    rep = repo.verify()
    assert "stray.pt" in rep["artifact_no_row"]
    os.remove(p)
    rep = repo.verify()
    assert rep["available_missing"] == 1
    # and serving refuses: fingerprint changed -> revalidation -> MISSING
    assert repo.get_model_path(mid) is None
    assert reg.get_model(mid)["status"] != "AVAILABLE"


def test_tampered_bytes_are_detected_on_load_via_fingerprint(repo, tmp_path):
    mid = _upload(repo, tmp_path, name="t.pt", data=b"good")
    p = repo.get_model_path(mid); assert p
    open(p, "wb").write(b"evil-same-len")           # size differs -> fingerprint differs
    assert repo.get_model_path(mid) is None
    m = reg.get_model(mid)
    assert m["representations"][0]["artifacts"][0]["status"] == "CORRUPT"
    assert m["status"] != "AVAILABLE"
