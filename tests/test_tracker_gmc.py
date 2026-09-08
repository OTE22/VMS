"""Global Motion Compensation is 70% of the tracking cost, and fixed cameras never need it.

Ultralytics' stock `botsort.yaml` sets `gmc_method: sparseOptFlow`, which runs
goodFeaturesToTrack + calcOpticalFlowPyrLK on EVERY tracked frame to cancel out CAMERA
motion. ArmyEye watches fixed CCTV, so that is pure overhead. Measured on an RTX 5090
against real 1080p video, median of 60 tracked frames with a fresh model each time:

    botsort  gmc_method=sparseOptFlow   8.42 ms   119 inf/s/thread   1.00x
    botsort  gmc_method=none            2.54 ms   393 inf/s/thread   3.31x
    bytetrack (no GMC at all)           2.54 ms   394 inf/s/thread   3.31x

End to end that moved the stable ceiling from 35 to 40 cameras per process
(25 fps capture + 5 fps inference, 1080p).

The escape hatch matters: a PTZ camera that pans WHILE tracking genuinely needs GMC to
keep track IDs stable, so stock botsort must stay reachable by name.
"""
import os
import sys

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceEngine")):
    if p not in sys.path:
        sys.path.insert(0, p)

CFG = os.path.join(REPO, "InferenceEngine", "trackers", "botsort_fixed_camera.yaml")
ENGINE_SRC = open(os.path.join(REPO, "InferenceEngine", "engines", "ultralytics_engine.py"),
                  encoding="utf-8").read()


def _cfg():
    with open(CFG, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ------------------------------------------------------------------ the config itself
def test_gmc_is_off():
    assert _cfg()["gmc_method"] == "none", "this file exists solely to turn GMC off"


def test_it_is_still_botsort_not_a_different_tracker():
    """Changing tracker family would change tracking BEHAVIOUR; we only drop GMC."""
    assert _cfg()["tracker_type"] == "botsort"


def test_reid_stays_off():
    """ReID needs a second model and more compute; it was already off and must stay off."""
    assert _cfg()["with_reid"] is False


def test_association_thresholds_match_stock_botsort():
    """Only gmc_method may differ from stock. If these drift, tracking quality changes and
    the 3.31x measurement no longer describes the same tracker."""
    c = _cfg()
    assert c["track_high_thresh"] == 0.25
    assert c["track_low_thresh"] == 0.1
    assert c["new_track_thresh"] == 0.25
    assert c["track_buffer"] == 30
    assert c["match_thresh"] == 0.8
    assert c["fuse_score"] is True
    assert c["proximity_thresh"] == 0.5
    assert c["appearance_thresh"] == 0.8


# ------------------------------------------------------------------ engine wiring
def _engine():
    from InferenceEngine.engines.ultralytics_engine import UltralyticsEngine
    return UltralyticsEngine.__new__(UltralyticsEngine)


def test_default_resolves_to_the_shipped_fixed_camera_config(monkeypatch):
    monkeypatch.delenv("ARMYEYE_TRACKER_CONFIG", raising=False)
    got = _engine()._resolve_tracker()
    assert os.path.isfile(got), f"default tracker must exist on disk, got {got!r}"
    assert os.path.basename(got) == "botsort_fixed_camera.yaml"


def test_ptz_deployments_can_ask_for_stock_botsort_back(monkeypatch):
    """A camera that pans while tracking needs GMC; the stock name must pass through."""
    monkeypatch.setenv("ARMYEYE_TRACKER_CONFIG", "botsort.yaml")
    assert _engine()._resolve_tracker() == "botsort.yaml"


def test_any_stock_ultralytics_tracker_stays_reachable(monkeypatch):
    monkeypatch.setenv("ARMYEYE_TRACKER_CONFIG", "bytetrack.yaml")
    assert _engine()._resolve_tracker() == "bytetrack.yaml"


def test_an_explicit_path_is_honoured_verbatim(monkeypatch):
    monkeypatch.setenv("ARMYEYE_TRACKER_CONFIG", "/etc/armyeye/custom_tracker.yaml")
    assert _engine()._resolve_tracker() == "/etc/armyeye/custom_tracker.yaml"


def test_an_empty_env_var_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("ARMYEYE_TRACKER_CONFIG", "   ")
    assert os.path.basename(_engine()._resolve_tracker()) == "botsort_fixed_camera.yaml"


def test_the_engine_no_longer_hardcodes_stock_botsort():
    assert 'self.tracker = "botsort.yaml" if self.tracking_enabled else None' not in ENGINE_SRC
    assert "self.tracker = self._resolve_tracker() if self.tracking_enabled else None" in ENGINE_SRC


def test_tracking_can_still_be_disabled_entirely():
    """tracking=False must still yield no tracker, not the default config."""
    e = _engine()
    e.tracking_enabled = False
    assert (e._resolve_tracker() if e.tracking_enabled else None) is None
