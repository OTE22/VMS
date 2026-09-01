import json
import time
import logging
import socket
import threading
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union
from datetime import datetime


@dataclass(frozen=True)
class DeliveryResult:
    """Structured verdict of exactly ONE delivery attempt.

    The verdict travels with the attempt - it is never stored on the destination
    object, because the synchronous path (publish_once) and the queue worker
    (_drain_queue) can be in flight against the same destination concurrently,
    and shared instance state would let one attempt overwrite another's verdict.

    `retryable` and `count_toward_destination_failure` are separate on purpose:
    "worth retrying" and "the destination is unhealthy" are different claims.
    Backpressure (HTTP 429, receiver queue-full 503) is retryable but says
    nothing about destination health - five of those must NOT auto-disable a
    healthy-but-busy receiver, while five refused connections must.
    """
    success: bool
    outcome: str                                   # SUCCESS, DNS_ERROR, ... (webhook taxonomy)
    retryable: bool = False
    terminal_delivery: bool = False                # 400/413/422: this delivery only
    disable_destination: bool = False              # 401/403/404: wrong for EVERY delivery
    count_toward_destination_failure: bool = False # advances max_failures; never inferred
    error: Optional[str] = None                    # safe message: never token/header/payload
    retry_after: Optional[float] = None            # parsed Retry-After (seconds), per attempt

    @staticmethod
    def ok() -> "DeliveryResult":
        return DeliveryResult(success=True, outcome="SUCCESS")

    @staticmethod
    def from_legacy(raw: bool, error: Optional[str] = None) -> "DeliveryResult":
        """Normalize a legacy bool _publish() result.

        A legacy plugin cannot distinguish backpressure from a real outage, so
        False conserves the pre-DeliveryResult behaviour exactly: retryable AND
        health-counting.
        """
        if raw:
            return DeliveryResult.ok()
        return DeliveryResult(
            success=False, outcome="CONNECTION_ERROR", retryable=True,
            count_toward_destination_failure=True,
            error=error or "publish returned False")


