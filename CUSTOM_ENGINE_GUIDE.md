# Custom Inference Engine Guide

This guide describes the custom-engine workflow implemented by the current codebase: engine discovery, model upload, pipeline creation, per-frame inference, result normalization, drawing, and result publishing.

## Runtime workflow

```text
InferenceEngine/engines/<engine>.py
    -> InferenceEngineFactory auto-discovery
    -> GET /api/inference/engines
    -> model upload and ModelRepository metadata
    -> PipelineManager builds the engine configuration
    -> InferencePipeline calls create() and load()
    -> each frame calls infer(), result_to_json(), and optionally draw()
    -> detections are filtered, deduplicated, and published
```

## Recommended integration: auto-discovery

Auto-discovery is the supported application workflow. Create a Python module in `InferenceEngine/engines/`, restart the node, and the factory will import the module and register its concrete `BaseInferenceEngine` subclasses.

The following files are deliberately skipped:

- `__init__.py`
- `base_engine.py`
- `example_engine_template.py`
- `__pycache__`

The engine key comes from the **class name**, not the filename:

| Class | Discovered key |
| --- | --- |
| `ThermalEngine` | `thermal` |
| `ThermalDetectionEngine` | `thermal_detection` |
| `ExampleDetectionEngine` | `example_detection` |

Use ordinary CamelCase names where possible. The current converter inserts an underscore before every uppercase character, so acronym-heavy names can produce surprising keys.

## Required interface

Every engine must inherit from `InferenceEngine.engines.base_engine.BaseInferenceEngine` and implement:

1. `_load_model(model_file, device)`
2. `check_valid_model(model_file)`
3. `_preprocess(image)`
4. `_infer(preprocessed_input)`
5. `_postprocess(raw_output)`
6. `draw(image, results)`
7. `result_to_json(results, output_format="dict")`

The base `infer()` method already runs the preprocessing, inference, and postprocessing stages in order. It also returns an error dictionary when inference raises an exception.

The constructor must accept `**kwargs`, call `super().__init__(**kwargs)`, and work without positional arguments. The engine metadata endpoint creates a temporary instance to determine availability, so constructors should remain lightweight and should not load a model.

## Complete minimal example

Create `InferenceEngine/engines/example_detection_engine.py`:

```python
import json
import os
from typing import Any, Dict

import cv2
import numpy as np

from .base_engine import BaseInferenceEngine


class ExampleDetectionEngine(BaseInferenceEngine):
    display_name = "Example Detection"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.model = None
        self.confidence_threshold = float(
            kwargs.get("confidence_threshold", 0.5)
        )

    def check_dependencies(self) -> bool:
        """Optional hook used by the engine metadata endpoint."""
        return True

    def check_valid_model(self, model_file: str) -> bool:
        return bool(
            model_file
            and os.path.isfile(model_file)
            and model_file.lower().endswith(".example")
        )

    def _load_model(self, model_file: str, device: str) -> bool:
        if not self.check_valid_model(model_file):
            self.logger.error("Invalid Example Detection model: %s", model_file)
            return False

        try:
            # Replace this placeholder with the framework-specific loader.
            self.model = {"path": model_file, "device": device}
            self.is_loaded = True
            return True
        except Exception as exc:
            self.logger.exception("Model loading failed: %s", exc)
            self.is_loaded = False
            return False

    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        if not isinstance(image, np.ndarray):
            raise TypeError("Input image must be a numpy array")

        # Replace with resizing, BGR-to-RGB conversion, normalization, and
        # tensor layout conversion required by the model.
        return image

    def _infer(self, preprocessed_input: np.ndarray) -> Dict[str, Any]:
        if self.model is None:
            raise RuntimeError("Model is not loaded")

        # Replace with the actual framework inference call. This placeholder
        # returns no detections but preserves the correct structure.
        return {"predictions": []}

    def _postprocess(self, raw_output: Dict[str, Any]) -> Dict[str, Any]:
        # Apply confidence filtering, NMS, label mapping, and conversion of
        # model coordinates back to source-image coordinates here.
        return raw_output

    def draw(
        self,
        image: np.ndarray,
        results: Dict[str, Any],
    ) -> np.ndarray:
        annotated = image.copy()
        for detection in results.get("predictions", []):
            x1, y1, x2, y2 = detection["bbox"]
            label = (
                f"{detection['class_name']} "
                f"{detection['confidence']:.2f}"
            )
            cv2.rectangle(
                annotated,
                (int(x1), int(y1)),
                (int(x2), int(y2)),
                (0, 255, 0),
                2,
            )
            cv2.putText(
                annotated,
                label,
                (int(x1), max(0, int(y1) - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 0),
                1,
            )
        return annotated

    def result_to_json(
        self,
        results: Dict[str, Any],
        output_format: str = "dict",
    ) -> Any:
        predictions = results.get("predictions", [])
        payload = {
            "task_type": "detection",
            "num_detections": len(predictions),
            "predictions": predictions,
        }

        if output_format == "dict":
            return payload
        if output_format == "json":
            return json.dumps(payload)
        raise ValueError(f"Unsupported output format: {output_format}")
```

