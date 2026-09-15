# Historical pre-fix reproductions; current behavioral coverage is in tests/test_event_delivery_regressions.py.
"""Diagnostic reproductions: passing tests confirm current problematic behavior.
No cameras, production database, or external HTTP destinations are used.
"""
import logging
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from InferenceNode.pipeline import InferencePipeline
from InferenceEngine.engines.ultralytics_engine import UltralyticsEngine
from InferenceEngine.engines.simple_custom_engine import SimpleCustomEngine
from InferenceEngine.engines.geti_engine import GetiEngine


def test_folder_frames_can_be_consumed_without_inference():
    p = InferencePipeline()
    p.TARGET_INFERENCE_FPS = 5
    p._frame_source_config = {'capture_type': 'folder', 'auto_delete': True}
    reads, inferred, deletion_calls = [], [], []

    def read():
        if len(reads) == 2:
            p._stop_requested = True
            return False, None
        reads.append(len(reads))
        return True, np.zeros((2, 2, 3), dtype=np.uint8)

    p.source = SimpleNamespace(connect=lambda: True, isOpened=lambda: True,
                               read=read, stop=lambda: None)
    p.inference_engine = SimpleNamespace(
        infer=lambda frame: inferred.append(frame) or [],
        result_to_json=lambda result: {'predictions': []})
    p.result_publisher = SimpleNamespace(do_any_destinations_need_result_image=lambda: False)
    # The fixed clock ensures two arrivals within one inference period.
    with patch('InferenceNode.pipeline.time.perf_counter', return_value=100.0), \
         patch.object(p, '_delete_current_image', side_effect=lambda: deletion_calls.append(1)):
        try:
            p.run()
            assert len(reads) == 2
            assert len(inferred) == 1
            assert len(deletion_calls) == 2
        finally:
            p._publisher_stop_event.set()
            p._publisher_thread.join(timeout=2)


def test_cleanup_discards_a_ready_candidate_after_capture_stall():
    p = InferencePipeline()
    det = {'class_name': 'person', 'confidence': .7, 'track_id': 7, 'bbox': [0, 0, 2, 2]}
    p._update_track_candidate(det, None, 100.0)
    with patch('InferenceNode.pipeline.time.time', return_value=111.0):
        assert p._track_ready(next(iter(p._track_best.values())), 111.0)
        p._cleanup_tracks()
        assert p._collect_ready_tracks(111.0) == []
        assert p._dropped_events == 0


def test_successful_tracker_fallback_is_not_remembered():
    e = UltralyticsEngine.__new__(UltralyticsEngine)
    e.device = 'cpu'
    e.use_openvino = False
    e.tracking_enabled = True
    e.tracker = 'broken-primary.yaml'
    calls = []

    def track(frame, **kw):
        calls.append(kw['tracker'])
        if kw['tracker'] != 'bytetrack.yaml':
            raise RuntimeError('synthetic tracker failure')
        return []

    e.model = SimpleNamespace(track=track)
    for _ in range(2):
        e._infer(np.zeros((2, 2, 3), dtype=np.uint8))
    assert calls == ['broken-primary.yaml', 'ocsort.yaml', 'bytetrack.yaml'] * 2


def test_node_id_is_populated_with_pipeline_id():
    p = InferencePipeline()
    p.id = 'pipeline-A'
    payload = p._build_payload({'class_name': 'person'}, {})
    assert payload['node_id'] == payload['pipeline_id'] == 'pipeline-A'


def test_custom_example_returns_string_for_dict_format():
    e = SimpleCustomEngine.__new__(SimpleCustomEngine)
    assert isinstance(e.result_to_json({}, output_format='dict'), str)


def test_geti_preserves_nested_object_instead_of_detection_list():
    e = GetiEngine.__new__(GetiEngine)
    e.logger = logging.getLogger('diagnostic-geti')
    result = SimpleNamespace(model_dump=lambda: {'annotations': [{'labels': [{'name': 'person'}]}]})
    converted = e.result_to_json(result)
    assert isinstance(converted['predictions'], dict)
    assert list(converted['predictions']) == ['annotations']
