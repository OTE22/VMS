"""Behavioral coverage for frame selection, delivery, restart, and tracker fixes."""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from InferenceNode.pipeline import InferencePipeline
from InferenceNode.event_outbox import EventOutbox
from InferenceEngine.detection_contract import normalize_result
from InferenceEngine.engines.onnx_engine import OnnxEngine
from InferenceEngine.engines.geti_engine import GetiEngine
from InferenceEngine.engines.ultralytics_engine import UltralyticsEngine
from ResultPublisher import ResultPublisher


class Destination:
    enabled = True
    is_paused = False
    is_configured = True
    include_image_data = False
    include_result_image = False

    def __init__(self, ident, statuses=('success',), images=False):
        self._id = ident
        self.statuses = list(statuses)
        self.payloads = []
        self.include_image_data = images

    def publish_once(self, payload):
        self.payloads.append(dict(payload))
        status = self.statuses.pop(0) if self.statuses else 'success'
        return {'status': status}


def pipeline(*destinations):
    p = InferencePipeline()
    p.result_publisher = ResultPublisher()
    for d in destinations:
        p.result_publisher.add(d)
    p.PUBLISH_RETRY_DELAY_SECONDS = .001
    p.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS = .2
    return p


def detection(track=7):
    return dict(class_name='person', confidence=.95, bbox=[1, 1, 8, 8], track_id=track)


def job(p, track=7):
    d = detection(track)
    return dict(det=d, track_key=p._make_track_key(d), first_seen=time.time(),
                frame=np.zeros((12, 12, 3), dtype=np.uint8))


@pytest.mark.parametrize('capture', [0, 1789554600.125, None])
def test_capture_time_is_timezone_aware_and_preserved_across_retries(capture):
    destination = Destination('d', ('failed', 'success'))
    p = pipeline(destination)
    j = job(p)
    j['captured_at'] = capture
    expected = j['first_seen'] if capture is None else capture
    p._deliver_job(j)
    assert len(destination.payloads) == 2
    for payload in destination.payloads:
        stamp = datetime.fromisoformat(payload['captured_at'].replace('Z', '+00:00'))
        assert stamp.tzinfo == timezone.utc
        assert stamp.timestamp() == pytest.approx(expected, abs=0.000001, rel=0)
    assert destination.payloads[0]['captured_at'] == destination.payloads[1]['captured_at']
    assert destination.payloads[0]['event_id'] == destination.payloads[1]['event_id']


def test_legacy_outbox_capture_time_is_normalized_without_rebuilding_event(tmp_path):
    p = pipeline(Destination('d'))
    p._outbox = EventOutbox(tmp_path, p.id)
    j = job(p)
    p._prepare_job(j)
    j['payload']['captured_at'] = 1789554600.125
    original_id = j['payload']['event_id']
    j['accepted'] = ['already-delivered']
    p._persist_job(j)
    restored = p._outbox.records()[0]['job']
    p._prepare_job(restored)
    stamp = datetime.fromisoformat(restored['payload']['captured_at'].replace('Z', '+00:00'))
    assert stamp.timestamp() == 1789554600.125
    assert stamp.tzinfo == timezone.utc
    assert restored['payload']['event_id'] == original_id
    assert restored['accepted'] == ['already-delivered']
    payload = dict(restored['payload'])
    p._prepare_job(restored)
    assert restored['payload'] == payload


def test_partial_success_retries_only_failed_destination():
    a, b = Destination('a'), Destination('b', ('failed', 'success'))
    p = pipeline(a, b)
    p._deliver_job(job(p))
    assert len(a.payloads) == 1 and len(b.payloads) == 2
    assert a.payloads[0]['event_id'] == b.payloads[1]['event_id']
    assert p._publish_successes == 1


def test_terminal_track_and_moving_success_are_not_recollected():
    p = pipeline(Destination('d', ('permanent_failure',)))
    j = job(p)
    p._deliver_job(j)
    p._update_track_candidate(detection(), j.get('frame'), time.time())
    assert p._collect_ready_tracks(time.time()) == []
    p._terminal_tracks.clear()
    p._track_last_sent[p._make_track_key(detection())] = {'sent_at': time.time() - 2, 'bbox': [1, 1, 8, 8]}
    p._update_track_candidate(dict(detection(), bbox=[80, 80, 90, 90]), None, time.time())
    assert p._collect_ready_tracks(time.time()) == []


def test_untracked_failure_uses_backoff():
    p = pipeline()
    d = detection(None)
    j = p._register_iou_candidate(d, None, time.time())
    p._handle_publish_failure(j, 'failure')
    assert p._register_iou_candidate(d, None, time.time()) is None