Replace the placeholder loader and inference call with the model framework. Keep the surrounding lifecycle and output contract.

## Result contract

For `output_format="dict"`, `result_to_json()` must return a Python dictionary, not a serialized JSON string. The running pipeline calls `.get()` on this value.

For detection-style publishing, `predictions` must be a list:

```python
{
    "task_type": "detection",
    "num_detections": 1,
    "predictions": [
        {
            "class_id": 0,
            "class_name": "person",
            "confidence": 0.95,
            "bbox": [100.0, 100.0, 200.0, 260.0],
            "bbox_format": "xyxy",
            "track_id": 12,
        }
    ],
}
```

Field requirements:

- `class_id` should be a Python integer.
- `class_name` should be a string.
- `confidence` should be a Python float between 0 and 1.
- `bbox` should contain four source-image coordinates.
- Use `[x1, y1, x2, y2]`. The current pipeline IoU and deduplication code assumes this layout.
- `track_id` is optional. When absent, the pipeline uses IoU-based deduplication.
- Convert NumPy arrays and scalars into normal Python lists, integers, floats, and booleans.
- Return an empty `predictions` list when there are no detections.

### Class filtering

The current pipeline only publishes these class names:

```text
person, bicycle, car, motorcycle, bus, train, truck, face
```

Other predictions can still be drawn in the preview but are filtered before publishing. Until the allow-list becomes configurable, map the custom model labels to one of these values when published detections are required.

## Optional framework dependencies

Discovery imports every engine module. An unconditional import of an unavailable framework can prevent the entire module from being registered. Prefer this pattern:

```python
try:
    import my_runtime
except ImportError:
    my_runtime = None


class MyRuntimeEngine(BaseInferenceEngine):
    display_name = "My Runtime"

    def check_dependencies(self) -> bool:
        return my_runtime is not None

    def _load_model(self, model_file: str, device: str) -> bool:
        if my_runtime is None:
            self.logger.error("Install my-runtime to use this engine")
            return False
        # Continue loading...
```

Add the dependency to `requirements.txt` when it is required by all deployments. For optional engines, add a named optional-dependency group in `pyproject.toml` and document its installation command.

## Verify discovery

Restart the application, then check discovery directly:

```python
from InferenceEngine import InferenceEngineFactory

print(InferenceEngineFactory.get_discovery_info())
print(InferenceEngineFactory.get_available_engines_with_metadata())
```

During development in a long-running process, force another scan with:

```python
InferenceEngineFactory.rediscover_engines()
```

You can also inspect the web API:

```bash
curl http://localhost:5555/api/inference/engines
```

The new engine should appear with its generated `type`, display name, availability, and dependency status.

## Test the engine directly

Test the engine independently before using the full pipeline:

```python
import cv2

from InferenceEngine import InferenceEngineFactory

engine = InferenceEngineFactory.create(
    "example_detection",
    model_path="path/to/model.example",
    device="cpu",
    confidence_threshold=0.6,
)

if not engine.load():
    raise RuntimeError("The model could not be loaded")

image = cv2.imread("test_image.jpg")
if image is None:
    raise RuntimeError("The test image could not be read")

results = engine.infer(image)
payload = engine.result_to_json(results)
annotated = engine.draw(image, results)

assert isinstance(payload, dict)
assert isinstance(payload.get("predictions"), list)
cv2.imwrite("annotated.jpg", annotated)
print(payload)
```

## Upload a model

After the engine is discovered, it appears dynamically in the Models page. Select it and upload the model so `ModelRepository` records the correct `engine_type`.

