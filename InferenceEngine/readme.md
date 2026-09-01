# InferenceEngine

`InferenceEngine` provides a common engine lifecycle and an auto-discovering factory for the inference pipeline.

For the complete extension tutorial, see [`../CUSTOM_ENGINE_GUIDE.md`](../CUSTOM_ENGINE_GUIDE.md).

## Public API

```python
from InferenceEngine import BaseInferenceEngine, InferenceEngineFactory
```

The package exports these two classes. It does not export an `InferenceEngine` wrapper object or module-level `register_engine()` and `list_engines()` functions.

## Create an engine

```python
from InferenceEngine import InferenceEngineFactory

engine = InferenceEngineFactory.create(
    "onnx",
    model_path="path/to/model.onnx",
    device="cpu",
)

if not engine.load():
    raise RuntimeError("Model loading failed")
```

`InferenceEngineFactory(...)` is not a creation shortcut. Always call `InferenceEngineFactory.create(...)`.

## Engine lifecycle

The pipeline uses this sequence:

```text
InferenceEngineFactory.create(**config)
    -> engine.load()
    -> engine.infer(frame)
         -> _preprocess(frame)
         -> _infer(preprocessed_input)
         -> _postprocess(raw_output)
    -> engine.result_to_json(results)
    -> engine.draw(frame, results) when an annotated image is needed
```

Concrete engines inherit from `engines.base_engine.BaseInferenceEngine`. See that class for the abstract method signatures.

## Auto-discovery

The first factory query scans `InferenceEngine/engines/` and imports each Python module except the base class and example template. Every concrete subclass defined by a scanned module is registered.

```python
from InferenceEngine import InferenceEngineFactory

print(InferenceEngineFactory.get_available_types())
print(InferenceEngineFactory.get_available_engines_with_names())
print(InferenceEngineFactory.get_available_engines_with_metadata())
print(InferenceEngineFactory.get_discovery_info())
```

The registry is cached for the process. Restart the application after adding an engine, or force a development rescan:

```python
InferenceEngineFactory.rediscover_engines()
```

## Current built-in engines

| Key | Class | Notes |
| --- | --- | --- |
| `ultralytics` | `UltralyticsEngine` | YOLO detection, segmentation, pose, OBB, classification, and tracking |
| `onnx` | `OnnxEngine` | Generic ONNX Runtime support for one specific detection-output layout |
| `geti` | `GetiEngine` | Geti deployment packages; requires the optional `geti-sdk` package |
| `pass` | `PassEngine` | No-model pass-through engine used for capture/testing |
| `simple_custom` | `SimpleCustomEngine` | Scaffold only; replace its placeholder inference and result conversion before production use |

Availability is reported separately from discovery. An engine can be registered but unavailable because its dependency is missing.

## Manual registration

Register a class for the current process with:

```python
InferenceEngineFactory.register_engine(
    "my_engine",
    MyEngine,
    display_name="My Engine",
)
```

Create and later unregister it with:

```python
engine = InferenceEngineFactory.create("my_engine", model_path="model.bin")
InferenceEngineFactory.unregister_engine("my_engine")
```

Registration is in-memory and process-local. The node does not currently load a user registration module during startup, so auto-discovered modules are preferred for application deployments.

## Result requirement

The pipeline expects `engine.result_to_json(results)` to return a Python dictionary. Detection output should have this shape:

```python
{
    "task_type": "detection",
    "num_detections": 1,
    "predictions": [
        {
            "class_id": 0,
            "class_name": "person",
            "confidence": 0.95,
            "bbox": [100, 100, 200, 250],
            "bbox_format": "xyxy",
            "track_id": 4,
        }
    ],
}
```

Do not return a JSON string when `output_format="dict"`. The pipeline reads the result with dictionary `.get()` calls.

## Error behavior

- Unknown engine keys cause `create()` to raise `ValueError` and include the available keys.
- Import failures during discovery are logged and that module is skipped.
- `BaseInferenceEngine.infer()` returns an error dictionary when preprocessing, inference, or postprocessing raises.
- Concrete `_load_model()` implementations return a boolean and are responsible for setting `is_loaded`.
- `register_engine()` warns about a class that does not inherit from `BaseInferenceEngine`, but pipeline-compatible engines should always inherit from it.

## Current integration constraints

- `PipelineManager` forwards a fixed engine configuration: engine type, model path, device, and `task="detect"` for model-backed engines.
- Only `pass` is currently recognized as not requiring a model.
- Device policy is hardcoded for Geti versus other engines.
- The publishing pipeline assumes detection boxes are `xyxy` even if an engine labels them differently.
- The publishing pipeline filters results through a fixed class-name allow-list.

See the custom-engine guide for safe implementation patterns, testing, model upload, Pipeline Builder usage, and Docker deployment.
