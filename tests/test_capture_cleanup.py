"""Capture lifecycle compatibility and thumbnail behavior at video EOF."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from flask import Flask, jsonify
from framesource.sources.video_file_capture import VideoFileCapture

from InferenceNode.pipeline import InferencePipeline
from ResultPublisher import ResultPublisher


def test_modern_disconnect_preferred_and_legacy_stop_supported():
    p = InferencePipeline()
    modern = SimpleNamespace(disconnect=Mock(), stop=Mock())
    p.source = modern
    p._disconnect_source()
    modern.disconnect.assert_called_once()
    modern.stop.assert_not_called()
    legacy = SimpleNamespace(stop=Mock())
    p.source = legacy
    p._disconnect_source()
    legacy.stop.assert_called_once()
    p.source = None


def test_real_video_eof_releases_capture_and_retains_thumbnail(tmp_path):
    video = tmp_path / 'sample.avi'
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
    assert writer.isOpened()
    for _ in range(3):
        writer.write(np.full((48, 64, 3), 120, dtype=np.uint8))
    writer.release()
    p = InferencePipeline()
    p.source = VideoFileCapture(str(video), real_time=False)
    p._frame_source_config = {'capture_type': 'video_file'}
    p.result_publisher = ResultPublisher()
    p._inference_enabled = False
    p.FAILED_READS_BEFORE_RECONNECT = 1
    p.set_thumbnail_path(str(tmp_path / 'thumbs'))
    p.run()
    assert p._error_state is None
    assert not p.is_running()
    assert p.source.cap is None
    assert cv2.imread(p.get_thumbnail_path()) is not None
    p.stop()  # repeated cleanup must also work with the real library
    assert p.source.cap is None


@pytest.mark.parametrize('instance', [None, SimpleNamespace(get_latest_frame=lambda: None)])
def test_thumbnail_without_live_frame_returns_actionable_conflict(instance):
    # Exercise the actual nested route in a minimal Flask app without starting
    # discovery, database bootstrap, cameras, or other node background services.
    tree = ast.parse(Path('InferenceNode/inference_node.py').read_text())
    route = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == 'generate_pipeline_thumbnail')
    route.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[]))
    manager = SimpleNamespace(
        get_pipeline=lambda _: {'id': 'p'},
        active_pipelines={} if instance is None else {'p': {'pipeline_instance': instance}},
        has_pipeline_thumbnail=lambda _: True,
        generate_pipeline_thumbnail=Mock(),
    )
    namespace = {'self': SimpleNamespace(pipeline_manager=manager, logger=logging.getLogger('test')),
                 'jsonify': jsonify}
    exec(compile(module, '<thumbnail route>', 'exec'), namespace)
    app = Flask(__name__)
    app.add_url_rule('/thumbnail/<pipeline_id>', view_func=namespace['generate_pipeline_thumbnail'], methods=['POST'])
    response = app.test_client().post('/thumbnail/p')
    assert response.status_code == 409
    assert response.json['has_thumbnail'] is True
    assert 'Start the pipeline' in response.json['error']
    manager.generate_pipeline_thumbnail.assert_not_called()


def test_model_load_failure_closes_source_without_masking_cause(monkeypatch):
    from InferenceNode import pipeline as module
    source = SimpleNamespace(disconnect=Mock())
    engine = SimpleNamespace(load=lambda: False)
    monkeypatch.setattr(module.FrameSourceFactory, 'create', lambda **_: source)
    monkeypatch.setattr(module.InferenceEngineFactory, 'create', lambda **_: engine)
    p = InferencePipeline()
    with pytest.raises(RuntimeError, match='Inference model failed to load'):
        p.configure({'capture_type': 'video_file'}, {}, ResultPublisher())
    source.disconnect.assert_called_once()
    p.source = None
