"""
Tests for webhook Bearer-token authentication.

Run:  python -m pytest tests/test_webhook_auth.py -v

Uses a local (plain HTTP, NOT HTTPS) test server to capture the exact headers the
WebhookDestination sends and to return scripted status codes.
"""
import os
import sys
import threading
import logging

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Make the repo importable when pytest is run from the repo root
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ResultPublisher.plugins.webhook_destination import WebhookDestination  # noqa: E402

TOKEN = "SUPERSECRET_TOKEN_xyz.123"

# Every auth-related env var we must control so the host environment can't leak in
_AUTH_ENV_VARS = ("WEBHOOK_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN_FILE", "WEBHOOK_AUTH_REQUIRED")


class _Capture:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []  # list of {"headers": {...}, "path": str, "body": bytes}


def _make_server(capture):
    """Start a local plain-HTTP server (NOT HTTPS) that records requests and returns
    the next scripted status (default 200)."""
    cap = capture

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            cap.requests.append({
                "headers": {k: v for k, v in self.headers.items()},
                "path": self.path,
                "body": body,
            })
            status = cap.statuses.pop(0) if cap.statuses else 200
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    """Ensure no ambient auth env leaks into a test; each test sets what it needs."""
    for var in _AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def server():
    cap = _Capture(statuses=[])
    srv = _make_server(cap)
    port = srv.server_address[1]
    yield cap, port
    srv.shutdown()


def _make_dest(port, statuses, cap=None, monkeypatch=None, token=TOKEN,
               auth_required=None, extra_headers=None, path="/api/webhooks/detections"):
    if cap is not None:
        cap.statuses = list(statuses)
    if monkeypatch is not None and token is not None:
        monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", token)
    dest = WebhookDestination()
    # Literal port in the URL (avoid {port}, which the sender rewrites to 80/443)
    dest.configure(url=f"http://127.0.0.1:{port}{path}", headers=extra_headers,
                   timeout=5, auth_required=auth_required)
    return dest


def _payload():
    return {"pipeline_id": "p1", "pipeline_name": "cam", "results": {"num_detections": 1, "predictions": []}}


# --------------------------------------------------------------------------- tests

def test_authorization_header_is_sent(server, monkeypatch):
    cap, port = server
    dest = _make_dest(port, [200], cap=cap, monkeypatch=monkeypatch)
    assert dest._publish(_payload()).success is True
    assert len(cap.requests) == 1
    assert "Authorization" in cap.requests[0]["headers"]


def test_authorization_header_exact_format(server, monkeypatch):
    cap, port = server
    dest = _make_dest(port, [200], cap=cap, monkeypatch=monkeypatch)
    dest._publish(_payload())
    assert cap.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_token_not_in_url_query_or_body(server, monkeypatch):
    cap, port = server
    dest = _make_dest(port, [200], cap=cap, monkeypatch=monkeypatch)
    dest._publish(_payload())
    req = cap.requests[0]
    assert TOKEN not in req["path"]
    assert TOKEN.encode() not in req["body"]


def test_token_not_exposed_in_logs(server, monkeypatch, caplog, capsys):
    cap, port = server
    dest = _make_dest(port, [200], cap=cap, monkeypatch=monkeypatch)
    with caplog.at_level(logging.DEBUG):
        dest._publish(_payload())
    out = capsys.readouterr()
    # Raw token must appear in neither logs nor stdout/stderr
    assert TOKEN not in caplog.text
    assert TOKEN not in out.out and TOKEN not in out.err
    # And the debug header log must be redacted
    assert "Bearer ***REDACTED***" in caplog.text


def test_missing_token_prevents_transmission(server, monkeypatch):
    cap, port = server
    # auth_required True (default) and NO token set -> must not send.
    # Driven through publish_once: the DISABLING now happens in the base
    # class's _account (single-owner lifecycle), not inside _publish.
    dest = _make_dest(port, [200], cap=cap, monkeypatch=monkeypatch, token=None)
    assert dest._auth_token is None
    out = dest.publish_once(_payload())
    assert out["status"] == "failed"
    assert out["outcome"] == "AUTH_FAILED"
    assert len(cap.requests) == 0          # no HTTP request made
    assert dest.enabled is False           # fail-closed disables the destination
    # Second attempt still makes no HTTP request
    assert dest.publish_once(_payload())["status"] == "disabled"
    assert len(cap.requests) == 0


