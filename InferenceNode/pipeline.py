from __future__ import annotations
import sys
import os
import threading
import queue
import uuid
import time
import json
import cv2
from typing import Dict, Any, Optional
from collections import defaultdict
import logging
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from frame_source import FrameSourceFactory
from InferenceEngine import InferenceEngineFactory
from frame_source.video_capture_base import VideoCaptureBase
from InferenceEngine.engines.base_engine import BaseInferenceEngine
from ResultPublisher import ResultPublisher
from ResultPublisher.result_destinations import MQTTDestination

# Setup logging to both file and console
def setup_pipeline_logging():
    """Setup logging to pipeline.log and console"""
    logger = logging.getLogger('InferencePipeline')
    logger.setLevel(logging.DEBUG)

    # Remove existing handlers to avoid duplicates
    logger.handlers.clear()

    # File handler
    file_handler = logging.FileHandler('pipeline.log')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - [%(levelname)s] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s')
    console_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

class InferencePipeline:

    def __init__(self) -> None:
        self.id = str(uuid.uuid4())  # Overwritten with the stable builder pipeline_id by PipelineManager
        self.pipeline_name = ""  # Human-readable builder name, set by PipelineManager
        self.logger = logging.getLogger(f'InferencePipeline.{self.id[:8]}')
        self.nodes = []
        self.source : VideoCaptureBase
        self.inference_engine : BaseInferenceEngine
        self.result_publisher : ResultPublisher
        self._stop_requested = False  # Flag to control pipeline execution
        self._latest_frame = None  # Store latest processed frame for streaming
        self._inference_enabled = True  # Flag to enable/disable inference processing

        self._frame_lock = threading.Lock()  # Thread-safe access to latest frame

        self._frame_counter = 0  # Count processed frames
        self._inference_counter = 0  # Count inferences performed
        self._start_time = 0  # Record the start time

        # --- Step 0 instrumentation: PRIMITIVE values only. No statistics are computed
        # here; scripts/benchmark.py collects the series and derives p50/p95/p99.
        self._failed_read_count = 0      # times source.read() returned no frame
        self._last_capture_wall = 0.0    # time.time() when the last frame was read
        self._last_read_wait_ms = 0.0    # how long the last source.read() blocked
        
        # FPS calculation over rolling 10-second window
        self._frame_timestamps = []  # Store timestamps of processed frames
        self._fps_window_seconds = 10  # Calculate FPS over last 10 seconds
        
        # Latency tracking over rolling window
        self._inference_latencies = []  # Store inference latencies in milliseconds
        self._latency_window_size = 100  # Keep last 100 inference times for rolling average
        
        # Frame source configuration for auto-delete functionality
        self._frame_source_config = None
        self._current_image_path = None  # Track current image path for deletion
        
        # Thumbnail support
        self._thumbnail_captured = False  # Flag to track if thumbnail has been captured
        self._thumbnail_path = None  # staging path; the manager registers/promotes it
        self._on_thumbnail_captured = None  # callback(staged_path) set by PipelineManager
        
        # Pipeline state tracking
        self._is_initialized = False  # True when configured and model is loaded
        self._is_running = False  # True when pipeline thread is actively running
        self._error_state = None  # None if no error, otherwise contains error message
        self._is_streaming = False  # True when streaming is active
        # ALLOWED_LIST: classes that are tracked and published
        self.ALLOWED_LIST = {
            "person", "bicycle", "car", "motorcycle", "bus", "train", "truck", "face"
        }

        # Detection sending thresholds - all overridable via configure(detection_config={...})
        self.MIN_CONFIDENCE = 0.4  # Minimum confidence for non-person classes
        self.MIN_CONFIDENCE_FOR_PERSON = 0.5  # Minimum confidence for persons
        self.SEND_BUFFER_SECONDS = 1.0  # Send once the track's confidence stopped improving for this long
        self.MAX_COLLECT_SECONDS = 3.0  # Hard cap: send after this long even if confidence keeps improving
        self.IMMEDIATE_SEND_CONFIDENCE = 0.90  # Send right away once a track reaches this confidence
        self.TRACK_TTL_SECONDS = 120.0  # Cooldown (seconds) after a successful send before the same track can be sent again
        self.TRACK_LOST_TIMEOUT_SECONDS = 2.0  # Publish a track's best if it hasn't been seen for this long (person left frame)
        self.DEDUP_IOU_THRESHOLD = 0.4
        self.DEDUP_TTL_SECONDS = 4.0
        self._last_cleanup_frame = 0  # Track last cleanup frame for optimization
        self._cleanup_interval = 100  # Run cleanup every 100 frames

        # Background publisher (queue + retry) parameters - overridable via detection_config
        self.PUBLISH_QUEUE_SIZE = 1000
        self.PUBLISH_MAX_RETRIES = 5
        self.PUBLISH_RETRY_DELAY_SECONDS = 1.0
        self.PUBLISH_RETRY_BACKOFF = 2.0
        self.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS = 10.0
        self.FAILED_BACKOFF_SECONDS = 60.0  # After exhausting retries, wait this long before a track is eligible again
        self.REUSE_IOU_THRESHOLD = 0.05  # Below this IoU vs the last sent bbox => treat a reused track_id as a new person
        self.REUSE_MIN_GAP_SECONDS = 1.0  # Only apply the reuse heuristic after at least this long since the last send

        # --- Tracking state (guarded by _tracking_lock) ---
        # track_key -> {best_det, best_frame, first_seen, last_seen, last_improved}
        self._track_best = {}
        # track_key -> {sent_at, bbox}  (successful-send cooldown record + reuse check)
        self._track_last_sent = {}
        # track_keys currently queued/publishing/retrying (prevents duplicate enqueue)
        self._pending_track_keys = set()
        # track_key -> earliest_retry_ts  (re-eligible-after-backoff on permanent failure)
        self._failed_backoff = {}
        # IOU fallback history (successfully published detections only)
        self._sent_objects = defaultdict(list)
        # temporary keys for no-track detections currently in flight
        self._pending_iou_keys = set()
        self._tracking_lock = threading.Lock()

        # --- Counters (guarded by _counter_lock) - reflect CONFIRMED deliveries ---
        self._counter_lock = threading.Lock()
        self._persons_sent = 0  # Successful person deliveries
        self._sent_person_track_ids = set()  # Distinct person track_ids successfully delivered
        self._publish_attempts = 0
        self._publish_successes = 0
        self._publish_failures = 0
        self._publish_retries = 0
        self._publish_rate_limited = 0
        self._queued_events = 0
        self._dropped_events = 0

        # --- Background publisher worker ---
        self._publish_queue: "queue.Queue" = queue.Queue(maxsize=self.PUBLISH_QUEUE_SIZE)
        self._publisher_stop_event = threading.Event()
        self._publisher_thread: Optional[threading.Thread] = None
        self._draining = False  # True during graceful shutdown while the queue drains

        self.logger.info(f"Pipeline initialized with ID: {self.id}")


    def _init_dedup(self):
        """Reset all tracking/counter state for a fresh run."""
        with self._tracking_lock:
            self._sent_objects = defaultdict(list)
            self._track_best = {}
            self._track_last_sent = {}
            self._pending_track_keys = set()
            self._failed_backoff = {}
            self._pending_iou_keys = set()
        with self._counter_lock:
            self._persons_sent = 0
            self._sent_person_track_ids = set()
            self._publish_attempts = 0
            self._publish_successes = 0
            self._publish_failures = 0
            self._publish_retries = 0
            self._publish_rate_limited = 0
            self._queued_events = 0
            self._dropped_events = 0
        self.logger.debug("Tracking and counter structures initialized")

    # ------------------------------------------------------------------ #
    # Tracking helpers
    # ------------------------------------------------------------------ #
    def _iou(self, b1, b2):
        try:
            x1 = max(b1[0], b2[0])
            y1 = max(b1[1], b2[1])
            x2 = min(b1[2], b2[2])
            y2 = min(b1[3], b2[3])

            inter = max(0, x2 - x1) * max(0, y2 - y1)
            a1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
            a2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
            union = a1 + a2 - inter
            return inter / union if union else 0
        except (IndexError, TypeError):
            return 0

    def _make_track_key(self, det):
        """Normalized key that avoids collisions across classes / tracker restarts.
        The original track_id is preserved separately in the payload."""
        return (str(det.get('class_name', 'unknown')).lower(), str(det.get('track_id')))

    def _update_track_candidate(self, det, frame, now):
        """Create/update the highest-confidence candidate for a tracked detection.
        frame.copy() happens only when the best confidence improves (memory-friendly).
        Applies the tracker-ID-reuse heuristic against the last successfully sent bbox."""
        track_key = self._make_track_key(det)
        confidence = det.get('confidence', 0)
        class_name = det.get('class_name', 'unknown')
        bbox = det.get('bbox', [])

        with self._tracking_lock:
            # Tracker-ID reuse: if this key was sent before and the new bbox is
            # spatially unrelated to the sent one after a short gap, treat it as a
            # NEW occupant of a reused id and clear the cooldown so it can be sent.
            sent_rec = self._track_last_sent.get(track_key)
            if sent_rec is not None:
                gap = now - sent_rec.get('sent_at', 0)
                if gap >= self.REUSE_MIN_GAP_SECONDS and len(bbox) == 4 and len(sent_rec.get('bbox', [])) == 4:
                    if self._iou(bbox, sent_rec['bbox']) < self.REUSE_IOU_THRESHOLD:
                        del self._track_last_sent[track_key]
                        self._failed_backoff.pop(track_key, None)
                        self.logger.info(f"TRACK_REUSED pipeline_id={self.id} track_key={track_key} "
                                         f"gap={gap:.1f}s - treating reused track_id as a new person")
                        sent_rec = None

            # Still within success cooldown -> ignore (already delivered recently)
            if sent_rec is not None and (now - sent_rec.get('sent_at', 0)) < self.TRACK_TTL_SECONDS:
                return

            # Already queued/publishing/retrying, or in post-failure backoff -> don't re-collect
            if track_key in self._pending_track_keys:
                return
            backoff_until = self._failed_backoff.get(track_key)
            if backoff_until is not None and now < backoff_until:
                return

            entry = self._track_best.get(track_key)
            if entry is None:
                self._track_best[track_key] = {
                    'best_det': det,
                    'best_frame': frame.copy() if frame is not None else None,
                    'first_seen': now,
                    'last_seen': now,
                    'last_improved': now,
                }
                self.logger.info(f"TRACK_CREATED pipeline_id={self.id} pipeline_name={self.pipeline_name} "
                                 f"class={class_name} track_id={det.get('track_id')} track_key={track_key} conf={confidence:.3f}")
            else:
                entry['last_seen'] = now
                if confidence > entry['best_det'].get('confidence', 0):
                    entry['best_det'] = det
                    entry['best_frame'] = frame.copy() if frame is not None else None
                    entry['last_improved'] = now
                    self.logger.info(f"TRACK_BEST_UPDATED pipeline_id={self.id} class={class_name} "
                                     f"track_id={det.get('track_id')} track_key={track_key} conf={confidence:.3f}")

    def _track_ready(self, entry, now):
        """A track is ready to publish once it has settled or the person has left."""
        best_conf = entry['best_det'].get('confidence', 0)
        return (best_conf >= self.IMMEDIATE_SEND_CONFIDENCE
                or (now - entry['last_improved']) >= self.SEND_BUFFER_SECONDS
                or (now - entry['first_seen']) >= self.MAX_COLLECT_SECONDS
                or (now - entry['last_seen']) >= self.TRACK_LOST_TIMEOUT_SECONDS)

    def _collect_ready_tracks(self, now):
        """Return publish jobs for all tracks ready to send; marks them pending.
        Runs even when the current frame has zero detections (handles people leaving)."""
        jobs = []
        with self._tracking_lock:
            for track_key in list(self._track_best.keys()):
                entry = self._track_best[track_key]
                if not self._track_ready(entry, now):
                    continue
                # Move out of the candidate map and into pending
                del self._track_best[track_key]
                self._pending_track_keys.add(track_key)
                jobs.append({
                    'track_key': track_key,
                    'det': entry['best_det'],
                    'frame': entry['best_frame'],
                    'first_seen': entry['first_seen'],
                })
        return jobs

    def _register_iou_candidate(self, det, frame, now):
        """No-track detection: return a job if it isn't a duplicate of a recently
        (successfully) published one and isn't already pending. Uses a temporary key."""
        cls = str(det.get('class_name', 'unknown')).lower()
        bbox = det.get('bbox', [])
        with self._tracking_lock:
            # Expire old successfully-published IOU history
            history = [h for h in self._sent_objects[cls] if now - h['ts'] < self.DEDUP_TTL_SECONDS]
            self._sent_objects[cls] = history
            # Skip if overlapping a recently published detection
            for h in history:
                if self._iou(bbox, h['bbox']) > self.DEDUP_IOU_THRESHOLD:
                    return None
            # Skip if an identical no-track detection is already in flight
            for pend_cls, pend_bbox in list(self._pending_iou_keys):
                if pend_cls == cls and self._iou(bbox, pend_bbox) > self.DEDUP_IOU_THRESHOLD:
                    return None
            iou_key = (cls, tuple(round(float(v), 1) for v in bbox) if len(bbox) == 4 else tuple())
            self._pending_iou_keys.add(iou_key)
        return {'track_key': None, 'iou_key': iou_key, 'det': det,
                'frame': frame.copy() if frame is not None else None, 'first_seen': now}

    def _cleanup_tracks(self):
        """Expire stale success-cooldown, candidate, backoff and IOU records.
        Never touches pending/in-flight jobs."""
        now = time.time()
        with self._tracking_lock:
            for k in [k for k, r in self._track_last_sent.items()
                      if now - r.get('sent_at', 0) > self.TRACK_TTL_SECONDS]:
                del self._track_last_sent[k]
            for k in [k for k, t in self._failed_backoff.items() if now >= t]:
                del self._failed_backoff[k]
            # Stale candidates that are not pending (defensive; normally consumed by collect)
            stale_after = self.MAX_COLLECT_SECONDS + self.TRACK_LOST_TIMEOUT_SECONDS + 5.0
            for k in [k for k, e in self._track_best.items()
                      if k not in self._pending_track_keys and now - e['first_seen'] > stale_after]:
                del self._track_best[k]
            for cls in list(self._sent_objects.keys()):
                self._sent_objects[cls] = [h for h in self._sent_objects[cls]
                                           if now - h['ts'] < self.DEDUP_TTL_SECONDS]

    # ------------------------------------------------------------------ #
    # Background publisher worker
    # ------------------------------------------------------------------ #
    def _start_publisher_worker(self):
        self._publisher_stop_event.clear()
        self._publisher_thread = threading.Thread(
            target=self._publisher_worker, name=f"pub-{self.id[:8]}", daemon=True)
        self._publisher_thread.start()

    def _enqueue_publish_job(self, job):
        """Enqueue a job for the background worker. Never silently drops: on a full
        queue we log QUEUE_FULL and, if still full after a brief wait, count it as a
        dropped event and release the pending mark so the track can be re-collected."""
        try:
            self._publish_queue.put_nowait(job)
        except queue.Full:
            self.logger.warning(f"QUEUE_FULL pipeline_id={self.id} queue_size={self._publish_queue.qsize()} "
                                f"track_key={job.get('track_key')} - waiting briefly")
            try:
                self._publish_queue.put(job, timeout=1.0)
            except queue.Full:
                with self._counter_lock:
                    self._dropped_events += 1
                self._release_pending(job)
                self.logger.error(f"QUEUE_FULL pipeline_id={self.id} dropped event track_key={job.get('track_key')} "
                                  f"(queue still full) - will be re-collected")
                return
        with self._counter_lock:
            self._queued_events += 1
        det = job['det']
        self.logger.info(f"PUBLISH_QUEUED pipeline_id={self.id} class={det.get('class_name')} "
                         f"track_id={det.get('track_id')} track_key={job.get('track_key')} "
                         f"conf={det.get('confidence', 0):.3f} queue_size={self._publish_queue.qsize()}")

    def _release_pending(self, job):
        """Remove a job's pending marker so the track/detection can be collected again."""
        with self._tracking_lock:
            if job.get('track_key') is not None:
                self._pending_track_keys.discard(job['track_key'])
            if job.get('iou_key') is not None:
                self._pending_iou_keys.discard(job['iou_key'])

    def _build_payload(self, det, json_results):
        # event_id: minted ONCE per delivery job, here and nowhere else. Every
        # retry of this job re-sends the same id (publish_sync deep-copies per
        # attempt, so nothing downstream can regenerate it), while a re-armed
        # track mints a NEW job -> a new id. The receiver deduplicates on it
        # within its dedup TTL - idempotent deduplication within that window,
        # NOT exactly-once processing.
        return {
            "event_id": uuid.uuid4().hex,
            "node_id": self.id,
            "pipeline_id": self.id,
            "pipeline_name": self.pipeline_name,
            "location_name": self.pipeline_name,  # Camera name shown on dashboard cards
            "results": {
                "task_type": json_results.get("task_type", "detection") if json_results else "detection",
                "num_detections": 1,
                "predictions": [det],
            },
        }

    def _publisher_worker(self):
        """Drain the publish queue, delivering each job with retries + backoff.
        Delivery is confirmed via ResultPublisher.publish_sync() before a track is
        marked sent. Rate-limited destinations cause a wait, not a failure."""
        while not (self._publisher_stop_event.is_set() and self._publish_queue.empty()):
            try:
                job = self._publish_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._deliver_job(job)
            except Exception as e:
                self.logger.error(f"PUBLISH_FAILED pipeline_id={self.id} unexpected worker error: {e}", exc_info=True)
                self._handle_publish_failure(job, str(e))
            finally:
                self._publish_queue.task_done()

    def _deliver_job(self, job):
        det = job['det']
        payload = self._build_payload(det, job.get('json_results'))
        frame = job.get('frame')
        need_image = self.result_publisher.do_any_destinations_need_image()
        need_result_image = self.result_publisher.do_any_destinations_need_result_image()
        img = frame if need_image else None
        result_img = self._latest_frame if need_result_image else None

        delay = self.PUBLISH_RETRY_DELAY_SECONDS
        last_error = None
        attempt = 0
        max_attempts = self.PUBLISH_MAX_RETRIES + 1

        # Keep trying while attempts remain and we're either not shutting down,
        # or we are but still draining the queue on graceful stop.
        while attempt < max_attempts and (not self._publisher_stop_event.is_set() or self._draining):
            attempt += 1
            self.logger.info(f"PUBLISH_ATTEMPT pipeline_id={self.id} class={det.get('class_name')} "
                             f"track_id={det.get('track_id')} attempt={attempt}/{max_attempts}")
            with self._counter_lock:
                self._publish_attempts += 1
            res = self.result_publisher.publish_sync(payload, img, result_img)

            if res.get("success"):
                self._handle_publish_success(job, res)
                return

            # Only rate-limited (nothing failed, nothing succeeded) -> wait, retry same attempt
            if (not res.get("failed_destinations") and not res.get("terminal_destinations")
                    and res.get("rate_limited_destinations")
                    and res.get("attempted", 0) > 0):
                with self._counter_lock:
                    self._publish_rate_limited += 1
                self.logger.info(f"PUBLISH_RATE_LIMITED pipeline_id={self.id} track_id={det.get('track_id')} "
                                 f"destinations={res.get('rate_limited_destinations')} - waiting")
                attempt -= 1  # this doesn't consume a retry
                if self._interruptible_wait(0.25):
                    break
                continue

            last_error = json.dumps(res.get("errors", {})) if res.get("errors") else "no enabled destination accepted"

            # TERMINAL: no retry slot can change these verdicts - stop now, make
            # no further HTTP request, sleep through none of the remaining
            # backoffs, and do NOT re-arm the track (see _handle_publish_failure).
            #   1. every attempted destination rejected the payload (400/413/422)
            #   2. a destination-level disable fired this attempt (401/403/404,
            #      or max_failures just crossed inside the destination lifecycle)
            #   3. nothing was attempted because every destination is already
            #      disabled - the old infinite 60s re-arm loop lived here
            attempted = res.get("attempted", 0)
            failed = res.get("failed_destinations") or []
            terminal = res.get("terminal_destinations") or []
            disabled = res.get("disabled_destinations") or []
            all_terminal = attempted > 0 and terminal and not failed and not res.get("rate_limited_destinations")
            all_disabled_now = attempted > 0 and failed and set(failed) == set(disabled)
            nothing_left = attempted == 0 and res.get("skipped_destinations")

            if all_terminal or all_disabled_now or nothing_left:
                reason = ("payload rejected (terminal for this delivery)" if all_terminal
                          else "destination disabled by lifecycle" if all_disabled_now
                          else "no enabled destination remains")
                self.logger.error(f"PUBLISH_TERMINAL pipeline_id={self.id} track_id={det.get('track_id')} "
                                  f"attempt={attempt} reason={reason} error={last_error}")
                self._handle_publish_failure(job, f"{reason}: {last_error}", terminal=True)
                return

            # A retryable failure: bounded backoff, honouring any backpressure
            # hint (Retry-After, already clamped at parse time) as the floor.
            if attempt < max_attempts:
                with self._counter_lock:
                    self._publish_retries += 1
                wait_s = max(delay, res.get("retry_after") or 0)
                self.logger.warning(f"PUBLISH_RETRY pipeline_id={self.id} track_id={det.get('track_id')} "
                                    f"attempt={attempt} error={last_error} backoff={wait_s:.1f}s")
                if self._interruptible_wait(wait_s):
                    break
                delay *= self.PUBLISH_RETRY_BACKOFF

        self._handle_publish_failure(job, last_error or "publisher stopped before delivery")

    def _interruptible_wait(self, seconds):
        """Wait up to `seconds`, but wake immediately on shutdown unless draining.
        Returns True if we should abort the retry loop (hard stop, not draining)."""
        if self._draining:
            time.sleep(min(seconds, 1.0))
            return False
        # returns True when the stop event fires during the wait
        return self._publisher_stop_event.wait(timeout=seconds)

    def _handle_publish_success(self, job, result):
        det = job['det']
        now = time.time()
        track_key = job.get('track_key')
        class_name = str(det.get('class_name', 'unknown')).lower()
        bbox = det.get('bbox', [])

        with self._tracking_lock:
            if track_key is not None:
                self._track_last_sent[track_key] = {'sent_at': now, 'bbox': bbox}
                self._pending_track_keys.discard(track_key)
                self._failed_backoff.pop(track_key, None)
            if job.get('iou_key') is not None:
                self._pending_iou_keys.discard(job['iou_key'])
                self._sent_objects[class_name].append({'bbox': bbox, 'ts': now})

        with self._counter_lock:
            self._publish_successes += 1
            if class_name == "person":
                self._persons_sent += 1
                if det.get('track_id') is not None:
                    self._sent_person_track_ids.add(det.get('track_id'))
            unique = len(self._sent_person_track_ids)
            total = self._persons_sent

        self.logger.info(f"PUBLISH_SUCCESS pipeline_id={self.id} pipeline_name={self.pipeline_name} "
                         f"class={class_name} track_id={det.get('track_id')} track_key={track_key} "
                         f"conf={det.get('confidence', 0):.3f} destinations={result.get('successful_destinations')}")
        if class_name == "person":
            msg = (f"[COUNTER] 👤 Persons delivered via webhook: {unique} unique ({total} total)")
            print(msg)

    def _handle_publish_failure(self, job, error, terminal=False):
        """Book a failed delivery. terminal=True means retrying can never help
        (payload rejected, destination disabled, or nothing enabled) - the track
        is NOT re-armed, ending the old infinite
        PUBLISH_FAILED -> re-arm -> 60s -> retry loop. Non-terminal failures
        keep today's behaviour: eligible again after FAILED_BACKOFF_SECONDS."""
        det = job['det']
        now = time.time()
        track_key = job.get('track_key')
        with self._tracking_lock:
            if track_key is not None:
                self._pending_track_keys.discard(track_key)
                if not terminal:
                    # Re-eligible after a backoff (not dropped, not hammered)
                    self._failed_backoff[track_key] = now + self.FAILED_BACKOFF_SECONDS
            if job.get('iou_key') is not None:
                self._pending_iou_keys.discard(job['iou_key'])
        with self._counter_lock:
            self._publish_failures += 1
        tail = ("terminal - track NOT re-armed" if terminal
                else f"not counted sent, re-eligible in {self.FAILED_BACKOFF_SECONDS:.0f}s")
        self.logger.error(f"PUBLISH_FAILED pipeline_id={self.id} pipeline_name={self.pipeline_name} "
                          f"class={det.get('class_name')} track_id={det.get('track_id')} track_key={track_key} "
                          f"error={error} - {tail}")

    def __str__(self) -> str:
        return f"InferencePipeline(id={self.id}, source={self.source}, inference_engine={self.inference_engine}, result_publisher={self.result_publisher})"

    def get_state(self) -> Dict[str, Any]:
        """Get the current state of the pipeline
        
        Returns:
            Dictionary with state information:
            - initialized: bool - True if pipeline is configured and model loaded
            - running: bool - True if pipeline thread is actively processing frames
            - error: str or None - Error message if in error state
            - status: str - Combined status string (e.g., 'initialized_running', 'initialized_stopped')
        """
        # Determine combined status string
        if self._error_state:
            status = f"{'initialized' if self._is_initialized else 'uninitialized'}_error"
        elif self._is_running:
            status = "initialized_running"  # Can only run if initialized
        elif self._is_initialized:
            status = "initialized_stopped"
        else:
            status = "uninitialized_stopped"
        
        return {
            'initialized': self._is_initialized,
            'running': self._is_running,
            'error': self._error_state,
            'status': status
        }
    
    def is_initialized(self) -> bool:
        """Check if pipeline is initialized (configured and model loaded)"""
        return self._is_initialized
    
    def is_running(self) -> bool:
        """Check if pipeline is currently running"""
        return self._is_running
    
    def has_error(self) -> bool:
        """Check if pipeline is in error state"""
        return self._error_state is not None
    
    def get_error(self) -> Optional[str]:
        """Get the error message if pipeline is in error state"""
        return self._error_state
    
    def clear_error(self):
        """Clear the error state"""
        self._error_state = None

    def get_effective_device(self) -> Optional[str]:
        """Best-effort report of the device the model ACTUALLY loaded on, as opposed to
        the device that was configured. Read-only and exception-safe: instrumentation must
        never be able to disturb a running pipeline.

        Ultralytics exposes the resolved torch device on the loaded model; other engines
        only know the string they were given.
        """
        engine = getattr(self, "inference_engine", None)
        if engine is None:
            return None
        try:
            model = getattr(engine, "model", None)
            dev = getattr(model, "device", None)
            if dev is not None:
                return str(dev)
        except Exception:
            pass
        try:
            if getattr(engine, "use_openvino", False):
                return f"openvino:{getattr(engine, 'device', None)}"
            return getattr(engine, "device", None)
        except Exception:
            return None

    def get_metrics(self) -> Dict[str, Any]:
        """
        Get the current metrics of the pipeline.
        """
        if self._start_time == 0:
            self._start_time = time.perf_counter()

        current_time = time.perf_counter()
        elapsed_time = current_time - self._start_time
        
        # Calculate FPS over the last 10 seconds
        fps_10sec = self._calculate_rolling_fps(current_time)
        
        # Calculate rolling average inference latency
        avg_latency = self._calculate_rolling_latency()
        
        # Format uptime as human-readable string
        uptime_formatted = self._format_uptime(elapsed_time)
        
        # Get current state
        state = self.get_state()
        
        return {
            "id": self.id,
            "frame_count": self._frame_counter,
            "inference_count": self._inference_counter,
            "elapsed_time": elapsed_time,
            "uptime": uptime_formatted,  # Human-readable uptime
            "fps": fps_10sec,  # Use 10-second rolling average
            "fps_overall": self._frame_counter / elapsed_time if elapsed_time > 0 else 0,  # Overall FPS since start
            "inference_enabled": self._inference_enabled,
            "latency_ms": avg_latency,  # Rolling average inference latency in milliseconds
            # --- Step 0 primitives (raw values; the benchmark harness does the statistics)
            "capture_timestamp": self._last_capture_wall,   # time.time() of last frame read
            "read_wait_ms": self._last_read_wait_ms,        # blocking time of last read()
            "failed_read_count": self._failed_read_count,
            "effective_device": self.get_effective_device(),
            "state": state,  # Include pipeline state information
            "initialized": state['initialized'],
            "running": state['running'],
            "error": state['error'],
        }
    
    def _calculate_rolling_fps(self, current_time: float) -> float:
        """
        Calculate FPS over the last 10 seconds using a rolling window.
        """
        # Remove timestamps older than the window
        cutoff_time = current_time - self._fps_window_seconds
        self._frame_timestamps = [ts for ts in self._frame_timestamps if ts >= cutoff_time]
        
        # Need at least 2 frames to calculate FPS
        if len(self._frame_timestamps) < 2:
            return 0.0
        
        # Calculate FPS based on frames in the window
        time_span = self._frame_timestamps[-1] - self._frame_timestamps[0]
        if time_span > 0:
            # Use len() - 1 because we're counting intervals between frames
            fps = (len(self._frame_timestamps) - 1) / time_span
            return round(fps, 1)  # Round to 1 decimal place for cleaner display
        else:
            # If all frames happened at the same time, can't calculate meaningful FPS
            return 0.0

    def _calculate_rolling_latency(self) -> float:
        """
        Calculate average inference latency over the last N inferences.
        """
        if not self._inference_latencies:
            return 0.0
        
        # Calculate average latency from the rolling window
        avg_latency = sum(self._inference_latencies) / len(self._inference_latencies)
        return round(avg_latency, 1)  # Round to 1 decimal place

    def _format_uptime(self, elapsed_seconds: float) -> str:
        """
        Format elapsed time into a human-readable uptime string.
        """
        if elapsed_seconds < 60:
            return f"{int(elapsed_seconds)}s"
        elif elapsed_seconds < 3600:  # Less than 1 hour
            minutes = int(elapsed_seconds // 60)
            seconds = int(elapsed_seconds % 60)
            return f"{minutes}m {seconds}s"
        elif elapsed_seconds < 86400:  # Less than 1 day
            hours = int(elapsed_seconds // 3600)
            minutes = int((elapsed_seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
        else:  # 1 day or more
            days = int(elapsed_seconds // 86400)
            hours = int((elapsed_seconds % 86400) // 3600)
            return f"{days}d {hours}h"

    def disable_publisher(self, id: str = 'all'):
        """Disable a specific result publisher by ID or all publishers"""
        print(f"DEBUG: disable_publisher called with id='{id}'")
        if id == 'all':
            for rp in self.result_publisher.destinations:
                rp.enabled = False
            print(f"Pipeline {self.id}: Disabled all result publishers")
            return

        if self.result_publisher:
            print(f"DEBUG: Looking for publisher with id='{id}' among {len(self.result_publisher.destinations)} destinations")
            for i, dest in enumerate(self.result_publisher.destinations):
                print(f"DEBUG: Destination {i}: _id='{getattr(dest, '_id', 'NO_ID')}', enabled={getattr(dest, 'enabled', 'NO_ENABLED')}")
            
            rp = self.result_publisher.get_by_id(id)
            if rp:
                rp.enabled = False
                print(f"Pipeline {self.id}: Disabled publisher {id} - new enabled state: {rp.enabled}")
            else:
                print(f"Pipeline {self.id}: Publisher {id} not found")
        else:
            print(f"Pipeline {self.id}: No result publisher configured")

    def enable_publisher(self, id: str = 'all'):
        """Enable a specific result publisher by ID or all publishers"""
        print(f"DEBUG: enable_publisher called with id='{id}'")
        if id == 'all':
            for rp in self.result_publisher.destinations:
                rp.enabled = True
                # Reset frame count if paused
                if hasattr(rp, 'frame_limit_reached') and rp.frame_limit_reached:
                    if hasattr(rp, 'reset_frame_count'):
                        rp.reset_frame_count()
                        print(f"Pipeline {self.id}: Reset frame count for paused publisher")
            print(f"Pipeline {self.id}: Enabled all result publishers")
            return

        if self.result_publisher:
            print(f"DEBUG: Looking for publisher with id='{id}' among {len(self.result_publisher.destinations)} destinations")
            for i, dest in enumerate(self.result_publisher.destinations):
                print(f"DEBUG: Destination {i}: _id='{getattr(dest, '_id', 'NO_ID')}', enabled={getattr(dest, 'enabled', 'NO_ENABLED')}")
            
            rp = self.result_publisher.get_by_id(id)
            if rp:
                # Reset frame count if paused (when re-enabling via UI toggle)
                if hasattr(rp, 'frame_limit_reached') and rp.frame_limit_reached:
                    if hasattr(rp, 'reset_frame_count'):
                        rp.reset_frame_count()
                        print(f"Pipeline {self.id}: Reset frame count for paused publisher {id}")
                
                rp.enabled = True
                print(f"Pipeline {self.id}: Enabled publisher {id} - new enabled state: {rp.enabled}")
            else:
                print(f"Pipeline {self.id}: Publisher {id} not found")
        else:
            print(f"Pipeline {self.id}: No result publisher configured")

    def get_publisher_states(self) -> Dict[str, Any]:
        """Get the current state of all publishers"""
        try:
            if not self.result_publisher:
                return {}
            
            states = {}
            
            for i, destination in enumerate(self.result_publisher.destinations):
                try:
                    if hasattr(destination, '_id'):
                        dest_id = str(destination._id)  # Ensure ID is string
                        
                        # Get attributes with type safety for JSON serialization
                        enabled = bool(getattr(destination, 'enabled', True))
                        dest_type = str(getattr(destination, 'type', 'unknown'))
                        is_configured = bool(getattr(destination, 'is_configured', False))
                        
                        # Ensure failure_count is always an integer
                        failure_count = getattr(destination, 'failure_count', 0)
                        
                        if not isinstance(failure_count, int):
                            try:
                                failure_count = int(failure_count) if str(failure_count).isdigit() else 0
                            except (ValueError, TypeError):
                                failure_count = 0
                        
                        # Get auto_disabled safely as boolean
                        try:
                            auto_disabled = bool(getattr(destination, 'auto_disabled', False))
                        except Exception:
                            auto_disabled = False
                        
                        # Get paused state (frame limit reached)
                        try:
                            is_paused = bool(getattr(destination, 'is_paused', False))
                        except Exception:
                            is_paused = False
                        
                        # Get frame count information
                        frame_count = int(getattr(destination, 'frame_count', 0))
                        max_frames = getattr(destination, 'max_frames', None)
                        if max_frames is not None:
                            try:
                                max_frames = int(max_frames)
                            except (ValueError, TypeError):
                                max_frames = None
                        
                        # Get last_error as string or None
                        last_error = getattr(destination, 'last_error', None)
                        if last_error is not None:
                            last_error = str(last_error)
                        
                        # Create the state dictionary with JSON-safe types
                        state_dict = {
                            'enabled': enabled,
                            'type': dest_type,
                            'configured': is_configured,
                            'failure_count': failure_count,
                            'auto_disabled': auto_disabled,
                            'is_paused': is_paused,
                            'frame_count': frame_count,
                            'max_frames': max_frames,
                            'last_error': last_error
                        }

                        # Where deliveries actually go (webhook: WEBHOOK_BASE_URL
                        # overrides any stored legacy url - the UI must show the
                        # effective destination, not the stored one).
                        if hasattr(destination, 'effective_destination'):
                            try:
                                eff = destination.effective_destination()
                                state_dict['effective_url'] = eff.get('url')
                                state_dict['effective_mode'] = eff.get('mode')
                            except Exception:
                                pass
                        
                        # Test JSON serialization of this state to catch issues early
                        try:
                            import json
                            json.dumps(state_dict)
                            states[dest_id] = state_dict
                        except Exception:
                            # Skip this destination to prevent the entire API from failing
                            pass
                            
                except Exception:
                    # Skip problematic destinations
                    pass
            
            # Test JSON serialization of the entire states dictionary
            try:
                import json
                json.dumps(states)
            except Exception:
                return {}  # Return empty dict if serialization fails
            
            return states
            
        except Exception:
            return {}


    def enable_inference(self):
        """Enable inference processing"""
        self._inference_enabled = True
        print(f"Pipeline {self.id}: Inference enabled")

    def disable_inference(self):
        """Disable inference processing"""
        self._inference_enabled = False
        print(f"Pipeline {self.id}: Inference disabled")

    def _should_auto_delete_images(self) -> bool:
        """Check if auto-delete is enabled for image folder sources"""
        if not self._frame_source_config:
            return False
        
        # Check if this is an image folder source with auto-delete enabled
        capture_type = self._frame_source_config.get('capture_type', '')
        auto_delete = self._frame_source_config.get('auto_delete', False)
        
        return capture_type in ['folder', 'image_folder'] and auto_delete
    
    def _is_folder_source(self) -> bool:
        """Check if this is a folder-based frame source"""
        if not self._frame_source_config:
            return False
        
        # Check if this is a folder source that should watch for new files
        capture_type = self._frame_source_config.get('capture_type', '')
        return capture_type in ['folder', 'image_folder']
    
    def _delete_current_image(self):
        """Delete the current image file if auto-delete is enabled"""
        if not self._should_auto_delete_images():
            return
            
        # Try to get the current file path from the frame source
        if hasattr(self.source, 'get_current_file_path'):
            try:
                current_file = self.source.get_current_file_path() # type: ignore
                if current_file and os.path.exists(current_file):
                    # Add pipeline ID to help identify which instance is deleting files
                    print(f"Pipeline {self.id}: Auto-deleting processed image: {current_file}")
                    os.remove(current_file)
                    print(f"Pipeline {self.id}: Successfully deleted: {current_file}")
            except FileNotFoundError:
                # File already deleted by another process/thread - this is expected in multi-instance scenarios
                print(f"Pipeline {self.id}: File already deleted (by another instance?): {getattr(self.source, 'get_current_file_path', lambda: 'unknown')()}")
            except Exception as e:
                print(f"Pipeline {self.id}: Error deleting image file: {e}")
        elif hasattr(self.source, 'current_file'):
            # Alternative attribute name
            try:
                current_file = self.source.current_file # type: ignore
                if current_file and os.path.exists(current_file):
                    # Add pipeline ID to help identify which instance is deleting files
                    print(f"Pipeline {self.id}: Auto-deleting processed image: {current_file}")
                    os.remove(current_file)
                    print(f"Pipeline {self.id}: Successfully deleted: {current_file}")
            except FileNotFoundError:
                # File already deleted by another process/thread - this is expected in multi-instance scenarios
                print(f"Pipeline {self.id}: File already deleted (by another instance?): {getattr(self.source, 'current_file', 'unknown')}")
            except Exception as e:
                print(f"Pipeline {self.id}: Error deleting image file: {e}")

    def is_inference_enabled(self) -> bool:
        """Check if inference is enabled"""
        return self._inference_enabled
    
    def set_thumbnail_path(self, thumbnail_dir: str):
        """Set the directory where thumbnails will be saved"""
        if not os.path.exists(thumbnail_dir):
            os.makedirs(thumbnail_dir, exist_ok=True)
        self._thumbnail_path = os.path.join(thumbnail_dir, f"thumbnail_{self.id}.jpg")
    
    def capture_thumbnail(self, frame):
        """Capture a thumbnail from the current frame"""
        if self._thumbnail_path and frame is not None:
            try:
                # Resize frame to thumbnail size (e.g., 320x240) for faster loading
                height, width = frame.shape[:2]
                thumbnail_width = 320
                thumbnail_height = int((thumbnail_width / width) * height)
                
                # Resize the frame
                thumbnail = cv2.resize(frame, (thumbnail_width, thumbnail_height))
                
                # Save the thumbnail into the STAGING area; the manager callback validates,
                # hashes, promotes it into ARTIFACT_ROOT/thumbnails and registers it in
                # PostgreSQL (thumbnail_registry) - the file alone is never the record.
                cv2.imwrite(self._thumbnail_path, thumbnail)
                self._thumbnail_captured = True
                cb = self._on_thumbnail_captured
                if cb is not None:
                    try:
                        cb(self._thumbnail_path)
                    except Exception as e:
                        print(f"Pipeline {self.id}: thumbnail registration failed: {e}")
                print(f"Pipeline {self.id}: Thumbnail captured ({self._thumbnail_path})")
                return True
            except Exception as e:
                print(f"Pipeline {self.id}: Failed to capture thumbnail: {e}")
                return False
        return False
    
    def get_thumbnail_path(self) -> Optional[str]:
        """Get the path to the thumbnail image"""
        if self._thumbnail_path and os.path.exists(self._thumbnail_path):
            return self._thumbnail_path
        return None
    
    def has_thumbnail(self) -> bool:
        """Check if a thumbnail exists for this pipeline"""
        return bool(self._thumbnail_path and os.path.exists(self._thumbnail_path))
    
    def delete_thumbnail(self):
        """Delete the thumbnail file"""
        if self._thumbnail_path and os.path.exists(self._thumbnail_path):
            try:
                os.remove(self._thumbnail_path)
                self._thumbnail_captured = False
                print(f"Pipeline {self.id}: Thumbnail deleted")
            except Exception as e:
                print(f"Pipeline {self.id}: Failed to delete thumbnail: {e}")
   
    def run(self):
        """Main pipeline execution loop"""
        self.logger.info(f"Starting pipeline run loop")
        print(f"Pipeline {self.id}: Starting run loop")

        self._is_running = True
        self._error_state = None
        self._draining = False

        # Initialize tracking/counters and start the background publisher worker
        self._init_dedup()
        self._start_publisher_worker()

        try:
            # Connect to frame source
            self.source.connect()
            self.logger.info("Frame source connected successfully")
            print(f"Pipeline {self.id}: Frame source connected")

            is_folder_source = self._is_folder_source()
            consecutive_empty_reads = 0
            max_empty_reads_before_sleep = 10

            while not self._stop_requested:
                # Check source connection
                if not self.source.isOpened():
                    if is_folder_source:
                        self.logger.debug("Folder source disconnected, reconnecting...")
                        time.sleep(1)
                        try:
                            self.source.connect()
                            continue
                        except Exception as e:
                            self.logger.error(f"Reconnection failed: {e}")
                            continue
                    else:
                        self.logger.warning("Source disconnected, ending pipeline")
                        break

                # Read frame (timed: a read that returns instantly while the source is
                # live means we are draining a buffered backlog, i.e. stale frames)
                _read_t0 = time.perf_counter()
                success, frame = self.source.read()
                self._last_read_wait_ms = (time.perf_counter() - _read_t0) * 1000.0
                if not success or frame is None:
                    self._failed_read_count += 1
                    consecutive_empty_reads += 1

                    if is_folder_source:
                        if consecutive_empty_reads >= max_empty_reads_before_sleep:
                            self.logger.debug("No files in folder, entering wait mode")
                            time.sleep(0.1)
                            consecutive_empty_reads = 0
                        continue
                    else:
                        continue

                # Frame successfully read
                consecutive_empty_reads = 0
                self._frame_counter += 1
                self._last_capture_wall = time.time()

                # Record timestamp for FPS calculation
                now_perf = time.perf_counter()
                self._frame_timestamps.append(now_perf)

                # Cleanup old timestamps periodically
                if self._frame_counter % 100 == 0:
                    cutoff = now_perf - (self._fps_window_seconds + 2)
                    self._frame_timestamps = [
                        ts for ts in self._frame_timestamps if ts >= cutoff
                    ]
                    self.logger.debug(f"Frame count: {self._frame_counter}, FPS: {self._calculate_rolling_fps(now_perf):.1f}")

                # Run inference if enabled
                results = None
                if self._inference_enabled:
                    t0 = time.perf_counter()
                    results = self.inference_engine.infer(frame)
                    latency_ms = (time.perf_counter() - t0) * 1000

                    self._inference_latencies.append(latency_ms)
                    if len(self._inference_latencies) > self._latency_window_size:
                        self._inference_latencies.pop(0)

                    self._inference_counter += 1

                # Handle frame storage for streaming
                if results is not None:
                    json_results = self.inference_engine.result_to_json(results)
                    # print(f"Pipeline {self.id}: Inference results: {json.dumps(json_results)}")

                    if self.result_publisher.do_any_destinations_need_result_image() or self._is_streaming:
                        with self._frame_lock:
                            output = self.inference_engine.draw(frame, results)
                            self._latest_frame = output.copy()

                            if not self._thumbnail_captured and self._thumbnail_path:
                                self.capture_thumbnail(output)
                    else:
                        with self._frame_lock:
                            self._latest_frame = frame.copy()

                            if not self._thumbnail_captured and self._thumbnail_path:
                                self.capture_thumbnail(frame)
                else:
                    with self._frame_lock:
                        self._latest_frame = frame.copy()

                        if not self._thumbnail_captured and self._thumbnail_path:
                            self.capture_thumbnail(frame)

                # Process detections. Selection happens on the inference thread;
                # actual network delivery is handed to the background publisher worker
                # so a slow/failing webhook never blocks inference.
                if results is not None:
                    # Periodic maintenance + counter heartbeat
                    if self._frame_counter - self._last_cleanup_frame >= self._cleanup_interval:
                        self._cleanup_tracks()
                        self._last_cleanup_frame = self._frame_counter
                        with self._counter_lock:
                            unique, total = len(self._sent_person_track_ids), self._persons_sent
                        print(f"[COUNTER] 👤 Persons delivered via webhook so far: {unique} unique "
                              f"({total} total) | queue={self._publish_queue.qsize()}")

                    # Support both "detections" and "predictions" keys
                    all_detections = json_results.get("detections", json_results.get("predictions", []))

                    now = time.time()
                    iou_jobs = []
                    # 1) Update candidates for every kept detection FIRST (so the
                    #    current frame is considered before deciding the best).
                    for det in all_detections:
                        class_name = det.get("class_name", "").lower()
                        confidence = det.get("confidence", 0)

                        if class_name not in self.ALLOWED_LIST:
                            self.logger.debug(f"[FILTER] Ignoring {class_name} (not in ALLOWED_LIST)")
                            continue

                        min_conf_threshold = self.MIN_CONFIDENCE_FOR_PERSON if class_name == "person" else self.MIN_CONFIDENCE
                        if confidence < min_conf_threshold:
                            self.logger.debug(f"[FILTER] Skipping {class_name} conf={confidence:.3f} < {min_conf_threshold:.3f}")
                            continue

                        if det.get("track_id") is not None:
                            self._update_track_candidate(det, frame, now)
                        else:
                            job = self._register_iou_candidate(det, frame, now)
                            if job is not None:
                                iou_jobs.append(job)

                    # 2) After all detections processed, collect tracks ready to send.
                    #    Runs even when all_detections == [] (person left the frame).
                    ready_jobs = self._collect_ready_tracks(now)

                    # 3) Enqueue jobs for the background worker (non-blocking).
                    for job in ready_jobs + iou_jobs:
                        job['json_results'] = json_results
                        if job.get('frame') is None:
                            job['frame'] = frame
                        self.logger.info(f"TRACK_READY pipeline_id={self.id} class={job['det'].get('class_name')} "
                                         f"track_id={job['det'].get('track_id')} conf={job['det'].get('confidence', 0):.3f}")
                        self._enqueue_publish_job(job)

                # Auto-delete processed image if enabled
                self._delete_current_image()

        except Exception as e:
            self._error_state = str(e)
            self._inference_enabled = False
            self.logger.error(f"Pipeline error: {e}", exc_info=True)
            print(f"Pipeline {self.id} ERROR: {e}")

        finally:
            self._is_running = False
            if self.source:
                self.source.stop()
            self.logger.info("Pipeline stopped")
            print(f"Pipeline {self.id}: Stopped")


    def _apply_detection_config(self, cfg: Dict[str, Any]):
        """Validate and apply detection_config overrides. Invalid values are
        rejected (kept at default) with a warning rather than crashing."""
        def num(key, current, lo=None, hi=None, allow_zero=True):
            if key not in cfg or cfg[key] is None:
                return current
            try:
                v = float(cfg[key])
            except (TypeError, ValueError):
                self.logger.warning(f"Invalid {key}={cfg[key]!r} (not a number) - keeping {current}")
                return current
            if lo is not None and v < lo:
                self.logger.warning(f"Invalid {key}={v} (< {lo}) - keeping {current}")
                return current
            if hi is not None and v > hi:
                self.logger.warning(f"Invalid {key}={v} (> {hi}) - keeping {current}")
                return current
            if not allow_zero and v == 0:
                self.logger.warning(f"Invalid {key}=0 - keeping {current}")
                return current
            return v

        self.MIN_CONFIDENCE = num('min_confidence', self.MIN_CONFIDENCE, 0.0, 1.0)
        self.MIN_CONFIDENCE_FOR_PERSON = num('person_confidence_threshold', self.MIN_CONFIDENCE_FOR_PERSON, 0.0, 1.0)
        self.IMMEDIATE_SEND_CONFIDENCE = num('immediate_send_confidence', self.IMMEDIATE_SEND_CONFIDENCE, 0.0, 1.0)
        self.SEND_BUFFER_SECONDS = num('send_buffer_seconds', self.SEND_BUFFER_SECONDS, 0.0)
        self.MAX_COLLECT_SECONDS = num('max_collect_seconds', self.MAX_COLLECT_SECONDS, 0.0)
        self.TRACK_TTL_SECONDS = num('track_ttl_seconds', self.TRACK_TTL_SECONDS, 0.0)
        self.TRACK_LOST_TIMEOUT_SECONDS = num('track_lost_timeout_seconds', self.TRACK_LOST_TIMEOUT_SECONDS, 0.0)
        self.PUBLISH_MAX_RETRIES = int(num('publish_max_retries', self.PUBLISH_MAX_RETRIES, 0))
        self.PUBLISH_RETRY_DELAY_SECONDS = num('publish_retry_delay_seconds', self.PUBLISH_RETRY_DELAY_SECONDS, 0.0)
        self.PUBLISH_RETRY_BACKOFF = num('publish_retry_backoff', self.PUBLISH_RETRY_BACKOFF, 1.0)
        self.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS = num('publisher_shutdown_timeout_seconds', self.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS, 0.0)
        self.PUBLISH_QUEUE_SIZE = int(num('publish_queue_size', self.PUBLISH_QUEUE_SIZE, 1))

    def configure(self, frame_source_config, inference_engine_config, result_publisher: ResultPublisher, detection_config=None):

        self._frame_source_config = frame_source_config  # Store for auto-delete functionality
        self.source = FrameSourceFactory.create(**frame_source_config)

        self.inference_engine_config = inference_engine_config
        self.inference_engine = InferenceEngineFactory.create(**inference_engine_config)
        self.inference_engine.load()

        self.result_publisher = result_publisher

        # Optional detection/publisher overrides from the pipeline config (validated)
        if detection_config:
            self._apply_detection_config(detection_config)

        # Re-create the publish queue if the size was overridden
        self._publish_queue = queue.Queue(maxsize=self.PUBLISH_QUEUE_SIZE)

        self.logger.info(
            f"Detection thresholds: person>={self.MIN_CONFIDENCE_FOR_PERSON}, other>={self.MIN_CONFIDENCE}, "
            f"buffer={self.SEND_BUFFER_SECONDS}s, max_collect={self.MAX_COLLECT_SECONDS}s, "
            f"immediate>={self.IMMEDIATE_SEND_CONFIDENCE}, cooldown={self.TRACK_TTL_SECONDS}s, "
            f"lost_timeout={self.TRACK_LOST_TIMEOUT_SECONDS}s | publisher: queue={self.PUBLISH_QUEUE_SIZE}, "
            f"retries={self.PUBLISH_MAX_RETRIES}, delay={self.PUBLISH_RETRY_DELAY_SECONDS}s, "
            f"backoff={self.PUBLISH_RETRY_BACKOFF}x, shutdown_timeout={self.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS}s"
        )
        print(f"Pipeline {self.id}: thresholds person>={self.MIN_CONFIDENCE_FOR_PERSON} other>={self.MIN_CONFIDENCE} "
              f"buffer={self.SEND_BUFFER_SECONDS}s cooldown={self.TRACK_TTL_SECONDS}s | "
              f"publisher queue={self.PUBLISH_QUEUE_SIZE} retries={self.PUBLISH_MAX_RETRIES}")

        # Mark as initialized once configuration is complete and model is loaded
        self._is_initialized = True
        self._error_state = None  # Clear any previous errors
        print(f"Pipeline {self.id}: Initialized successfully")

    def start(self):
        """
        Start the inference pipeline on a separate thread.
        """
        if not self._is_initialized:
            raise RuntimeError(f"Pipeline {self.id} cannot start - not initialized. Call configure() first.")
        
        if self._is_running:
            print(f"Pipeline {self.id} is already running")
            return
        
        self._start_time = time.perf_counter()  # Record the start time
        self._stop_requested = False  # Reset stop flag
        
        # Reset FPS tracking and counters
        self._frame_timestamps = []
        self._frame_counter = 0
        self._inference_counter = 0
        self._inference_latencies = []  # Reset latency tracking
        
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def stop(self):
        """
        Stop the inference pipeline.
        """
        print(f"Stopping pipeline {self.id}")
        self._stop_requested = True  # Signal the run loop to stop
        self._is_streaming = False  # Reset streaming flag when pipeline stops
        
        # Update thumbnail with the last received frame before stopping
        if self._latest_frame is not None and self._thumbnail_path:
            try:
                with self._frame_lock:
                    last_frame = self._latest_frame.copy()
                self.capture_thumbnail(last_frame)
                print(f"Pipeline {self.id}: Updated thumbnail with last frame before stopping")
            except Exception as e:
                print(f"Pipeline {self.id}: Failed to update thumbnail with last frame: {e}")
        
        # Give the inference thread some time to stop gracefully
        if hasattr(self, 'thread') and self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)  # Wait up to 5 seconds
            if self.thread.is_alive():
                print(f"Warning: Pipeline {self.id} thread did not stop within timeout")

        # Graceful publisher shutdown: flush remaining candidates, drain the queue,
        # then stop the worker so in-flight/unique events aren't lost.
        self._flush_and_stop_publisher()

        # Ensure source is stopped
        if hasattr(self, 'source') and self.source:
            try:
                self.source.stop()
            except Exception as e:
                print(f"Error stopping source: {e}")

        # Mark as not running (thread will also set this in finally block)
        self._is_running = False

        print(f"Pipeline {self.id} stopped")

    def _flush_and_stop_publisher(self):
        """Graceful publisher shutdown (spec #15):
        1) convert remaining valid candidates into jobs,
        2) let the queue drain for up to PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS,
        3) stop and join the worker,
        4) log anything left undelivered."""
        if self._publisher_thread is None or not self._publisher_thread.is_alive():
            return

        now = time.time()
        # 1) Flush current best candidates into jobs
        with self._tracking_lock:
            pending_keys = list(self._track_best.keys())
        flushed = 0
        for track_key in pending_keys:
            with self._tracking_lock:
                entry = self._track_best.pop(track_key, None)
                if entry is None:
                    continue
                self._pending_track_keys.add(track_key)
            job = {'track_key': track_key, 'det': entry['best_det'],
                   'frame': entry['best_frame'], 'first_seen': entry['first_seen'],
                   'json_results': None}
            self.logger.info(f"PIPELINE_SHUTDOWN_FLUSH pipeline_id={self.id} track_key={track_key} "
                             f"conf={entry['best_det'].get('confidence', 0):.3f}")
            self._enqueue_publish_job(job)
            flushed += 1
        if flushed:
            print(f"Pipeline {self.id}: flushed {flushed} pending candidate(s) to the publisher on shutdown")

        # 2) Drain: allow retries to complete during the shutdown window
        self._draining = True
        deadline = now + self.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS
        while time.time() < deadline:
            if self._publish_queue.empty():
                break
            time.sleep(0.1)

        # 3) Stop and join the worker
        self._draining = False
        self._publisher_stop_event.set()
        self._publisher_thread.join(timeout=max(2.0, self.PUBLISH_RETRY_DELAY_SECONDS + 1.0))

        # 4) Report anything undelivered
        leftover = self._publish_queue.qsize()
        if leftover:
            self.logger.warning(f"PIPELINE_SHUTDOWN_FLUSH pipeline_id={self.id} {leftover} event(s) "
                               f"could not be delivered before shutdown")
            print(f"Pipeline {self.id}: WARNING {leftover} event(s) undelivered at shutdown")

    def get_publish_stats(self) -> Dict[str, Any]:
        """Snapshot of confirmed-delivery counters (thread-safe)."""
        with self._counter_lock:
            return {
                'persons_sent': self._persons_sent,
                'unique_person_track_ids': len(self._sent_person_track_ids),
                'publish_attempts': self._publish_attempts,
                'publish_successes': self._publish_successes,
                'publish_failures': self._publish_failures,
                'publish_retries': self._publish_retries,
                'publish_rate_limited': self._publish_rate_limited,
                'queued_events': self._queued_events,
                'dropped_events': self._dropped_events,
                'queue_size': self._publish_queue.qsize(),
            }

    def get_latest_frame(self):
        """Get the latest processed frame for streaming"""
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def start_streaming(self):
        """Enable streaming flag to indicate frames should be drawn with results"""
        self._is_streaming = True
        print(f"Pipeline {self.id}: Streaming enabled")

    def stop_streaming(self):
        """Disable streaming flag to optimize performance when not streaming"""
        self._is_streaming = False
        print(f"Pipeline {self.id}: Streaming disabled")

    def is_streaming(self) -> bool:
        """Check if streaming is currently active"""
        return self._is_streaming

    def __del__(self):
        try:
            self.stop()
            # Note: We do NOT delete thumbnails here - they should persist
            # across sessions and only be deleted when pipeline is explicitly deleted
        except Exception:
            pass  # Ignore errors during cleanup


def main():
    import cv2

    mqtt_destination = MQTTDestination()
    # rate_limit is the MINIMUM SECONDS BETWEEN MESSAGES (0 = unlimited). For
    # one-event-per-unique-person we don't want throttling, so use 0.0.
    mqtt_destination.configure(server='192.168.1.241', port=1883, topic='inference/results', rate_limit=0.0)
    mqtt_destination.include_image_data = False  # Include image data in published messages
    
    result_publisher = ResultPublisher()
    result_publisher.add(mqtt_destination)

    frame_source_config = {'capture_type': 'webcam', 'source': 0, 'threaded': True, 'width': 640, 'height': 480, 'fps': 30}
    # frame_source_config = {'capture_type': 'realsense', 'width': 1280, 'height': 720, 'threaded': False, 'fps': 30}
    inference_config = {'engine_type': 'ultralytics', 'model_path': 'yolo11n-pose.pt', 'device': 'intel:cpu'}
    # inference_config = {'engine_type': 'geti', 'model_path': 'C:\\Users\\olive\\OneDrive\\Projects\\InferNode\\InferenceNode\\model_repository\\models\\Deployment-juggling-balls (1)_dd785c2f.zip', 'device': 'cpu'}


    pipeline = InferencePipeline()
    pipeline.configure(
        frame_source_config=frame_source_config,
        inference_engine_config=inference_config,
        result_publisher=result_publisher
    )

    print(pipeline)

    pipeline.start()

    while True:
        frame = pipeline.get_latest_frame()
        if frame is not None:
            cv2.imshow(f"Pipeline {pipeline.id}", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    pipeline.stop()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()