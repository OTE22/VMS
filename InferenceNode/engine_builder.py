"""Guided custom-engine builder: wizard fields -> server-owned preset generator ->
generated Python -> static AST validation -> atomic install -> factory rediscovery.

SECURITY: the generated file is trusted administrator code; once installed the factory
imports it, which is code execution. This is admin-only AND gated by
ENABLE_ENGINE_BUILDER. AST validation is NOT a sandbox - it is a correctness check, not
a security boundary. The default mode does NOT accept arbitrary manual Python; advanced
raw-code editing is a separate trusted-admin mode disabled unless ENGINE_BUILDER_ADVANCED=true.
"""
from __future__ import annotations

import ast
import os
import re
import hashlib
import logging
from functools import wraps
from typing import Callable, Optional

from flask import request, jsonify, render_template, abort
from flask_login import current_user

logger = logging.getLogger("InferenceNode.engine_builder")

REQUIRED_METHODS = ("_load_model", "check_valid_model", "_preprocess", "_infer",
                    "_postprocess", "draw", "result_to_json")
_KEY_SUFFIXES = ("Engine", "Inference", "AI", "Model")
SKIP_NAMES = {"__init__.py", "base_engine.py", "example_engine_template.py", "__pycache__"}
_FILENAME_RE = re.compile(r"^[a-z0-9_]+_engine\.py$")
PRESETS = ("blank", "onnx", "ultralytics", "opencv_dnn")