def test_auth_optional_allows_unauthenticated(server, monkeypatch):
    cap, port = server
    dest = _make_dest(port, [200], cap=cap, monkeypatch=monkeypatch, token=None, auth_required=False)
    assert dest._publish(_payload()).success is True
    assert len(cap.requests) == 1
    assert "Authorization" not in cap.requests[0]["headers"]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_non_retryable(server, monkeypatch, status):
    cap, port = server
    dest = _make_dest(port, [status], cap=cap, monkeypatch=monkeypatch)
    out = dest.publish_once(_payload())
    assert out["status"] == "failed"
    assert out["outcome"] == "AUTH_FAILED"
    assert len(cap.requests) == 1
    assert dest.enabled is False
    # Second-call assertion: no additional HTTP request after disabling
    assert dest.publish_once(_payload())["status"] == "disabled"
    assert len(cap.requests) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_log_has_no_token(server, monkeypatch, caplog, capsys, status):
    cap, port = server
    dest = _make_dest(port, [status], cap=cap, monkeypatch=monkeypatch)
    with caplog.at_level(logging.DEBUG):
        dest._publish(_payload())
    out = capsys.readouterr()
    assert TOKEN not in caplog.text
    assert TOKEN not in out.out and TOKEN not in out.err
    assert str(status) in (caplog.text + out.out)  # safe status is reported


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_failures_remain_retryable(server, monkeypatch, status):
    cap, port = server
    dest = _make_dest(port, [status], cap=cap, monkeypatch=monkeypatch)
    result = dest._publish(_payload())
    assert result.success is False and result.retryable is True
    assert len(cap.requests) == 1
    # Retryable: destination stays enabled so the existing retry path applies
    assert dest.enabled is True
    # A subsequent attempt DOES make another HTTP request (retry allowed)
    cap.statuses = [200]
    assert dest._publish(_payload()).success is True
    assert len(cap.requests) == 2


@pytest.mark.parametrize("multi_5xx", [[500, 500, 200], [503, 502, 500, 200]])
def test_multiple_5xx_then_success(server, monkeypatch, multi_5xx):
    cap, port = server
    dest = _make_dest(port, list(multi_5xx), cap=cap, monkeypatch=monkeypatch)
    results = [dest.publish_once(_payload())["status"] for _ in multi_5xx]
    assert results[:-1] == ["failed"] * (len(multi_5xx) - 1)
    assert results[-1] == "success"
    assert dest.enabled is True
    assert dest.failure_count == 0     # the success reset the consecutive count
    assert len(cap.requests) == len(multi_5xx)


@pytest.mark.parametrize("header_name", ["Authorization", "authorization", "AUTHORIZATION", "AuThOrIzAtIoN"])
def test_configured_authorization_header_is_stripped(server, monkeypatch, caplog, header_name):
    cap, port = server
    with caplog.at_level(logging.WARNING):
        dest = _make_dest(port, [200], cap=cap, monkeypatch=monkeypatch,
                          extra_headers={"Content-Type": "application/json", header_name: "Bearer HACKED_FROM_CONFIG"})
    # The config-provided header must not survive on self.headers (case-insensitive)
    assert not any(k.lower() == "authorization" for k in dest.headers)
    # A warning is logged, but never the header value
    assert "HACKED_FROM_CONFIG" not in caplog.text
    dest._publish(_payload())
    # Only the env token is sent, never the config value
    assert cap.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_no_warning_for_localhost_http(server, monkeypatch, caplog):
    cap, port = server
    dest = _make_dest(port, [200], cap=cap, monkeypatch=monkeypatch)
    with caplog.at_level(logging.WARNING):
        dest._publish(_payload())
    assert "non-HTTPS" not in caplog.text  # 127.0.0.1 is exempt


def test_warn_only_for_non_local_http(monkeypatch, caplog):
    # Warn-only: sending a token over http to a non-local host logs a warning but
    # still sends. We stub requests.post so no real network call is made.
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    sent = {}

    class _Resp:
        status_code = 200

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["headers"] = headers
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    dest = WebhookDestination()
    dest.configure(url="http://example.com/api/webhooks/detections", timeout=5)
    with caplog.at_level(logging.WARNING):
        assert dest._publish(_payload()).success is True
    assert "non-HTTPS" in caplog.text          # warned
    assert sent["headers"]["Authorization"] == f"Bearer {TOKEN}"  # but still sent
    assert TOKEN not in caplog.text            # warning didn't leak the token


def test_token_from_secret_file(tmp_path, server, monkeypatch):
    cap, port = server
    secret = tmp_path / "webhook_auth_token"
    secret.write_text(f"  {TOKEN}\n")  # whitespace should be stripped
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN_FILE", str(secret))
    dest = WebhookDestination()
    dest.configure(url=f"http://127.0.0.1:{port}/api/webhooks/detections", timeout=5)
    assert dest._auth_token == TOKEN
    cap.statuses = [200]
    dest._publish(_payload())
    assert cap.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_oversized_token_file_is_rejected(tmp_path, monkeypatch, caplog):
    big = tmp_path / "big_token"
    big.write_text("A" * 9000)  # > 8192 byte cap
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN_FILE", str(big))
    dest = WebhookDestination()
    with caplog.at_level(logging.ERROR):
        dest.configure(url="http://127.0.0.1:1/api/webhooks/detections", timeout=5)
    assert dest._auth_token is None            # oversized file ignored
    assert "AAAA" not in caplog.text           # contents never logged


def test_empty_token_file_is_rejected(tmp_path, monkeypatch):
    empty = tmp_path / "empty_token"
    empty.write_text("   \n")
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN_FILE", str(empty))
    dest = WebhookDestination()
    dest.configure(url="http://127.0.0.1:1/api/webhooks/detections", timeout=5)
    assert dest._auth_token is None
