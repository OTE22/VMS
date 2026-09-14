"""Small disk-backed event outbox. Files contain payloads, never destination credentials."""
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid


class EventOutbox:
    def __init__(self, root, pipeline_id, max_bytes=64 * 1024 * 1024):
        self.path = Path(root) / 'outbox' / hashlib.sha256(pipeline_id.encode()).hexdigest()
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = max_bytes
        self.lock = threading.RLock()

    def put(self, event_id, record):
        if not event_id or any(c not in '0123456789abcdef' for c in event_id):
            raise ValueError('Invalid event ID')
        data = json.dumps(record, allow_nan=False, separators=(',', ':')).encode()
        target = self.path / (event_id + '.json')
        with self.lock:
            used = sum(p.stat().st_size for p in self.path.glob('*.json'))
            previous = target.stat().st_size if target.exists() else 0
            if used - previous + len(data) > self.max_bytes:
                raise ValueError('Outbox byte limit reached; delivery not accepted')
            tmp = self.path / (uuid.uuid4().hex + '.tmp')
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, target)
                directory = os.open(self.path, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                tmp.unlink(missing_ok=True)

    def records(self):
        with self.lock:
            return [json.loads(p.read_text()) for p in sorted(self.path.glob('*.json'))]

    def remove(self, event_id):
        with self.lock:
            (self.path / (event_id + '.json')).unlink(missing_ok=True)
