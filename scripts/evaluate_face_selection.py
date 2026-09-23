"""Evaluate offline replay using the receiver's SCRFD and quality admission code.

Run in the receiver image, with no network and read-only receiver/video mounts.
No identity recognition, enrollment, database connection, or image export.
"""
import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import cv2
import onnxruntime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--receiver', default='/receiver')
    parser.add_argument('--video', required=True)
    parser.add_argument('--selection', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    # Match the container CPU limit rather than creating a thread per host core.
    create_session = onnxruntime.InferenceSession
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    onnxruntime.InferenceSession = lambda *a, **kw: create_session(*a, sess_options=options, **kw)
    root = Path(args.receiver)
    sys.path.insert(0, str(root))
    # Avoid application bootstrap and production config entirely.
    sys.modules['backend.core'] = SimpleNamespace()
    sys.modules['backend.core.gpu_runtime'] = SimpleNamespace(
        select_providers=lambda: ['CPUExecutionProvider'], verify_session_providers=lambda *a: None)
    sys.modules['config'] = SimpleNamespace(settings=SimpleNamespace(
        CAMERA_FACE_ACCEPTANCE_ENABLED=True, CAMERA_FACE_MIN_SOURCE_COVERAGE=.9,
        CAMERA_FACE_MIN_ALIGNED_SHARPNESS=5))
    from models.scrfd import SCRFD
    from utils.helpers import face_alignment
    spec = importlib.util.spec_from_file_location('admission', root / 'backend/core/aligned_face_quality.py')
    admission = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admission)
    detector = SCRFD(str(root / 'weights/det_10g.onnx'))
    replay = json.loads(Path(args.selection).read_text())
    cap = cv2.VideoCapture(args.video)
    results = {}
    records = replay['observations'] + [r for group in replay['selected'].values() for r in group]

    def key(record):
        return (record['frame'], tuple(record['bbox']))

    for record in records:
        k = key(record)
        if k in results:
            continue
        cap.set(cv2.CAP_PROP_POS_FRAMES, record['frame'])
        ok, frame = cap.read()
        assert ok
        # Production publisher JPEG-encodes the original frame before transport.
        encoded, image = cv2.imencode('.jpg', frame)
        assert encoded
        frame = cv2.imdecode(image, cv2.IMREAD_COLOR)
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in record['bbox'])
        crop = frame[max(0, y1):min(height, y2), max(0, x1):min(width, x2)]
        if crop.size == 0 or min(crop.shape[:2]) < 10:
            results[k] = 'invalid_crop'
            continue
        boxes, landmarks = detector.detect(crop, max_num=1)
        if landmarks is None or not len(landmarks):
            results[k] = 'no_face'
            continue
        aligned, inverse = face_alignment(crop, landmarks[0], image_size=112)
        verdict = admission.assess_aligned_face(aligned, crop.shape, inverse, landmarks[0],
                                               [int(v) for v in boxes[0][:4]])
        results[k] = 'usable' if verdict['accepted'] else verdict['reason']
    cap.release()
    report = dict(video=replay['video'], sampled_frames=replay['sampled_frames'],
                  seconds=replay['seconds'], observations=len(replay['observations']), methods={})
    eligible_tracks = {r['track_id'] for r in replay['observations'] if results[key(r)] == 'usable'}
    report['tracks_with_usable_face_opportunity'] = len(eligible_tracks)
    for name, group in replay['selected'].items():
        covered = {r['track_id'] for r in group if results[key(r)] == 'usable'}
        # Simulate stopping further captures after the first usable committed face.
        feedback_group, completed = [], set()
        for r in group:
            if r['track_id'] in completed:
                continue
            feedback_group.append(r)
            if results[key(r)] == 'usable':
                completed.add(r['track_id'])
        report['methods'][name] = dict(captures=len(group), reasons=dict(Counter(results[key(r)] for r in group)),
                                      tracks_with_usable_capture=len(covered),
                                      missed_opportunity_tracks=len(eligible_tracks - covered),
                                      captures_if_usable_faces_commit=len(feedback_group))
    Path(args.output).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
