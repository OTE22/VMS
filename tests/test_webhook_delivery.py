"""Webhook delivery lifecycle: DeliveryResult contract, single-owner accounting,
health-vs-backpressure separation, sync/queue parity, retry termination, and
thread safety.

Why this suite exists: every VMS webhook used to fail as ConnectionError ->
3 retries -> PUBLISH_FAILED -> track re-armed 60s later -> forever. The root
cause was publish_once() returning "failed" WITHOUT feeding _record_failure(),
so max_failures=5 never engaged. Fixing that exposed three more design gaps
(payload rejections disabling the whole destination, backpressure counted as
ill-health, unlocked lifecycle counters) - each pinned here.

All servers are local stubs; no production or development data is used.
Run:  python -m pytest tests/test_webhook_delivery.py -v
"""
import json
import logging
import os
import sys
import threading
import time

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ResultPublisher.base_destination import BaseResultDestination, DeliveryResult  # noqa: E402
from ResultPublisher.publisher import ResultPublisher                               # noqa: E402
from ResultPublisher.plugins.webhook_destination import (                           # noqa: E402
    WebhookDestination, _parse_retry_after, _classify_transport_exception)

TOKEN = "TEST_TOKEN_never_logged.42"
PID = "pipe-lifecycle-1"


# ------------------------------------------------------------------ stub server

class _Script:
    """Scripted responses: each entry is (status, headers_dict, body_bytes)."""
    def __init__(self):
        self.responses = []
        self.requests = []
        self.lock = threading.Lock()

    def push(self, status, headers=None, body=b"{}"):
        self.responses.append((status, headers or {}, body))


def _make_server(script):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            with script.lock:
                script.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": body,
                })
                status, headers, resp_body = (
                    script.responses.pop(0) if script.responses else (200, {}, b"{}"))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

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


