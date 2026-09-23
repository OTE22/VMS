"""Offline replay; writes frame indexes/boxes and aggregate counts, never faces.

Run in an isolated VMS image with --network none, a read-only video/model mount,
and /tmp as the working directory. The image's /app pipeline is the baseline.
"""
import argparse
import ast
import contextlib
import io
import json
import logging
from pathlib import Path
import time
from types import MethodType
from unittest.mock import patch

import cv2
from ultralytics import YOLO
from InferenceNode.pipeline import InferencePipeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--video', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--baseline', default='/app/InferenceNode/pipeline.py')
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS)
    assert cap.isOpened() and fps > 0
    logging.disable(logging.CRITICAL)
    pipelines = {'baseline': InferencePipeline(), 'quality': InferencePipeline()}
    pipelines['quality'].PERSON_QUALITY_SELECTION = True
    tree = ast.parse(Path(args.baseline).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'InferencePipeline')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef)
               and n.name in ('_update_track_candidate', '_track_ready')]
    ns = {}
    exec(compile(ast.Module(body=methods, type_ignores=[]), args.baseline, 'exec'), ns)
    for name, function in ns.items():
        if callable(function):
            setattr(pipelines['baseline'], name, MethodType(function, pipelines['baseline']))
    model = YOLO(args.model)
    observations, selected = [], {name: [] for name in pipelines}
    started = time.perf_counter()
    next_sample, index, sampled = 0.0, -1, 0
    while True:
        ok, frame = cap.read()
        index += 1
        if not ok or index / fps >= args.seconds:
            break
        if index / fps + 1e-6 < next_sample:
            continue
        next_sample += .2
        sampled += 1
        result = model.track(frame, device='cpu', persist=True, conf=.1,
                             tracker='/workspace/InferenceEngine/trackers/botsort_fixed_camera.yaml',
                             verbose=False)[0]
        now = 1000 + index / fps
        for box in result.boxes:
            if int(box.cls[0]) != 0 or float(box.conf[0]) < .5 or box.id is None:
                continue
            det = dict(class_name='person', confidence=float(box.conf[0]),
                       track_id=int(box.id[0]), bbox=box.xyxy[0].tolist())
            observations.append(dict(frame=index, **det))
            for pipeline in pipelines.values():
                pipeline._last_capture_wall = index + 1
                pipeline._update_track_candidate(det, frame, now)
        for name, pipeline in pipelines.items():
            for job in pipeline._collect_ready_tracks(now):
                selected[name].append(dict(frame=int(job['captured_at']) - 1, **job['det']))
                with patch('InferenceNode.pipeline.time.time', return_value=now), contextlib.redirect_stdout(io.StringIO()):
                    pipeline._handle_publish_success(job, {'successful_destinations': ['offline']})
    cap.release()
    report = dict(video=Path(args.video).name, seconds=min(index / fps, args.seconds),
                  sampled_frames=sampled, elapsed_seconds=round(time.perf_counter() - started, 2),
                  observations=observations, selected=selected)
    Path(args.output).write_text(json.dumps(report))
    print(json.dumps({k: v for k, v in report.items() if k not in ('observations', 'selected')}))
    print('Selected captures:', {k: len(v) for k, v in selected.items()})


if __name__ == '__main__':
    main()