def test_person_gets_three_fresh_views_then_cooldown():
    destination = Destination('d', images=True)
    p = pipeline(destination)
    now = 1000.0
    for capture in range(3):
        frame = np.full((12, 12, 3), capture * 80, dtype=np.uint8)
        p._update_track_candidate(detection(), frame, now)
        jobs = p._collect_ready_tracks(now)
        assert len(jobs) == 1
        assert np.array_equal(jobs[0]['frame'], frame)
        with patch('InferenceNode.pipeline.time.time', return_value=now):
            p._deliver_job(jobs[0])
        p._update_track_candidate(detection(), frame, now + 1)
        assert p._collect_ready_tracks(now + 1) == []
        now += 2
    assert len({payload['event_id'] for payload in destination.payloads}) == 3
    assert len({payload['image'] for payload in destination.payloads}) == 3
    p._update_track_candidate(detection(), frame, now)
    assert p._collect_ready_tracks(now) == []
    # A different tracker ID is still eligible during the original ID's cooldown.
    p._update_track_candidate(detection(8), frame, now)
    assert len(p._collect_ready_tracks(now)) == 1
    now = 1125.0
    p._update_track_candidate(detection(), frame, now)
    jobs = p._collect_ready_tracks(now)
    assert len(jobs) == 1
    with patch('InferenceNode.pipeline.time.time', return_value=now):
        p._deliver_job(jobs[0])
    assert p._track_last_sent[p._make_track_key(detection())]['capture_count'] == 1


@pytest.mark.parametrize('class_name,capture_count', [('car', 3), ('person', 1)])
def test_followup_capture_is_person_only_and_can_be_disabled(class_name, capture_count):
    p = pipeline(Destination('d'))
    p._apply_detection_config({'person_capture_count': capture_count})
    det = dict(detection(), class_name=class_name)
    p._update_track_candidate(det, None, 1000)
    jobs = p._collect_ready_tracks(1000)
    with patch('InferenceNode.pipeline.time.time', return_value=1000):
        p._deliver_job(jobs[0])
    p._update_track_candidate(det, None, 1003)
    assert p._collect_ready_tracks(1003) == []


@pytest.mark.parametrize('value', [0, -1, 4, 'bad', float('nan'), float('inf')])
def test_person_capture_count_rejects_unbounded_or_invalid_values(value):
    p = pipeline()
    p._apply_detection_config({'person_capture_count': value})
    assert p.PERSON_CAPTURE_COUNT == 3


def test_encode_once_across_retries_and_use_event_frame():
    d = Destination('d', ('failed', 'success'), images=True)
    d.include_result_image = True
    p = pipeline(d)
    p._latest_frame = np.full((12, 12, 3), 255, dtype=np.uint8)
    with patch('ResultPublisher.publisher.cv2.imencode', wraps=cv2.imencode) as encode:
        p._deliver_job(job(p))
    assert encode.call_count == 2
    assert d.payloads[0]['image'] == d.payloads[1]['image']
    import base64
    annotated = cv2.imdecode(np.frombuffer(base64.b64decode(d.payloads[0]['result_image']), np.uint8), 1)
    assert float(annotated.mean()) < 200  # event is black, latest preview is white


def test_encoding_failure_does_not_send_metadata_only():
    d = Destination('d', images=True)
    p = pipeline(d)
    with patch('ResultPublisher.publisher.cv2.imencode', return_value=(False, None)):
        assert not p._enqueue_publish_job(job(p))
    assert d.payloads == [] and p._dropped_events == 1


def test_ready_candidates_survive_cleanup_and_idle_source():
    d = Destination('d')
    p = pipeline(d)
    p._update_track_candidate(dict(detection(), confidence=.7), None, time.time() - 20)
    p._cleanup_tracks()
    p._start_publisher_worker()
    try:
        end = time.monotonic() + 2
        while not d.payloads and time.monotonic() < end:
            time.sleep(.01)
        assert len(d.payloads) == 1
    finally:
        p._flush_and_stop_publisher()


def test_outbox_restart_preserves_event_and_partial_ack(tmp_path):
    a, b = Destination('a'), Destination('b', ('failed', 'success'))
    p = pipeline(a, b)
    p._outbox = EventOutbox(tmp_path, p.id)
    j = job(p)
    p._prepare_job(j)
    p._persist_job(j)
    p._deliver_job(j, deferred=True)
    saved = p._outbox.records()[0]['job']
    q = pipeline(a, b)
    q._outbox = EventOutbox(tmp_path, p.id)
    q._deliver_job(saved)
    assert len(a.payloads) == 1 and len(b.payloads) == 2
    assert b.payloads[0]['event_id'] == b.payloads[1]['event_id']
    assert q._outbox.records() == []


