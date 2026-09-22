from __future__ import annotations
import sys
import os
import threading
import queue
import uuid
import time
import json
import math
import heapq
import itertools
import copy
from datetime import datetime, timezone
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
        self.node_id = os.getenv("ARMYEYE_NODE_ID", "")
        self.pipeline_name = ""  # Human-readable builder name, set by PipelineManager
        self.logger = logging.getLogger(f'InferencePipeline.{self.id[:8]}')
        self.nodes = []
        self.source : VideoCaptureBase
        self.inference_engine : BaseInferenceEngine
        self.result_publisher : ResultPublisher
        self._stop_requested = False  # Flag to control pipeline execution
        self._latest_frame = None  # Store latest processed frame for streaming
        self._inference_enabled = True  # Flag to enable/disable inference processing

        self._preview_cache = {}
        self._preview_lock = threading.Lock()
        self._viewer_count = 0
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

        # --- target inference rate (Step 3) --------------------------------------------
        # Frames are ALWAYS read so the decoder is drained and the pipeline stays at the
        # live edge; inference runs at most TARGET_INFERENCE_FPS times per second. A 25 fps
        # camera can therefore be WATCHED at 25 fps while AI samples 5 fps.
        # 0 = infer every frame (the historical behaviour).
        # Node-wide default: ARMYEYE_TARGET_INFERENCE_FPS. Per-pipeline override:
        # detection_config.target_inference_fps.
        self.TARGET_INFERENCE_FPS = self._env_target_fps()
        self._last_inference_at = None      # None = never inferred yet
        # Cleared for good if the capture backend ever refuses grab(); see _grab_only.
        self._skip_decode_supported = True

        # --- live-source reconnect (Step 5) --------------------------------------------
        # A camera that drops must not end its pipeline permanently, and a dead source must
        # not spin a CPU core. Bounded backoff, unbounded attempts: a camera can come back
        # hours later and should be picked up when it does.
        self.RECONNECT_INITIAL_DELAY = 1.0
        self.RECONNECT_MAX_DELAY = 30.0
        self.FAILED_READS_BEFORE_RECONNECT = 30      # ~1.2 s at 25 fps
        self.FAILED_READ_SLEEP = 0.02                # stops the hot spin on a dead source
        self._reconnect_attempts = 0
        
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
        # Person confidence says nothing about face visibility. Allow fresh views
        # before the long cooldown; HTTP acceptance is not face recognition.
        self.PERSON_CAPTURE_COUNT = 3
        self.PERSON_CAPTURE_INTERVAL_SECONDS = 2.0
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
        # track_key -> {sent_at, bbox} (successful-send cooldown)
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
        self._retry_jobs = []
        self._retry_sequence = itertools.count()
        self._work_lock = threading.RLock()
        self._candidate_stop = threading.Event()
        self._candidate_thread = None
        self._shutdown_lock = threading.Lock()
        self._inflight = 0
        self._queue_bytes = 0
        self._durable_failures = 0
        self.PUBLISH_QUEUE_BYTES = 64 * 1024 * 1024
        self.PUBLISH_MAX_AGE_SECONDS = 300.0
        self._terminal_tracks = {}
        self._failed_iou = []
        self._outbox = None
        self._file_failures = set()
        self._file_groups = {}
        self._file_seen = {}
        self._tracking_session = uuid.uuid4().hex
        self._snapshot_source = None
        self._snapshot_frame = None
        self._tracking_epoch = 0
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
            self._terminal_tracks = {}
            self._failed_iou = []
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
        return (str(det.get('class_name', 'unknown')).lower(),
                f"{getattr(self, '_tracking_session', '')}:{getattr(self, '_tracking_epoch', 0)}:{det.get('track_id')}")

    def _update_track_candidate(self, det, frame, now):
        """Create/update the highest-confidence candidate for a tracked detection.
        An owned snapshot is shared across detections on the same captured frame.
        Only higher-confidence observations replace a candidate's selected frame."""
        track_key = self._make_track_key(det)
        confidence = det.get('confidence', 0)
        class_name = det.get('class_name', 'unknown')
        bbox = det.get('bbox', [])

        with self._tracking_lock:
            # Movement is not evidence of a new person. Epochs change on tracker resets.
            sent_rec = self._track_last_sent.get(track_key)
            if track_key in self._terminal_tracks:
                self._terminal_tracks[track_key] = now
                return

            if sent_rec is not None:
                cooldown = self.TRACK_TTL_SECONDS
                if (str(class_name).lower() == 'person'
                        and sent_rec.get('capture_count', self.PERSON_CAPTURE_COUNT) < self.PERSON_CAPTURE_COUNT):
                    cooldown = min(cooldown, self.PERSON_CAPTURE_INTERVAL_SECONDS)
                if (now - sent_rec.get('sent_at', 0)) < cooldown:
                    return

            # Already queued/publishing/retrying, or in post-failure backoff -> don't re-collect
            if track_key in self._pending_track_keys:
                return
            backoff_until = self._failed_backoff.get(track_key)
            if backoff_until is not None and now < backoff_until:
                return

            frames = {id(e['best_frame']): e['best_frame'] for e in self._track_best.values()
                      if e.get('best_frame') is not None}
            if frame is not None and self._snapshot_source is not frame and (
                    sum(f.nbytes for f in frames.values()) + frame.nbytes + self._queue_bytes > self.PUBLISH_QUEUE_BYTES):
                with self._counter_lock:
                    self._dropped_events += 1
                return
            entry = self._track_best.get(track_key)
            if entry is None:
                self._track_best[track_key] = {
                    'best_det': det,
                    'best_frame': self._snapshot(frame),
                    'captured_at': self._last_capture_wall or now,
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
                    entry['best_frame'] = self._snapshot(frame)
                    entry['captured_at'] = self._last_capture_wall or now
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
                    'captured_at': entry.get('captured_at', entry['first_seen']),
                })
        return jobs

    def _register_iou_candidate(self, det, frame, now):
        """No-track detection: return a job if it isn't a duplicate of a recently
        (successfully) published one and isn't already pending. Uses a temporary key."""
        cls = str(det.get('class_name', 'unknown')).lower()
        bbox = det.get('bbox', [])
        with self._tracking_lock:
            self._failed_iou = [r for r in self._failed_iou if now < r['until']]
            if any(r['cls'] == cls and self._iou(bbox, r['bbox']) > self.DEDUP_IOU_THRESHOLD
                   for r in self._failed_iou):
                return None
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
                'frame': self._snapshot(frame), 'first_seen': now,
                'captured_at': self._last_capture_wall or now}

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
            # Ready candidates are never expired before delivery. Terminal suppression
            # is released only after the track has actually disappeared for the TTL.
            self._terminal_tracks = {k: t for k, t in self._terminal_tracks.items()
                                     if now - t < self.TRACK_TTL_SECONDS}
            for cls in list(self._sent_objects.keys()):
                self._sent_objects[cls] = [h for h in self._sent_objects[cls]
                                           if now - h['ts'] < self.DEDUP_TTL_SECONDS]

    # ------------------------------------------------------------------ #
    # Background publisher worker
    # ------------------------------------------------------------------ #
    def _snapshot(self, frame):
        if frame is None:
            return None
        # One owned snapshot per captured frame, shared by all its detections.
        if self._snapshot_source is not frame:
            self._snapshot_source = frame
            self._snapshot_frame = frame.copy()
            self._snapshot_frame.flags.writeable = False
        return self._snapshot_frame

    def _start_publisher_worker(self):
        if self._publisher_thread and self._publisher_thread.is_alive():
            raise RuntimeError('Previous publisher has not stopped')
        self._publisher_stop_event.clear()
        self._candidate_stop.clear()
        if self._outbox:
            # Rehydrate once, including when this same instance is restarted.
            with self._work_lock:
                self._retry_jobs.clear()
                self._publish_queue = queue.Queue(maxsize=self.PUBLISH_QUEUE_SIZE)
                self._queue_bytes = 0
                self._durable_failures = 0
            for record in self._outbox.records():
                if record.get('state') == 'failed':
                    self._durable_failures += 1
                recovered = record['job']
                if recovered.get('source_file'):
                    self._file_seen[recovered['source_file']] = recovered.get('source_fingerprint')
                if record.get('state') == 'pending':
                    job = record['job']
                    if job.get('track_key'):
                        job['track_key'] = tuple(job['track_key'])
                        self._pending_track_keys.add(job['track_key'])
                    if job.get('iou_key'):
                        job['iou_key'] = (job['iou_key'][0], tuple(job['iou_key'][1]))
                        self._pending_iou_keys.add(job['iou_key'])
                    job['queued_bytes'] = True
                    self._queue_bytes += job.get('bytes', 0)
                    self._schedule(job, 0)
        self._publisher_thread = threading.Thread(
            target=self._publisher_worker, name=f"pub-{self.id[:8]}", daemon=True)
        self._publisher_thread.start()
        self._candidate_thread = threading.Thread(target=self._candidate_loop,
                                                  name=f"events-{self.id[:8]}", daemon=True)
        self._candidate_thread.start()

    def _candidate_loop(self):
        while not self._candidate_stop.wait(0.1):
            for job in self._collect_ready_tracks(time.time()):
                self._enqueue_publish_job(job)

    def _prepare_job(self, job):
        if isinstance(job.get('track_key'), list):
            job['track_key'] = tuple(job['track_key'])
        if isinstance(job.get('iou_key'), list):
            job['iou_key'] = (job['iou_key'][0], tuple(job['iou_key'][1]))
        if 'payload' in job:
            # Older durable outbox records contain Unix seconds. Preserve the
            # original capture instant and event identity when replaying them.
            captured_at = job['payload'].get('captured_at')
            if isinstance(captured_at, (int, float)) and not isinstance(captured_at, bool):
                job['payload']['captured_at'] = datetime.fromtimestamp(
                    captured_at, timezone.utc).isoformat().replace('+00:00', 'Z')
            return
        frame = job.get('frame')
        annotated = None
        if self.result_publisher.do_any_destinations_need_result_image() and frame is not None:
            # Annotate the event's selected frame, never the latest preview frame.
            annotated = frame.copy()
            det = job['det']
            x1, y1, x2, y2 = (int(v) for v in det['bbox'])
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(annotated, f"{det.get('class_name')} {det.get('confidence', 0):.2f}",
                        (x1, max(15, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 255, 0), 1)
        job['images'] = self.result_publisher.prepare_images(frame, annotated)
        job['payload'] = self._build_payload(job['det'], job.get('json_results'))
        captured_at = job.get('captured_at')
        if captured_at is None:
            captured_at = job.get('first_seen', time.time())
        job['payload']['captured_at'] = datetime.fromtimestamp(
            captured_at, timezone.utc).isoformat().replace('+00:00', 'Z')
        job['payload']['capture_clock'] = 'application_read'
        job['targets'] = self.result_publisher.destination_ids()
        job['accepted'] = []
        job['terminal'] = []
        job['created_at'] = time.time()
        job['attempt'] = 0
        job.pop('frame', None)
        job.pop('json_results', None)
        job['bytes'] = len(json.dumps(job, allow_nan=False, separators=(',', ':')).encode())

    def _persist_job(self, job, state='pending'):
        if self._outbox:
            saved = job.get('outbox_saved', False)
            job['outbox_saved'] = True
            try:
                self._outbox.put(job['payload']['event_id'], {'state': state, 'job': job})
            except Exception:
                job['outbox_saved'] = saved
                raise

    def _enqueue_publish_job(self, job):
        try:
            self._prepare_job(job)
            if not job['targets']:
                raise ValueError('No enabled configured destination')
            with self._work_lock:
                if self._queue_bytes + job['bytes'] > self.PUBLISH_QUEUE_BYTES:
                    raise ValueError('Event queue byte limit reached')
                if self._publish_queue.full():
                    raise ValueError('Event queue count limit reached')
                self._persist_job(job)
                self._publish_queue.put_nowait(job)
                self._queue_bytes += job['bytes']
                job['queued_bytes'] = True
            with self._counter_lock:
                self._queued_events += 1
            return True
        except Exception as exc:
            with self._counter_lock:
                self._dropped_events += 1
            if job.get('source_file'):
                self._file_failures.add(job['source_file'])
            self._handle_publish_failure(job, str(exc), terminal=False)
            self.logger.error('Event not accepted: %s', type(exc).__name__)
            return False

    def _schedule(self, job, delay):
        with self._work_lock:
            heapq.heappush(self._retry_jobs, (time.monotonic() + delay,
                                            next(self._retry_sequence), job))

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
            "node_id": self.node_id or self.id,
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
        while not self._publisher_stop_event.is_set():
            job, queued = None, False
            with self._work_lock:
                if self._retry_jobs and self._retry_jobs[0][0] <= time.monotonic():
                    _, _, job = heapq.heappop(self._retry_jobs)
            if job is None:
                try:
                    job = self._publish_queue.get(timeout=0.1)
                    queued = True
                except queue.Empty:
                    continue
            with self._work_lock:
                self._inflight += 1
            try:
                self._deliver_job(job, deferred=True)
            except Exception as exc:
                self.logger.exception('Event worker failed')
                self._handle_publish_failure(job, type(exc).__name__)
            finally:
                with self._work_lock:
                    self._inflight -= 1
                if queued:
                    self._publish_queue.task_done()

    def _deliver_job(self, job, deferred=False):
        self._prepare_job(job)
        if not job['targets']:
            self._handle_publish_failure(job, 'No enabled destination', terminal=True)
            return
        while not self._publisher_stop_event.is_set():
            if time.time() - job['created_at'] > self.PUBLISH_MAX_AGE_SECONDS:
                self._handle_publish_failure(job, 'Event delivery deadline exceeded')
                return
            remaining = set(job['targets']) - set(job['accepted']) - set(job['terminal'])
            if not remaining:
                if not job['terminal']:
                    self._handle_publish_success(job, {'successful_destinations': job['accepted']})
                else:
                    self._handle_publish_failure(job, 'One or more destinations rejected the event', terminal=True)
                return
            with self._counter_lock:
                self._publish_attempts += 1
            res = self.result_publisher.publish_sync(job['payload'], prepared_images=job['images'],
                                                     destination_ids=remaining)
            job['accepted'] = sorted(set(job['accepted']) | set(res['successful_destinations']))
            job['terminal'] = sorted(set(job['terminal']) | set(res['terminal_destinations']) |
                                     set(res['disabled_destinations']) | set(res['skipped_destinations']))
            # Deleted destinations must not make an event look delivered.
            reported = set().union(*(set(res[k]) for k in ('successful_destinations',
                'terminal_destinations', 'disabled_destinations', 'skipped_destinations',
                'failed_destinations', 'rate_limited_destinations')))
            job['terminal'] = sorted(set(job['terminal']) | (remaining - reported))
            self._persist_job(job)
            remaining = set(job['targets']) - set(job['accepted']) - set(job['terminal'])
            if not remaining:
                continue
            limited = bool(res['rate_limited_destinations']) and not res['failed_destinations']
            if limited:
                with self._counter_lock:
                    self._publish_rate_limited += 1
                delay = max(0.05, res.get('retry_after') or 0.25)
            else:
                job['attempt'] += 1
                if job['attempt'] >= self.PUBLISH_MAX_RETRIES + 1:
                    self._handle_publish_failure(job, 'Retry budget exhausted')
                    return
                delay = max(min(self.PUBLISH_RETRY_DELAY_SECONDS *
                                self.PUBLISH_RETRY_BACKOFF ** min(job['attempt'] - 1, 20), 30.0),
                            res.get('retry_after') or 0)
                with self._counter_lock:
                    self._publish_retries += 1
            if deferred:
                self._schedule(job, delay)
                return
            if self._interruptible_wait(delay):
                return

    def _interruptible_wait(self, seconds):
        return self._publisher_stop_event.wait(timeout=seconds)

    def _finish_job(self, job, success):
        with self._work_lock:
            if job.get('_complete'):
                return
            job['_complete'] = True
            if job.get('queued_bytes'):
                self._queue_bytes = max(0, self._queue_bytes - job.get('bytes', 0))
            path, group = job.get('source_file'), job.get('file_group')
            if not success and path:
                self._file_failures.add(path)
            if self._outbox and job.get('outbox_saved'):
                if success and not path:
                    self._outbox.remove(job['payload']['event_id'])
                else:
                    self._persist_job(job, 'delivered' if success else 'failed')
                    if not success:
                        self._durable_failures += 1
            if not path:
                return
            if self._outbox:
                records = [r for r in self._outbox.records() if r['job'].get('file_group') == group]
                complete = (len(records) == job.get('file_group_size') and
                            all(r['state'] == 'delivered' for r in records))
            else:
                state = self._file_groups.setdefault(group, {'remaining': job.get('file_group_size', 1), 'failed': False})
                state['remaining'] -= 1
                state['failed'] |= not success
                complete = state['remaining'] == 0 and not state['failed']
                if state['remaining'] == 0:
                    self._file_groups.pop(group, None)
            if complete and path not in self._file_failures:
                try:
                    st = os.stat(path)
                    if [st.st_size, st.st_mtime_ns] == job.get('source_fingerprint'):
                        os.remove(path)
                except FileNotFoundError:
                    pass
                if self._outbox:
                    for record in records:
                        self._outbox.remove(record['job']['payload']['event_id'])

    def _handle_publish_success(self, job, result):
        self._finish_job(job, True)
        det = job['det']
        now = time.time()
        track_key = job.get('track_key')
        class_name = str(det.get('class_name', 'unknown')).lower()
        bbox = det.get('bbox', [])

        with self._tracking_lock:
            if track_key is not None:
                previous = self._track_last_sent.get(track_key, {})
                count = (previous.get('capture_count', 0)
                         if now - previous.get('sent_at', 0) < self.TRACK_TTL_SECONDS else 0)
                self._track_last_sent[track_key] = {
                    'sent_at': now, 'bbox': bbox, 'capture_count': count + 1}
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
                    self._sent_person_track_ids.add(track_key or str(det.get('track_id')))
            unique = len(self._sent_person_track_ids)
            total = self._persons_sent

        self.logger.info(f"PUBLISH_SUCCESS pipeline_id={self.id} pipeline_name={self.pipeline_name} "
                         f"class={class_name} track_id={det.get('track_id')} track_key={track_key} "
                         f"conf={det.get('confidence', 0):.3f} destinations={result.get('successful_destinations')}")
        if class_name == "person":
            msg = (f"[COUNTER] 👤 Persons delivered to all event destinations: {unique} unique ({total} total)")
            print(msg)

    def _handle_publish_failure(self, job, error, terminal=False):
        """Book a failed delivery. terminal=True means retrying can never help
        (payload rejected, destination disabled, or nothing enabled) - the track
        is NOT re-armed, ending the old infinite
        PUBLISH_FAILED -> re-arm -> 60s -> retry loop. Non-terminal failures
        keep today's behaviour: eligible again after FAILED_BACKOFF_SECONDS."""
        self._finish_job(job, False)
        det = job['det']
        now = time.time()
        track_key = job.get('track_key')
        with self._tracking_lock:
            if track_key is not None:
                self._pending_track_keys.discard(track_key)
                if terminal:
                    self._terminal_tracks[track_key] = now
                if not terminal:
                    # Re-eligible after a backoff (not dropped, not hammered)
                    self._failed_backoff[track_key] = now + self.FAILED_BACKOFF_SECONDS
            if job.get('iou_key') is not None:
                self._pending_iou_keys.discard(job['iou_key'])
                self._failed_iou.append({'cls': str(det.get('class_name', '')).lower(),
                                         'bbox': det.get('bbox', []),
                                         'until': now + (self.TRACK_TTL_SECONDS if terminal else self.FAILED_BACKOFF_SECONDS)})
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
    
    # ---- skip-decode (Step 8) --------------------------------------------------------
    # cv2's read() = grab() + retrieve(): grab pulls the next frame off the wire and decodes
    # it; retrieve converts it to a BGR numpy array and copies it into Python. At 25 fps
    # capture against 5 fps inference, 4 frames in 5 are decoded into an array that nothing
    # ever looks at. Measured against a REAL RTSP camera (mediamtx + libx264, 1080p25):
    #     read()                6.45 ms CPU/frame
    #     grab() only           3.14 ms CPU/frame
    #     grab x5 + retrieve x1 3.57 ms CPU/frame   -> 1.81x cheaper capture
    # (On a local video FILE the same test shows 3.84x. RTSP is the honest number: the
    # network + H.264 decode still happen under grab(); only the conversion is skipped.)
    SKIP_DECODE_ENABLED = os.environ.get("ARMYEYE_SKIP_DECODE", "1").strip().lower() \
        not in ("0", "false", "no", "off")

    def _can_skip_decode(self) -> bool:
        """Whether this source can hand back a frame WITHOUT decoding it to an array.

        LIVE sources only, deliberately. A video file paces its own playback inside
        read() (`real_time=True`), so bypassing that would make files race through at
        full speed - files are a testing input and stay on the original path.
        """
        if not self.SKIP_DECODE_ENABLED or not self._skip_decode_supported:
            return False
        if not self._is_live_source():
            return False
        # read() also applies any attached frame processors; the grab/retrieve path
        # bypasses them, so a source with processors stays on the original path.
        if getattr(self.source, "_processors", None):
            return False
        cap = getattr(self.source, "cap", None)
        return cap is not None and hasattr(cap, "grab") and hasattr(cap, "retrieve")

    def _want_decoded_frame(self, now: float) -> bool:
        """Pixels are only worth producing if something will actually read them."""
        if self._inference_enabled and self._due_for_inference(now):
            return True
        if self._is_streaming:                      # a live viewer is watching
            return True
        if not self._thumbnail_captured and self._thumbnail_path:
            return True
        return False

    def _grab_then_maybe_retrieve(self):
        """Advance the stream, then decode ONLY if something will read the pixels.

        Returns (success, frame_or_None, wanted_pixels).

        grab() is called FIRST so the inference gate is evaluated against the moment the
        frame actually arrived. Deciding before the read used the time at the top of the
        iteration - up to a full frame period stale - which pushed every inference onto the
        following frame and cost 18% of the inference rate (measured against a real RTSP
        camera: 4.43 -> 3.62 fps at a 5 fps target).

        On ANY unexpected error this permanently reverts the pipeline to full read(), so a
        surprise in the capture backend can never be mistaken for a dead camera.
        """
        try:
            if not self.source.cap.grab():
                return False, None, True          # a failed grab IS a failed read
            if not self._want_decoded_frame(time.perf_counter()):
                return True, None, False          # advanced the stream, skipped the decode
            ok, raw = self.source.cap.retrieve()
            return bool(ok), (raw if ok else None), True
        except Exception as e:
            self.logger.warning(f"grab/retrieve unusable ({e}); reverting to full read()")
            self._skip_decode_supported = False
            success, frame = self.source.read()
            return success, frame, True

    def _is_live_source(self) -> bool:
        """True for sources that can legitimately drop and come back (cameras).

        A video file reaching its end is NOT a disconnect - reopening it would silently
        restart playback - so files and folders are excluded and keep their existing
        end-of-stream behaviour.
        """
        if not self._frame_source_config:
            return False
        return self._frame_source_config.get('capture_type', '') in (
            'ipcam', 'ip_camera', 'webcam', 'realsense', 'basler', 'genicam')

    def _sleep_interruptible(self, seconds: float) -> bool:
        """Sleep, but wake immediately on stop. Returns False if a stop was requested."""
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            if self._stop_requested:
                return False
            time.sleep(min(0.25, deadline - time.perf_counter()))
        return not self._stop_requested

    def _reconnect_source(self) -> bool:
        """Reconnect a live source with bounded exponential backoff.

        Returns True when reconnected, False when the pipeline should stop (stop requested,
        or the source is not the kind that can be reconnected). Delay doubles 1s -> 30s and
        is then held; attempts are unbounded because a camera may return at any time.
        """
        if not self._is_live_source():
            return False
        delay = min(self.RECONNECT_INITIAL_DELAY * (2 ** min(self._reconnect_attempts, 30)),
                    self.RECONNECT_MAX_DELAY)
        self._reconnect_attempts += 1
        self.logger.warning(
            f"Source unavailable - reconnect attempt {self._reconnect_attempts} in {delay:.0f}s")
        if not self._sleep_interruptible(delay):
            return False
        try:
            try:
                self._disconnect_source()          # release the old handle before reopening
            except Exception:
                pass
            self.source.connect()
            if self.source.isOpened():
                reset = getattr(getattr(self, 'inference_engine', None), 'reset_tracking', None)
                if reset:
                    reset()
                    self._tracking_epoch += 1
                # NOTE: do NOT reset the backoff here. cv2.VideoCapture reports a dead RTSP
                # stream as "opened", so treating this as success made the backoff restart
                # at 1s forever (measured: 78 reconnects in 200s against a dead camera).
                # The counter is reset only when a frame is actually READ, which is the
                # only real evidence the camera is back.
                self.logger.info(
                    f"Source handle reopened (attempt {self._reconnect_attempts}) - "
                    f"awaiting frames")
                return True
            self.logger.warning("Reconnect attempt did not open the source")
        except Exception as e:
            self.logger.warning(f"Reconnect attempt failed: {e.__class__.__name__}: {e}")
        return not self._stop_requested     # keep trying unless we are shutting down

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
   
    def _disconnect_source(self):
        """Release modern frame sources, retaining compatibility with older adapters."""
        source = getattr(self, 'source', None)
        if source is None:
            return
        disconnect = getattr(source, 'disconnect', None)
        if not callable(disconnect):
            disconnect = getattr(source, 'stop', None)
        if not callable(disconnect):
            raise TypeError('Frame source provides neither disconnect() nor stop()')
        return disconnect()

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
                        # A live camera dropping is routine (network blip, PoE reset,
                        # reboot). Retry with backoff instead of ending the pipeline;
                        # _reconnect_source returns False only when stopping or when the
                        # source is a file/folder, where end-of-stream really is terminal.
                        if self._reconnect_source():
                            continue
                        self.logger.warning("Source disconnected, ending pipeline")
                        break

                # Read frame (timed: a read that returns instantly while the source is
                # live means we are draining a buffered backlog, i.e. stale frames)
                # A grabbed-but-not-decoded frame still drains the socket, so the pipeline
                # stays at the live edge exactly as before; only the BGR conversion and copy
                # of a frame nothing will look at are skipped.
                _read_t0 = time.perf_counter()
                if self._can_skip_decode():
                    success, frame, _decode = self._grab_then_maybe_retrieve()
                else:
                    _decode = True
                    success, frame = self.source.read()
                self._last_read_wait_ms = (time.perf_counter() - _read_t0) * 1000.0
                # A frame is only MISSING if we asked for pixels and did not get them.
                # A deliberately-grabbed frame has no array by design; treating that as a
                # failed read made every skipped frame sleep on the failure path and
                # collapsed capture from 25 fps to 4 fps against a real RTSP camera.
                if not success or (_decode and frame is None):
                    self._failed_read_count += 1
                    consecutive_empty_reads += 1

                    if is_folder_source:
                        if consecutive_empty_reads >= max_empty_reads_before_sleep:
                            self.logger.debug("No files in folder, entering wait mode")
                            time.sleep(0.1)
                            consecutive_empty_reads = 0
                        continue
                    else:
                        # Never spin: a dead source used to burn a core here returning
                        # instantly forever. Sleep briefly, and once failures persist treat
                        # it as a disconnect and reconnect (live sources only).
                        if consecutive_empty_reads >= self.FAILED_READS_BEFORE_RECONNECT:
                            if self._reconnect_source():
                                consecutive_empty_reads = 0
                                continue
                            if self._is_live_source():
                                break                      # stopping
                            self.logger.info("End of stream - ending pipeline")
                            break
                        if not self._sleep_interruptible(self.FAILED_READ_SLEEP):
                            break
                        continue

                if is_folder_source and self._should_auto_delete_images():
                    path = self.source.get_current_file_path()
                    if path:
                        st = os.stat(path)
                        if self._file_seen.get(path) == [st.st_size, st.st_mtime_ns]:
                            continue

                # Frame successfully read - the ONLY proof the source is genuinely back,
                # so this is where the reconnect backoff resets.
                if self._reconnect_attempts:
                    self.logger.info(
                        f"Source recovered after {self._reconnect_attempts} reconnect attempt(s)")
                consecutive_empty_reads = 0
                self._reconnect_attempts = 0
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

                # Run inference if enabled AND this frame is due under the target rate.
                # The frame has already been read, so gating here keeps the pipeline at the
                # live edge; a skipped frame still refreshes the preview below.
                results = None
                # `frame is None` means this one was grabbed, not decoded. The gate is
                # re-checked here against post-read time, so it CAN come due on a frame we
                # chose to skip - inferring on None would crash the pipeline.
                if (frame is not None and self._inference_enabled
                        and (is_folder_source or self._due_for_inference(now_perf))):
                    self._mark_inferred(now_perf)
                    t0 = time.perf_counter()
                    if is_folder_source:
                        reset = getattr(self.inference_engine, 'reset_tracking', None)
                        if reset:
                            reset()
                    results = self.inference_engine.infer(frame)
                    self._tracking_epoch = getattr(self.inference_engine, "tracking_epoch", self._tracking_epoch)
                    if isinstance(results, dict) and results.get('success') is False:
                        self._error_state = results.get('error', 'Inference failed')
                        results = None
                    elif results is not None:
                        self._error_state = None
                    latency_ms = (time.perf_counter() - t0) * 1000

                    self._inference_latencies.append(latency_ms)
                    if len(self._inference_latencies) > self._latency_window_size:
                        self._inference_latencies.pop(0)

                    self._inference_counter += 1

                # Handle frame storage for streaming
                if results is not None:
                    from InferenceEngine.detection_contract import normalize_result
                    json_results = normalize_result(self.inference_engine.result_to_json(results))
                    # print(f"Pipeline {self.id}: Inference results: {json.dumps(json_results)}")

                    if self._is_streaming:
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
                    # GATED frame: no inference ran, so there is nothing drawn on it.
                    # Since Step 3 this branch fires at CAPTURE rate while the two above
                    # fire at inference rate, making it the hottest copy in the system -
                    # measured 0.25 ms and 5.9 MB per 1080p frame, i.e. ~4.35 GB/s of
                    # memory bandwidth at 750 frames/s, and ~17 GB/s at the 120-camera
                    # target. Copying is only worth it when something actually reads
                    # _latest_frame:
                    #   * a live viewer  -> start_streaming() sets _is_streaming BEFORE
                    #     the preview polls get_latest_frame(), so this flag is a reliable
                    #     "someone is watching" signal
                    #   * the one-time thumbnail
                    # Event annotations are generated independently from the selected
                    # event snapshot; preview refreshes cannot change webhook images.
                    if frame is not None and (self._is_streaming
                                              or (not self._thumbnail_captured and self._thumbnail_path)):
                        with self._frame_lock:
                            self._latest_frame = frame.copy()

                            if not self._thumbnail_captured and self._thumbnail_path:
                                self.capture_thumbnail(frame)

                # Process detections. Selection happens on the inference thread;
                # actual network delivery is handed to the background publisher worker
                # so HTTP retries never execute on the inference thread.
                if results is not None:
                    # Periodic maintenance + counter heartbeat
                    if self._frame_counter - self._last_cleanup_frame >= self._cleanup_interval:
                        self._cleanup_tracks()
                        self._last_cleanup_frame = self._frame_counter
                        with self._counter_lock:
                            unique, total = len(self._sent_person_track_ids), self._persons_sent
                        print(f"[COUNTER] 👤 Persons delivered to all event destinations so far: {unique} unique "
                              f"({total} total) | queue={self._publish_queue.qsize()}")

                    # Support both "detections" and "predictions" keys
                    all_detections = json_results["predictions"]

                    now = time.time()
                    iou_jobs = []
                    source_file, fingerprint = None, None
                    if is_folder_source and self._should_auto_delete_images():
                        source_file = self.source.get_current_file_path()
                        if source_file:
                            st = os.stat(source_file)
                            fingerprint = [st.st_size, st.st_mtime_ns]
                            self._file_seen[source_file] = fingerprint
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

                        if is_folder_source:
                            iou_jobs.append({'track_key': None, 'det': det, 'frame': self._snapshot(frame),
                                             'first_seen': now, 'captured_at': self._last_capture_wall})
                        elif det.get("track_id") is not None:
                            self._update_track_candidate(det, frame, now)
                        else:
                            job = self._register_iou_candidate(det, frame, now)
                            if job is not None:
                                iou_jobs.append(job)

                    # 2) After all detections processed, collect tracks ready to send.
                    #    Runs even when all_detections == [] (person left the frame).
                    ready_jobs = self._collect_ready_tracks(now)

                    # 3) Preserve file ownership until all its events succeed.
                    file_group = uuid.uuid4().hex
                    if is_folder_source and source_file:
                        self._file_groups[file_group] = {'remaining': len(iou_jobs), 'failed': False}
                        for file_job in iou_jobs:
                            file_job.update(source_file=source_file, source_fingerprint=fingerprint,
                                            file_group=file_group, file_group_size=len(iou_jobs))
                    for job in ready_jobs + iou_jobs:
                        job['json_results'] = json_results
                        if job.get('frame') is None:
                            job['frame'] = frame
                        self.logger.info(f"TRACK_READY pipeline_id={self.id} class={job['det'].get('class_name')} "
                                         f"track_id={job['det'].get('track_id')} conf={job['det'].get('confidence', 0):.3f}")
                        self._enqueue_publish_job(job)

                    # No accepted detections: an analyzed file is complete locally.
                    if is_folder_source and not iou_jobs:
                        self._delete_current_image()

        except Exception as e:
            self._error_state = str(e)
            self._inference_enabled = False
            self.logger.error(f"Pipeline error: {e}", exc_info=True)
            print(f"Pipeline {self.id} ERROR: {e}")

        finally:
            self._flush_and_stop_publisher()
            if self._publisher_thread and self._publisher_thread is not threading.current_thread():
                self._publisher_thread.join()
            for destination in getattr(self.result_publisher, 'destinations', ()):
                close = getattr(destination, 'close', None)
                if close:
                    try:
                        close()
                    except Exception:
                        self.logger.warning("Failed to close publisher destination", exc_info=True)
            self._is_running = False
            if self.source:
                self._disconnect_source()
            self.logger.info("Pipeline stopped")
            print(f"Pipeline {self.id}: Stopped")


    @staticmethod
    def _env_target_fps() -> float:
        """Node-wide default target inference rate. An invalid value is ignored (0 = every
        frame) and logged, rather than silently changing how much of the stream is analysed."""
        raw = (os.environ.get("ARMYEYE_TARGET_INFERENCE_FPS") or "").strip()
        if not raw:
            return 0.0
        try:
            v = float(raw)
        except ValueError:
            logging.getLogger(__name__).warning(
                f"Invalid ARMYEYE_TARGET_INFERENCE_FPS={raw!r} - ignoring (inferring every frame)")
            return 0.0
        if not math.isfinite(v) or v < 0:
            logging.getLogger(__name__).warning(
                f"Negative ARMYEYE_TARGET_INFERENCE_FPS={v} - ignoring (inferring every frame)")
            return 0.0
        return v

    def _mark_inferred(self, now: float) -> None:
        """Record an inference WITHOUT letting the schedule drift.

        Anchoring to the actual inference time bakes that frame's read and scheduling
        overhead into the next deadline, so the frame that should trigger it misses by a
        few milliseconds and the cycle slips to the FOLLOWING frame. Because inference can
        only happen when a frame arrives, the achievable rates are stream_fps/n - at 25 fps
        that is 25/5 = 5.00 or 25/6 = 4.17, with nothing in between. Slipping one frame
        therefore costs 17% of the requested rate, and it slipped every time: measured
        4.17 fps against a 5 fps target on 60 real RTSP cameras.

        Advancing the anchor by exactly one period keeps the schedule aligned to the
        original start instead of compounding per-cycle overhead.

        The re-anchor below is what still guarantees no catch-up burst: if we are already a
        full period behind (a stall, a reconnect, a slow model load), the schedule is
        abandoned and restarted from now rather than firing repeatedly to "catch up".
        """
        period = (1.0 / self.TARGET_INFERENCE_FPS) if self.TARGET_INFERENCE_FPS > 0 else 0.0
        if self._last_inference_at is None or period <= 0.0:
            self._last_inference_at = now
            return
        self._last_inference_at += period
        if now - self._last_inference_at >= period:
            self._last_inference_at = now          # behind schedule: re-anchor, never burst

    def _due_for_inference(self, now: float) -> bool:
        """True when this frame should be inferred.

        Deliberately NOT a scheduler: it never queues, never sleeps and only ever considers
        the frame in hand, so a skipped frame is dropped immediately and no backlog of stale
        frames can accumulate. The schedule advances by one period and re-anchors after a stall.
        """
        if self.TARGET_INFERENCE_FPS <= 0:
            return True                                   # 0 = infer every frame
        if self._last_inference_at is None:
            return True                                   # first frame after start
        # 1 microsecond of boundary tolerance. Frame timestamps that land exactly on an
        # interval boundary lose to floating-point representation (0.6 - 0.4 is
        # 0.19999999999999996, which is < 0.2), pushing that inference to the NEXT frame and
        # silently running below the requested rate - measurably so when the target divides
        # the stream rate: 10 fps from a 30 fps camera yielded 25 inferences instead of 30.
        return (now - self._last_inference_at) >= (1.0 / self.TARGET_INFERENCE_FPS) - 1e-6

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
            if not math.isfinite(v):
                self.logger.warning("Non-finite %s rejected", key)
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
        self.PERSON_CAPTURE_COUNT = int(num('person_capture_count', self.PERSON_CAPTURE_COUNT, 1, 3))
        self.PERSON_CAPTURE_INTERVAL_SECONDS = num(
            'person_capture_interval_seconds', self.PERSON_CAPTURE_INTERVAL_SECONDS, 1.0)
        self.TRACK_LOST_TIMEOUT_SECONDS = num('track_lost_timeout_seconds', self.TRACK_LOST_TIMEOUT_SECONDS, 0.0)
        self.PUBLISH_MAX_RETRIES = int(num('publish_max_retries', self.PUBLISH_MAX_RETRIES, 0, 20))
        self.PUBLISH_RETRY_DELAY_SECONDS = num('publish_retry_delay_seconds', self.PUBLISH_RETRY_DELAY_SECONDS, 0.0, 30.0)
        self.PUBLISH_RETRY_BACKOFF = num('publish_retry_backoff', self.PUBLISH_RETRY_BACKOFF, 1.0, 10.0)
        self.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS = num('publisher_shutdown_timeout_seconds', self.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS, 0.0)
        self.PUBLISH_QUEUE_SIZE = int(num('publish_queue_size', self.PUBLISH_QUEUE_SIZE, 1, 10000))
        self.PUBLISH_QUEUE_BYTES = int(num('publish_queue_bytes', getattr(self, 'PUBLISH_QUEUE_BYTES', 64 * 1024 * 1024), 1024, 1024**3))
        self.PUBLISH_MAX_AGE_SECONDS = num('publish_max_age_seconds', getattr(self, 'PUBLISH_MAX_AGE_SECONDS', 300), 1, 3600)
        # Capped at 1000: a target above any real camera rate means 'every frame' anyway,
        # and a typo like 5000 should not read as a meaningful setting.
        self.TARGET_INFERENCE_FPS = num('target_inference_fps', self.TARGET_INFERENCE_FPS, 0.0, 1000.0)

    def configure(self, frame_source_config, inference_engine_config, result_publisher: ResultPublisher, detection_config=None):

        self._frame_source_config = frame_source_config  # Store for auto-delete functionality
        from InferenceNode.capture_options import configure_ip_capture
        self.source = configure_ip_capture(FrameSourceFactory.create(**frame_source_config), frame_source_config)

        self.inference_engine_config = inference_engine_config
        self.inference_engine = InferenceEngineFactory.create(**inference_engine_config)
        if not self.inference_engine.load():
            self._disconnect_source()
            raise RuntimeError('Inference model failed to load')

        self.result_publisher = result_publisher

        # Optional detection/publisher overrides from the pipeline config (validated)
        if detection_config:
            self._apply_detection_config(detection_config)

        root = os.environ.get('ARMYEYE_ARTIFACT_ROOT')
        if root and os.getenv('ARMYEYE_OUTBOX_ENABLED', 'true').lower() in ('1', 'true', 'yes'):
            from InferenceNode.event_outbox import EventOutbox
            self._outbox = EventOutbox(root, self.id)

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
        
        if (self._publisher_thread and self._publisher_thread.is_alive()) or (hasattr(self, "thread") and self.thread.is_alive()):
            raise RuntimeError("Previous pipeline workers are still stopping")
        if self._is_running:
            print(f"Pipeline {self.id} is already running")
            return
        
        self._start_time = time.perf_counter()  # Record the start time
        self._stop_requested = False  # Reset stop flag
        
        # Reset FPS tracking and counters
        self._frame_timestamps = []
        self._frame_counter = 0
        self._inference_counter = 0
        self._last_inference_at = None  # first frame after start is always inferred
        self._inference_latencies = []  # Reset latency tracking
        
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def stop(self):
        """
        Stop the inference pipeline.
        """
        print(f"Stopping pipeline {self.id}")
        self._stop_requested = True  # Signal the run loop to stop
        with self._frame_lock:
            self._viewer_count = 0
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
        
        self._candidate_stop.set()
        # Give the inference thread some time to stop gracefully
        if hasattr(self, 'thread') and self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)  # Wait up to 5 seconds
            if self.thread.is_alive():
                self.logger.warning("Pipeline thread is still stopping; retaining runtime ownership")
                return

        # Graceful publisher shutdown: flush remaining candidates, drain the queue,
        # then stop the worker so in-flight/unique events aren't lost.
        self._flush_and_stop_publisher()

        # Ensure source is stopped
        if hasattr(self, 'source') and self.source:
            try:
                self._disconnect_source()
            except Exception as e:
                print(f"Error stopping source: {e}")

        # Mark as not running (thread will also set this in finally block)
        self._is_running = False

        print(f"Pipeline {self.id} stopped")

    def _flush_and_stop_publisher(self):
        with self._shutdown_lock:
            self._candidate_stop.set()
            if self._candidate_thread and self._candidate_thread is not threading.current_thread():
                self._candidate_thread.join(timeout=2)
            if not self._publisher_thread or not self._publisher_thread.is_alive():
                return
            # Mark all candidates ready without discarding their selected frame.
            with self._tracking_lock:
                for entry in self._track_best.values():
                    entry['last_improved'] = 0
            for job in self._collect_ready_tracks(time.time()):
                self._enqueue_publish_job(job)
            deadline = time.monotonic() + self.PUBLISHER_SHUTDOWN_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                with self._work_lock:
                    busy = self._inflight or self._retry_jobs or not self._publish_queue.empty()
                if not busy:
                    break
                time.sleep(.05)
            self._publisher_stop_event.set()
            self._publisher_thread.join(timeout=2)
            with self._work_lock:
                remaining = self._inflight + len(self._retry_jobs) + self._publish_queue.qsize()
            if remaining:
                self.logger.warning('%s unfinished events at shutdown; durable outbox=%s', remaining, bool(self._outbox))
            self._snapshot_source = self._snapshot_frame = None
            with self._preview_lock:
                self._preview_cache.clear()

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
                'queue_size': self._publish_queue.qsize() + len(self._retry_jobs),
                'inflight_events': self._inflight,
                'queue_bytes': self._queue_bytes,
                'durable_failed_events': self._durable_failures,
            }

    def get_latest_frame(self):
        """Get the latest processed frame for streaming"""
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def get_preview_jpeg(self, width=640, quality=70):
        # Serialize one encode per frame/quality across viewers. Producer only swaps arrays.
        with self._preview_lock:
            with self._frame_lock:
                frame = self._latest_frame
            if frame is None:
                return None
            key = (width, quality)
            cached = self._preview_cache.get(key)
            if cached and cached[0] is frame:
                return cached[1]
            h, w = frame.shape[:2]
            preview = cv2.resize(frame, (width, max(1, int(h * width / w))),
                                 interpolation=cv2.INTER_AREA) if w > width else frame
            ok, buf = cv2.imencode('.jpg', preview, [cv2.IMWRITE_JPEG_QUALITY, quality,
                                                    cv2.IMWRITE_JPEG_OPTIMIZE, 1])
            if not ok:
                return None
            encoded = buf.tobytes()
            self._preview_cache[key] = (frame, encoded)
            return encoded

    def start_streaming(self):
        with self._frame_lock:
            self._viewer_count += 1
            self._is_streaming = self._viewer_count > 0

    def stop_streaming(self):
        with self._frame_lock:
            self._viewer_count = max(0, self._viewer_count - 1)
            self._is_streaming = self._viewer_count > 0

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
