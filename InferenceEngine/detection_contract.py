"""Normalize plugin results at the pipeline boundary, before event selection."""
import json
import math


def normalize_result(result):
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise ValueError('Engine must return a detection object')
    if result.get('success') is False:
        raise ValueError('Engine reported inference failure')
    predictions = result.get('predictions', result.get('detections', []))
    if not isinstance(predictions, list):
        raise ValueError('Engine predictions must be a list')
    normalized = []
    for item in predictions:
        if not isinstance(item, dict):
            raise ValueError('Each prediction must be an object')
        det = dict(item)
        name = det.get('class_name')
        confidence = float(det.get('confidence', -1))
        bbox = det.get('bbox', [])
        if not isinstance(name, str) or not name or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError('Detection requires a class name and finite confidence')
        if len(bbox) != 4:
            raise ValueError('Detection requires four bounding-box coordinates')
        bbox = [float(v) for v in bbox]
        if not all(math.isfinite(v) for v in bbox):
            raise ValueError('Detection bounding box must be finite')
        fmt = det.get('bbox_format', 'xyxy')
        if fmt in ('xywh_center', 'xywh'):
            x, y, w, h = bbox
            bbox = [x - w / 2, y - h / 2, x + w / 2, y + h / 2]
        elif fmt != 'xyxy':
            raise ValueError('Unsupported bounding-box format')
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        det.update(class_name=name.lower(), confidence=confidence, bbox=bbox, bbox_format='xyxy')
        normalized.append(det)
    return dict(result, predictions=normalized, num_detections=len(normalized))
