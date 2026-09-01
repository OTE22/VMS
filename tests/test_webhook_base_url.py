"""WEBHOOK_BASE_URL productionization tests.

Covers URL construction ({base}/webhook/{pipeline_id}), base-URL validation,
pipeline-id safety, exact-host preservation (NO automatic 127.0.0.1 rewriting),
Bearer auth, token-never-logged, status handling (400/401/403/404/408/429/5xx),
network failures (DNS/refused/timeout), legacy fallback + deprecation warning.

Run:  python -m pytest tests/test_webhook_base_url.py -v
"""
import logging
import os
import sys
import threading

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from ResultPublisher.plugins.webhook_destination import (  # noqa: E402
    WebhookDestination, validate_base_url, safe_pipeline_id,
    build_webhook_url, load_webhook_base_url)

TOKEN = "SUPERSECRET_TOKEN_xyz.123"
PID = "1971528f-d514-4275-9879-bf68ae00ff6b"
_AUTH_ENV = ("WEBHOOK_BASE_URL", "WEBHOOK_AUTH_TOKEN", "WEBHOOK_AUTH_TOKEN_FILE",
             "WEBHOOK_AUTH_REQUIRED")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in _AUTH_ENV:
        monkeypatch.delenv(v, raising=False)
    yield


# --------------------------------------------------------------- validation
@pytest.mark.parametrize("raw,expected", [
    ("http://192.168.1.50:8000", "http://192.168.1.50:8000"),      # IPv4 + custom port
    ("https://192.168.1.50", "https://192.168.1.50"),               # IPv4 + https
    ("https://face-detector.internal", "https://face-detector.internal"),  # DNS
    ("https://face-detector.internal:8443", "https://face-detector.internal:8443"),
    ("http://192.168.1.50:8000/", "http://192.168.1.50:8000"),      # trailing slash
    ("https://face-detector.internal:8443/", "https://face-detector.internal:8443"),
    ("  http://10.0.0.50  ", "http://10.0.0.50"),                    # whitespace
    ("http://host.docker.internal", "http://host.docker.internal"),  # same-host, explicit
])
def test_validate_base_url_accepts(raw, expected):
    assert validate_base_url(raw) == expected


@pytest.mark.parametrize("bad", [
    "", None, "ftp://192.168.1.50", "192.168.1.50:8000",            # scheme
    "http://", "https://",                                            # no host
    "http://user:pass@192.168.1.50",                                  # credentials
    "http://192.168.1.50/webhook",                                    # path component
    "http://192.168.1.50/some/path",
    "http://192.168.1.50?x=1", "http://192.168.1.50#frag",            # query/fragment
    "http://192.168.1.50:70000", "http://192.168.1.50:0",             # bad port
])
def test_validate_base_url_rejects(bad):
    assert validate_base_url(bad) is None


def test_load_base_url_from_env(monkeypatch):
    monkeypatch.setenv("WEBHOOK_BASE_URL", "http://192.168.1.50:8000/")
    assert load_webhook_base_url() == "http://192.168.1.50:8000"


def test_load_base_url_invalid_returns_none_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("WEBHOOK_BASE_URL", "not-a-url")
    with caplog.at_level(logging.ERROR):
        assert load_webhook_base_url() is None
    assert "Invalid WEBHOOK_BASE_URL" in caplog.text


# ------------------------------------------------------- pipeline id safety
@pytest.mark.parametrize("pid", [PID, "cam-001", "cam_002", "a.b.c", "123"])
def test_safe_pipeline_id_accepts(pid):
    assert safe_pipeline_id(pid) == pid


@pytest.mark.parametrize("bad", [
    "../x", "a/b", "", "   ", None, "..", "a b", "x?y=1", "x#f",
    "%2e%2e", "http://evil.com", "a\\b", "a" * 256,
])
def test_safe_pipeline_id_rejects(bad):
    assert safe_pipeline_id(bad) is None


def test_pipeline_id_cannot_change_destination_host():
    assert build_webhook_url("http://192.168.1.50:8000", "../../evil") is None
    assert build_webhook_url("http://192.168.1.50:8000", "http://evil.com") is None


# ----------------------------------------------------------- URL building
def test_build_url_exact_spec_example():
    assert build_webhook_url("http://192.168.1.50:8000", "cam-001") == \
        "http://192.168.1.50:8000/webhook/cam-001"


def test_build_url_real_pipeline_id():
    assert build_webhook_url("http://192.168.1.50:8000", PID) == \
        f"http://192.168.1.50:8000/webhook/{PID}"


def test_trailing_slash_no_double_slash():
    url = build_webhook_url("http://192.168.1.50:8000/", "cam-001")
    assert url == "http://192.168.1.50:8000/webhook/cam-001"
    assert "//webhook" not in url