# --------------------------------------------------------------------------- #
# name / key derivation (mirrors InferenceEngineFactory._class_name_to_key)
# --------------------------------------------------------------------------- #
def class_name_from_display(display: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", display or "")
    if not words:
        raise ValueError("Display name must contain letters or digits")
    name = "".join(w[:1].upper() + w[1:] for w in words)
    if name[0].isdigit():
        name = "E" + name
    if not name.endswith("Engine"):
        name += "Engine"
    return name


def key_from_class(class_name: str) -> str:
    stripped = class_name
    for suffix in _KEY_SUFFIXES:
        if class_name.endswith(suffix) and len(class_name) > len(suffix):
            stripped = class_name[: -len(suffix)]
            break
    out = []
    for i, ch in enumerate(stripped):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def filename_for_key(key: str) -> str:
    return f"{key}_engine.py"


# --------------------------------------------------------------------------- #
# static AST validation (never executes the source)
# --------------------------------------------------------------------------- #
def validate_source(code: str) -> dict:
    result = {"valid": False, "class_name": None, "engine_key": None,
              "missing_methods": [], "warnings": [], "error": None}
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        result["error"] = f"Syntax error: line {e.lineno}: {e.msg}"
        return result

    engine_classes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            base_names = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    base_names.append(b.id)
                elif isinstance(b, ast.Attribute):
                    base_names.append(b.attr)
            if "BaseInferenceEngine" in base_names:
                engine_classes.append(node)

    if len(engine_classes) == 0:
        result["error"] = "No BaseInferenceEngine subclass found"
        return result
    if len(engine_classes) > 1:
        result["error"] = "Exactly one BaseInferenceEngine subclass is required"
        return result

    cls = engine_classes[0]
    methods = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [m for m in REQUIRED_METHODS if m not in methods]
    result["missing_methods"] = missing
    result["class_name"] = cls.name
    try:
        result["engine_key"] = key_from_class(cls.name)
    except Exception:
        result["engine_key"] = None
    if "__init__" not in methods:
        result["warnings"].append("No __init__ defined; ensure a lightweight, zero-arg-safe constructor")
    result["valid"] = not missing and bool(result["engine_key"])
    if missing:
        result["error"] = "Missing required methods: " + ", ".join(missing)
    return result


# --------------------------------------------------------------------------- #
# preset source generator (single source of truth)
# --------------------------------------------------------------------------- #
def _draw_body(color):
    r, g, b = color
    return f"""        annotated = image.copy()
        for det in results.get("predictions", []):
            bbox = det.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x1, y1, x2, y2 = [int(v) for v in bbox]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), ({b}, {g}, {r}), 2)
            label = f"{{det.get('class_name','obj')}} {{det.get('confidence',0):.2f}}"
            cv2.putText(annotated, label, (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, ({b}, {g}, {r}), 1)
        return annotated"""


def generate_engine_source(preset: str, fields: dict) -> str:
    preset = (preset or "blank").lower()
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}")
    display = fields.get("display_name") or "My Custom"
    class_name = class_name_from_display(display)
    task = fields.get("task", "detection")
    conf = float(fields.get("confidence", 0.4))
    exts = fields.get("extensions") or [".onnx"]
    exts_l = [e.lower() if e.startswith(".") else "." + e.lower() for e in exts]
    color = fields.get("draw_color") or [0, 255, 0]
    class_map = fields.get("class_map") or {}

    header = f'''"""{display} inference engine (generated by ArmyEye engine builder).

Preset: {preset}. Fill in the model-specific TODO sections for real inference.
"""
import json
import os
from typing import Any, Dict

import cv2
import numpy as np

try:
    from .base_engine import BaseInferenceEngine
except ImportError:
    from base_engine import BaseInferenceEngine
'''

    optional_import = ""
    dep_check = "        return True"
    load_body = f'''        if not self.check_valid_model(model_file):
            self.logger.error("Invalid model: %s", model_file)
            return False
        try:
            self.model = {{"path": model_file, "device": device}}
            self.is_loaded = True
            return True
        except Exception as exc:
            self.logger.exception("Load failed: %s", exc)
            self.is_loaded = False
            return False'''
    infer_body = '''        # TODO: run the model. Return raw output for _postprocess.
        return {"predictions": []}'''

    if preset == "onnx":
        optional_import = ("try:\n    import onnxruntime as ort\nexcept ImportError:\n    ort = None\n")
        dep_check = "        return ort is not None"
        load_body = f'''        if ort is None:
            self.logger.error("Install onnxruntime to use this engine")
            return False
        if not self.check_valid_model(model_file):
            return False
        try:
            providers = ["CPUExecutionProvider"]
            self.session = ort.InferenceSession(model_file, providers=providers)
            self.input_name = self.session.get_inputs()[0].name
            self.is_loaded = True
            return True
        except Exception as exc:
            self.logger.exception("ONNX load failed: %s", exc)
            return False'''
        infer_body = '''        # TODO: shape the blob to your model's expected input; parse outputs.
        outputs = self.session.run(None, {self.input_name: preprocessed_input})
        return {"raw": outputs, "predictions": []}'''
    elif preset == "ultralytics":
        optional_import = ("try:\n    from ultralytics import YOLO\nexcept ImportError:\n    YOLO = None\n")
        dep_check = "        return YOLO is not None"
        load_body = f'''        if YOLO is None:
            self.logger.error("Install ultralytics to use this engine")
            return False
        if not self.check_valid_model(model_file):
            return False
        try:
            self.model = YOLO(model_file)
            self.is_loaded = True
            return True
        except Exception as exc:
            self.logger.exception("YOLO load failed: %s", exc)
            return False'''
        infer_body = '''        results = self.model(preprocessed_input, conf=self.confidence_threshold, verbose=False)
        preds = []
        for r in results:
            for b in getattr(r, "boxes", []) or []:
                xyxy = [float(v) for v in b.xyxy[0].tolist()]
                cls_id = int(b.cls[0]); conf = float(b.conf[0])
                name = self.model.names.get(cls_id, str(cls_id))
                preds.append({"class_id": cls_id, "class_name": self.class_map.get(name, name),
                              "confidence": conf, "bbox": xyxy, "bbox_format": "xyxy"})
        return {"predictions": [p for p in preds if p["confidence"] >= self.confidence_threshold]}'''
    elif preset == "opencv_dnn":
        load_body = f'''        if not self.check_valid_model(model_file):
            return False
        try:
            self.net = cv2.dnn.readNet(model_file)
            self.is_loaded = True
            return True
        except Exception as exc:
            self.logger.exception("OpenCV DNN load failed: %s", exc)
            return False'''
        infer_body = '''        # TODO: build blob (cv2.dnn.blobFromImage), forward, decode detections.
        return {"predictions": []}'''

    body = f'''{optional_import}

class {class_name}(BaseInferenceEngine):
    display_name = {display!r}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.model = None
        self.session = None
        self.net = None
        self.confidence_threshold = float(kwargs.get("confidence_threshold", {conf}))
        self.class_map = {class_map!r}

    def check_dependencies(self) -> bool:
{dep_check}

    def check_valid_model(self, model_file: str) -> bool:
        exts = {exts_l!r}
        return bool(model_file and os.path.isfile(model_file)
                    and any(model_file.lower().endswith(e) for e in exts))

    def _load_model(self, model_file: str, device: str) -> bool:
{load_body}

    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        if not isinstance(image, np.ndarray):
            raise TypeError("Input image must be a numpy array")
        # TODO: resize / normalize / layout as your model expects.
        return image

    def _infer(self, preprocessed_input: Any) -> Dict[str, Any]:
        if not self.is_loaded:
            raise RuntimeError("Model is not loaded")
{infer_body}

    def _postprocess(self, raw_output: Dict[str, Any]) -> Dict[str, Any]:
        # TODO: confidence filter / NMS / map labels to source coords.
        return raw_output

    def draw(self, image: np.ndarray, results: Dict[str, Any]) -> np.ndarray:
{_draw_body(color)}

    def result_to_json(self, results: Dict[str, Any], output_format: str = "dict") -> Any:
        preds = results.get("predictions", []) if isinstance(results, dict) else []
        payload = {{"task_type": {task!r}, "num_detections": len(preds), "predictions": preds}}
        if output_format == "dict":
            return payload
        if output_format == "json":
            return json.dumps(payload)
        raise ValueError(f"Unsupported output format: {{output_format}}")
'''
    return header + body