The current Models page has hardcoded file-picker extensions for unknown engines. If the custom format is not selectable in the browser, use the upload API until UI metadata for accepted extensions is implemented:

```bash
curl -X POST http://localhost:5555/api/models/upload \
  -F "file=@path/to/model.example" \
  -F "engine_type=example_detection" \
  -F "name=Example model" \
  -F "description=Model for Example Detection Engine"
```

The backend currently stores the file and engine association but does not call `check_valid_model()` during upload. Validation happens when the engine loads the model, so direct engine testing is important.

## Create and run a pipeline in the web UI

1. Start the node with `python main.py`.
2. Open `http://localhost:5555/models` and upload the model for the custom engine.
3. Open `http://localhost:5555/pipeline-builder`.
4. Select the frame source.
5. Select the uploaded model. The builder auto-selects the model's `engine_type`.
6. Select the device.
7. Configure at least one result destination.
8. Save the pipeline.
9. Start the pipeline and inspect its status, logs, metrics, preview, and publisher output.

The current builder persists `model.id`, `model.engine_type`, and `model.device`. It does not expose arbitrary engine-specific constructor parameters.

## Create a pipeline from Python

Direct construction can pass custom engine parameters because `InferencePipeline.configure()` forwards the complete inference configuration to the factory:

```python
from InferenceNode.pipeline import InferencePipeline
from ResultPublisher import ResultPublisher

pipeline = InferencePipeline()
pipeline.configure(
    frame_source_config={
        "capture_type": "webcam",
        "source": 0,
    },
    inference_engine_config={
        "engine_type": "example_detection",
        "model_path": "path/to/model.example",
        "device": "cpu",
        "confidence_threshold": 0.6,
    },
    result_publisher=ResultPublisher(),
)
pipeline.start()
```

When a pipeline is created through `PipelineManager`, the current implementation forwards only `engine_type`, `model_path`, `device`, and a hardcoded `task="detect"`. Supporting custom settings in saved/web pipelines requires extending the builder configuration and `PipelineManager._initialize_pipeline()`.

## Runtime registration

Manual registration is available through class methods:

```python
from InferenceEngine import BaseInferenceEngine, InferenceEngineFactory


class RuntimeEngine(BaseInferenceEngine):
    display_name = "Runtime Engine"

    # Implement all abstract methods here.


InferenceEngineFactory.register_engine(
    "runtime",
    RuntimeEngine,
    display_name="Runtime Engine",
)

engine = InferenceEngineFactory.create("runtime")
```

Registration lasts only for the current Python process. The application has no configured startup plugin hook, so code must register the class before the engine endpoint or a pipeline tries to use it. Auto-discovery is therefore recommended for normal node deployments.

## Docker deployment

The Docker image copies the engine source and installs `requirements.txt` during the build. After adding engine code or dependencies, rebuild the service:

```bash
docker compose build infernode
docker compose up -d infernode
```

The model repository is mounted persistently by `docker-compose.yml`, but `InferenceEngine/engines/` is not mounted. A container restart alone does not add newly written host engine code to an existing image.

## Acceptance checklist

- The engine module imports without its optional runtime installed.
- The engine appears in `GET /api/inference/engines`.
- Availability is false with a clear dependency message when required packages are missing.
- `check_valid_model()` rejects unsupported or missing models.
- `load()` returns `True` only after the model is ready.
- `infer()` handles an OpenCV NumPy frame.
- `result_to_json(..., "dict")` returns a dictionary.
- `predictions` is always a list.
- Detection boxes use source-image `xyxy` coordinates.
- All result values are JSON serializable.
- `draw()` returns an image for detections, empty results, and error results.
- The model is uploaded with the matching engine type.
- The pipeline starts, processes frames, and publishes expected detections.
- The pipeline can stop and start again without leaking model or device resources.

## Known platform limitations

- Pipeline startup currently does not check the boolean returned by `engine.load()` before marking configuration complete.
- The Models page does not yet receive accepted extensions from engine metadata.
- Saved/web pipeline configurations do not forward arbitrary engine-specific parameters.
- Device validation is special-cased for Geti and otherwise assumes PyTorch/CUDA-like behavior.
- Published class names are constrained by the pipeline's hardcoded allow-list.
- Bounding boxes are not normalized centrally; custom detection engines should emit `xyxy`.

Treat these as integration requirements when extending the engine platform, not as behavior supplied automatically by `BaseInferenceEngine`.
