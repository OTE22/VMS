# InferNode
## A scalable inference platform that provides multi-node management and control for AI/ML inference workloads.

### It enables easy deployment and management of inference pipelines across distributed nodes with auto-discovery, telemetry, and flexible result publishing.

## 📺 Demo Video
https://github.com/user-attachments/assets/5ed323d7-e8cf-421a-be8f-781e3f51c9a0

## 🚀 Features - Scalable Inference Platform

### Core Capabilities
- **Multi-engine support**: Ultralytics YOLO, Geti, and custom engines
- **Auto-discovery**: Nodes automatically discover each other on the network
- **Real-time telemetry**: System monitoring and performance metrics via MQTT
- **Flexible result publishing**: MQTT, webhooks, serial, and custom destinations
- **RESTful API**: Complete HTTP API for remote management
- **Rate limiting**: Built-in rate limiting for all result destinations

### Supported Inference Engines
- **Ultralytics**: YOLO object detection models (YOLOv8, YOLOv11, etc.)
- **Geti**: Intel's computer vision platform
- **ONNX Runtime**: Cross-platform ML model inference with CPU, OpenVINO, and GPU acceleration
- **Pass-through**: For testing and development
- **Custom**: Extensible framework for custom implementations

### Result Destinations
- **MQTT**: Publish results to MQTT brokers
- **Webhook**: HTTP POST to custom endpoints
- **Serial**: Output to serial ports (RS-232, USB)
- **OPC UA**: Industrial automation protocol
- **ROS2**: Robot Operating System 2
- **ZeroMQ**: High-performance messaging
- **Folder**: Save to local/network filesystem
- **Roboflow**: Integration with Roboflow platform
- **Geti**: Geti platform integration
- **Custom**: Implement your own destinations

## 📋 Requirements

- Python 3.10+
- Compatible with Windows, Linux
- Optional: CUDA for GPU acceleration
- Optional: MQTT broker for telemetry and result publishing

> **Note:** Only tested on a limited set of configurations so far (Windows / Ubuntu) x (Intel / Nvidia) - AMD and more is on the #todo list

## 🛠️ Installation

### Quick Start
```bash
# Clone the repository
git clone https://github.com/olkham/inference_node.git
cd inference_node

# Run the setup script (Windows)
setup.bat

# Or on Linux/macOS
chmod +x setup.sh
./setup.sh
```

### Manual Installation
```bash
# Install core dependencies
pip install -r requirements.txt

# Optional: Install AI/ML frameworks (if not already in requirements.txt)
pip install torch torchvision ultralytics geti-sdk

# Optional: Install ONNX Runtime (choose based on your hardware)
pip install onnxruntime>=1.16.0                    # CPU version
pip install onnxruntime-openvino>=1.16.0           # Intel OpenVINO acceleration
pip install "onnxruntime-gpu[cuda12,cudnn]>=1.16.0" # NVIDIA GPU acceleration

# Or use optional dependency groups from pyproject.toml
pip install -e .[onnx]              # CPU version
pip install -e .[onnx-openvino]     # Intel OpenVINO
pip install -e .[onnx-gpu]          # NVIDIA GPU

# Optional: Install GPU monitoring (uses nvidia-ml-py, not deprecated pynvml)
pip install nvidia-ml-py>=12.0.0

# Optional: Install serial communication
pip install pyserial>=3.5
```

## 🏃‍♂️ Quick Start

### 1. Start an Inference Node
```python
from InferenceNode import InferenceNode

# Create and start a node
node = InferenceNode("MyNode", port=5555)
node.start(enable_discovery=True, enable_telemetry=True)
```

Or use the command line:
```bash
# Start full node with all services using Flask
python main.py

# Start full node with all services using waitress (production mode)
python main.py --production

# Start with custom settings
python main.py --port 8080 --name "ProductionNode" --no-telemetry
```

### 2. Using Inference Engines
```python
import cv2

from InferenceEngine import InferenceEngineFactory

# Create and load an engine directly
engine = InferenceEngineFactory.create(
    'ultralytics',
    model_path='path/to/model.pt',
    device='cuda',
)

if not engine.load():
    raise RuntimeError('Model loading failed')

image = cv2.imread('path/to/image.jpg')
results = engine.infer(image)
payload = engine.result_to_json(results)
annotated = engine.draw(image, results)
```

Model upload and model IDs belong to the node's `ModelRepository`; they are not methods on an engine instance. Use the Models page or `POST /api/models/upload` for repository-backed pipelines.

