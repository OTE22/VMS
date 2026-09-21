import json
import os
import re
import socket
import time
import threading
import math
import logging
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from urllib.parse import urlparse, quote
try:
    from ..base_destination import BaseResultDestination, DeliveryResult
except ImportError:
    # Fallback for when running directly
    from base_destination import BaseResultDestination, DeliveryResult

_module_logger = logging.getLogger(__name__)

# Bearer-token auth configuration ------------------------------------------------
# The token is read from the environment / a Docker secret at configure() time and
# is NEVER hard-coded, printed, serialized, returned, logged, or stored in
# self.headers. Changing the env var or secret file requires the destination to be
# reconfigured (configure() re-run) or the service restarted for it to take effect.
_MAX_TOKEN_FILE_BYTES = 8192            # reject implausibly large token files
_DEFAULT_SECRET_PATH = "/run/secrets/webhook_auth_token"  # default Docker secret path
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}          # exempt from the non-HTTPS warning


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _read_token_file(path: str) -> Optional[str]:
    """Read a Bearer token from a file (e.g. a Docker secret). Returns the stripped
    token or None. Handles missing/unreadable/empty/oversized files by logging only a
    safe error - never the file contents."""
    try:
        size = os.path.getsize(path)
        if size > _MAX_TOKEN_FILE_BYTES:
            _module_logger.error(
                f"Webhook auth token file too large ({size} bytes > {_MAX_TOKEN_FILE_BYTES}); ignoring")
            return None
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        if not token:
            _module_logger.error("Webhook auth token file is empty; ignoring")
            return None
        return token
    except OSError as e:
        # Never log the path contents; only the error class for diagnostics
        _module_logger.error(f"Could not read webhook auth token file: {e.__class__.__name__}")
        return None


def _load_auth_token() -> Optional[str]:
    """Resolve the Bearer token. Resolution order:
      1. WEBHOOK_AUTH_TOKEN_FILE  - path to a file / Docker secret
      2. WEBHOOK_AUTH_TOKEN       - environment variable
      3. /run/secrets/webhook_auth_token - default Docker secret path (if present)
    Returns the token string or None. Never logs the token value."""
    file_path = os.environ.get("WEBHOOK_AUTH_TOKEN_FILE")
    if file_path:
        return _read_token_file(file_path)
    env_token = os.environ.get("WEBHOOK_AUTH_TOKEN")
    if env_token is not None:
        env_token = env_token.strip()
        return env_token or None
    if os.path.exists(_DEFAULT_SECRET_PATH):
        return _read_token_file(_DEFAULT_SECRET_PATH)
    return None