def test_multiple_pipelines_share_one_base():
    base = "https://face-detector.internal"
    urls = [build_webhook_url(base, p) for p in ("cam-001", "cam-002", "cam-003")]
    assert urls == [f"{base}/webhook/cam-00{i}" for i in (1, 2, 3)]
    assert len({u.rsplit("/webhook/", 1)[0] for u in urls}) == 1  # same host for all


def test_remote_ip_preserved_exactly_no_rewrite():
    """The configured host is used verbatim - notably 127.0.0.1 is NEVER
    auto-rewritten to host.docker.internal."""
    assert build_webhook_url("http://127.0.0.1:8000", "cam-001") == \
        "http://127.0.0.1:8000/webhook/cam-001"
    assert "host.docker.internal" not in build_webhook_url("http://127.0.0.1", "x")
    assert build_webhook_url("http://10.20.30.40:9000", "x").startswith("http://10.20.30.40:9000/")


# ------------------------------------------------- live server (statuses)
class _Cap:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []


def _server(cap):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            cap.requests.append({"path": self.path,
                                 "auth": self.headers.get("Authorization")})
            code = cap.statuses.pop(0) if cap.statuses else 200
            self.send_response(code)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def server():
    cap = _Cap([])
    srv = _server(cap)
    yield cap, srv.server_address[1]
    srv.shutdown()


def _dest(port, monkeypatch, token=TOKEN, base=None):
    monkeypatch.setenv("WEBHOOK_BASE_URL", base or f"http://127.0.0.1:{port}")
    if token:
        monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", token)
    d = WebhookDestination()
    d.configure(timeout=5)          # no per-destination url: base-URL mode
    return d


def _payload(pid=PID):
    return {"pipeline_id": pid, "results": {"num_detections": 1, "predictions": []}}


def test_success_2xx_and_exact_path(server, monkeypatch):
    cap, port = server
    cap.statuses = [200]
    d = _dest(port, monkeypatch)
    assert d._publish(_payload()).success is True
    assert cap.requests[0]["path"] == f"/webhook/{PID}"


def test_bearer_token_sent(server, monkeypatch):
    cap, port = server
    cap.statuses = [200]
    _dest(port, monkeypatch)._publish(_payload())
    assert cap.requests[0]["auth"] == f"Bearer {TOKEN}"


