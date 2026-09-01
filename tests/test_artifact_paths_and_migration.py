"""Phase 5 - single path resolver + physical migration primitive."""
import hashlib
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from InferenceNode import artifact_paths as ap                       # noqa: E402
from InferenceNode.artifact_migration import (                       # noqa: E402
    stage_copy_verify_promote, resolve_legacy_path, enumerate_representation_dir,
    manifest_sha256, sha256_file)
from InferenceNode.artifact_states import (                          # noqa: E402
    ArtifactStatus, Reason, ValidationStatus, transition, IllegalTransition,
    is_servable, fingerprint, ArtifactStatus as S)


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("ARMYEYE_ARTIFACT_ROOT", str(tmp_path / "root"))
    ap.ensure_layout()
    return tmp_path


# ------------------------------------------------------------------ resolver
@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd", "C:/win/x", "%2e%2e/x", "..%2fx", "", "   "])
def test_resolver_rejects_traversal_absolute_and_encoded(root, bad):
    with pytest.raises(ap.ArtifactPathError):
        ap.resolve("models", bad)


def test_resolver_accepts_normal_relative_paths(root):
    p = ap.resolve("models", "m1/model.pt")
    assert p.startswith(os.path.realpath(ap.kind_root("models")))
    assert ap.to_relative("models", p) == "m1/model.pt"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unsupported")