### 3. Configure Result Publishing
```python
from ResultPublisher import ResultPublisher, ResultDestination

# Create result publisher
rp = ResultPublisher()

# Configure MQTT destination
rd_mqtt = ResultDestination('mqtt')
rd_mqtt.configure(
    server='localhost',
    topic='infernode/results',
    rate_limit=1.0  # 1 second between publishes
)
rp.add(rd_mqtt)

# Configure webhook destination
rd_webhook = ResultDestination('webhook')
rd_webhook.configure(
    url='http://myserver.com/webhook',
    rate_limit=0.5
)
rp.add(rd_webhook)

# Publish results
rp.publish({"inference_results": "data"})
```

## 🔧 API Reference

### Node Information
```bash
GET /api/info
```
Returns node capabilities and status.

### Engine Discovery
```bash
GET /api/inference/engines
```

Returns all auto-discovered engines with display metadata and dependency availability.

### Model Management
```bash
# Upload multipart form fields: file, engine_type, name, description
POST /api/models/upload

# List repository models
GET /api/models

# Get or delete one model
GET /api/models/<model_id>
DELETE /api/models/<model_id>
```

### Pipeline Management
```bash
# Create a persisted pipeline
POST /api/pipeline/create
{
  "name": "Camera pipeline",
  "frame_source": {
    "capture_type": "webcam",
    "config": {"source": 0}
  },
  "model": {
    "id": "model_123",
    "engine_type": "ultralytics",
    "device": "cpu"
  },
  "destinations": [
    {
      "type": "null",
      "config": {},
      "enabled": true
    }
  ]
}

# Start a persisted pipeline
POST /api/pipeline/<pipeline_id>/start
```

### Result Publisher Configuration
```bash
POST /api/publisher/configure
{
  "type": "mqtt",
  "config": {
    "server": "localhost",
    "topic": "results",
    "rate_limit": 1.0
  }
}
```

### Telemetry Control
```bash
# Start telemetry
POST /api/telemetry/start
{
  "mqtt": {
    "mqtt_server": "localhost",
    "mqtt_topic": "telemetry"
  }
}

# Stop telemetry
POST /api/telemetry/stop
```

## 📁 Project Structure

```
inference_node/
├── InferenceEngine/          # Inference engine implementations
│   ├── engines/
│   │   ├── base_engine.py        # Base class for all engines
│   │   ├── ultralytics_engine.py # Ultralytics YOLO support
│   │   ├── geti_engine.py        # Geti support
│   │   ├── onnx_engine.py         # ONNX Runtime support
│   │   ├── pass_engine.py        # Pass-through engine
│   │   └── example_engine_template.py # Custom engine template
│   ├── inference_engine_factory.py
│   └── result_converters.py
├── InferenceNode/            # Main node implementation
│   ├── inference_node.py     # Core node class
│   ├── pipeline_manager.py   # Pipeline orchestration
│   ├── pipeline.py           # Pipeline definitions
│   ├── discovery_manager.py  # Network discovery
│   ├── telemetry.py          # System telemetry
│   ├── model_repo.py         # Model repository
│   ├── hardware_detector.py  # Hardware detection
│   ├── log_manager.py        # Logging
│   ├── static/               # Web UI assets
│   └── templates/            # Web UI templates
├── ResultPublisher/          # Result publishing system
│   ├── publisher.py          # Main publisher class
│   ├── base_destination.py   # Base destination class
│   ├── result_destinations.py # Built-in destinations
│   └── plugins/              # Pluggable destinations
│       ├── mqtt_destination.py
│       ├── webhook_destination.py
│       ├── serial_destination.py
│       ├── opcua_destination.py
│       ├── ros2_destination.py
│       ├── zeromq_destination.py
│       ├── folder_destination.py
│       ├── roboflow_destination.py
│       ├── geti_destination.py
│       └── null_destination.py
├── main.py                   # Entry point
├── setup.bat                 # Windows setup script
├── setup.sh                  # Linux/macOS setup script
├── requirements.txt          # Dependencies
├── pyproject.toml            # Project configuration
├── Dockerfile                # Docker container
├── docker-compose.yml        # Docker compose configuration
└── readme.md                 # This file
```

## 🔧 Configuration

The node can be configured through:
- **Command-line arguments**: `python main.py --port 8080 --name "MyNode"`
- **Web UI**: Access the dashboard at `http://localhost:8080`
- **REST API**: Configure via API endpoints