def test_token_never_logged(server, monkeypatch, caplog, capsys):
    cap, port = server
    cap.statuses = [200]
    d = _dest(port, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        d._publish(_payload())
    out = capsys.readouterr()
    assert TOKEN not in caplog.text
    assert TOKEN not in out.out and TOKEN not in out.err


def test_delivery_log_has_context(server, monkeypatch, caplog):
    cap, port = server
    cap.statuses = [200]
    d = _dest(port, monkeypatch)
    with caplog.at_level(logging.INFO):
        d._publish(_payload())
    assert f"pipeline_id={PID}" in caplog.text
    assert "host=127.0.0.1" in caplog.text and "duration_ms=" in caplog.text
    assert "outcome=SUCCESS" in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_disables(server, monkeypatch, status):
    cap, port = server
    cap.statuses = [status]
    d = _dest(port, monkeypatch)
    out = d.publish_once(_payload())            # disable happens in _account
    assert out["status"] == "failed" and out["outcome"] == "AUTH_FAILED"
    assert d.enabled is False
    assert d.publish_once(_payload())["status"] == "disabled"   # no further HTTP
    assert len(cap.requests) == 1


def test_wrong_route_404_disables(server, monkeypatch):
    """404 is destination-level: the receiver never 404s an unknown pipeline_id
    (it returns 202), so a 404 can only mean the base URL / route is wrong for
    EVERY delivery."""
    cap, port = server
    cap.statuses = [404]
    d = _dest(port, monkeypatch)
    out = d.publish_once(_payload())
    assert out["status"] == "failed" and out["outcome"] == "NOT_FOUND"
    assert d.enabled is False
    assert d.publish_once(_payload())["status"] == "disabled"
    assert len(cap.requests) == 1


@pytest.mark.parametrize("status,outcome", [
    (400, "INVALID_PAYLOAD"), (413, "PAYLOAD_TOO_LARGE"), (422, "INVALID_PAYLOAD")])
def test_payload_rejection_is_delivery_terminal_only(server, monkeypatch, status, outcome):
    """400/413/422 kill THIS delivery, never the destination: one malformed or
    oversized payload must not take webhook delivery offline for every event."""
    cap, port = server
    cap.statuses = [status]
    d = _dest(port, monkeypatch)
    out = d.publish_once(_payload())
    assert out["status"] == "permanent_failure" and out["outcome"] == outcome
    assert d.enabled is True                    # destination survives
    assert d.failure_count == 0                 # zero transport-failure increments
    cap.statuses = [200]                        # the next delivery still goes out
    assert d.publish_once(_payload())["status"] == "success"
    assert len(cap.requests) == 2


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_transient_statuses_stay_retryable(server, monkeypatch, status):
    cap, port = server
    cap.statuses = [status]
    d = _dest(port, monkeypatch)
    result = d._publish(_payload())
    assert result.success is False and result.retryable is True
    assert d.enabled is True                    # retryable -> worker retries (bounded)
    cap.statuses = [200]
    assert d._publish(_payload()).success is True


def test_no_token_logged_on_auth_failure(server, monkeypatch, caplog, capsys):
    cap, port = server
    cap.statuses = [401]
    d = _dest(port, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        d._publish(_payload())
    out = capsys.readouterr()
    assert TOKEN not in caplog.text and TOKEN not in out.out


# ------------------------------------------------------- network failures
def test_connection_refused(monkeypatch, caplog):
    monkeypatch.setenv("WEBHOOK_BASE_URL", "http://127.0.0.1:9")   # discard port
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    d = WebhookDestination(); d.configure(timeout=2)
    with caplog.at_level(logging.WARNING):
        result = d._publish(_payload())
    assert result.success is False
    # Proven root cause -> CONNECTION_REFUSED; an unproven chain may only
    # degrade to the honest generic label, never to a wrong specific one.
    assert result.outcome in ("CONNECTION_REFUSED", "CONNECTION_ERROR")
    assert result.count_toward_destination_failure is True
    assert f"outcome={result.outcome}" in caplog.text and "exception=" in caplog.text
    assert d.enabled is True                     # transient -> still retryable


def test_dns_failure(monkeypatch, caplog):
    monkeypatch.setenv("WEBHOOK_BASE_URL", "http://nonexistent.invalid.armyeye")
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    d = WebhookDestination(); d.configure(timeout=2)
    with caplog.at_level(logging.WARNING):
        result = d._publish(_payload())
    assert result.success is False
    assert result.outcome in ("DNS_ERROR", "CONNECTION_ERROR")
    assert result.count_toward_destination_failure is True
    assert f"outcome={result.outcome}" in caplog.text
    assert d.enabled is True


def test_timeout(monkeypatch, caplog):
    """Non-routable address -> connect timeout inside the 5s connect budget."""
    monkeypatch.setenv("WEBHOOK_BASE_URL", "http://10.255.255.1:8000")
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    d = WebhookDestination(); d.configure(timeout=2)
    with caplog.at_level(logging.WARNING):
        result = d._publish(_payload())
    assert result.success is False
    assert result.outcome in ("CONNECT_TIMEOUT", "CONNECTION_ERROR")
    assert result.count_toward_destination_failure is True
    assert f"outcome={result.outcome}" in caplog.text
    assert d.enabled is True


def test_invalid_pipeline_id_drops_delivery(server, monkeypatch, caplog):
    cap, port = server
    d = _dest(port, monkeypatch)
    with caplog.at_level(logging.ERROR):
        result = d._publish({"pipeline_id": "../etc/passwd"})
    assert result.success is False and result.terminal_delivery is True
    assert len(cap.requests) == 0                # never left the process
    assert "invalid pipeline_id" in caplog.text


# ------------------------------------------------------------ legacy mode
def test_legacy_fallback_when_base_unset(server, monkeypatch, caplog):
    cap, port = server
    cap.statuses = [200]
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    d = WebhookDestination()
    with caplog.at_level(logging.WARNING):
        d.configure(url=f"http://127.0.0.1:{port}/webhook/{{pipeline_id}}", timeout=5)
    assert "webhook_mode=legacy" in caplog.text and "DEPRECATED" in caplog.text
    assert d._publish(_payload()).success is True
    assert cap.requests[0]["path"] == f"/webhook/{PID}"


def test_base_url_overrides_legacy(server, monkeypatch, caplog):
    """Base URL wins; the legacy per-destination host is ignored."""
    cap, port = server
    cap.statuses = [200]
    monkeypatch.setenv("WEBHOOK_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    d = WebhookDestination()
    with caplog.at_level(logging.INFO):
        d.configure(url="http://192.0.2.99:1234/webhook/{pipeline_id}", timeout=5)
    assert "webhook_mode=base_url" in caplog.text
    assert d._publish(_payload()).success is True
    assert cap.requests[0]["path"] == f"/webhook/{PID}"   # went to the base host


def test_invalid_base_falls_back_to_legacy(server, monkeypatch, caplog):
    cap, port = server
    cap.statuses = [200]
    monkeypatch.setenv("WEBHOOK_BASE_URL", "totally-bogus")
    monkeypatch.setenv("WEBHOOK_AUTH_TOKEN", TOKEN)
    d = WebhookDestination()
    with caplog.at_level(logging.ERROR):
        d.configure(url=f"http://127.0.0.1:{port}/webhook/{{pipeline_id}}", timeout=5)
    assert "Invalid WEBHOOK_BASE_URL" in caplog.text
    assert d._publish(_payload()).success is True      # legacy still delivers


def test_no_url_and_no_base_is_unconfigured(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        d = WebhookDestination(); d.configure()
    assert d.is_configured is False
    assert "no URL" in caplog.text
