"""
Regression test for the pipeline -> ResultPublisher -> WebhookDestination path
after adding Bearer-token auth. Confirms the background publisher worker still
delivers with confirmed success + retries, and that every HTTP attempt carries
the Authorization header. Local plain-HTTP server (NOT HTTPS).

Run:  python -m pytest tests/test_publisher_webhook_integration.py -v
"""
import os
import sys
import time
import threading

import pytest
import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ResultPublisher import ResultPublisher                              # noqa: E402
from ResultPublisher.plugins.webhook_destination import WebhookDestination  # noqa: E402
from pipeline import InferencePipeline                                    # noqa: E402

TOKEN = "integration-token-ABC"


class _Cap:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.auth_headers = []


def _server(cap):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            cap.auth_headers.append(self.headers.get("Authorization"))
            status = cap.statuses.pop(0) if cap.statuses else 200
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("WEBHOOK_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN_FILE", "WEBHOOK_AUTH_REQUIRED"):
        monkeypatch.delenv(v, raising=False)
    yield


def _pipeline(pub):
    p = InferencePipeline()
    p.pipeline_name = "cam-integration"
    p.result_publisher = pub
    p.SEND_BUFFER_SECONDS = 0.1
    p.IMMEDIATE_SEND_CONFIDENCE = 0.9
    p.PUBLISH_RETRY_DELAY_SECONDS = 0.05
    p.PUBLISH_MAX_RETRIES = 4
    p._init_dedup()
    p._start_publisher_worker()
    return p


def _drain(p, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if p._publish_queue.empty() and not p._pending_track_keys:
            time.sleep(0.05)
            if p._publish_queue.empty() and not p._pending_track_keys:
                return
        time.sleep(0.02)


def test_pipeline_delivers_with_auth_and_retries(monkeypatch):
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    cap = _Cap([500, 500, 200])  # two transient failures then success
    srv = _server(cap)
    port = srv.server_address[1]

    dest = WebhookDestination()
    dest.configure(url=f"http://127.0.0.1:{port}/api/webhooks/detections", rate_limit=0.0, timeout=5)
    dest._id = "wh"
    rp = ResultPublisher(); rp.add(dest)

    p = _pipeline(rp)
    det = {"class_name": "person", "confidence": 0.95, "bbox": [10, 10, 100, 200], "track_id": 1}
    p._update_track_candidate(det, np.zeros((2, 2, 3), dtype=np.uint8), time.time())
    for job in p._collect_ready_tracks(time.time()):
        job["json_results"] = {}
        p._enqueue_publish_job(job)
    _drain(p)

    stats = p.get_publish_stats()
    srv.shutdown()
    p._publisher_stop_event.set()
    if p._publisher_thread:
        p._publisher_thread.join(timeout=3.0)
    dest.close()

    assert len(cap.auth_headers) == 3, f"expected 3 HTTP attempts, got {len(cap.auth_headers)}"
    # Every attempt carried the exact Bearer header (retries too)
    assert all(h == f"Bearer {TOKEN}" for h in cap.auth_headers), cap.auth_headers
    assert stats["persons_sent"] == 1
    assert stats["publish_retries"] == 2
    assert dest.enabled is True  # transient failures did not disable it