def test_retry_delay_does_not_block_following_job():
    d = Destination('d', ('failed', 'success', 'success'))
    p = pipeline(d)
    p.PUBLISH_RETRY_DELAY_SECONDS = .25
    p._start_publisher_worker()
    try:
        p._enqueue_publish_job(job(p, 1))
        p._enqueue_publish_job(job(p, 2))
        end = time.monotonic() + 2
        while len(d.payloads) < 3 and time.monotonic() < end:
            time.sleep(.01)
        assert [x['results']['predictions'][0]['track_id'] for x in d.payloads] == [1, 2, 1]
    finally:
        p._flush_and_stop_publisher()


def test_folder_every_image_inferred_and_eof_worker_stops():
    p = pipeline(Destination('d'))
    p.TARGET_INFERENCE_FPS = 5
    p._frame_source_config = {'capture_type': 'folder'}
    reads, inferred = [], []
    def read():
        if len(reads) == 2:
            p._stop_requested = True
            return False, None
        reads.append(1)
        return True, np.zeros((12, 12, 3), dtype=np.uint8)
    p.source = SimpleNamespace(connect=lambda: True, isOpened=lambda: True, read=read, stop=lambda: None)
    p.inference_engine = SimpleNamespace(infer=lambda f: inferred.append(1) or [],
                                        result_to_json=lambda _: {'predictions': []})
    p.run()
    assert len(inferred) == 2
    assert not p._publisher_thread.is_alive() and not p._candidate_thread.is_alive()


@pytest.mark.parametrize('fail', [False, True])
def test_file_deleted_only_after_all_events_acknowledged(tmp_path, fail):
    d = Destination('d', ('success', 'permanent_failure' if fail else 'success'))
    p = pipeline(d)
    p._outbox = EventOutbox(tmp_path, p.id)
    source = tmp_path / 'input.jpg'
    source.write_bytes(b'fixture')
    st = source.stat()
    jobs = [job(p, 1), job(p, 2)]
    for j in jobs:
        j.update(source_file=str(source), source_fingerprint=[st.st_size, st.st_mtime_ns],
                 file_group='group', file_group_size=2)
        p._prepare_job(j)
        p._persist_job(j)
    p._deliver_job(jobs[0])
    assert source.exists()
    p._deliver_job(jobs[1])
    assert source.exists() == fail


def test_full_queue_rejects_without_waiting():
    p = pipeline(Destination('d'))
    p.PUBLISH_QUEUE_BYTES = 1
    d = p.result_publisher.destinations[0]
    d.include_image_data = True
    start = time.monotonic()
    assert not p._enqueue_publish_job(job(p))
    assert time.monotonic() - start < .5


def test_viewer_count_and_shared_preview_encoding():
    p = pipeline()
    p._latest_frame = np.zeros((12, 12, 3), dtype=np.uint8)
    p.start_streaming(); p.start_streaming(); p.stop_streaming()
    assert p.is_streaming()
    with patch('InferenceNode.pipeline.cv2.imencode', wraps=cv2.imencode) as encode:
        assert p.get_preview_jpeg() == p.get_preview_jpeg()
        assert encode.call_count == 1
    p.stop_streaming()
    assert not p.is_streaming()


def test_onnx_geti_and_json_contract():
    e = OnnxEngine(cat_map={0: 'person'})
    raw = np.array([[[5], [5], [4], [4], [.95]]], dtype=np.float32)
    result = normalize_result(e.result_to_json(e._postprocess(raw)))
    assert result['predictions'][0]['bbox'] == [3, 3, 7, 7]
    assert result['predictions'][0]['class_name'] == 'person'
    g = GetiEngine.__new__(GetiEngine)
    g.logger = logging.getLogger('test')
    prediction = SimpleNamespace(model_dump=lambda: {'annotations': [
        {'shape': {'x': 1, 'y': 2, 'width': 3, 'height': 4},
         'labels': [{'name': 'person', 'probability': .8}]}]})
    assert normalize_result(g.result_to_json(prediction))['predictions'][0]['bbox'] == [1, 2, 4, 6]
    assert normalize_result(json.dumps({'predictions': [detection()]}))['num_detections'] == 1


def test_bad_model_load_and_nonfinite_settings_rejected():
    p = pipeline()
    with patch('InferenceNode.pipeline.FrameSourceFactory.create', return_value=SimpleNamespace(stop=lambda: None)), \
         patch('InferenceNode.pipeline.InferenceEngineFactory.create', return_value=SimpleNamespace(load=lambda: False)):
        with pytest.raises(RuntimeError, match='failed to load'):
            p.configure({}, {}, p.result_publisher)
    p._apply_detection_config({'target_inference_fps': 'nan', 'publish_max_retries': 'inf'})
    assert np.isfinite(p.TARGET_INFERENCE_FPS) and p.PUBLISH_MAX_RETRIES == 5