def test_resolver_rejects_symlink_escape(root, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    link = os.path.join(ap.kind_root("models"), "escape")
    try:
        os.symlink(str(outside), link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlink on this host")
    with pytest.raises(ap.ArtifactPathError):
        ap.resolve("models", "escape/anything")


def test_no_module_joins_artifact_roots_outside_the_resolver():
    """The single-resolver rule: nobody else builds paths under ARTIFACT_ROOT."""
    offenders = []
    for dirpath, _d, files in os.walk(os.path.join(REPO, "InferenceNode")):
        for f in files:
            if not f.endswith(".py") or f in ("artifact_paths.py",):
                continue
            src = open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read()
            if re.search(r"os\.path\.join\(\s*(artifact_root|kind_root)\(", src):
                offenders.append(f)
    assert not offenders, f"resolve() is the only place that may join artifact roots: {offenders}"


# ------------------------------------------------------------------ migration primitive
def _write(p, data=b"hello"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def test_copy_verify_promote_registers_only_verified_bytes(root, tmp_path):
    src = _write(str(tmp_path / "legacy" / "m.pt"), b"model-bytes")
    r = stage_copy_verify_promote("models", src, "m1/m.pt")
    assert r.status is ArtifactStatus.AVAILABLE and r.validation_status is ValidationStatus.PASSED
    dst = ap.resolve("models", "m1/m.pt")
    assert sha256_file(dst)[0] == r.sha256 == hashlib.sha256(b"model-bytes").hexdigest()
    assert os.path.exists(src), "legacy source is retained for rollback"
    assert not os.path.exists(ap.staging_path("models", "m1/m.pt")), "staging area is clean after promote"
    assert r.fingerprint and r.fingerprint["verified_size_bytes"] == len(b"model-bytes")


def test_copy_is_idempotent_and_collision_safe(root, tmp_path):
    src = _write(str(tmp_path / "legacy" / "m.pt"), b"same")
    assert stage_copy_verify_promote("models", src, "m1/m.pt").status is ArtifactStatus.AVAILABLE
    assert stage_copy_verify_promote("models", src, "m1/m.pt").status is ArtifactStatus.AVAILABLE  # rerun ok
    other = _write(str(tmp_path / "legacy2" / "m.pt"), b"DIFFERENT")
    r = stage_copy_verify_promote("models", other, "m1/m.pt")                                # collision
    assert r.status is ArtifactStatus.FAILED and r.reason is Reason.COPY_FAILED
    assert open(ap.resolve("models", "m1/m.pt"), "rb").read() == b"same", "never overwritten"


def test_missing_source_is_missing_with_reason_not_a_fake_state(root, tmp_path):
    r = stage_copy_verify_promote("models", str(tmp_path / "nope.pt"), "m1/nope.pt")
    assert r.status is ArtifactStatus.MISSING and r.reason is Reason.LEGACY_FILE_NOT_FOUND
    assert r.status.value in ("STAGING", "VALIDATING", "AVAILABLE", "FAILED", "MISSING", "CORRUPT", "DELETING")


def test_legacy_path_resolution_unique_zero_and_ambiguous(root, tmp_path):
    d1 = tmp_path / "a"; d2 = tmp_path / "b"
    _write(str(d1 / "x.pt")); _write(str(d2 / "x.pt")); _write(str(d1 / "unique.pt"))
    p, why = resolve_legacy_path(r"C:\gone\unique.pt", [str(d1), str(d2)])
    assert p and p.endswith("unique.pt") and why is None
    p, why = resolve_legacy_path(r"C:\gone\x.pt", [str(d1), str(d2)])
    assert p is None and why is Reason.AMBIGUOUS_LEGACY_PATH
    p, why = resolve_legacy_path(r"C:\gone\zzz.pt", [str(d1), str(d2)])
    assert p is None and why is Reason.LEGACY_FILE_NOT_FOUND


def test_openvino_dir_enumerates_xml_and_bin_only(root, tmp_path):
    d = tmp_path / "yolo_openvino_model"
    _write(str(d / "yolo.xml"), b"<xml/>"); _write(str(d / "yolo.bin"), b"\x00\x01")
    _write(str(d / "metadata.yaml"), b"a: 1"); _write(str(d / "notes.txt"), b"junk")
    fmt, comps, other = enumerate_representation_dir(str(d))
    assert fmt == "openvino"
    assert sorted(os.path.basename(c) for c in comps) == ["metadata.yaml", "yolo.bin", "yolo.xml"]
    assert [os.path.basename(o) for o in other] == ["notes.txt"], "unknown files reported, not registered"


def test_manifest_hash_is_deterministic_and_order_independent():
    a = [("m/x.xml", 3, "aa"), ("m/x.bin", 2, "bb")]
    b = list(reversed(a))
    assert manifest_sha256(a) == manifest_sha256(b)
    assert manifest_sha256(a) != manifest_sha256([("m/x.xml", 3, "aa"), ("m/x.bin", 99, "bb")])
    assert manifest_sha256(a) != hashlib.sha256(b"m").hexdigest()  # never the dir name


# ------------------------------------------------------------------ state machine + serving
def test_transition_table_legal_and_illegal_edges():
    assert transition(S.STAGING, S.VALIDATING) is S.VALIDATING
    assert transition(S.VALIDATING, S.AVAILABLE) is S.AVAILABLE
    assert transition(S.AVAILABLE, S.VALIDATING) is S.VALIDATING       # revalidation
    assert transition(S.VALIDATING, S.CORRUPT) is S.CORRUPT
    assert transition(S.VALIDATING, S.MISSING) is S.MISSING
    for cur in (S.CORRUPT, S.MISSING, S.FAILED):
        assert transition(cur, S.VALIDATING) is S.VALIDATING
        with pytest.raises(IllegalTransition):
            transition(cur, S.AVAILABLE)                                 # must pass VALIDATING
    with pytest.raises(IllegalTransition):
        transition(S.STAGING, S.AVAILABLE)
    with pytest.raises(IllegalTransition):
        transition(S.DELETING, S.AVAILABLE)
    with pytest.raises(IllegalTransition):
        transition("NEEDS_REVIEW", S.AVAILABLE)


def test_non_available_artifact_is_never_served(root, tmp_path):
    f = _write(str(tmp_path / "f.bin"), b"x")
    fp = fingerprint(f)
    for st in (S.STAGING, S.VALIDATING, S.FAILED, S.MISSING, S.CORRUPT, S.DELETING):
        assert not is_servable(st, "PASSED", f, fp)
    assert not is_servable(S.AVAILABLE, "FAILED", f, fp)
    assert not is_servable(S.AVAILABLE, "PASSED", str(tmp_path / "gone"), fp)
    assert is_servable(S.AVAILABLE, "PASSED", f, fp)


def test_changed_fingerprint_invalidates_cached_verdict(root, tmp_path):
    f = _write(str(tmp_path / "f.bin"), b"xx")
    fp = fingerprint(f)
    assert is_servable(S.AVAILABLE, "PASSED", f, fp)
    with open(f, "ab") as h:
        h.write(b"y")                       # size + mtime change
    assert not is_servable(S.AVAILABLE, "PASSED", f, fp)
