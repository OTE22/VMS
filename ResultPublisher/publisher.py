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
            return any(getattr(dest, 'enabled', True) and dest.include_image_data for dest in self.destinations)

    def do_any_destinations_need_result_image(self) -> bool:
        """Check if any destination needs result image data"""
        with self._lock:
            return any(getattr(dest, 'enabled', True) and dest.include_result_image for dest in self.destinations)

    def publish(self, data: Dict[str, Any], 
                original_image: Optional[np.ndarray] = None, 
                result_image: Optional[np.ndarray] = None) -> None:
        """Publish data to all configured destinations (non-blocking)"""
        if self._shutdown:
            self.logger.warning("Publisher is shutting down, ignoring publish request")
            return
        
        # Create a deep copy of data to avoid race conditions
        dest_data = copy.deepcopy(data)
        
        # Encode image once if any destination needs it
        encoded_image = None
        if original_image is not None:
            success, buffer = cv2.imencode('.jpg', original_image)
            if success:
                encoded_image = base64.b64encode(buffer.tobytes()).decode('utf-8')

        # Similarly encode result image if needed
        encoded_result_image = None
        if result_image is not None:
            success, buffer = cv2.imencode('.jpg', result_image)
            if success:
                encoded_result_image = base64.b64encode(buffer.tobytes()).decode('utf-8')

        # Submit publishing tasks to thread pool
        with self._lock:
            # Only publish to enabled destinations that are not paused
            enabled_destinations = [dest for dest in self.destinations 
                                  if getattr(dest, 'enabled', True) and not getattr(dest, 'is_paused', False)]
        
        for destination in enabled_destinations:
            # Prepare data for this destination
            if encoded_image is not None and destination.include_image_data:
                dest_data["image"] = encoded_image

            if encoded_result_image is not None and destination.include_result_image:
                dest_data["result_image"] = encoded_result_image
            
            # Submit to thread pool
            print(f"submitting dest_data {dest_data.keys()} to {destination.__class__.__name__}")
            future = self._executor.submit(self._publish_to_destination, destination, dest_data)
            
            # Optionally add a callback for logging results
            def log_result(fut, dest_name=destination.__class__.__name__):
                try:
                    success = fut.result()
                    if success:
                        print(f"[PUBLISH] ✓ Successfully sent POST to {dest_name}")
                    else:
                        self.logger.debug(f"Failed to publish to {dest_name}")
                        print(f"[PUBLISH] ✗ Failed to publish to {dest_name}")
                except Exception as e:
                    self.logger.error(f"Unexpected error in publishing task for {dest_name}: {str(e)}")
                    print(f"[PUBLISH] ✗ Unexpected error in publishing task for {dest_name}: {str(e)}")
            
            future.add_done_callback(log_result)
    
    def publish_sync(self, data: Dict[str, Any],
                     original_image: Optional[np.ndarray] = None,
                     result_image: Optional[np.ndarray] = None) -> Dict[str, Any]:
        """Publish data to all enabled destinations SYNCHRONOUSLY and return a
        structured, aggregated delivery result.

        Unlike publish() (fire-and-forget via a thread pool), this blocks until
        every enabled destination has made a single attempt, so the caller gets
        real confirmation of delivery. Each destination makes exactly one attempt
        (via publish_once); retries/rate-limit waiting are the caller's job.

        Returns:
            {
              "success": bool,   # True if at least one enabled destination accepted
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
            "attempted": 0,
            "retry_after": None,
        }

        if self._shutdown:
            self.logger.warning("Publisher is shutting down, ignoring publish_sync request")
            return result

        # Deep copy so per-destination image injection can't race across threads
        dest_data = copy.deepcopy(data)

        # Encode images once, reused across destinations that want them
        encoded_image = None
        if original_image is not None:
            ok, buffer = cv2.imencode('.jpg', original_image)
            if ok:
                encoded_image = base64.b64encode(buffer.tobytes()).decode('utf-8')

        encoded_result_image = None
        if result_image is not None:
            ok, buffer = cv2.imencode('.jpg', result_image)
            if ok:
                encoded_result_image = base64.b64encode(buffer.tobytes()).decode('utf-8')

        with self._lock:
            destinations = list(self.destinations)

        for destination in destinations:
            dest_id = str(getattr(destination, '_id', destination.__class__.__name__))

            if not getattr(destination, 'enabled', True) or getattr(destination, 'is_paused', False):
                result["skipped_destinations"].append(dest_id)
                continue

            # Per-destination payload with only the images it asked for
            payload = dict(dest_data)
            if encoded_image is not None and destination.include_image_data:
                payload["image"] = encoded_image
            if encoded_result_image is not None and destination.include_result_image:
                payload["result_image"] = encoded_result_image

            result["attempted"] += 1
            try:
                outcome = destination.publish_once(payload)
            except Exception as e:
                outcome = {"status": "failed", "error": str(e)}

            status = outcome.get("status")
            if status == "success":
                result["successful_destinations"].append(dest_id)
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

        # Success = at least one enabled destination accepted the event
        result["success"] = len(result["successful_destinations"]) >= 1
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