class BaseResultDestination(ABC):
    """Base class for all result destinations"""
    
    def __init__(self):
        self.type = self.__class__.__name__
        self.logger = logging.getLogger(self.__class__.__name__)
        self.rate_limit = None
        self.last_publish_time = 0
        self.is_configured = False
        self._id: Optional[str] = None  # Unique identifier for this destination
        self.enabled = True  # Whether this destination is enabled
        # Guards ALL lifecycle state: failure_count, failure_threshold_reached,
        # enabled, success_count_since_failure, frame_count, frame_limit_reached,
        # plus the rate-limit slot. RLock (not Lock) so an accidental future
        # nesting degrades to correctness instead of a silent deadlock.
        self._lock = threading.RLock()
        self.include_image_data = False  # Whether to include image data in the published results
        self.include_result_image = False  # Whether to include result image in the published results
        self.context_variables = {} # Context variables for substitution
        
        # Frame/call limit tracking
        self.max_frames = None  # Maximum number of frames/calls before auto-pause
        self.frame_count = 0  # Current count of published frames
        self.frame_limit_reached = False  # Whether frame limit has been reached (paused state)
        self._pause_warning_logged = False  # Flag to prevent spam logging when paused
        
        # Circuit breaker for automatic disabling on repeated failures
        self.failure_count = 0
        self.max_failures = 5  # Disable after 5 consecutive failures
        self.failure_threshold_reached = False
        self.last_failure_time = 0
        self.success_count_since_failure = 0
        self.last_error = None  # Last error message

        # Outbound queue: rate-limited or failed events are queued and retried
        # instead of being silently dropped (bounded to avoid unbounded memory growth)
        self._send_queue = deque(maxlen=100)
        self._queue_lock = threading.Lock()
        self._queue_event = threading.Event()
        self._queue_worker: Optional[threading.Thread] = None
        self._queue_closing = False
        self.max_retries = 3  # Attempts per event before counting a failure

    @classmethod
    def get_config_schema(cls) -> Dict[str, Any]:
        """
        Get the configuration schema for this destination type.
        This defines what UI fields should be displayed and their validation rules.
        
        Returns:
            Dictionary defining the configuration schema
        """
        return {
            'fields': [
                {
                    'name': 'rate_limit',
                    'label': 'Rate Limit',
                    'type': 'number',
                    'min': 0,
                    'max': 1000,
                    'step': 0.1,
                    'placeholder': 'e.g., 1.0',
                    'description': 'Minimum seconds between messages (0 for unlimited)',
                    'required': False,
                    'default': None,
                    'unit': 'seconds',
                    'col_width': 6  # Display in half width column
                },
                {
                    'name': 'max_frames',
                    'label': 'Max Frames/Calls',
                    'type': 'number',
                    'min': 0,
                    'max': 1000000,
                    'step': 1,
                    'placeholder': 'e.g., 1000',
                    'description': 'Maximum number of frames to publish before auto-disabling (0 or empty for unlimited)',
                    'required': False,
                    'default': None,
                    'unit': 'frames',
                    'col_width': 6  # Display in half width column
                },
                {
                    'name': 'include_image_data',
                    'label': 'Include Image Data',
                    'type': 'checkbox',
                    'description': 'Include image data in published results',
                    'required': False,
                    'default': False,
                    'col_width': 6  # Display in half width column
                },
                {
                    'name': 'include_result_image',
                    'label': 'Include Result Image',
                    'type': 'checkbox',
                    'description': 'Include result image in published results',
                    'required': False,
                    'default': False,
                    'col_width': 6  # Display in half width column
                }
            ]
        }

    def __str__(self) -> str:
        status = "enabled" if self.enabled else "disabled"
        if self.failure_threshold_reached:
            status += " (auto-disabled due to failures)"
        if self.frame_limit_reached:
            status += " (paused: frame limit reached)"
        frame_info = f", frames={self.frame_count}"
        if self.max_frames:
            frame_info += f"/{self.max_frames}"
        return f"BaseResultDestination(type={self.type}, id={self._id}, is_configured={self.is_configured}, status={status}, failures={self.failure_count}{frame_info})"
    
    @property
    def auto_disabled(self) -> bool:
        """Check if this destination was auto-disabled due to failures (not frame limit pause)"""
        return self.failure_threshold_reached
    
    @property
    def is_paused(self) -> bool:
        """Check if this destination is paused due to frame limit"""
        return self.frame_limit_reached

    def _record_failure(self, error_msg: str = "") -> bool:
        """Advance the consecutive-failure counter; auto-disable at max_failures.

        Caller MUST hold self._lock (all callers go through _account, which
        does). Returns True when THIS call crossed the disable threshold - the
        caller emits the warning after releasing the lock, so a slow log
        handler can never extend the critical section.
        """
        self.failure_count += 1
        self.success_count_since_failure = 0
        self.last_failure_time = time.time()

        # Ensure types are integers (defensive programming)
        if not isinstance(self.failure_count, int):
            self.failure_count = int(self.failure_count) if str(self.failure_count).isdigit() else 0
        if not isinstance(self.max_failures, int):
            self.max_failures = int(self.max_failures) if str(self.max_failures).isdigit() else 5

        if self.failure_count >= self.max_failures and not self.failure_threshold_reached:
            self.failure_threshold_reached = True
            self.enabled = False
            return True
        if self.failure_count < self.max_failures:
            self.logger.debug(f"Failure {self.failure_count}/{self.max_failures}: {error_msg}")
        return False

    def _record_success(self) -> None:
        """Reset the CONSECUTIVE failure counter. Caller must hold self._lock.

        One success resets the count - that is what "5 consecutive failures"
        has always meant here (the earlier require-3-successes variant made
        4 failures + 1 success + 1 failure count as 5 in a row, which it isn't).
        An auto-disabled destination stays disabled; re-enabling is a deliberate
        operator action (reset_failure_count), never a side effect.
        """
        self.success_count_since_failure += 1
        if self.failure_count:
            self.failure_count = 0

    def _account(self, result: DeliveryResult) -> None:
        """SINGLE owner of lifecycle accounting: exactly one call per attempt,
        made by whichever path performed it (publish_once / publish /
        _drain_queue). _try_send never accounts - it only reports.

        one attempt = one DeliveryResult = one lifecycle decision:
          success                                  -> _record_success()
          count_toward_destination_failure = true  -> _record_failure()
          disable_destination = true               -> disable (401/403/404 class)
          terminal_delivery = true                 -> nothing: this delivery only
          retryable backpressure (count=false)     -> nothing: busy is not broken

        All state moves under self._lock; log lines are emitted after release.
        """
        disabled_now = False
        paused_now = False
        with self._lock:
            if result.success:
                self._record_success()
                self.frame_count += 1
                if (self.max_frames is not None and self.frame_count >= self.max_frames
                        and not self.frame_limit_reached):
                    self.frame_limit_reached = True
                    if not self._pause_warning_logged:
                        self._pause_warning_logged = True
                        paused_now = True
            elif result.disable_destination:
                if self.enabled:
                    self.enabled = False
                    disabled_now = True
            elif result.count_toward_destination_failure:
                disabled_now = self._record_failure(result.error or result.outcome)
            # terminal_delivery / backpressure: no counter movement by design

        if disabled_now and result.disable_destination:
            self.logger.error(
                f"Disabling destination: {result.outcome} - {result.error or 'configuration error'}. "
                f"Fix the configuration, then re-enable manually or via API.")
        elif disabled_now:
            self.logger.warning(
                f"Auto-disabling destination after {self.max_failures} consecutive failures. "
                f"Last error: {result.error or result.outcome}. "
                f"Re-enable manually or via API when issue is resolved.")
        if paused_now:
            self.logger.warning(
                f"Frame limit reached ({self.max_frames} frames). Destination paused. "
                f"Toggle the destination off/on in the UI to reset and continue.")

    def reset_failure_count(self) -> None:
        """Manually reset failure count and re-enable if auto-disabled"""
        with self._lock:
            self.failure_count = 0
            self.success_count_since_failure = 0
            self.failure_threshold_reached = False
            if not self.enabled and self.is_configured:
                self.enabled = True
                self.logger.info("Destination manually re-enabled and failure count reset")

    def reset_frame_count(self) -> None:
        """Manually reset frame count and unpause if paused due to frame limit"""
        with self._lock:
            self.frame_count = 0
            self.frame_limit_reached = False
            self._pause_warning_logged = False
        self.logger.info("Destination frame count reset and unpaused")
    
    def set_max_frames(self, max_frames: Optional[int]) -> None:
        """Set maximum number of frames before auto-disable (None or 0 for unlimited)"""
        if max_frames is not None and max_frames > 0:
            self.max_frames = int(max_frames)
        else:
            self.max_frames = None

    def set_context_variables(self, **kwargs) -> None:
        """Set context variables for string substitution"""
        self.context_variables.update(kwargs)
        self.logger.debug(f"Context variables updated: {self.context_variables}")

    def get_available_variables(self, additional_vars: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Get all available variables for substitution (useful for debugging)"""
        now = datetime.utcnow()
        variables = {
            # Time-based variables (always available)
            'timestamp': now.isoformat(),
            'date': now.strftime('%Y-%m-%d'),
            'time': now.strftime('%H:%M:%S'),
            'unix_time': str(int(time.time())),
            'hostname': socket.gethostname(),
            
            # Default values for common variables
            'node_id': 'unknown-node',
            'node_name': 'InferNode',
            'pipeline_id': 'unknown-pipeline',
            'model_name': 'unknown-model',
            'api_port': '80',  # Default to port 80 for HTTP (nginx proxy)
            'port': '80'  # Default to port 80 for HTTP (nginx proxy)
        }
        
        # Override defaults with context variables
        if self.context_variables:
            variables.update(self.context_variables)
        
        # Add any additional variables passed to this call
        if additional_vars:
            variables.update(additional_vars)
            
        return variables

    def substitute_variables(self, text: str, additional_vars: Optional[Dict[str, Any]] = None) -> str:
        """
        Substitute variables in text using format like {variable_name}
        
        Supported variables:
        - {node_id}: Node identifier
        - {node_name}: Node name
        - {hostname}: System hostname
        - {port} or {api_port}: API server port (dynamically detected)
        - {pipeline_id}: Pipeline identifier (when available)
        - {model_name}: Model name (when available)
        - {timestamp}: Current timestamp (ISO format)
        - {date}: Current date (YYYY-MM-DD)
        - {time}: Current time (HH:MM:SS)
        - {unix_time}: Unix timestamp
        - Any custom variables set via set_context_variables()
        """
        if not text:
            return text
            
        # Build substitution variables with defaults
        now = datetime.utcnow()
        variables = {
            # Time-based variables (always available)
            'timestamp': now.isoformat(),
            'date': now.strftime('%Y-%m-%d'),
            'time': now.strftime('%H:%M:%S'),
            'unix_time': str(int(time.time())),
            'hostname': socket.gethostname(),
            
            # Default values for common variables
            'node_id': 'unknown-node',
            'node_name': 'InferNode',
            'pipeline_id': 'unknown-pipeline',
            'model_name': 'unknown-model',
            'api_port': '80',  # Default to port 80 for HTTP (nginx proxy)
            'port': '80'  # Default to port 80 for HTTP (nginx proxy)
        }
        
        # Override defaults with context variables
        if self.context_variables:
            variables.update(self.context_variables)
        
        # Add any additional variables passed to this call (highest priority)
        # Note: WebhookDestination will override port based on protocol (80 for HTTP, 443 for HTTPS)
        if additional_vars:
            variables.update(additional_vars)
        
        try:
            # Use str.format() for variable substitution
            return text.format(**variables)
        except KeyError as e:
            self.logger.warning(f"Variable substitution failed - unknown variable: {e}")
            return text
        except Exception as e:
            self.logger.warning(f"Variable substitution failed: {str(e)}")
            return text

    def set_rate_limit(self, rate_limit: Optional[float]) -> None:
        """Set rate limit as minimum seconds between publishes (0 or None for unlimited)"""
        self.rate_limit = rate_limit
    
    def can_publish(self) -> bool:
        """Check if enough time has passed since last publish (based on rate_limit in seconds) and if destination is enabled"""
        try:
            if not self.enabled:
                return False
            
            # Check if paused due to frame limit
            if self.frame_limit_reached:
                return False
                
            if self.rate_limit is None:
                return True
            
            # Ensure types are numeric (defensive programming)
            current_time = time.time()
            rate_limit = float(self.rate_limit) if self.rate_limit is not None else 0
            last_publish_time = float(self.last_publish_time) if self.last_publish_time is not None else 0
            
            return (current_time - last_publish_time) >= rate_limit
        except (ValueError, TypeError) as e:
            self.logger.warning(f"Type error in can_publish comparison: {e}, defaulting to allow publish")
            return True
    
    def publish_once(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Single synchronous publish attempt with a structured result.

        Unlike publish(), this does NOT enqueue and does NOT retry - it makes
        exactly one delivery attempt and reports what happened, so the caller
        (e.g. the pipeline's background publisher worker) can decide whether to
        wait (rate limited) or retry (failed). The rate limit is CHECKED here,
        not waited on: if within the window the event is not sent and status is
        'rate_limited'.

        Returns: {"status": "success"|"failed"|"permanent_failure"|"rate_limited"|
                            "disabled"|"paused"|"unconfigured",
                  "error": str|None,
                  "outcome": str|None,        # DeliveryResult taxonomy code
                  "retry_after": float|None}  # backpressure hint for the caller's backoff

        "permanent_failure" = THIS delivery can never succeed (400/413/422):
        the caller must not retry it, but the destination itself stays enabled.
        "failed" may be worth retrying (see "outcome" / destination.enabled).
        """
        if not self.enabled:
            return {"status": "disabled", "error": None, "outcome": None, "retry_after": None}

        if not self.is_configured:
            self._account(DeliveryResult(
                success=False, outcome="CONFIGURATION", retryable=False,
                count_toward_destination_failure=True,
                error="Destination not configured"))
            return {"status": "unconfigured", "error": "Destination not configured",
                    "outcome": "CONFIGURATION", "retry_after": None}

        # Frame-limit pause and rate-limit are checked under the lock; the actual
        # network send happens outside it.
        with self._lock:
            if self.frame_limit_reached:
                return {"status": "paused", "error": None, "outcome": None, "retry_after": None}

            if self.rate_limit is not None:
                current_time = time.time()
                rate_limit = float(self.rate_limit)
                last_publish_time = float(self.last_publish_time) if self.last_publish_time is not None else 0
                if (current_time - last_publish_time) < rate_limit:
                    return {"status": "rate_limited", "error": None, "outcome": None, "retry_after": None}

            # Reserve the slot before sending to avoid a concurrent double-send
            self.last_publish_time = time.time()

        result = self._try_send(data)
        self._account(result)  # exactly once for this attempt

        if result.success:
            return {"status": "success", "error": None,
                    "outcome": result.outcome, "retry_after": None}

        # Failed - free the slot so a retry isn't blocked by our own reservation
        with self._lock:
            self.last_publish_time = 0

        status = "permanent_failure" if result.terminal_delivery else "failed"
        return {"status": status,
                "error": result.error or "publish returned failure",
                "outcome": result.outcome,
                "retry_after": result.retry_after}

    def publish(self, data: Dict[str, Any]) -> bool:
        """Publish data to destination with rate limiting and enabled check.

        Rate-limited or failed events are queued and retried by a background
        worker instead of being dropped, so every accepted event is eventually
        delivered (up to max_retries attempts each). Returns True when the
        event was sent or accepted into the outbound queue.
        """
        if not self.enabled:
            if not self.failure_threshold_reached:
                self.logger.debug("Destination disabled, skipping publish")
            # Don't log if auto-disabled to avoid spam
            return False

        if not self.is_configured:
            self._account(DeliveryResult(
                success=False, outcome="CONFIGURATION", retryable=False,
                count_toward_destination_failure=True,
                error="Destination not configured"))
            return False

        # Check if paused due to frame limit
        with self._lock:
            if self.frame_limit_reached:
                # Silently skip - user can unpause by toggling the destination
                # Don't log to avoid spam (warning already logged when paused)
                return False

        # Preserve ordering: if events are already queued, queue behind them
        with self._queue_lock:
            queue_busy = bool(self._send_queue)

        # Thread-safe rate limit check - CRITICAL SECTION
        rate_blocked = False
        with self._lock:
            if self.rate_limit is not None:
                current_time = time.time()
                rate_limit = float(self.rate_limit)
                last_publish_time = float(self.last_publish_time) if self.last_publish_time is not None else 0

                if (current_time - last_publish_time) < rate_limit:
                    rate_blocked = True

            if not rate_blocked and not queue_busy:
                # Update last_publish_time BEFORE publishing to prevent race condition
                self.last_publish_time = time.time()

        if rate_blocked or queue_busy:
            # Queue instead of dropping - background worker delivers it
            self.logger.debug("Rate limit active or queue busy - queueing event for delivery")
            self._enqueue(data)
            return True

        # Direct path: attempt the publish now (outside the lock)
        result = self._try_send(data)
        self._account(result)  # exactly once for this attempt

        if result.success:
            return True

        # Failed - revert last_publish_time so the retry isn't blocked by our
        # own slot reservation
        with self._lock:
            self.last_publish_time = 0

        if result.terminal_delivery or result.disable_destination or not result.retryable:
            # Retrying cannot help: a payload-terminal (400/413/422) or
            # destination-fatal (401/403/404) verdict must not enter the queue,
            # or the queue worker would loop on it.
            self.logger.warning(
                f"Dropping event after non-retryable failure "
                f"outcome={result.outcome}: {result.error or 'no detail'}")
            return False

        self.logger.debug("Publish attempt failed - queueing event for retry")
        self._enqueue(data, attempts=1)
        return True

    def _try_send(self, data: Dict[str, Any]) -> DeliveryResult:
        """Exactly ONE delivery attempt, as a pure function of the transport.

        Performs the attempt and returns its DeliveryResult - ALWAYS a complete
        DeliveryResult, never a bool (legacy bool _publish() results are
        normalized here, so no caller above this line reasons about bools).

        Deliberately does NOT touch failure_count / success / frame counters:
        _account() is the single owner of lifecycle accounting, called exactly
        once per attempt by whichever path made it. Never raises.
        """
        try:
            raw = self._publish(data)
        except Exception as e:
            self.last_error = f"Failed to publish: {str(e)}"
            self.logger.debug(self.last_error)
            return DeliveryResult(
                success=False, outcome="CONNECTION_ERROR", retryable=True,
                count_toward_destination_failure=True, error=self.last_error)

        if isinstance(raw, DeliveryResult):
            if not raw.success:
                # Kept for display/back-compat only - control flow reads the
                # DeliveryResult, never this attribute.
                self.last_error = raw.error or f"publish failed ({raw.outcome})"
            return raw

        result = DeliveryResult.from_legacy(bool(raw))
        if not result.success:
            self.last_error = result.error
        return result

    def _enqueue(self, data: Dict[str, Any], attempts: int = 0) -> None:
        """Add an event to the outbound queue and ensure the worker is running"""
        with self._queue_lock:
            if len(self._send_queue) == self._send_queue.maxlen:
                self.logger.warning("Outbound queue full - dropping oldest queued event")
            self._send_queue.append((data, attempts))
        self._queue_event.set()
        self._ensure_queue_worker()

    def _ensure_queue_worker(self) -> None:
        """Start the queue drain worker if it isn't running"""
        with self._queue_lock:
            if self._queue_worker is None or not self._queue_worker.is_alive():
                self._queue_closing = False
                self._queue_worker = threading.Thread(
                    target=self._drain_queue,
                    daemon=True,
                    name=f"{self.type}-send-queue"
                )
                self._queue_worker.start()

    def _drain_queue(self) -> None:
        """Background worker: deliver queued events respecting rate_limit, with retries"""
        idle_timeout = 10.0  # Exit worker after this long with an empty queue
        while not self._queue_closing:
            with self._queue_lock:
                item = self._send_queue.popleft() if self._send_queue else None

            if item is None:
                self._queue_event.clear()
                if not self._queue_event.wait(timeout=idle_timeout):
                    with self._queue_lock:
                        if not self._send_queue:
                            return  # Idle - worker restarts on next enqueue
                continue

            # Drop queued events if destination was disabled/paused meanwhile
            if not self.enabled or self.frame_limit_reached:
                with self._queue_lock:
                    self._send_queue.clear()
                continue

            data, attempts = item

            # Respect rate limit before sending
            if self.rate_limit:
                while not self._queue_closing:
                    with self._lock:
                        wait = float(self.rate_limit) - (time.time() - float(self.last_publish_time or 0))
                    if wait <= 0:
                        break
                    time.sleep(min(wait, 0.5))

            if self._queue_closing:
                return

            with self._lock:
                self.last_publish_time = time.time()

            result = self._try_send(data)
            self._account(result)  # exactly once per attempt - same rule as the sync path

            if result.success:
                continue

            if result.terminal_delivery or result.disable_destination or not result.retryable:
                # Same semantics as the synchronous path (parity by design):
                # payload-terminal or destination-fatal events are dropped, not
                # re-queued - retrying the identical payload cannot succeed.
                self.logger.warning(
                    f"Dropping queued event after non-retryable failure "
                    f"outcome={result.outcome}: {result.error or 'no detail'}")
                continue

            attempts += 1
            if attempts >= self.max_retries:
                # Every attempt already fed the lifecycle via _account - a
                # second _record_failure here would double-count this attempt.
                self.logger.warning(
                    f"Publish failed after {attempts} attempts (queued event dropped)")
            else:
                # Backoff before retrying: 1s, 2s, 4s - with any Retry-After
                # from a backpressure response as the floor (already clamped
                # at parse time).
                delay = min(2 ** (attempts - 1), 4)
                if result.retry_after:
                    delay = max(delay, result.retry_after)
                time.sleep(delay)
                with self._queue_lock:
                    self._send_queue.appendleft((data, attempts))

    def stop_queue(self) -> None:
        """Stop the outbound queue worker and discard queued events (call from close())"""
        self._queue_closing = True
        with self._queue_lock:
            self._send_queue.clear()
        self._queue_event.set()
    
    def configure_common(self, rate_limit: Optional[float] = None, 
                        max_frames: Optional[int] = None,
                        include_image_data: bool = False,
                        include_result_image: bool = False, **kwargs) -> None:
        """
        Configure common destination parameters. 
        Call this from subclass configure() methods to handle common parameters.
        
        Args:
            rate_limit: Minimum seconds between publishes (None or 0 for unlimited)
            max_frames: Maximum frames before auto-pause (None or 0 for unlimited)
            include_image_data: Whether to include image data in published results
            include_result_image: Whether to include result image in published results
            **kwargs: Additional subclass-specific parameters (ignored here)
        """
        self.include_image_data = include_image_data
        self.include_result_image = include_result_image
        self.set_rate_limit(rate_limit)
        self.set_max_frames(max_frames)
    
    @abstractmethod
    def configure(self, **kwargs) -> None:
        """Configure the destination"""
        pass
    
    @abstractmethod
    def _publish(self, data: Dict[str, Any]) -> bool:
        """Actual publish implementation"""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close the destination"""
        pass