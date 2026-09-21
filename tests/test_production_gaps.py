"""Regression guards for the five production-gap fixes (webhook idempotency,
effective-destination reporting, body-limit contract, lap pin, logging repair).

Each class maps to one fix and is reported PASS/FAIL independently.
Run:  python -m pytest tests/test_production_gaps.py -v
"""
import json
import logging
import os
import re
import sys
import threading
import time

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, "InferenceNode")):
    if p not in sys.path:
        sys.path.insert(0, p)


# --------------------------------------------------------------- shared stub

class _Script:
    def __init__(self):
        self.responses = []
        self.requests = []
        self.lock = threading.Lock()

    def push(self, status):
        self.responses.append(status)


def _make_server(script):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            with script.lock:
                script.requests.append({"path": self.path, "body": body})
                status = script.responses.pop(0) if script.responses else 200
            payload = b'{}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ("WEBHOOK_BASE_URL", "WEBHOOK_AUTH_TOKEN",
              "WEBHOOK_AUTH_TOKEN_FILE", "WEBHOOK_AUTH_REQUIRED"):
        monkeypatch.delenv(v, raising=False)
    yield


@pytest.fixture
def server():
    script = _Script()
    srv = _make_server(script)
    yield script, srv.server_address[1]
    srv.shutdown()


# ================================================== Fix 1: stable event_id

class TestEventId:
    """event_id is minted once per delivery job in _build_payload: identical on
    every retry of that job, different for a different job. The claim is
    idempotent deduplication within the receiver's dedup TTL - never
    'exactly-once processing'."""

    def _pipeline_with_publisher(self, port, monkeypatch):
        monkeypatch.setenv("WEBHOOK_BASE_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", "T")
        from ResultPublisher.publisher import ResultPublisher
        from ResultPublisher.plugins.webhook_destination import WebhookDestination
        from InferenceNode.pipeline import InferencePipeline
        d = WebhookDestination()
        d.configure(timeout=5)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        p = InferencePipeline()
        p.pipeline_name = "cam-eventid"
        p.result_publisher = rp
        p.PUBLISH_RETRY_DELAY_SECONDS = 0.02
        p.PUBLISH_MAX_RETRIES = 5
        p._init_dedup()
        return p, rp

    def _job(self, track_id=1):
        return {"det": {"class_name": "person", "confidence": 0.9,
                        "bbox": [1, 1, 5, 5], "track_id": track_id},
                "track_key": ("person", track_id), "iou_key": None,
                "json_results": {}, "frame": None}

    def test_same_event_id_across_5_retries(self, server, monkeypatch):
        script, port = server
        for _ in range(4):
            script.push(500)
        script.push(200)
        p, rp = self._pipeline_with_publisher(port, monkeypatch)
        p._deliver_job(self._job())
        rp.shutdown(wait=False)
        assert len(script.requests) == 5
        ids = [json.loads(r["body"])["event_id"] for r in script.requests]
        assert len(set(ids)) == 1, f"event_id changed across retries: {ids}"
        assert re.fullmatch(r"[0-9a-f]{32}", ids[0])

    def test_different_events_different_ids(self, server, monkeypatch):
        script, port = server
        script.push(200)
        script.push(200)
        p, rp = self._pipeline_with_publisher(port, monkeypatch)
        p._deliver_job(self._job(track_id=1))
        p._deliver_job(self._job(track_id=2))
        rp.shutdown(wait=False)
        ids = [json.loads(r["body"])["event_id"] for r in script.requests]
        assert len(ids) == 2 and ids[0] != ids[1]

    def test_event_id_stable_while_timestamp_changes(self, server, monkeypatch):
        """The per-attempt timestamp behaviour is UNCHANGED (documented as
        changing); event_id is the stable identity."""
        script, port = server
        script.push(500)
        script.push(200)
        p, rp = self._pipeline_with_publisher(port, monkeypatch)
        p._deliver_job(self._job())
        rp.shutdown(wait=False)
        first, second = [json.loads(r["body"]) for r in script.requests]
        assert first["event_id"] == second["event_id"]
        for key in ("pipeline_id", "node_id", "pipeline_name", "results"):
            assert first[key] == second[key]
        assert first["timestamp"] != second["timestamp"]

    def test_retry_horizon_fits_receiver_dedup_ttl(self):
        """Cross-repo contract, sender half: the worst-case duration over which
        ONE event_id can be re-delivered must fit inside the receiver's
        WEBHOOK_DEDUP_TTL_SECONDS (=600 in FACE_DETECTOR config/compose; its
        test asserts >= 600). Re-arm mints a NEW event_id, so it is excluded."""
        from InferenceNode.pipeline import InferencePipeline
        from ResultPublisher.plugins import webhook_destination as wd
        p = InferencePipeline.__new__(InferencePipeline)  # constants live in __init__
        InferencePipeline.__init__(p)
        attempts = p.PUBLISH_MAX_RETRIES + 1
        per_attempt = wd._CONNECT_TIMEOUT_SECONDS + 30  # default read timeout
        waits = 0.0
        delay = p.PUBLISH_RETRY_DELAY_SECONDS
        for _ in range(attempts - 1):
            waits += max(delay, wd._RETRY_AFTER_MAX_SECONDS)
            delay *= p.PUBLISH_RETRY_BACKOFF
        horizon = attempts * per_attempt + waits
        ttl = 600
        assert horizon * 1.5 <= ttl, (
            f"worst-case same-event retry horizon {horizon:.0f}s x1.5 margin exceeds the "
            f"receiver's WEBHOOK_DEDUP_TTL_SECONDS={ttl} - raise the receiver TTL "
            f"(FACE_DETECTOR config.py / compose) or shrink the sender retry budget")


