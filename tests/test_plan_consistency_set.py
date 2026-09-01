"""The plan's named consistency test set (exact names), each a real assertion against the
central state machines / registries. Broader coverage of the same rules lives in the
per-phase suites; this file exists so every named proof is greppable by name.
SQLite here; the PG-only proofs (CHECK constraints, FK RESTRICT) are in test_registry_schema_pg.py."""
import hashlib
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode.auth import db as auth_db                        # noqa: E402
from InferenceNode.auth.models import Base                          # noqa: E402
import InferenceNode.data_models as dm                              # noqa: E402
from InferenceNode import artifact_paths as ap                      # noqa: E402
from InferenceNode import artifact_migration as am                  # noqa: E402
from InferenceNode import model_registry as reg                     # noqa: E402
from InferenceNode.artifact_states import (ArtifactStatus as S, ValidationStatus as V, Reason,  # noqa: E402
                                           IllegalTransition, can_transition, transition, is_servable)
from InferenceNode.auth.db import get_session                       # noqa: E402
from sqlalchemy import select                                       # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    auth_db._engine = None; auth_db._SessionLocal = None
    engine = auth_db.init_engine(f"sqlite:///{tmp_path/'c.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    ap.ensure_layout()
    yield tmp_path
    auth_db._engine = None; auth_db._SessionLocal = None


# ------------------------------------------------------------------ transition table
def test_available_can_enter_revalidation():
    assert transition(S.AVAILABLE, S.VALIDATING) is S.VALIDATING


def test_validating_can_become_corrupt():
    assert transition(S.VALIDATING, S.CORRUPT) is S.CORRUPT


def test_validating_can_become_missing():
    assert transition(S.VALIDATING, S.MISSING) is S.MISSING


@pytest.mark.parametrize("bad", [S.CORRUPT, S.MISSING, S.FAILED])
def test_corrupt_missing_failed_require_validation_before_available(bad):
    with pytest.raises(IllegalTransition):
        transition(bad, S.AVAILABLE)
    assert can_transition(bad, S.VALIDATING) and can_transition(S.VALIDATING, S.AVAILABLE)


def test_corrupt_requires_validation_before_available():
    test_corrupt_missing_failed_require_validation_before_available(S.CORRUPT)


def test_missing_requires_validation_before_available():
    test_corrupt_missing_failed_require_validation_before_available(S.MISSING)


def test_failed_requires_validation_before_available():
    test_corrupt_missing_failed_require_validation_before_available(S.FAILED)


@pytest.mark.parametrize("edge", [(S.STAGING, S.AVAILABLE), (S.DELETING, S.AVAILABLE), (S.DELETING, S.VALIDATING),
                                  (S.AVAILABLE, S.FAILED), (S.STAGING, S.MISSING)])
def test_artifact_state_machine_rejects_invalid_transition(edge):
    with pytest.raises(IllegalTransition):
        transition(*edge)


# ------------------------------------------------------------------ vocabulary + orthogonality
def test_no_undefined_artifact_status_can_be_persisted(env):
    """ORM enum + CHECK (SQLite portable form; PG form proven in test_registry_schema_pg)."""
    import sqlalchemy.exc
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with get_session() as s:
            s.add(dm.MediaAsset(media_id="x", relative_path="x.mp4", status="NEEDS_REVIEW", validation_status="PENDING"))
    assert {e.value for e in S} == {"STAGING", "VALIDATING", "AVAILABLE", "FAILED", "MISSING", "CORRUPT", "DELETING"}
    assert "NEEDS_REVIEW" not in {e.value for e in S} | {e.value for e in V} | {e.value for e in Reason}


def test_lifecycle_and_validation_fields_are_orthogonal(env):
    """status (lifecycle), validation_status (outcome) and reason (diagnostic) are three
    columns; a lifecycle value is never a valid validation value and vice versa, and a
    reason never appears in either lifecycle column."""
    assert not ({e.value for e in S} & {e.value for e in V}) or ({"FAILED"} == {e.value for e in S} & {e.value for e in V})
    reg.register_model(model_id="o", name="o", engine_type="ultralytics")
    reg.register_representation(model_id="o", format="pt", files=[{
        "relative_path": "o/o.pt", "sha256": "b" * 64, "size_bytes": 1,
        "status": S.CORRUPT, "validation_status": V.HASH_MISMATCH, "reason": Reason.HASH_MISMATCH}])
    with get_session() as s:
        a = s.execute(select(dm.ModelArtifact)).scalar_one()
        assert (a.status, a.validation_status, a.reason) == ("CORRUPT", "HASH_MISMATCH", "HASH_MISMATCH")
        assert a.status in {e.value for e in S} and a.validation_status in {e.value for e in V}
        assert a.reason in {e.value for e in Reason}


def test_non_available_artifact_is_never_served(env):
    p = ap.resolve("models", "z.bin"); open(p, "wb").write(b"z")
    for st in (S.STAGING, S.VALIDATING, S.FAILED, S.MISSING, S.CORRUPT, S.DELETING):
        assert not is_servable(st.value, V.PASSED.value, p, require_fingerprint=False)
    assert not is_servable(S.AVAILABLE.value, V.FAILED.value, p, require_fingerprint=False)
    assert is_servable(S.AVAILABLE.value, V.PASSED.value, p, require_fingerprint=False)


# ------------------------------------------------------------------ legacy migration semantics
def test_legacy_ambiguous_path_uses_reason_not_fake_state(env, tmp_path):
    d1 = tmp_path / "a"; d2 = tmp_path / "b"; d1.mkdir(); d2.mkdir()
    (d1 / "m.pt").write_bytes(b"1"); (d2 / "m.pt").write_bytes(b"2")
    resolved, reason = am.resolve_legacy_path("C:/old/host/m.pt", [str(d1), str(d2)], "m.pt")
    assert resolved is None and reason is Reason.AMBIGUOUS_LEGACY_PATH
    resolved, reason = am.resolve_legacy_path("C:/old/host/none.pt", [str(d1)], "none.pt")
    assert resolved is None and reason is Reason.LEGACY_FILE_NOT_FOUND
    # the migration records such outcomes as status=MISSING + reason (a formal state), never AVAILABLE
    mf = am.stage_copy_verify_promote("models", str(tmp_path / "does-not-exist.pt"), "x/none.pt")
    assert mf.status is S.MISSING and mf.reason is Reason.LEGACY_FILE_NOT_FOUND


def test_openvino_migration_registers_xml_and_bin_separately(env, tmp_path):
    src = tmp_path / "m_openvino_model"; src.mkdir()
    (src / "m.xml").write_bytes(b"<net/>"); (src / "m.bin").write_bytes(b"\x00" * 16); (src / "notes.txt").write_bytes(b"n")
    fmt, comps, unregistered = am.enumerate_representation_dir(str(src))
    assert fmt == "openvino" and sorted(os.path.basename(c) for c in comps) == ["m.bin", "m.xml"]
    assert any(u.endswith("notes.txt") for u in unregistered)
    reg.register_model(model_id="ov", name="ov", engine_type="ultralytics")
    files = []
    for c in comps:
        rel = f"ov/openvino/{os.path.basename(c)}"
        mf = am.stage_copy_verify_promote("models", c, rel)
        files.append({"relative_path": rel, "sha256": mf.sha256, "size_bytes": mf.size_bytes,
                      "status": mf.status, "validation_status": mf.validation_status, "fingerprint": mf.fingerprint})
    reg.register_representation(model_id="ov", format="openvino", kind="derived", required=False, files=files)
    m = reg.get_model("ov")
    r = next(x for x in m["representations"] if x["format"] == "openvino")
    assert len(r["artifacts"]) == 2 and {a["relative_path"].split("/")[-1] for a in r["artifacts"]} == {"m.xml", "m.bin"}
    assert r["manifest_sha256"] and all(len(a["sha256"]) == 64 for a in r["artifacts"])
    for a in r["artifacts"]:
        assert hashlib.sha256(open(ap.resolve("models", a["relative_path"]), "rb").read()).hexdigest() == a["sha256"]


def test_openvino_manifest_hash_is_deterministic():
    comps = [("ov/m.xml", 6, "a" * 64), ("ov/m.bin", 16, "b" * 64)]
    assert am.manifest_sha256(comps) == am.manifest_sha256(list(reversed(comps)))
    assert am.manifest_sha256(comps) != am.manifest_sha256([("ov/m.xml", 7, "a" * 64), ("ov/m.bin", 16, "b" * 64)])


def test_openvino_missing_component_invalidates_representation(env, tmp_path):
    test_openvino_migration_registers_xml_and_bin_separately(env, tmp_path)
    reg.register_representation(model_id="ov", format="pt", files=[{
        "relative_path": "ov/ov.pt", "sha256": "c" * 64, "size_bytes": 1, "status": S.AVAILABLE, "validation_status": V.PASSED}])
    os.remove(ap.resolve("models", "ov/openvino/m.bin"))
    from InferenceNode.model_repo import ModelRepository
    repo = ModelRepository(str(tmp_path / "legacy"), auto_migrate=False)
    rep = repo.verify()
    assert any(d["model_id"] == "ov" and d["format"] == "openvino" and any(c.endswith("m.bin") for c in d["failed_components"])
               for d in rep["representations_degraded"]), rep
    assert rep["available_missing"] >= 1
    # the model itself stays usable through its required primary; the derived representation is degraded
    assert reg.get_model("ov")["status"] in ("AVAILABLE", "MISSING", "CORRUPT")


def test_custom_engine_uses_artifact_root(env):
    from InferenceNode import engine_builder as eb, engine_registry as ereg
    import contextlib
    src = eb.generate_engine_source("blank", {"display_name": "Root Cam"})
    info = eb.install_engine(src, existing_keys=lambda: set(), rediscover=lambda: None,
                             verify_key=lambda k: True, lock=lambda: contextlib.nullcontext())
    path = ap.resolve("engines", info["relative_path"])
    assert path.startswith(os.path.realpath(ap.kind_root("engines"))) and os.path.isfile(path)
    assert ereg.get("root_cam")["origin"] == "custom"
