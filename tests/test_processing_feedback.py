import json
from unittest.mock import patch

import numpy as np
import pytest
import requests

from InferenceNode.candidate_quality import candidate_quality
from ResultPublisher.plugins.webhook_destination import _classify_status
from test_event_delivery_regressions import pipeline, Destination, detection, job
from test_webhook_delivery import server, clean_env, _dest


@pytest.mark.parametrize('state', ['saved', 'no_face', 'quality_rejected', 'duplicate', 'failed', 'invalid_image'])
def test_processing_results_are_distinct_from_transport_acceptance(state):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({'processing_status': state}).encode()
    result = _classify_status(response)
    assert result.success and result.outcome == 'FACE_' + state.upper()


def test_processing_pending_is_backpressure_without_destination_failure():
    response = requests.Response()
    response.status_code = 202
    response._content = b'{"processing_status":"pending"}'
    result = _classify_status(response)
    assert not result.success and result.retryable
    assert not result.count_toward_destination_failure
    assert result.retry_after == 2


@pytest.mark.parametrize('outcome,expected', [('FACE_SAVED', 3), ('FACE_NO_FACE', 1),
                                           ('FACE_FAILED', 1), ('SUCCESS', 1)])
def test_only_committed_face_ends_capture_burst(outcome, expected):
    p = pipeline(Destination('receiver'))
    j = job(p)
    j['processing_outcomes'] = {'receiver': outcome}
    p._handle_publish_success(j, {'successful_destinations': ['receiver']})
    assert p._track_last_sent[j['track_key']]['capture_count'] == expected


def test_visible_sharp_face_can_replace_higher_person_confidence():
    p = pipeline()
    p._apply_detection_config({'person_quality_selection': True})
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    first = dict(detection(), bbox=[0, 0, 100, 100], confidence=.99)
    later = dict(first, confidence=.6)
    with patch('InferenceNode.candidate_quality.candidate_quality', side_effect=[(0, 100, .99), (1, 40, .6)]):
        p._update_track_candidate(first, frame, 1000)
        assert p._collect_ready_tracks(1000) == []
        p._update_track_candidate(later, frame, 1000.5)
    selected = p._collect_ready_tracks(1001.5)
    assert len(selected) == 1 and selected[0]['det']['confidence'] == .6


def test_quality_hint_never_rejects_a_person_without_a_frontal_face():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    det = dict(detection(), bbox=[0, 0, 100, 100])
    quality = candidate_quality(frame, det)
    assert quality is not None and quality[0] == 0
    assert candidate_quality(frame, dict(det, bbox=[100, 100, 100, 100])) is None


def test_quality_ranking_is_opt_in():
    p = pipeline()
    assert not p.PERSON_QUALITY_SELECTION
    p._apply_detection_config({'person_quality_selection': 'false'})
    assert not p.PERSON_QUALITY_SELECTION
    p._apply_detection_config({'person_quality_selection': True})
    assert p.PERSON_QUALITY_SELECTION


def test_pending_then_saved_preserves_event_and_stops_followups(server, monkeypatch):
    script, port = server
    script.push(202, body=b'{"processing_status":"pending"}')
    script.push(200, body=b'{"processing_status":"saved"}')
    destination = _dest(port, monkeypatch)
    p = pipeline()
    p.result_publisher.add(destination)
    j = job(p)
    with patch.object(p, '_interruptible_wait', return_value=False):
        p._deliver_job(j)
    assert len(script.requests) == 2
    events = [json.loads(r['body'])['event_id'] for r in script.requests]
    assert events[0] == events[1]
    assert p._track_last_sent[j['track_key']]['capture_count'] == 3
    assert destination.failure_count == 0
    assert destination.frame_count == 1