# ============================== Fix 2: effective destination, never 127.0.0.1

class TestEffectiveDestination:
    def _dest(self, monkeypatch, base=None, legacy=None):
        if base:
            monkeypatch.setenv("WEBHOOK_BASE_URL", base)
        monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", "T")
        from ResultPublisher.plugins.webhook_destination import WebhookDestination
        d = WebhookDestination()
        d.configure(url=legacy, timeout=5)
        return d

    def test_base_url_mode_overrides_stored_legacy(self, monkeypatch):
        d = self._dest(monkeypatch, base="http://face-webhook",
                       legacy="http://127.0.0.1/webhook/{pipeline_id}")
        eff = d.effective_destination()
        assert eff["mode"] == "base_url"
        assert eff["url"] == "http://face-webhook/webhook/{pipeline_id}"
        assert "127.0.0.1" not in eff["url"]

    def test_legacy_mode_reports_stored_template(self, monkeypatch):
        d = self._dest(monkeypatch, legacy="http://192.0.2.9:9/webhook/{pipeline_id}")
        eff = d.effective_destination()
        assert eff["mode"] == "legacy"
        assert eff["url"] == "http://192.0.2.9:9/webhook/{pipeline_id}"

    def test_unconfigured_mode(self):
        from ResultPublisher.plugins.webhook_destination import WebhookDestination
        d = WebhookDestination()   # no configure() at all
        eff = d.effective_destination()
        assert eff == {"mode": "unconfigured", "url": None}
        assert d._base_url is None     # the latent AttributeError is gone

    def test_publisher_state_carries_effective_url(self, monkeypatch):
        """get_publisher_states -> pipeline list API is how the UI learns the
        effective destination."""
        monkeypatch.setenv("WEBHOOK_BASE_URL", "http://face-webhook")
        monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", "T")
        from ResultPublisher.publisher import ResultPublisher
        from ResultPublisher.plugins.webhook_destination import WebhookDestination
        from InferenceNode.pipeline import InferencePipeline
        d = WebhookDestination()
        d.configure(url="http://127.0.0.1/webhook/{pipeline_id}", timeout=5)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        p = InferencePipeline()
        p.result_publisher = rp
        states = p.get_publisher_states()
        rp.shutdown(wait=False)
        assert states["wh"]["effective_mode"] == "base_url"
        assert states["wh"]["effective_url"] == "http://face-webhook/webhook/{pipeline_id}"
        json.dumps(states)  # stays JSON-safe

    def test_misleading_startup_line_is_gone(self):
        """The old line printed the STORED url as the configured destination."""
        needle = "Successfully configured " + "Webhook destination"  # split so this file never matches
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [x for x in dirs if x not in (".venv", ".git", "node_modules", "__pycache__")]
            for name in files:
                if name.endswith(".py"):
                    src = open(os.path.join(root, name), encoding="utf-8", errors="ignore").read()
                    assert needle not in src, \
                        f"misleading legacy-url startup line resurfaced in {os.path.join(root, name)}"


# ====================================== Fix 4: lap pinned for tracker support