def _dest(port, monkeypatch, **cfg):
    monkeypatch.setenv("WEBHOOK_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    d = WebhookDestination()
    d.configure(timeout=5, **cfg)
    return d


def _payload(pid=PID):
    return {"pipeline_id": pid, "pipeline_name": "cam",
            "node_id": "node-1",
            "results": {"num_detections": 1, "predictions": [{"class_name": "person"}]}}


QUEUE_FULL = json.dumps({"status": "queue_full", "job_id": "x",
                         "pipeline_id": PID, "queued": 0, "dropped": 1}).encode()


# ================================================================ classification

class TestClassification:
    """Every response class maps to its exact DeliveryResult verdict."""

    @pytest.mark.parametrize("status,outcome,retryable,terminal,disable,count", [
        (400, "INVALID_PAYLOAD",   False, True,  False, False),
        (401, "AUTH_FAILED",       False, False, True,  False),
        (403, "AUTH_FAILED",       False, False, True,  False),
        (404, "NOT_FOUND",         False, False, True,  False),
        (408, "SERVER_ERROR",      True,  False, False, True),
        (413, "PAYLOAD_TOO_LARGE", False, True,  False, False),
        (422, "INVALID_PAYLOAD",   False, True,  False, False),
        (429, "RATE_LIMITED",      True,  False, False, False),
        (500, "SERVER_ERROR",      True,  False, False, True),
        (502, "SERVER_ERROR",      True,  False, False, True),
        (504, "SERVER_ERROR",      True,  False, False, True),
        (409, "INVALID_PAYLOAD",   False, True,  False, False),  # unknown 4xx
    ])
    def test_status_matrix(self, server, monkeypatch, status, outcome,
                           retryable, terminal, disable, count):
        script, port = server
        script.push(status)
        r = _dest(port, monkeypatch)._publish(_payload())
        assert (r.outcome, r.retryable, r.terminal_delivery,
                r.disable_destination, r.count_toward_destination_failure) == \
               (outcome, retryable, terminal, disable, count)

    def test_queue_full_503_is_backpressure(self, server, monkeypatch):
        """The receiver's documented queue-full contract: 503 + Retry-After: 2 +
        {"status": "queue_full"}. Identified by the body marker, never by the
        status code alone."""
        script, port = server
        script.push(503, {"Retry-After": "2"}, QUEUE_FULL)
        r = _dest(port, monkeypatch)._publish(_payload())
        assert r.outcome == "BACKPRESSURE"
        assert r.retryable is True
        assert r.count_toward_destination_failure is False
        assert r.retry_after == 2.0

    def test_generic_503_counts_toward_health(self, server, monkeypatch):
        """A 503 WITHOUT the queue-full marker could be nginx limit_req or a
        dead upstream - never guessed as queue-full."""
        script, port = server
        script.push(503, {}, b"<html>Service Unavailable</html>")
        r = _dest(port, monkeypatch)._publish(_payload())
        assert r.outcome == "SERVER_ERROR"
        assert r.count_toward_destination_failure is True

    def test_transport_taxonomy_specific_first(self):
        """ConnectTimeout/SSLError subclass ConnectionError - the specific
        types must win, and DNS/refused need a PROVEN root cause."""
        import requests.exceptions as rex
        import socket as sock
        assert _classify_transport_exception(rex.ConnectTimeout()).outcome == "CONNECT_TIMEOUT"
        assert _classify_transport_exception(rex.ReadTimeout()).outcome == "READ_TIMEOUT"
        assert _classify_transport_exception(rex.SSLError()).outcome == "TLS_ERROR"
        assert _classify_transport_exception(
            rex.ConnectionError(sock.gaierror(11001, "getaddrinfo failed"))).outcome == "DNS_ERROR"
        assert _classify_transport_exception(
            rex.ConnectionError(ConnectionRefusedError())).outcome == "CONNECTION_REFUSED"
        # Unproven chain -> the honest generic label, not a guess
        assert _classify_transport_exception(
            rex.ConnectionError("who knows")).outcome == "CONNECTION_ERROR"
        for e in (rex.ConnectTimeout(), rex.ReadTimeout(), rex.SSLError(),
                  rex.ConnectionError()):
            r = _classify_transport_exception(e)
            assert r.retryable and r.count_toward_destination_failure

    @pytest.mark.parametrize("raw,expected", [
        ("2", 2.0), ("0", 0.0), ("2.5", 2.5),
        ("9999", 30.0),                      # clamped to the safe maximum
        ("", None), (None, None), ("soon", None), ("-3", None),
    ])
    def test_retry_after_delta_seconds(self, raw, expected):
        assert _parse_retry_after(raw) == expected

    def test_retry_after_http_date(self):
        from email.utils import format_datetime
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(seconds=10)
        parsed = _parse_retry_after(format_datetime(future, usegmt=True))
        assert parsed is not None and 5.0 <= parsed <= 30.0
        past = datetime.now(timezone.utc) - timedelta(seconds=60)
        assert _parse_retry_after(format_datetime(past, usegmt=True)) is None


# ============================================================ accounting exactness

class TestAccountingExactness:
    """one attempt = one DeliveryResult = one lifecycle decision."""

    def test_one_success_one_update(self, server, monkeypatch):
        script, port = server
        script.push(200)
        d = _dest(port, monkeypatch)
        assert d.publish_once(_payload())["status"] == "success"
        assert d.frame_count == 1                    # exactly one, not two
        assert d.success_count_since_failure == 1
        assert d.failure_count == 0

    def test_one_health_failure_one_increment(self, server, monkeypatch):
        script, port = server
        script.push(500)
        d = _dest(port, monkeypatch)
        assert d.publish_once(_payload())["status"] == "failed"
        assert d.failure_count == 1                  # exactly one, not two

    def test_one_backpressure_zero_increments(self, server, monkeypatch):
        script, port = server
        script.push(429)
        d = _dest(port, monkeypatch)
        assert d.publish_once(_payload())["status"] == "failed"
        assert d.failure_count == 0                  # busy is not broken

    def test_four_failures_then_success_resets(self, server, monkeypatch):
        script, port = server
        for _ in range(4):
            script.push(500)
        script.push(200)
        d = _dest(port, monkeypatch)
        for i in range(1, 5):
            d.publish_once(_payload())
            assert d.failure_count == i              # exactly one per attempt
        assert d.enabled is True                     # 4 < max_failures
        assert d.publish_once(_payload())["status"] == "success"
        assert d.failure_count == 0                  # one success resets
        assert d.enabled is True

    def test_terminal_delivery_zero_lifecycle_updates(self, server, monkeypatch):
        script, port = server
        script.push(413)
        d = _dest(port, monkeypatch)
        out = d.publish_once(_payload())
        assert out["status"] == "permanent_failure"
        assert d.failure_count == 0 and d.frame_count == 0
        assert d.enabled is True

    def test_try_send_is_pure(self, server, monkeypatch):
        """_try_send performs the attempt but must not account - _account owns
        every counter. If accounting creeps back into _try_send, every path
        double-counts."""
        script, port = server
        script.push(200)
        script.push(500)
        d = _dest(port, monkeypatch)
        assert d._try_send(_payload()).success is True
        assert d.frame_count == 0 and d.success_count_since_failure == 0
        assert d._try_send(_payload()).success is False
        assert d.failure_count == 0

    def test_legacy_bool_normalization(self):
        ok = DeliveryResult.from_legacy(True)
        assert ok.success and ok.outcome == "SUCCESS"
        assert not ok.count_toward_destination_failure and not ok.retryable
        bad = DeliveryResult.from_legacy(False)
        assert not bad.success and bad.outcome == "CONNECTION_ERROR"
        assert bad.retryable and bad.count_toward_destination_failure
        assert not bad.terminal_delivery and not bad.disable_destination


# ============================================================= health vs backpressure

class TestHealthVsBackpressure:
    """5 consecutive health failures disable; 5x backpressure never does."""

    def _run_five(self, script, port, monkeypatch, pushes):
        d = _dest(port, monkeypatch)
        for push in pushes:
            push(script)
            d.publish_once(_payload())
        return d

    def test_five_500_disables(self, server, monkeypatch):
        script, port = server
        d = self._run_five(script, port, monkeypatch, [lambda s: s.push(500)] * 5)
        assert d.failure_count == 5
        assert d.failure_threshold_reached is True
        assert d.enabled is False

    def test_five_connection_refused_disables(self, monkeypatch):
        monkeypatch.setenv("WEBHOOK_BASE_URL", "http://127.0.0.1:9")  # discard port
        monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
        d = WebhookDestination(); d.configure(timeout=2)
        for _ in range(5):
            d.publish_once(_payload())
        assert d.failure_count == 5
        assert d.enabled is False
        # After disable: no NEW request starts
        assert d.publish_once(_payload())["status"] == "disabled"

    def test_five_429_stays_enabled(self, server, monkeypatch):
        script, port = server
        d = self._run_five(script, port, monkeypatch, [lambda s: s.push(429)] * 5)
        assert d.failure_count == 0
        assert d.enabled is True

    def test_five_queue_full_503_stays_enabled(self, server, monkeypatch):
        script, port = server
        d = self._run_five(
            script, port, monkeypatch,
            [lambda s: s.push(503, {"Retry-After": "2"}, QUEUE_FULL)] * 5)
        assert d.failure_count == 0
        assert d.enabled is True
        # And it still delivers once the receiver recovers
        script.push(200)
        assert d.publish_once(_payload())["status"] == "success"

    def test_auth_disable_precedes_counting(self, server, monkeypatch):
        """401 disables immediately via disable_destination, without waiting
        for (or moving) the max_failures counter."""
        script, port = server
        script.push(401)
        d = _dest(port, monkeypatch)
        d.publish_once(_payload())
        assert d.enabled is False
        assert d.failure_count == 0


# ================================================================ sync/queue parity

class TestSyncQueueParity:
    """400/413/422 behave identically whether the event went through
    publish_once (pipeline sync path) or publish()/_drain_queue (queue path):
    delivery dropped, no retry, no counter movement, destination enabled."""

    @pytest.mark.parametrize("status", [400, 413, 422])
    def test_sync_terminal(self, server, monkeypatch, status):
        script, port = server
        script.push(status)
        d = _dest(port, monkeypatch)
        assert d.publish_once(_payload())["status"] == "permanent_failure"
        assert d.enabled is True and d.failure_count == 0
        assert len(script.requests) == 1             # exactly one attempt, no retry

    @pytest.mark.parametrize("status", [400, 413, 422])
    def test_queue_terminal(self, server, monkeypatch, status):
        script, port = server
        script.push(status)
        d = _dest(port, monkeypatch)
        accepted = d.publish(_payload())             # direct path inside publish()
        assert accepted is False                     # dropped, not queued for retry
        deadline = time.time() + 3
        while time.time() < deadline and not script.requests:
            time.sleep(0.02)
        time.sleep(0.3)                              # would-be retry window
        assert len(script.requests) == 1             # no retry ever fired
        assert d.enabled is True and d.failure_count == 0
        d.close()

    def test_queued_event_terminal_dropped_not_looped(self, server, monkeypatch):
        """An event that reaches the queue worker and gets a terminal verdict is
        dropped there too - not re-appended forever."""
        script, port = server
        d = _dest(port, monkeypatch, rate_limit=0.0)
        d._enqueue(_payload(), attempts=0)           # place it on the queue directly
        script.push(422)
        deadline = time.time() + 3
        while time.time() < deadline and not script.requests:
            time.sleep(0.02)
        time.sleep(0.3)
        assert len(script.requests) == 1
        with d._queue_lock:
            assert len(d._send_queue) == 0           # gone, not re-queued
        assert d.enabled is True and d.failure_count == 0
        d.close()


# ================================================================== termination

class TestTermination:
    """max_failures stops NEW requests; disabled destinations stop the pipeline
    loop instead of re-arming it forever."""

    def test_no_new_request_after_threshold(self, server, monkeypatch):
        script, port = server
        for _ in range(7):
            script.push(500)
        d = _dest(port, monkeypatch)
        statuses = [d.publish_once(_payload())["status"] for _ in range(7)]
        assert statuses[:5] == ["failed"] * 5
        assert statuses[5:] == ["disabled", "disabled"]   # no 6th/7th HTTP call
        assert len(script.requests) == 5
        assert d.enabled is False

    def test_publish_sync_reports_disabled_destination(self, server, monkeypatch):
        """publish_sync surfaces disabled_destinations the moment the attempt's
        accounting disables it, so the pipeline stops retrying immediately."""
        script, port = server
        script.push(401)
        d = _dest(port, monkeypatch)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        res = rp.publish_sync(_payload())
        assert res["success"] is False
        assert res["failed_destinations"] == ["wh"]
        assert res["disabled_destinations"] == ["wh"]
        # Next event: nothing attempted, destination skipped - terminal for caller
        res2 = rp.publish_sync(_payload())
        assert res2["attempted"] == 0
        assert res2["skipped_destinations"] == ["wh"]
        rp.shutdown(wait=False)

    def test_publish_sync_reports_terminal_destination(self, server, monkeypatch):
        script, port = server
        script.push(413)
        d = _dest(port, monkeypatch)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        res = rp.publish_sync(_payload())
        assert res["terminal_destinations"] == ["wh"]
        assert res["failed_destinations"] == []
        assert d.enabled is True                     # destination survives
        rp.shutdown(wait=False)

    def test_publish_sync_carries_retry_after(self, server, monkeypatch):
        script, port = server
        script.push(503, {"Retry-After": "2"}, QUEUE_FULL)
        d = _dest(port, monkeypatch)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        res = rp.publish_sync(_payload())
        assert res["retry_after"] == 2.0             # backoff floor for the caller
        assert d.failure_count == 0
        rp.shutdown(wait=False)


# ================================================================ pipeline loop

class TestPipelineTermination:
    """The pipeline's retry loop stops - and does NOT re-arm the track - when
    the verdict is terminal. This is the end of PUBLISH_FAILED -> re-arm -> 60s
    -> retry -> forever."""

    def _pipeline(self, publisher):
        sys.path.insert(0, os.path.join(REPO, "InferenceNode"))
        from InferenceNode.pipeline import InferencePipeline
        p = InferencePipeline()
        p.pipeline_name = "cam-termination"
        p.result_publisher = publisher
        p.PUBLISH_RETRY_DELAY_SECONDS = 0.05
        p.PUBLISH_MAX_RETRIES = 5
        p._init_dedup()
        return p

    def _job(self):
        return {"det": {"class_name": "person", "confidence": 0.9,
                        "bbox": [1, 1, 5, 5], "track_id": 7},
                "track_key": ("person", 7), "iou_key": None,
                "json_results": {}, "frame": None}

    def test_all_destinations_disabled_stops_without_rearm(self, server, monkeypatch):
        script, port = server
        script.push(401)                             # first attempt disables
        d = _dest(port, monkeypatch)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        p = self._pipeline(rp)
        t0 = time.time()
        p._deliver_job(self._job())
        elapsed = time.time() - t0
        # Attempt 1 disabled the destination; attempt 2 sees attempted==0 and
        # stops - never sleeping through the remaining 4 backoffs.
        assert len(script.requests) == 1
        assert elapsed < 2.0
        assert ("person", 7) not in p._failed_backoff     # NOT re-armed
        assert p._publish_failures == 1
        rp.shutdown(wait=False)

    def test_terminal_payload_stops_immediately_without_rearm(self, server, monkeypatch):
        script, port = server
        script.push(422)
        d = _dest(port, monkeypatch)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        p = self._pipeline(rp)
        p._deliver_job(self._job())
        assert len(script.requests) == 1             # one POST, zero retries
        assert ("person", 7) not in p._failed_backoff
        assert d.enabled is True                     # destination survives
        rp.shutdown(wait=False)

    def test_retryable_failure_still_rearms(self, server, monkeypatch):
        """Non-terminal failures keep today's behaviour: bounded retries, then
        the track becomes re-eligible after FAILED_BACKOFF_SECONDS."""
        script, port = server
        for _ in range(3):
            script.push(500)
        script.push(200)                             # never reached (retries=2 here)
        d = _dest(port, monkeypatch)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        p = self._pipeline(rp)
        p.PUBLISH_MAX_RETRIES = 2                    # 3 attempts -> exhausted
        p._deliver_job(self._job())
        assert len(script.requests) == 3
        assert ("person", 7) in p._failed_backoff    # re-armed: retryable class
        rp.shutdown(wait=False)

    def test_max_failures_beats_remaining_retry_slots(self, server, monkeypatch):
        """PUBLISH_MAX_RETRIES=5 allows 6 attempts, but attempt 5 crosses
        max_failures and disables - the 6th HTTP request must never start."""
        script, port = server
        for _ in range(8):
            script.push(500)
        d = _dest(port, monkeypatch)
        d._id = "wh"
        rp = ResultPublisher()
        rp.add(d)
        p = self._pipeline(rp)                       # PUBLISH_MAX_RETRIES = 5
        p._deliver_job(self._job())
        assert len(script.requests) == 5             # not 6
        assert d.enabled is False
        assert d.failure_count == 5
        assert ("person", 7) not in p._failed_backoff  # terminal: no re-arm
        rp.shutdown(wait=False)


# ================================================================= thread safety

class TestThreadSafety:
    """The lifecycle counters live under the destination's lock: concurrent
    attempts lose no increments, disable exactly once, and backpressure moves
    nothing."""

    class _StubDest(BaseResultDestination):
        def __init__(self, result):
            super().__init__()
            self._result = result
            self.is_configured = True

        def configure(self, **kwargs):
            pass

        def close(self):
            pass

        def _publish(self, data):
            return self._result

    def _hammer(self, dest, n_threads=16, per_thread=200):
        """Concurrent attempts with an adversarial GIL switch interval, so the
        interpreter actually interleaves mid-increment. Without this, CPython's
        default 5ms switch hides the read-modify-write race that the lock
        exists to close (verified: the unlocked variant passes the friendly
        hammer and fails this one)."""
        barrier = threading.Barrier(n_threads)
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            def work():
                barrier.wait()
                for _ in range(per_thread):
                    result = dest._try_send({})
                    dest._account(result)

            threads = [threading.Thread(target=work) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            sys.setswitchinterval(old_interval)
        return n_threads * per_thread

    def test_no_lost_failure_increments(self):
        dest = self._StubDest(DeliveryResult(
            success=False, outcome="SERVER_ERROR", retryable=True,
            count_toward_destination_failure=True, error="x"))
        dest.max_failures = 10 ** 9                  # keep it enabled throughout
        total = self._hammer(dest)
        assert dest.failure_count == total           # read-modify-write never lost

    def test_no_double_counted_successes(self):
        dest = self._StubDest(DeliveryResult.ok())
        total = self._hammer(dest)
        assert dest.frame_count == total
        assert dest.success_count_since_failure == total

    def test_disables_exactly_once_under_concurrency(self, caplog):
        dest = self._StubDest(DeliveryResult(
            success=False, outcome="SERVER_ERROR", retryable=True,
            count_toward_destination_failure=True, error="x"))
        dest.max_failures = 5
        with caplog.at_level(logging.WARNING):
            self._hammer(dest)
        assert dest.enabled is False
        assert dest.failure_threshold_reached is True
        disable_lines = [r for r in caplog.records
                         if "Auto-disabling destination" in r.getMessage()]
        assert len(disable_lines) == 1               # once - not once per thread

    def test_concurrent_backpressure_moves_nothing(self):
        dest = self._StubDest(DeliveryResult(
            success=False, outcome="RATE_LIMITED", retryable=True,
            count_toward_destination_failure=False, error="429"))
        self._hammer(dest)
        assert dest.failure_count == 0
        assert dest.enabled is True

    def test_frame_limit_pauses_exactly_once(self, caplog):
        dest = self._StubDest(DeliveryResult.ok())
        dest.max_frames = 50
        with caplog.at_level(logging.WARNING):
            self._hammer(dest)
        assert dest.frame_limit_reached is True
        pause_lines = [r for r in caplog.records
                       if "Frame limit reached" in r.getMessage()]
        assert len(pause_lines) == 1

    def test_inflight_straddling_disable_is_safe(self):
        """A request already in flight when the disable transition fires must
        finish without double-disabling or corrupting counters, and no NEW
        request starts afterwards."""
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class _Slow(BaseResultDestination):
            def configure(self, **kwargs):
                pass

            def close(self):
                pass

            def _publish(self, data):
                calls.append(1)
                entered.set()
                release.wait(timeout=5)
                return DeliveryResult(
                    success=False, outcome="SERVER_ERROR", retryable=True,
                    count_toward_destination_failure=True, error="slow")

        dest = _Slow()
        dest.is_configured = True
        dest.max_failures = 5
        dest.failure_count = 4                       # one failure away

        slow = threading.Thread(target=lambda: dest._account(dest._try_send({})))
        slow.start()
        entered.wait(timeout=5)
        # While it is in flight, another attempt crosses the threshold
        dest._account(DeliveryResult(
            success=False, outcome="CONNECTION_REFUSED", retryable=True,
            count_toward_destination_failure=True, error="refused"))
        assert dest.enabled is False                 # disabled by the fast attempt
        release.set()
        slow.join(timeout=5)
        # In-flight attempt completed and accounted exactly once - no corruption
        assert dest.failure_count == 6
        assert dest.failure_threshold_reached is True
        # No NEW request starts after the disable
        assert dest.publish_once({})["status"] == "disabled"
        assert len(calls) == 1

    def test_lock_is_reentrant(self):
        dest = self._StubDest(DeliveryResult.ok())
        with dest._lock:
            with dest._lock:                          # RLock: nesting degrades safely
                dest._account(DeliveryResult.ok())
        assert dest.frame_count == 1


# ============================================================== payload stability

class TestPayloadStability:
    """What is actually stable across retries - asserted honestly. There is NO
    stable event id (no event_id/detection_id field), so no test here claims
    'payload identity': the receiver can only deduplicate image-bearing posts
    via its content hash. The per-attempt timestamp is documented as CHANGING."""

    def test_logical_fields_stable_timestamp_documented_changing(self, server, monkeypatch):
        script, port = server
        script.push(500)
        script.push(200)
        d = _dest(port, monkeypatch)
        payload = _payload()
        d.publish_once(payload)
        time.sleep(0.02)
        d.publish_once(payload)
        first = json.loads(script.requests[0]["body"])
        second = json.loads(script.requests[1]["body"])
        for key in ("pipeline_id", "pipeline_name", "node_id", "results"):
            assert first[key] == second[key], f"{key} must be stable across retries"
        # Known limitation (reported, not hidden): the timestamp is stamped per
        # attempt, so retries are NOT byte-identical.
        assert first["timestamp"] != second["timestamp"]

    def test_bearer_on_every_attempt(self, server, monkeypatch):
        script, port = server
        script.push(500)
        script.push(200)
        d = _dest(port, monkeypatch)
        d.publish_once(_payload())
        d.publish_once(_payload())
        assert [r["auth"] for r in script.requests] == [f"Bearer {TOKEN}"] * 2

    def test_retry_after_never_stored_on_destination(self, server, monkeypatch):
        script, port = server
        script.push(503, {"Retry-After": "2"}, QUEUE_FULL)
        d = _dest(port, monkeypatch)
        out = d.publish_once(_payload())
        assert out["retry_after"] == 2.0             # travels with the attempt
        assert not hasattr(d, "retry_after")         # never shared mutable state
        assert not hasattr(d, "last_outcome")