Default settings:
- Node Port: 5555
- Discovery: Enabled
- Telemetry: Disabled by default
- Model Repository: `InferenceNode/model_repository/models/`
- Pipelines: `InferenceNode/pipelines/`

## 🧪 Testing

Run the current test suite with:

```bash
pytest -q
```

The existing automated coverage focuses on webhook authentication and the pipeline-to-webhook publishing path. Engine lifecycle, discovery, result-schema, and model-loading tests should be added for every new engine.


## 🔍 Monitoring and Telemetry

InferNode provides comprehensive system monitoring:

- **CPU usage and frequency**
- **Memory utilization**
- **Disk usage**
- **Network statistics**
- **GPU information (NVIDIA)**
- **Inference performance metrics**

Telemetry data is published to MQTT in JSON format:

```json
{
  "node_id": "uuid-here",
  "timestamp": "2025-07-28T10:30:00Z",
  "cpu": {"usage_percent": 45.2, "count": 8},
  "memory": {"usage_percent": 67.3, "total_gb": 16},
  "gpu": {"available": true, "devices": [...]}
}
```

## 🌐 Network Discovery

Nodes automatically discover each other using UDP broadcasts:

```python
from discovery import NodeDiscovery

# Discover nodes on network
discovered = NodeDiscovery.discover_nodes(timeout=5.0)
for node_id, info in discovered.items():
    print(f"Found node: {node_id} at {info['address']}")
```

## 🔌 Extending the Platform

### Creating Custom Inference Engines

```python
import json

from InferenceEngine.engines.base_engine import BaseInferenceEngine

class MyCustomEngine(BaseInferenceEngine):
    display_name = "My Custom Engine"

    def _load_model(self, model_file, device):
        # Load the model, then mark the engine ready.
        self.model = load_my_model(model_file, device=device)
        self.is_loaded = True
        return True

    def check_valid_model(self, model_file):
        return model_file.endswith('.myformat')
    
    def _preprocess(self, image):
        return processed_image
    
    def _infer(self, preprocessed_input):
        return raw_output
    
    def _postprocess(self, raw_output):
        return final_results

    def draw(self, image, results):
        return annotated_image

    def result_to_json(self, results, output_format="dict"):
        payload = {
            "task_type": "detection",
            "num_detections": len(results),
            "predictions": results,
        }
        return payload if output_format == "dict" else json.dumps(payload)
```

Place the implementation in `InferenceEngine/engines/` and restart the node. The factory generates the engine key from the class name and exposes it through `/api/inference/engines`.

For the complete result contract, dependency pattern, model upload steps, Pipeline Builder workflow, direct tests, and Docker deployment, see [CUSTOM_ENGINE_GUIDE.md](CUSTOM_ENGINE_GUIDE.md).

### Creating Custom Result Destinations

```python
from ResultPublisher.result_destinations import BaseResultDestination

class MyCustomDestination(BaseResultDestination):
    def configure(self, **kwargs):
        # Configure your destination
        self.is_configured = True
    
    def _publish(self, data):
        # Publish data to your destination
        return True  # Success
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit a pull request

## ⚠️ Known Issues

### Intel Geti SDK Compatibility
- **Issue**: Geti SDK support is limited to Python 3.10-3.13 only
- **Impact**: Users running Python 3.14+ cannot use Geti integration features
- **Workaround**: 
  - Use Python 3.10-3.13 for Geti functionality
  - Or install in a separate virtual environment with a compatible Python version
  - Geti SDK is in optional dependencies and won't block installation on incompatible Python versions

### Ultralytics on Intel Hardware
- **Issue**: On first run with Ultralytics models on Intel hardware, nodes may report failure to start
- **Cause**: Extra dependencies and model downloads required for OpenVINO conversion are not pre-installed
- **Impact**: Initial startup may fail or take longer than expected
- **Workaround**: 
  - Re-run the node after the initial failure - subsequent starts should work correctly
  - The required dependencies will be downloaded automatically on first run

## 📝 License

This project is licensed under the Apache 2.0 License - see the LICENSE file for details.

## 🆘 Support

For questions and support:
- Create an issue on GitHub
- Check the documentation
- Review the example code

## 🗺️ Roadmap

- [x] Web-based management interface
- [x] Integration with FrameSource library
- [x] Docker containers and orchestration
- [ ] Advanced load balancing
- [ ] Model versioning and A/B testing
- [ ] Enhanced pipeline builder UI
- [ ] Additional inference engine integrations