# --------------------------------------------------------------------------- #
# atomic install
# --------------------------------------------------------------------------- #
def default_engines_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "InferenceEngine", "engines")


class EngineInstallError(Exception):
    pass


def _dry_run_import(path: str, class_name: str) -> None:
    """Import the promoted engine file under a throwaway module name and check that the
    expected class exists. Raises EngineInstallError on any failure; never registers the
    module with the factory (that happens only for AVAILABLE engines)."""
    import importlib.util
    import sys as _sys
    mod_name = f"_armyeye_engine_validate_{os.path.basename(os.path.dirname(path))}_{os.getpid()}"
    # Generated engines import `base_engine` from the shipped engines package (same as the
    # factory's runtime import); make it resolvable for the dry run only.
    shipped = default_engines_dir()
    added = False
    if os.path.isdir(shipped) and shipped not in _sys.path:
        _sys.path.insert(0, shipped); added = True
    try:
        # bind `base_engine` to the factory's BaseInferenceEngine module (one class object)
        try:
            from InferenceEngine.inference_engine_factory import BaseInferenceEngine as _Base
            base_mod = _sys.modules.get(_Base.__module__)
            if base_mod is not None and _sys.modules.get("base_engine") is not base_mod:
                _sys.modules["base_engine"] = base_mod
        except Exception:  # noqa: BLE001 - factory unavailable: plain import still validates syntax/class
            pass
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise EngineInstallError("engine module could not be loaded")
        module = importlib.util.module_from_spec(spec)
        _sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            _sys.modules.pop(mod_name, None)
        cls = getattr(module, class_name, None)
        if cls is None or not isinstance(cls, type):
            raise EngineInstallError(f"engine class {class_name} not found after import")
    except EngineInstallError:
        raise
    except Exception as e:  # noqa: BLE001
        raise EngineInstallError(f"engine import failed: {e.__class__.__name__}: {e}")
    finally:
        if added:
            try:
                _sys.path.remove(shipped)
            except ValueError:
                pass


