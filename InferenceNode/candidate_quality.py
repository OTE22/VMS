"""Cheap frame ranking, not face recognition or an admission filter."""
from functools import lru_cache
from pathlib import Path
import threading

import cv2

_lock = threading.Lock()


@lru_cache(maxsize=1)
def _detector():
    directory = getattr(getattr(cv2, 'data', None), 'haarcascades', '')
    path = Path(directory) / 'haarcascade_frontalface_default.xml'
    if not directory or not path.is_file():
        return None
    model = cv2.CascadeClassifier(str(path))
    return None if model.empty() else model


def candidate_quality(frame, det):
    """Prefer a visible frontal face, then sharpness, then person confidence.

    Bound CPU work to a 192px crop of the person's upper body. Haar is only
    a ranking hint: absence of a frontal face never rejects a detection.
    """
    if frame is None or str(det.get('class_name', '')).lower() != 'person':
        return None
    try:
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in det['bbox'])
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(width, x2), min(height, y2)
        crop = frame[y1:y1 + max(0, int((y2 - y1) * .65)), x1:x2]
        if crop.size == 0 or min(crop.shape[:2]) < 12:
            return None
        scale = min(1.0, 192 / max(crop.shape[:2]))
        gray = cv2.cvtColor(cv2.resize(crop, None, fx=scale, fy=scale), cv2.COLOR_BGR2GRAY)
        with _lock:
            detector = _detector()
            faces = detector.detectMultiScale(gray, 1.15, 4, minSize=(20, 20)) if detector is not None else []
        if len(faces):
            x, y, w, h = max(faces, key=lambda box: box[2] * box[3])
            gray = gray[y:y + h, x:x + w]
        gray = cv2.resize(gray, (64, 64))
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        return (int(bool(len(faces))), sharpness, float(det.get('confidence', 0)))
    except (cv2.error, ValueError, TypeError, KeyError):
        return None  # Preserve the existing confidence-only fallback.