def test_tracker_fallback_remembered_and_uses_recovery_confidence():
    e = UltralyticsEngine.__new__(UltralyticsEngine)
    e.device, e.tracker = 'cpu', 'broken.yaml'
    e.tracking_enabled = True
    e.tracker_buffer_seconds, e.tracking_fps = 6, 5
    e._last_tracking_at, e.tracking_epoch = None, 0
    e.logger = logging.getLogger('test')
    calls = []
    def track(frame, **kw):
        calls.append(kw['tracker'])
        assert kw['conf'] == .1
        if kw['tracker'] != 'bytetrack.yaml':
            raise RuntimeError('fixture')
        return []
    e.model = SimpleNamespace(track=track)
    e._infer(np.zeros((12, 12, 3), dtype=np.uint8))
    e._infer(np.zeros((12, 12, 3), dtype=np.uint8))
    assert calls == ['broken.yaml', 'ocsort.yaml', 'bytetrack.yaml', 'bytetrack.yaml']


def test_outbox_full_does_not_damage_existing_event(tmp_path):
    p = pipeline(Destination('d'))
    p._outbox = EventOutbox(tmp_path, p.id, max_bytes=4096)
    first = job(p)
    assert p._enqueue_publish_job(first)
    p._outbox.max_bytes = 1
    second = job(p, 8)
    assert not p._enqueue_publish_job(second)
    assert p._queue_bytes == first['bytes']
    assert len(p._outbox.records()) == 1


def test_shutdown_tracks_an_inflight_request(tmp_path):
    entered, release = threading.Event(), threading.Event()
    d = Destination('d')
    def send(payload):
        entered.set()
        release.wait(3)
        return {'status': 'success'}
    d.publish_once = send
    p = pipeline(d)
    p._outbox = EventOutbox(tmp_path, p.id)
    p._start_publisher_worker()
    p._enqueue_publish_job(job(p))
    try:
        assert entered.wait(1)
        assert p._publish_queue.empty() and p._inflight == 1
        stopper = threading.Thread(target=p._flush_and_stop_publisher)
        stopper.start()
        time.sleep(.05)
        assert stopper.is_alive()
        release.set()
        stopper.join(3)
        assert not stopper.is_alive() and not p._publisher_thread.is_alive()
    finally:
        release.set()
        p._flush_and_stop_publisher()


def test_camera_adapter_supplies_timeouts_without_logging_credentials(caplog):
    from InferenceNode.capture_options import configure_ip_capture
    cap = SimpleNamespace(isOpened=lambda: False, release=lambda: None)
    source = SimpleNamespace(stream_url='rtsp://private-user:private-pass@camera/live', cap=None)
    configure_ip_capture(source, {'capture_type': 'ipcam'})
    with patch('InferenceNode.capture_options.cv2.VideoCapture', return_value=cap) as create:
        assert source.connect() is False
    params = create.call_args.args[2]
    assert params == [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000]
    assert 'private-pass' not in caplog.text


def test_long_outage_never_overflows():
    p = pipeline()
    p._frame_source_config = {'capture_type': 'ipcam'}
    p._reconnect_attempts = 100000
    with patch.object(p, '_sleep_interruptible', return_value=False) as wait:
        assert not p._reconnect_source()
    assert wait.call_args.args == (30.0,)


def test_file_replaced_before_ack_is_preserved(tmp_path):
    p = pipeline(Destination('d'))
    p._outbox = EventOutbox(tmp_path, p.id)
    path = tmp_path / 'input.jpg'
    path.write_bytes(b'old')
    st = path.stat()
    j = job(p)
    j.update(source_file=str(path), source_fingerprint=[st.st_size, st.st_mtime_ns],
             file_group='replacement', file_group_size=1)
    p._prepare_job(j)
    p._persist_job(j)
    path.write_bytes(b'new image')
    p._deliver_job(j)
    assert path.read_bytes() == b'new image'


def test_same_instance_restart_rehydrates_outbox_once(tmp_path):
    p = pipeline(Destination('a'))
    p._outbox = EventOutbox(tmp_path, p.id)
    j = job(p)
    p._prepare_job(j)
    p._persist_job(j)
    # Keep workers idle so both starts see the same unfinished event.
    with patch.object(threading.Thread, 'start'), patch.object(threading.Thread, 'is_alive', return_value=False):
        p._start_publisher_worker()
        initial_bytes = p._queue_bytes
        assert len(p._retry_jobs) == 1
        p._start_publisher_worker()
        assert len(p._retry_jobs) == 1
        assert p._queue_bytes == initial_bytes > 0