def install_engine(source: str, *, engines_dir: Optional[str] = None,
                   existing_keys: Optional[Callable[[], set]] = None,
                   rediscover: Optional[Callable[[], None]] = None,
                   verify_key: Optional[Callable[[str], bool]] = None,
                   lock=None, created_by: Optional[int] = None,
                   register: bool = True) -> dict:
    """Install a validated CUSTOM engine as a managed artifact (Phase 11).

    Storage: ARTIFACT_ROOT/engines/<key>/engine.py (persistent, bind-mounted - survives
    container recreation), never the application source tree. Registry: PostgreSQL
    `inference_engines` (origin=custom) with the explicit creation state machine:

        INSERT STAGING -> COMMIT
        write source to <engines>/.staging/<key>/engine.py + fsync
        VALIDATING (AST + security checks already done; recheck uniqueness under lock)
        sha256 + size
        atomic rename to final + dir fsync
        rediscover + verify importable
        AVAILABLE + PASSED + fingerprint + enabled -> COMMIT

    Any failure -> FAILED (+ quarantine of whatever was written), reported STRUCTURED
    (err.state = {"state": "FAILED", "quarantined": True}) - callers never parse error
    text. `engines_dir` overrides the root ONLY for tests (register=False keeps them
    DB-free).
    """
    val = validate_source(source)
    if not val["valid"]:
        raise EngineInstallError(val.get("error") or "Invalid engine source")
    key = val["engine_key"]
    class_name = val["class_name"]
    filename = filename_for_key(key)

    if not _FILENAME_RE.match(filename):
        raise EngineInstallError(f"Unsafe generated filename: {filename}")
    if filename in SKIP_NAMES:
        raise EngineInstallError("Reserved engine filename")
    if not re.match(r"^[a-z][a-z0-9_]{0,63}$", key):
        raise EngineInstallError("Unsafe engine key")

    from InferenceNode import artifact_paths as ap
    rel = f"{key}/engine.py"
    if engines_dir is None:
        ap.ensure_layout()
        final_path = ap.resolve("engines", rel)                # traversal/symlink-safe
        staged_path = ap.staging_path("engines", rel)
    else:                                                       # test override: still guarded
        engines_dir = os.path.abspath(engines_dir)
        final_path = os.path.abspath(os.path.join(engines_dir, key, "engine.py"))
        if os.path.commonpath([final_path, engines_dir]) != engines_dir:
            raise EngineInstallError("Path traversal rejected")
        staged_path = os.path.join(engines_dir, ".staging", key, "engine.py")

    if existing_keys is None or rediscover is None or verify_key is None:
        from InferenceEngine import InferenceEngineFactory as F
        existing_keys = existing_keys or (lambda: set(F.get_available_types()))
        rediscover = rediscover or F.rediscover_engines
        verify_key = verify_key or (lambda k: k in set(F.get_available_types()))
    if lock is None:
        from InferenceNode.auth.db import advisory_lock
        lock = advisory_lock

    src_bytes = source.encode("utf-8")
    sha = hashlib.sha256(src_bytes).hexdigest()

    reg = None
    if register:
        from InferenceNode import engine_registry as reg

    with lock():
        if os.path.exists(final_path):
            raise EngineInstallError(f"An engine file for '{key}' already exists")
        if key in existing_keys():
            raise EngineInstallError(f"Engine key '{key}' already exists")
        if reg is not None:
            reg.begin_custom(key, class_name, display_name=key.replace("_", " ").title(),
                             created_by=created_by)                    # STAGING -> COMMIT
        try:
            os.makedirs(os.path.dirname(staged_path), exist_ok=True)
            with open(staged_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(source)
                f.flush()
                os.fsync(f.fileno())
            if reg is not None:
                from InferenceNode.artifact_states import ArtifactStatus as S, ValidationStatus as V
                reg.set_state(key, S.VALIDATING, V.PENDING)
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            os.replace(staged_path, final_path)                        # atomic promotion
            try:
                dfd = os.open(os.path.dirname(final_path), os.O_RDONLY)
                os.fsync(dfd)
                os.close(dfd)
            except (OSError, AttributeError):
                pass
            # VALIDATING: dry-run import of the PROMOTED bytes in isolation (module object is
            # discarded). The factory only activates AVAILABLE engines (registry-gated), so
            # discoverability is checked AFTER the AVAILABLE transition, not before it.
            _dry_run_import(final_path, class_name)
            if reg is not None:
                from InferenceNode.artifact_states import (ArtifactStatus as S, ValidationStatus as V,
                                                           fingerprint)
                reg.set_state(key, S.AVAILABLE, V.PASSED, sha256=sha, size_bytes=len(src_bytes),
                              fp=fingerprint(final_path), enabled=True)
            rediscover()
            if not verify_key(key):
                raise EngineInstallError("Engine not discoverable after install")
        except Exception as e:
            _quarantine(final_path if os.path.exists(final_path) else staged_path)
            if reg is not None:
                try:
                    from InferenceNode.artifact_states import ArtifactStatus as S, ValidationStatus as V, Reason
                    cur = reg.get(key)
                    if cur and cur.get("status") == S.AVAILABLE.value:
                        # post-promotion failure (e.g. not discoverable): AVAILABLE has no
                        # direct FAILED edge - route through VALIDATING (integrity re-check)
                        reg.set_state(key, S.VALIDATING, V.PENDING)
                    reg.set_state(key, S.FAILED, V.FAILED, Reason.VALIDATION_FAILED)
                except Exception as se:  # noqa: BLE001
                    logger.error(f"[ENGINES] could not record FAILED state for {key}: {se}")
            try:
                rediscover()
            except Exception:
                pass
            err = EngineInstallError(f"Install failed, engine quarantined: {e}")
            err.state = {"state": "FAILED", "quarantined": True}
            raise err

    return {"engine_key": key, "class_name": class_name, "filename": filename, "sha256": sha,
            "relative_path": rel, "size_bytes": len(src_bytes), "state": "AVAILABLE",
            "origin": "custom"}


def delete_engine(engine_key: str) -> dict:
    """Custom engines only (builtins are read-only and 404 here). Batch-safe single-file
    delete: DELETING -> move to managed trash -> remove row -> purge; failure keeps the
    artifact recoverable, never AVAILABLE + missing."""
    from InferenceNode import artifact_paths as ap, engine_registry as reg
    from InferenceNode.artifact_states import ArtifactStatus as S, ValidationStatus as V
    e = reg.get(engine_key)
    if e is None or e["origin"] != "custom":
        raise EngineInstallError("Custom engine not found")
    reg.set_state(engine_key, S.DELETING, V.PENDING)
    final = ap.resolve("engines", e["relative_path"])
    trash = ap.trash_path("engines", e["relative_path"])
    moved = False
    try:
        if os.path.exists(final):
            os.makedirs(os.path.dirname(trash), exist_ok=True)
            os.replace(final, trash)
            moved = True
        reg.remove(engine_key)
    except Exception as ex:
        if moved:
            try:
                os.replace(trash, final)
            except Exception:
                pass
        raise EngineInstallError(f"Delete interrupted; engine preserved: {ex}")
    try:
        if moved:
            os.remove(trash)
    except OSError:
        pass
    # the now-empty <key>/ directory (and its trash mirror) - never anything with content
    for d in (os.path.dirname(final), os.path.dirname(trash)):
        try:
            if d and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    try:
        from InferenceEngine import InferenceEngineFactory as F
        F.rediscover_engines()
    except Exception:
        pass
    return {"engine_key": engine_key, "state": "DELETED"}


def _quarantine(path: str):
    try:
        if os.path.exists(path):
            os.replace(path, path + ".quarantine")
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
def _builder_enabled() -> bool:
    return os.environ.get("ENABLE_ENGINE_BUILDER", "").strip().lower() in ("1", "true", "yes", "on")


def _advanced_enabled() -> bool:
    return os.environ.get("ENGINE_BUILDER_ADVANCED", "").strip().lower() in ("1", "true", "yes", "on")


def builder_gate(view):
    """404 unless admin AND the builder is enabled (do not reveal it exists otherwise)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(current_user, "is_admin", False) or not _builder_enabled():
            abort(404)
        return view(*args, **kwargs)
    return wrapped


def register_engine_builder(app, audit_cb: Optional[Callable] = None):
    from .auth.admin_routes import _require_csrf  # reuse CSRF check

    @app.route("/create-engine")
    @builder_gate
    def create_engine_page():
        return render_template("create_engine.html", advanced=_advanced_enabled())

    @app.route("/api/inference/engines/preview", methods=["POST"])
    @builder_gate
    def api_engine_preview():
        _require_csrf()
        data = request.get_json(silent=True) or {}
        try:
            code = generate_engine_source(data.get("preset", "blank"), data.get("fields", {}))
            val = validate_source(code)
            return jsonify({"status": "success", "code": code,
                            "class_name": val["class_name"], "engine_key": val["engine_key"]})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/inference/engines/validate", methods=["POST"])
    @builder_gate
    def api_engine_validate():
        _require_csrf()
        data = request.get_json(silent=True) or {}
        return jsonify(validate_source(data.get("code", "")))

    @app.route("/api/inference/engines", methods=["POST"])
    @builder_gate
    def api_engine_create():
        _require_csrf()
        data = request.get_json(silent=True) or {}
        preset = data.get("preset", "blank")
        fields = data.get("fields", {})
        # Default mode ALWAYS regenerates server-side; raw code only in advanced mode.
        if _advanced_enabled() and data.get("code"):
            source = data["code"]
        else:
            try:
                source = generate_engine_source(preset, fields)
            except Exception as e:
                return jsonify({"error": str(e)}), 400
        try:
            info = install_engine(source, created_by=getattr(current_user, "id", None))
        except EngineInstallError as e:
            # STRUCTURED failure state (the UI must never parse error text for control flow)
            body = {"error": str(e)}
            body.update(getattr(e, "state", {}) or {})
            return jsonify(body), 400
        except Exception as e:
            logger.error(f"engine install error: {e}")
            return jsonify({"error": "Engine install failed", "state": "FAILED"}), 500

        if audit_cb:
            try:
                audit_cb(actor=current_user, action="engine_created", target=info["engine_key"],
                         detail={"class_name": info["class_name"], "filename": info["filename"],
                                 "sha256": info["sha256"], "preset": preset})
            except Exception:
                pass
        from InferenceEngine import InferenceEngineFactory as F
        meta = next((m for m in F.get_available_engines_with_metadata()
                     if m.get("type") == info["engine_key"]), None)
        return jsonify({"status": "success", "engine": info, "metadata": meta,
                        "discovery": F.get_discovery_info()}), 201

    @app.route("/api/inference/engines/<engine_key>", methods=["DELETE"])
    @builder_gate
    def api_engine_delete(engine_key):
        """Delete a CUSTOM engine (registry + artifact, batch-safe). Builtins -> 404."""
        _require_csrf()
        try:
            info = delete_engine(engine_key)
        except EngineInstallError as e:
            return jsonify({"error": str(e)}), 404
        if audit_cb:
            try:
                audit_cb(actor=current_user, action="engine_deleted", target=engine_key, detail={})
            except Exception:
                pass
        return jsonify({"status": "success", "engine": info})

    @app.route("/api/inference/engines/registry", methods=["GET"])
    @builder_gate
    def api_engine_registry():
        """Registry view: builtin vs custom, lifecycle status, hash - never file contents."""
        from InferenceNode import engine_registry as reg
        return jsonify({"engines": reg.list_engines(), "verify": reg.verify_all()})
