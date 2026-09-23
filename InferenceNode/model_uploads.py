"""Non-executing upload checks and cross-process ingestion serialization."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import PurePosixPath
import pickletools
import re
import threading
import zipfile


class ModelInputError(ValueError):
    pass


class ModelConflictError(ValueError):
    pass


_sqlite_lock = threading.RLock()


@contextmanager
def model_ingest_lock(model_id):
    from .auth.db import get_session
    from sqlalchemy import text
    # Held across the registry's short transactions and file promotion. PostgreSQL
    # releases this transaction lock on failure/disconnect as well as normal exit.
    key = int.from_bytes(hashlib.sha256(('model-upload:' + model_id).encode()).digest()[:8], 'big', signed=True)
    with get_session() as session:
        if session.get_bind().dialect.name == 'postgresql':
            session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
            yield
        else:
            with _sqlite_lock:
                yield


def validate_upload(path, filename, engine_type):
    from InferenceEngine import InferenceEngineFactory
    available = InferenceEngineFactory.get_available_types()
    if engine_type not in available or engine_type == 'pass':
        raise ModelInputError('Select an available model inference engine')
    ext = os.path.splitext(filename)[1].lower()
    supported = {'ultralytics': {'.pt', '.onnx', '.engine', '.pb', '.tflite'},
                 'onnx': {'.onnx'}, 'geti': {'.zip'}}
    if ext not in supported.get(engine_type, {'.pt', '.onnx', '.engine', '.xml', '.bin', '.tflite', '.pb'}):
        raise ModelInputError('This file format is not supported by the selected engine')
    if not os.path.getsize(path):
        raise ModelInputError('The model file is empty')
    if ext in {'.zip', '.pt'} and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            files = archive.infolist()
            names = [f.filename.replace('\\', '/') for f in files]
            if len(files) > 10000 or sum(f.file_size for f in files) > 4 * 1024**3:
                raise ModelInputError('Model archive exceeds the supported unpacked size')
            if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts
                   or ':' in n or (f.external_attr >> 16) & 0o170000 == 0o120000
                   for n, f in zip(names, files)):
                raise ModelInputError('Model archive contains unsafe paths')
            try:
                if archive.testzip() is not None:
                    raise ModelInputError('Model archive is damaged')
            except (RuntimeError, zipfile.BadZipFile) as exc:
                raise ModelInputError('Model archive could not be verified') from exc
            if ext == '.pt' and not any(n.endswith('/data.pkl') or n == 'data.pkl' for n in names):
                raise ModelInputError('The file is not a PyTorch checkpoint archive')
            if ext == '.zip' and not (any(n.endswith('.xml') for n in names) and any(n.endswith('.bin') for n in names)):
                raise ModelInputError('Geti deployment ZIP must contain model XML and BIN files')
    elif ext == '.zip':
        raise ModelInputError('The file is not a valid deployment ZIP')
    elif ext == '.pt':
        # Legacy pickle checkpoints: inspect syntax only, never unpickle uploaded bytes.
        try:
            with open(path, 'rb') as stream:
                if stream.read(1) != b'\x80':
                    raise ValueError('not pickle')
                stream.seek(0)
                list(pickletools.genops(stream))
        except Exception as exc:
            raise ModelInputError('The file is not a supported PyTorch checkpoint') from exc
    elif ext == '.onnx':
        try:
            import onnx
            model = onnx.load(path, load_external_data=False)
            if any(t.external_data for t in model.graph.initializer):
                raise ModelInputError('Upload a self-contained ONNX model')
            onnx.checker.check_model(model)
        except Exception as exc:
            raise ModelInputError('The ONNX model could not be validated') from exc


def validate_download_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'(?:yolov[58]|yolo11)[nslmx](?:-seg|-pose|-cls|-obb)?\.pt', name):
        raise ModelInputError('Select a supported pretrained model from the list')
