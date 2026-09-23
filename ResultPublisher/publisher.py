import base64
import threading
import logging
import uuid
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
import copy

import cv2
import numpy as np
from .result_destinations import BaseResultDestination

class ResultPublisher:
    """Main result publisher that manages multiple destinations"""
    
    def __init__(self, max_workers: int = 4):
        self.destinations: List[BaseResultDestination] = []
        self.logger = logging.getLogger(self.__class__.__name__)
        self._lock = threading.Lock()
        # Thread pool for non-blocking publishing
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ResultPublisher")
        self._shutdown = False
        self._slots = threading.BoundedSemaphore(max_workers * 4)
    
    def add(self, destination: BaseResultDestination) -> str:
        """Add a result destination and return its ID"""
        with self._lock:
            # Use existing ID if available, otherwise generate a new one
            if hasattr(destination, '_id') and destination._id:
                destination_id = destination._id
            else:
                destination_id = str(uuid.uuid4())
                destination._id = destination_id  # Add ID attribute to destination
            
            self.destinations.append(destination)
            self.logger.info(f"Added destination: {destination.__class__.__name__} with ID: {destination_id}")
            return destination_id
    
    def remove(self, destination: BaseResultDestination) -> None:
        """Remove a result destination"""
        with self._lock:
            if destination in self.destinations:
                self.destinations.remove(destination)
                self.logger.info(f"Removed destination: {destination.__class__.__name__}")
    
    def remove_by_id(self, destination_id: str) -> bool:
        """Remove a destination by its ID"""
        with self._lock:
            for destination in self.destinations:
                if hasattr(destination, '_id') and str(destination._id) == str(destination_id):
                    self.destinations.remove(destination)
                    self.logger.info(f"Removed destination with ID: {destination_id}")
                    return True
            return False
    
    def get_by_id(self, destination_id: str) -> Optional[BaseResultDestination]:
        """Get a destination by its ID"""
        with self._lock:
            for destination in self.destinations:
                if hasattr(destination, '_id') and str(destination._id) == str(destination_id):
                    return destination
            return None
    
    def _publish_to_destination(self, destination: BaseResultDestination, data: Dict[str, Any]) -> bool:
        """Helper method to publish to a single destination"""
        try:
            return destination.publish(data)
        except Exception as e:
            self.logger.error(f"Error publishing to {destination.__class__.__name__}: {str(e)}")
            return False
        
    def do_any_destinations_need_image(self) -> bool:
        """Check if any destination needs image data"""
        with self._lock:
            return any(getattr(dest, 'enabled', True) and not getattr(dest, 'is_paused', False)
                       and getattr(dest, 'is_configured', True) and dest.include_image_data for dest in self.destinations)

    def do_any_destinations_need_result_image(self) -> bool:
        """Check if any destination needs result image data"""
        with self._lock:
            return any(getattr(dest, 'enabled', True) and not getattr(dest, 'is_paused', False)
                       and getattr(dest, 'is_configured', True) and dest.include_result_image for dest in self.destinations)

    def publish(self, data: Dict[str, Any], 
                original_image: Optional[np.ndarray] = None, 
                result_image: Optional[np.ndarray] = None) -> None:
        """Publish data to all configured destinations (non-blocking)"""
        if self._shutdown:
            self.logger.warning("Publisher is shutting down, ignoring publish request")
            return
        
        try:
            images = self.prepare_images(original_image, result_image)
        except (ValueError, cv2.error):
            self.logger.error('Legacy event rejected: required image encoding failed')
            return
        encoded_image = images.get('image')
        encoded_result_image = images.get('result_image')

        # Submit publishing tasks to thread pool
        with self._lock:
            # Only publish to enabled destinations that are not paused
            enabled_destinations = [dest for dest in self.destinations 
                                  if getattr(dest, 'enabled', True) and not getattr(dest, 'is_paused', False)]
        
        for destination in enabled_destinations:
            # Each destination owns its payload; image flags cannot leak.
            dest_data = copy.deepcopy(data)
            # Prepare data for this destination
            if encoded_image is not None and destination.include_image_data:
                dest_data["image"] = encoded_image

            if encoded_result_image is not None and destination.include_result_image:
                dest_data["result_image"] = encoded_result_image
            
            # Submit to thread pool
            self.logger.debug("Submitting event to %s", destination.__class__.__name__)
            if not self._slots.acquire(blocking=False):
                self.logger.warning("Legacy publisher saturated; event not submitted")
                continue
            try:
                future = self._executor.submit(self._publish_to_destination, destination, dest_data)
            except Exception:
                self._slots.release()
                raise
            future.add_done_callback(lambda _: self._slots.release())
            
            # Optionally add a callback for logging results
            def log_result(fut, dest_name=destination.__class__.__name__):
                try:
                    success = fut.result()
                    if success:
                        self.logger.debug("Destination accepted event: %s", dest_name)
                    else:
                        self.logger.debug(f"Failed to publish to {dest_name}")
                        print(f"[PUBLISH] ✗ Failed to publish to {dest_name}")
                except Exception as e:
                    self.logger.error(f"Unexpected error in publishing task for {dest_name}: {str(e)}")
                    print(f"[PUBLISH] ✗ Unexpected error in publishing task for {dest_name}: {str(e)}")
            
            future.add_done_callback(log_result)
    
    def destination_ids(self):
        with self._lock:
            return [str(getattr(d, '_id', d.__class__.__name__)) for d in self.destinations
                    if d.enabled and not getattr(d, 'is_paused', False) and getattr(d, 'is_configured', True)]

    @staticmethod
    def encode_image(frame):
        if frame is None:
            raise ValueError('Required event image is missing')
        ok, buffer = cv2.imencode('.jpg', frame)
        if not ok:
            raise ValueError('Event JPEG encoding failed')
        return base64.b64encode(buffer.tobytes()).decode('ascii')

    def prepare_images(self, original_image=None, result_image=None):
        images = {}
        if self.do_any_destinations_need_image():
            images['image'] = self.encode_image(original_image)
        if self.do_any_destinations_need_result_image():
            images['result_image'] = self.encode_image(result_image)
        return images

    def publish_sync(self, data: Dict[str, Any],
                     original_image: Optional[np.ndarray] = None,
                     result_image: Optional[np.ndarray] = None, *, prepared_images=None,
                     destination_ids=None) -> Dict[str, Any]:
        """Publish data to all enabled destinations SYNCHRONOUSLY and return a
        structured, aggregated delivery result.

        Unlike publish() (fire-and-forget via a thread pool), this blocks until
        every enabled destination has made a single attempt, so the caller gets
        real confirmation of delivery. Each destination makes exactly one attempt
        (via publish_once); retries/rate-limit waiting are the caller's job.

        Returns:
            {
              "success": bool,   # True if all selected destinations accepted
              "successful_destinations": [id, ...],
              "failed_destinations": [id, ...],       # retryable failures only
              "terminal_destinations": [id, ...],     # 400/413/422-class: THIS delivery
                                                      #   can never succeed there - do not
                                                      #   retry it (destination stays enabled)
              "disabled_destinations": [id, ...],     # destination got disabled by this
                                                      #   attempt (auth/route/max_failures)
              "rate_limited_destinations": [id, ...],
              "skipped_destinations": [id, ...],   # disabled/paused/unconfigured
              "errors": {id: message, ...},
              "attempted": int,  # number of enabled destinations attempted
              "retry_after": float|None,  # largest backpressure hint seen (seconds)
            }
        """
        result = {
            "success": False,
            "successful_destinations": [],
            "failed_destinations": [],
            "terminal_destinations": [],
            "disabled_destinations": [],
            "rate_limited_destinations": [],
            "skipped_destinations": [],
            "errors": {},
            "processing_outcomes": {},
            "attempted": 0,
            "retry_after": None,
        }

        if self._shutdown:
            self.logger.warning("Publisher is shutting down, ignoring publish_sync request")
            return result

        dest_data = copy.deepcopy(data)
        with self._lock:
            destinations = list(self.destinations)
        images = prepared_images
        image_error = None
        # Pipeline supplies immutable cached encodings. Legacy callers encode only once.
        if images is None:
            try:
                images = self.prepare_images(original_image, result_image)
            except (ValueError, cv2.error) as exc:
                images, image_error = {}, str(exc)

        for destination in destinations:
            dest_id = str(getattr(destination, '_id', destination.__class__.__name__))

            if destination_ids is not None and dest_id not in destination_ids:
                continue
            if not getattr(destination, 'enabled', True) or getattr(destination, 'is_paused', False):
                result["skipped_destinations"].append(dest_id)
                continue

            # Per-destination payload with only the images it asked for
            payload = dict(dest_data)
            missing = False
            for key, needed in (('image', destination.include_image_data),
                                ('result_image', destination.include_result_image)):
                payload.pop(key, None)
                if needed:
                    if not images.get(key):
                        missing = True
                    else:
                        payload[key] = images[key]
            if missing:
                result['terminal_destinations'].append(dest_id)
                result['errors'][dest_id] = image_error or 'Required event image unavailable'
                result['attempted'] += 1
                continue

            result["attempted"] += 1
            try:
                outcome = destination.publish_once(payload)
            except Exception as e:
                outcome = {"status": "failed", "error": str(e)}

            status = outcome.get("status")
            if status == "success":
                result["successful_destinations"].append(dest_id)
                result['processing_outcomes'][dest_id] = outcome.get('outcome')
            elif status == "rate_limited":
                result["rate_limited_destinations"].append(dest_id)
            elif status in ("disabled", "paused", "unconfigured"):
                result["skipped_destinations"].append(dest_id)
                if outcome.get("error"):
                    result["errors"][dest_id] = outcome["error"]
            elif status == "permanent_failure":
                # 400/413/422-class verdict: retrying this exact payload cannot
                # succeed; the destination itself stays enabled for future events.
                result["terminal_destinations"].append(dest_id)
                result["errors"][dest_id] = outcome.get("error") or "payload rejected"
            else:  # failed
                result["failed_destinations"].append(dest_id)
                result["errors"][dest_id] = outcome.get("error") or "unknown error"
                # The attempt's _account may have just disabled the destination
                # (401/403/404 or max_failures). Surface that so the caller stops
                # retrying instead of sleeping through attempts that can't send.
                if not getattr(destination, 'enabled', True):
                    result["disabled_destinations"].append(dest_id)

            # Largest backpressure hint wins - the caller uses it as a backoff floor
            hint = outcome.get("retry_after")
            if hint and (result["retry_after"] is None or hint > result["retry_after"]):
                result["retry_after"] = hint

        # A single attempt is complete only when all selected destinations accepted.
        result["success"] = bool(result["successful_destinations"]) and not any(
            result[k] for k in ("failed_destinations", "terminal_destinations",
                               "rate_limited_destinations", "skipped_destinations"))
        return result

    def get_destinations(self) -> List[str]:
        """Get list of configured destination types"""
        with self._lock:
            return [dest.__class__.__name__ for dest in self.destinations]
    
    def shutdown(self, wait: bool = True, timeout: float = 30.0) -> None:
        """Shutdown the publisher and wait for pending tasks to complete"""
        self.logger.info("Shutting down ResultPublisher...")
        self._shutdown = True
        
        if wait:
            # Submit a dummy task to help with graceful shutdown timing
            try:
                self._executor.shutdown(wait=True)
            except Exception as e:
                self.logger.warning(f"Exception during executor shutdown: {e}")
        else:
            # Cancel pending futures and shutdown immediately
            try:
                # Try to cancel pending futures if supported
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Fallback for older Python versions
                self._executor.shutdown(wait=False)
        
        self.logger.info("ResultPublisher shutdown complete")
    
    def clear(self) -> None:
        """Remove all destinations"""
        with self._lock:
            # Close any resources that need cleanup
            for destination in self.destinations:
                if hasattr(destination, 'close'):
                    try:
                        destination.close()
                    except Exception as e:
                        self.logger.error(f"Error closing {destination.__class__.__name__}: {str(e)}")
            
            self.destinations.clear()
            self.logger.info("All destinations cleared")
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures proper cleanup"""
        self.shutdown(wait=True)