def _redact_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Return a copy of headers with any Authorization value redacted (case-insensitive)."""
    redacted = {}
    for k, v in (headers or {}).items():
        redacted[k] = "Bearer ***REDACTED***" if k.lower() == "authorization" else v
    return redacted


# --------------------------------------------------------------------------- #
# Delivery-attempt classification
#
# Transport exceptions and HTTP statuses map onto the DeliveryResult decision
# fields via ONE complete matrix (no status is unclassified):
#
#   condition                       outcome            retry term  disable count
#   ConnectTimeout                  CONNECT_TIMEOUT      Y    -      -      Y
#   ReadTimeout                     READ_TIMEOUT         Y    -      -      Y
#   SSLError                        TLS_ERROR            Y    -      -      Y
#   ConnectionError<-gaierror       DNS_ERROR            Y    -      -      Y
#   ConnectionError<-refused        CONNECTION_REFUSED   Y    -      -      Y
#   other ConnectionError           CONNECTION_ERROR     Y    -      -      Y   (no guessing)
#   2xx                             SUCCESS              -    -      -      -
#   400                             INVALID_PAYLOAD      -    Y      -      -
#   401/403                         AUTH_FAILED          -    -      Y      -
#   404                             NOT_FOUND            -    -      Y      -   (route/base URL
#                                                                              wrong for EVERY
#                                                                              delivery: receiver
#                                                                              never 404s an
#                                                                              unknown pipeline)
#   408                             SERVER_ERROR         Y    -      -      Y
#   413                             PAYLOAD_TOO_LARGE    -    Y      -      -
#   422                             INVALID_PAYLOAD      -    Y      -      -
#   429                             RATE_LIMITED         Y    -      -      -   backpressure
#   503 + queue-full marker         BACKPRESSURE         Y    -      -      -   backpressure
#   500/502/504/other 503/other 5xx SERVER_ERROR         Y    -      -      Y
#   any other 4xx                   INVALID_PAYLOAD      -    Y      -      -
#   anything else                   SERVER_ERROR         Y    -      -      Y
#
# Backpressure (429, receiver queue-full 503) is retryable but NEVER advances
# failure_count: a busy receiver is not a broken one. A 503 WITHOUT the
# receiver's documented queue-full body marker cannot be told apart from a real
# upstream outage (nginx limit_req also answers a bare 503 on this route), so
# per "do not guess" it stays SERVER_ERROR and counts toward health.
# --------------------------------------------------------------------------- #

_RETRY_AFTER_MAX_SECONDS = 30.0  # clamp: a hint, never an unbounded stall


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header: delta-seconds or HTTP-date (RFC 9110).
    Returns seconds clamped to [0, _RETRY_AFTER_MAX_SECONDS], or None when the
    header is absent/malformed - callers then use their normal backoff."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, _RETRY_AFTER_MAX_SECONDS)


def _exception_chain(exc: BaseException):
    """Walk an exception's wrappers (__cause__/__context__, args, urllib3's
    .reason) to find the root cause requests buried. Cycle-safe."""
    seen = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        stack.append(e.__cause__)
        stack.append(e.__context__)
        reason = getattr(e, "reason", None)
        if isinstance(reason, BaseException):
            stack.append(reason)
        for arg in getattr(e, "args", ()):
            if isinstance(arg, BaseException):
                stack.append(arg)


def _classify_transport_exception(exc: Exception) -> DeliveryResult:
    """Map a requests transport exception onto the taxonomy. Order matters:
    ConnectTimeout and SSLError both subclass ConnectionError, so the specific
    types are tested first. DNS vs refused is decided only by a PROVEN root
    cause (socket.gaierror / ConnectionRefusedError in the chain); anything
    unproven stays CONNECTION_ERROR rather than a guess."""
    import requests.exceptions as rex

    def _health(outcome: str) -> DeliveryResult:
        return DeliveryResult(
            success=False, outcome=outcome, retryable=True,
            count_toward_destination_failure=True,
            error=f"{outcome}: {exc.__class__.__name__}")

    if isinstance(exc, rex.ConnectTimeout):
        return _health("CONNECT_TIMEOUT")
    if isinstance(exc, rex.ReadTimeout):
        return _health("READ_TIMEOUT")
    if isinstance(exc, rex.SSLError):
        return _health("TLS_ERROR")
    if isinstance(exc, rex.ConnectionError):
        for cause in _exception_chain(exc):
            if isinstance(cause, socket.gaierror):
                return _health("DNS_ERROR")
            if isinstance(cause, ConnectionRefusedError):
                return _health("CONNECTION_REFUSED")
        return _health("CONNECTION_ERROR")
    if isinstance(exc, rex.Timeout):
        return _health("READ_TIMEOUT")
    return _health("CONNECTION_ERROR")


def _looks_like_queue_full(response) -> bool:
    """True only for the receiver's documented queue-full contract:
    503 + JSON body {"status": "queue_full", ...}. A 503 without that marker
    could equally be nginx rate-limiting or a dead upstream - never guessed."""
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("status") == "queue_full"


def _classify_status(response) -> DeliveryResult:
    """Map an HTTP response onto the taxonomy (see matrix above)."""
    status = response.status_code

    def _r(outcome, *, retryable=False, terminal=False, disable=False,
           count=False, retry_after=None):
        return DeliveryResult(
            success=False, outcome=outcome, retryable=retryable,
            terminal_delivery=terminal, disable_destination=disable,
            count_toward_destination_failure=count,
            error=f"HTTP {status}", retry_after=retry_after)

    if 200 <= status < 300:
        return DeliveryResult.ok()
    if status in (401, 403):
        return _r("AUTH_FAILED", disable=True)
    if status == 404:
        return _r("NOT_FOUND", disable=True)
    if status == 400:
        return _r("INVALID_PAYLOAD", terminal=True)
    if status == 413:
        return _r("PAYLOAD_TOO_LARGE", terminal=True)
    if status == 422:
        return _r("INVALID_PAYLOAD", terminal=True)
    if status == 429:
        return _r("RATE_LIMITED", retryable=True,
                  retry_after=_parse_retry_after(response.headers.get("Retry-After")))
    if status == 408:
        return _r("SERVER_ERROR", retryable=True, count=True)
    if status == 503 and _looks_like_queue_full(response):
        return _r("BACKPRESSURE", retryable=True,
                  retry_after=_parse_retry_after(response.headers.get("Retry-After")))
    if 500 <= status < 600:
        return _r("SERVER_ERROR", retryable=True, count=True)
    if 400 <= status < 500:
        return _r("INVALID_PAYLOAD", terminal=True)
    return _r("SERVER_ERROR", retryable=True, count=True)


# --------------------------------------------------------------------------- #
# Deployment-level destination: WEBHOOK_BASE_URL (single source of truth)
#
# The receiver contract (verified against FACE_DETECTOR's routes) is:
#     POST {WEBHOOK_BASE_URL}/webhook/{pipeline_id}
# The remote host/port is DEPLOYMENT configuration; only pipeline_id varies per
# pipeline. The configured value is used EXACTLY - no automatic host rewriting
# (host.docker.internal is only appropriate when the admin explicitly configures
# it for a receiver on the same Docker host).
# --------------------------------------------------------------------------- #
_WEBHOOK_PATH_PREFIX = "/webhook/"
_PIPELINE_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,255}$")
_CONNECT_TIMEOUT_SECONDS = 5.0  # separate connect timeout; read timeout stays configurable


def validate_base_url(url: str) -> Optional[str]:
    """Validate and normalize a webhook base URL. Returns the normalized base
    (scheme://host[:port], no trailing slash) or None if invalid.

    Accepts http/https, IPv4 or DNS hostname, optional port 1-65535, and an
    empty or '/' path. Rejects embedded credentials, query, fragment, and any
    real path component - the /webhook/{pipeline_id} path is appended by us.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None  # never embed credentials in the destination
    if parsed.query or parsed.fragment or parsed.params:
        return None
    if parsed.path not in ("", "/"):
        return None  # arbitrary path components are rejected
    try:
        port = parsed.port  # raises ValueError for out-of-range ports
    except ValueError:
        return None
    netloc = parsed.hostname
    if port is not None:
        if not (1 <= port <= 65535):
            return None
        netloc = f"{parsed.hostname}:{port}"
    return f"{parsed.scheme}://{netloc}"


def load_webhook_base_url() -> Optional[str]:
    """Read + validate WEBHOOK_BASE_URL from the environment. Returns the
    normalized base, or None when unset/invalid (invalid values are logged as a
    clear configuration error - they never crash startup)."""
    raw = (os.environ.get("WEBHOOK_BASE_URL") or "").strip()
    if not raw:
        return None
    normalized = validate_base_url(raw)
    if normalized is None:
        _module_logger.error(
            f"Invalid WEBHOOK_BASE_URL {raw!r} - expected http(s)://host[:port] with no "
            f"path/query/credentials. Falling back to legacy per-destination URL if configured.")
        return None
    return normalized


def safe_pipeline_id(pipeline_id) -> Optional[str]:
    """Validate a pipeline id for safe insertion into the URL path. Returns the
    URL-encoded id, or None for empty/malformed/malicious values (path traversal,
    separators, whitespace, query/fragment injection)."""
    if pipeline_id is None:
        return None
    pid = str(pipeline_id).strip()
    if not pid or not _PIPELINE_ID_RE.match(pid) or ".." in pid:
        return None
    return quote(pid, safe="")


def build_webhook_url(base_url: str, pipeline_id) -> Optional[str]:
    """Construct {base}/webhook/{pipeline_id}. Returns None when either part is
    invalid. Base is expected pre-normalized (validate_base_url)."""
    normalized = validate_base_url(base_url)
    encoded = safe_pipeline_id(pipeline_id)
    if normalized is None or encoded is None:
        return None
    return f"{normalized}{_WEBHOOK_PATH_PREFIX}{encoded}"


class WebhookDestination(BaseResultDestination):
    """Webhook/HTTP POST result destination with Bearer-token authentication."""

    def __init__(self):
        super().__init__()
        self.url_template = None  # Store the original URL template with variables
        self.url = None
        self.headers = {}
        self.timeout = 30
        # Deployment-level base URL (resolved in configure()). Initialised here
        # so an unconfigured instance reports cleanly instead of raising
        # AttributeError inside _publish.
        self._base_url: Optional[str] = None
        # Auth state (token is resolved in configure(); never logged/stored in headers)
        self._auth_token: Optional[str] = None
        self._auth_required: bool = True
        self._sessions = threading.local()
        self._session_lock = threading.Lock()
        self._open_sessions = []

    def effective_destination(self) -> Dict[str, Optional[str]]:
        """The destination deliveries ACTUALLY go to - never the stored legacy
        url when WEBHOOK_BASE_URL overrides it. This is what startup logs,
        status APIs and the UI must show; the stored config is what the edit
        form shows. URLs never contain credentials (validate_base_url rejects
        them), so this is safe to log and display."""
        if self._base_url:
            return {"mode": "base_url",
                    "url": f"{self._base_url}{_WEBHOOK_PATH_PREFIX}{{pipeline_id}}"}
        if self.url_template:
            return {"mode": "legacy", "url": self.url_template}
        return {"mode": "unconfigured", "url": None}

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """Get configuration schema for Webhook destination"""
        base_schema = super().get_config_schema()

        webhook_fields = [
            {
                'name': 'url',
                'label': 'Webhook URL (legacy)',
                'type': 'url',
                'placeholder': 'Leave empty when WEBHOOK_BASE_URL is configured',
                'description': 'DEPRECATED: prefer the deployment-level WEBHOOK_BASE_URL environment variable; the final URL is then {WEBHOOK_BASE_URL}/webhook/{pipeline_id} for every pipeline. This legacy per-destination template is only used when WEBHOOK_BASE_URL is unset.',
                'required': False
            },
            {
                'name': 'timeout',
                'label': 'Timeout',
                'type': 'number',
                'min': 1,
                'max': 300,
                'placeholder': '30',
                'description': 'Request timeout in seconds',
                'required': False,
                'default': 30,
                'unit': 'seconds'
            },
            {
                'name': 'headers',
                'label': 'Custom Headers',
                'type': 'textarea',
                'placeholder': 'Custom-Header: value',
                'description': 'Optional HTTP headers (one per line, format: Header: Value). The Authorization/Bearer token is NOT configured here - it is read from the WEBHOOK_AUTH_TOKEN environment variable or Docker secret. Any Authorization header set here is ignored for security.',
                'required': False,
                'rows': 3
            }
        ]

        # Add webhook-specific fields to base schema (which already has common fields)
        base_schema['fields'].extend(webhook_fields)
        return base_schema

    def configure(self, url: Optional[str] = None, headers: Optional[Dict[str, str]] = None,
                 timeout: int = 30, rate_limit: Optional[float] = None,
                 max_frames: Optional[int] = None,
                 include_image_data: bool = False, include_result_image: bool = False,
                 auth_required: Optional[bool] = None) -> None:
        """Configure webhook destination.

        Destination resolution: when WEBHOOK_BASE_URL is set (deployment-level,
        single source of truth) the final URL is {base}/webhook/{pipeline_id} and
        the legacy per-destination `url` is ignored. Without it, the legacy `url`
        template keeps working (deprecated). The Bearer token is resolved here from
        the environment / Docker secret; changes require reconfigure or restart.
        """
        # Configure common parameters
        self.configure_common(rate_limit=rate_limit, max_frames=max_frames,
                            include_image_data=include_image_data, include_result_image=include_result_image)

        # Configure webhook-specific parameters
        self.url_template = url  # Store original template
        self.url = url
        from ResultPublisher.config_validation import normalize_config
        self.headers = normalize_config("webhook", {"headers": headers})["headers"] or {"Content-Type": "application/json"}
        if not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 300:
            raise ValueError("Webhook timeout must be in (0, 300] seconds")
        self.timeout = float(timeout)

        # Never accept a token via configured headers: strip any Authorization header
        # (case-insensitive). Log only that it was rejected - never the headers object.
        removed = [k for k in list(self.headers.keys()) if k.lower() == "authorization"]
        for k in removed:
            del self.headers[k]
        if removed:
            self.logger.warning(
                "Ignoring configured 'Authorization' header; the Bearer token must be provided "
                "via the WEBHOOK_AUTH_TOKEN environment variable or Docker secret")

        # Resolve auth configuration (token value is never logged)
        self._auth_token = _load_auth_token()
        self._auth_required = auth_required if auth_required is not None else _env_bool("WEBHOOK_AUTH_REQUIRED", True)

        # Destination mode: WEBHOOK_BASE_URL (deployment-level, single source of
        # truth) overrides the host portion of any legacy per-destination URL.
        self._base_url = load_webhook_base_url()
        if self._base_url:
            parsed = urlparse(self._base_url)
            self.logger.info(
                f"webhook_mode=base_url webhook_scheme={parsed.scheme} "
                f"webhook_host={parsed.hostname} "
                f"webhook_port={parsed.port or (443 if parsed.scheme == 'https' else 80)} "
                f"path={_WEBHOOK_PATH_PREFIX}{{pipeline_id}}")
            if url:
                self.logger.info(
                    "Legacy per-destination webhook URL is ignored while WEBHOOK_BASE_URL is set")
        else:
            if url:
                self.logger.warning(
                    "webhook_mode=legacy - per-destination webhook URLs are DEPRECATED; "
                    "set WEBHOOK_BASE_URL (e.g. http://<receiver-host>:<port>) so all "
                    "pipelines share one deployment-level destination")
            else:
                self.logger.error(
                    "Webhook destination has no URL: set WEBHOOK_BASE_URL or a legacy url")

        self.is_configured = bool(self._base_url or url)
        self.logger.info(
            f"Webhook configured (auth_required={self._auth_required}, "
            f"token={'set' if self._auth_token else 'missing'})")

    def _publish(self, data: Dict[str, Any]) -> DeliveryResult:
        """One webhook POST with Bearer-token authentication.

        Returns a DeliveryResult (see the classification matrix above). This
        method never mutates lifecycle state - enabling/disabling and all
        counters belong to the base class's _account(), which consumes the
        returned verdict exactly once per attempt.
        """
        try:
            import requests

            # Fail-closed / disabled guard: once disabled, no NEW HTTP request
            # starts (an attempt already in flight may finish - by design).
            if not self.enabled:
                return DeliveryResult(
                    success=False, outcome="CONFIGURATION", retryable=False,
                    error="destination disabled")

            token = self._auth_token

            # Missing required auth is non-retryable: refuse to send (fail-closed)
            # and report destination-fatal so _account disables it.
            if self._auth_required and not token:
                self.logger.error(
                    "[WEBHOOK] Authentication required but no token configured "
                    "(set WEBHOOK_AUTH_TOKEN); refusing to send (fail-closed)")
                return DeliveryResult(
                    success=False, outcome="AUTH_FAILED", retryable=False,
                    disable_destination=True,
                    error="auth required but no token configured")

            # Add timestamp
            data["timestamp"] = datetime.utcnow().isoformat()

            if self._base_url:
                # Primary mode: {WEBHOOK_BASE_URL}/webhook/{pipeline_id}.
                # The configured host is used EXACTLY (no rewriting); the pipeline id
                # is validated + URL-encoded so it cannot manipulate the destination.
                pipeline_id = data.get('pipeline_id') or self.context_variables.get('pipeline_id')
                resolved_url = build_webhook_url(self._base_url, pipeline_id)
                if resolved_url is None:
                    self.logger.error(
                        f"[WEBHOOK] Cannot build URL: invalid pipeline_id "
                        f"{str(pipeline_id)[:64]!r} - dropping delivery")
                    return DeliveryResult(
                        success=False, outcome="INVALID_PAYLOAD", retryable=False,
                        terminal_delivery=True, error="invalid pipeline_id for URL")
            else:
                # Legacy mode (deprecated): per-destination URL template.
                additional_vars = {}
                if 'pipeline_id' in data:
                    additional_vars['pipeline_id'] = data['pipeline_id']
                if 'model_name' in data:
                    additional_vars['model_name'] = data['model_name']

                # Detect protocol and set appropriate port (80 for HTTP, 443 for HTTPS)
                url_template = self.url_template or ''
                if url_template.startswith('https://'):
                    additional_vars['port'] = '443'
                    additional_vars['api_port'] = '443'
                elif url_template.startswith('http://'):
                    additional_vars['port'] = '80'
                    additional_vars['api_port'] = '80'
                # If no protocol specified, keep the original port from context

                resolved_url = self.substitute_variables(url_template, additional_vars)

            # Build per-request headers. The token is attached ONLY here and is never
            # stored on self.headers, placed in the URL/query, or serialized into the body.
            req_headers = dict(self.headers)
            if token:
                req_headers["Authorization"] = f"Bearer {token}"

            # Insecure-HTTP control is warn-only (do not block). Warn when a token would
            # be sent over non-HTTPS, except to localhost / 127.0.0.1.
            if token:
                parsed = urlparse(resolved_url)
                if parsed.scheme == "http" and (parsed.hostname or "").lower() not in _LOCAL_HOSTS:
                    self.logger.warning(
                        f"[WEBHOOK] Sending bearer token over non-HTTPS to host '{parsed.hostname}'; "
                        f"use HTTPS in production")

            # Any header logging is redacted so the token is never exposed
            self.logger.debug(f"[WEBHOOK] POST {resolved_url} headers={_redact_headers(req_headers)}")

            parsed = urlparse(resolved_url)
            # Structured, token-free log context: destination host/port, webhook
            # path, pipeline id. Payload contents are never logged.
            log_ctx = (f"pipeline_id={data.get('pipeline_id')} scheme={parsed.scheme} "
                       f"host={parsed.hostname} "
                       f"port={parsed.port or (443 if parsed.scheme == 'https' else 80)} "
                       f"path={parsed.path}")
            started = time.perf_counter()
            try:
                session = getattr(self._sessions, 'session', None)
                if session is None:
                    session = requests.Session()
                    self._sessions.session = session
                    with self._session_lock:
                        self._open_sessions.append(session)
                response = session.post(
                    resolved_url,
                    json=data,
                    headers=req_headers,
                    # Separate connect/read timeouts: fail fast on unreachable hosts,
                    # allow the configured read timeout for slow receivers.
                    timeout=(_CONNECT_TIMEOUT_SECONDS, self.timeout)
                )
            except requests.exceptions.RequestException as e:
                # Specific-first classification (see _classify_transport_exception):
                # CONNECT_TIMEOUT / READ_TIMEOUT / TLS_ERROR / DNS_ERROR /
                # CONNECTION_REFUSED, else CONNECTION_ERROR - never a guess.
                duration_ms = (time.perf_counter() - started) * 1000
                result = _classify_transport_exception(e)
                self.logger.warning(
                    f"[WEBHOOK] delivery {log_ctx} outcome={result.outcome} "
                    f"exception={e.__class__.__name__} duration_ms={duration_ms:.0f}")
                print(f"[WEBHOOK] ✗ POST {result.outcome} to {parsed.hostname}: {e.__class__.__name__}")
                return result

            duration_ms = (time.perf_counter() - started) * 1000
            status = response.status_code
            result = _classify_status(response)
            delivery_line = (f"[WEBHOOK] delivery {log_ctx} status={status} "
                             f"outcome={result.outcome} duration_ms={duration_ms:.0f}"
                             + (f" retry_after={result.retry_after:.0f}s" if result.retry_after else ""))

            if result.success:
                self.logger.info(delivery_line)
                print(f"[WEBHOOK] ✓ POST sent successfully to {resolved_url} (status: {status})")
            elif result.disable_destination:
                # Wrong for EVERY delivery (auth / route). Never log the credential.
                self.logger.error(
                    f"{delivery_line} - destination-level failure; verify "
                    f"{'WEBHOOK_AUTH_TOKEN' if result.outcome == 'AUTH_FAILED' else 'WEBHOOK_BASE_URL and the receiver route'}")
                print(f"[WEBHOOK] ✗ {result.outcome} from {resolved_url} - destination will be disabled")
            elif result.terminal_delivery:
                # This payload only - the destination stays enabled.
                self.logger.error(f"{delivery_line} - dropping this delivery (payload rejected)")
                print(f"[WEBHOOK] ✗ {result.outcome} from {resolved_url} - delivery dropped, destination stays enabled")
            elif not result.count_toward_destination_failure:
                # Backpressure: reachable but busy. Retry, never disable.
                self.logger.info(f"{delivery_line} - receiver busy, will retry")
                print(f"[WEBHOOK] ↻ {result.outcome} from {resolved_url} - busy, retrying")
            else:
                self.logger.warning(delivery_line)
                print(f"[WEBHOOK] ✗ POST failed to {resolved_url} (status: {status})")
            return result

        except ImportError:
            # This is a configuration issue, log it once
            self.logger.error("requests package not installed. Install with: pip install requests")
            return DeliveryResult(
                success=False, outcome="CONFIGURATION", retryable=False,
                count_toward_destination_failure=True,
                error="requests package not installed")
        except Exception as e:
            # Unexpected internal error (never contains the token - it lives only in
            # the request headers). Remains retryable via the base-class lifecycle.
            self.logger.error(f"[WEBHOOK] internal error: {e.__class__.__name__}: {e}")
            print(f"[WEBHOOK] ✗ POST error: {e.__class__.__name__}")
            return DeliveryResult(
                success=False, outcome="CONNECTION_ERROR", retryable=True,
                count_toward_destination_failure=True,
                error=f"internal error: {e.__class__.__name__}")

    def close(self) -> None:
        """Close the webhook connection"""
        self.stop_queue()
        with self._session_lock:
            for session in self._open_sessions:
                session.close()
            self._open_sessions.clear()
        eff = self.effective_destination()
        self.logger.info(f"Webhook connection closed: mode={eff['mode']} url={eff['url']}")