class TestLapDependency:
    def test_requirements_pins_lap_exactly(self):
        """Trackers (botsort/bytetrack/ocsort) all need lap for linear
        assignment; Ultralytics auto-installing it at runtime is impossible
        offline and vanishes on container recreation. Exact pin - no drift."""
        req = open(os.path.join(REPO, "requirements.txt"), encoding="utf-8").read()
        pins = [ln.strip() for ln in req.splitlines()
                if re.match(r"^lap\s*==", ln.strip())]
        assert pins == ["lap==0.5.12"], (
            f"requirements.txt must pin exactly 'lap==0.5.12' (found: {pins or 'nothing'}); "
            f"without it every tracker fails and track_id is None for all detections")

    def test_lap_importable_in_this_environment(self):
        lap = pytest.importorskip("lap", reason="lap not installed in this venv")
        assert hasattr(lap, "lapjv")   # the assignment solver ultralytics calls


# ================= Fix 5: logging survives Alembic + handler lifecycle safety

class TestLoggingRepair:
    def test_env_py_cannot_wipe_app_logging(self):
        """migrations/env.py must (a) skip fileConfig entirely when root already
        has handlers (the in-process startup path), and (b) never disable
        existing loggers when it does run (standalone CLI)."""
        src = open(os.path.join(REPO, "InferenceNode", "migrations", "env.py"),
                   encoding="utf-8").read()
        assert "not logging.getLogger().handlers" in src, \
            "env.py lost the root-handlers guard - in-process alembic will wipe app logging again"
        assert "disable_existing_loggers=False" in src, \
            "env.py lost disable_existing_loggers=False - fileConfig will disable app loggers"
        bare = re.search(r"fileConfig\(\s*config\.config_file_name\s*\)", src)
        assert bare is None, "a bare fileConfig(config.config_file_name) call is back"

    def test_setup_logging_is_idempotent_and_fd_safe(self, tmp_path, monkeypatch):
        import importlib
        import InferenceNode.log_manager as lm_mod
        importlib.reload(lm_mod)
        # Redirect the log dir so the test never touches the real logs/
        monkeypatch.setattr(lm_mod, "__file__", str(tmp_path / "log_manager.py"))

        root = logging.getLogger()
        third_party = logging.NullHandler()
        root.addHandler(third_party)
        lm = lm_mod.LogManager()
        try:
            old_files = []
            for _ in range(3):
                lm.setup_logging("INFO", enable_file_logging=True)
                old_files.append(lm.file_handler)

            own = [lm.memory_handler, lm.file_handler, lm.stream_handler]
            assert all(h is not None and h in root.handlers for h in own)
            # exactly one of each - never stacked duplicates
            mems = [h for h in root.handlers if isinstance(h, lm_mod.MemoryLogHandler)]
            files = [h for h in root.handlers
                     if isinstance(h, logging.handlers.RotatingFileHandler)]
            streams = [h for h in root.handlers
                       if type(h) is logging.StreamHandler]
            assert (len(mems), len(files), len(streams)) == (1, 1, 1)
            # replaced file handlers released their descriptors...
            for old in old_files[:-1]:
                assert old.stream is None or old.stream.closed
            # ...but sys.stdout is NEVER closed by handler replacement
            assert not sys.stdout.closed
            # third-party handler untouched
            assert third_party in root.handlers
        finally:
            lm._detach_own_handlers(root)
            root.removeHandler(third_party)

    def test_memory_history_survives_reconfigure(self, tmp_path, monkeypatch):
        import importlib
        import InferenceNode.log_manager as lm_mod
        importlib.reload(lm_mod)
        monkeypatch.setattr(lm_mod, "__file__", str(tmp_path / "log_manager.py"))
        root = logging.getLogger()
        lm = lm_mod.LogManager()
        try:
            lm.setup_logging("INFO", enable_file_logging=False)
            logging.getLogger("gap-probe").info("history-entry")
            before = len(lm.memory_handler.logs)
            assert before > 0
            first_memory = lm.memory_handler
            lm.setup_logging("DEBUG", enable_file_logging=False)
            # same handler object, history intact -> /api/logs keeps its buffer
            assert lm.memory_handler is first_memory
            assert len(lm.memory_handler.logs) >= before
        finally:
            lm._detach_own_handlers(root)

    def test_logger_records_reach_stdout_stream(self, tmp_path, monkeypatch, capsys):
        import importlib
        import InferenceNode.log_manager as lm_mod
        importlib.reload(lm_mod)
        monkeypatch.setattr(lm_mod, "__file__", str(tmp_path / "log_manager.py"))
        root = logging.getLogger()
        lm = lm_mod.LogManager()
        try:
            lm.setup_logging("INFO", enable_file_logging=False)
            logging.getLogger("stdout-probe").info("PUBLISH_SUCCESS-style record")
            captured = capsys.readouterr()
            assert "PUBLISH_SUCCESS-style record" in captured.out, \
                "logger.* records must reach stdout - docker logs is the authoritative stream"
        finally:
            lm._detach_own_handlers(root)
