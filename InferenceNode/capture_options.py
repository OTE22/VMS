"""Apply bounded, credential-safe opening only to OpenCV IP-camera sources."""
import logging
import math
from types import MethodType

import cv2


def configure_ip_capture(source, config):
    if config.get('capture_type') not in ('ipcam', 'ip_camera') or not hasattr(source, 'stream_url'):
        return source

    def milliseconds(key, default):
        value = float(config.get(key, default))
        if not math.isfinite(value) or not 0 < value <= 30:
            raise ValueError(f'{key} must be in (0, 30] seconds')
        return max(1, round(value * 1000))

    params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, milliseconds('open_timeout_seconds', 10),
              cv2.CAP_PROP_READ_TIMEOUT_MSEC, milliseconds('read_timeout_seconds', 5)]

    def connect(self):
        self.is_connected = False
        try:
            if self.cap is not None:
                self.cap.release()
            self.cap = cv2.VideoCapture(self.stream_url, cv2.CAP_FFMPEG, params)
            if not self.cap.isOpened():
                self.cap.release()
                self.cap = None
                return False
            # Best effort: backend support varies; do not claim this bounds latency.
            if not self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1):
                logging.getLogger(__name__).debug('Camera backend does not support buffer-size hint')
            self.is_connected = True
            return True
        except Exception as exc:
            logging.getLogger(__name__).warning('Camera open failed (%s)', type(exc).__name__)
            return False

    source.connect = MethodType(connect, source)
    return source
