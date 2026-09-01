# InferenceNode - Standalone Mode

The InferenceNode can run as a standalone AI inference server with a full web interface and REST API. It now uses the feature-rich templates and styling from the root project directory.

## Features

- **🧠 AI Inference**: Auto-discovered Ultralytics, ONNX Runtime, Geti, pass-through, and custom engines
- **🌐 Web Interface**: Full-featured web dashboard with system monitoring
- **📡 REST API**: Complete RESTful API for programmatic access
- **🔍 Network Discovery**: Automatic node discovery and announcement
- **📊 Telemetry**: System monitoring and metrics publishing
- **📤 Result Publishing**: Configurable result publishing to multiple destinations
- **🎨 Modern UI**: Bootstrap 5-based responsive interface with custom styling

## Quick Start

### Option 1: Interactive Startup (Recommended)
```cmd
cd InferenceNode
start_node.bat
```
This opens an interactive menu for different configuration options.

### Option 2: Direct Python Execution
```cmd
cd InferenceNode
python inference_node.py [port] [name] [discovery] [telemetry]
```

#### Examples:
```cmd
# Default settings (port 5000, auto-generated name, discovery enabled)
python inference_node.py

# Custom port
python inference_node.py 6000

# Custom port and name
python inference_node.py 6000 "ProductionNode"

# Disable discovery
python inference_node.py 5000 "PrivateNode" false

# Enable telemetry
python inference_node.py 5000 "MonitoredNode" true true
```

### Option 3: Using Full Python Path
```cmd
"c:\Users\olive\OneDrive\Projects\InferNode\venv\Scripts\python.exe" inference_node.py 5000 "MyNode"
```

## Web Interface

Once running, access the web interface at `http://localhost:[port]` where `[port]` is your chosen port (default 5000).

### Available Pages:

- **📊 Dashboard**: System overview, hardware info, quick actions
- **🧠 Models**: Upload and manage models associated with discovered engines
- **🔀 Pipeline Builder**: Combine a frame source, model/engine, device, and result destinations
- **▶️ Pipeline Management**: Start, stop, inspect, and stream persisted pipelines
- **📤 Publishers**: Configure result publishing destinations
- **📈 Telemetry**: System monitoring and performance metrics
- **📚 API Docs**: Complete REST API documentation
- **ℹ️ Node Info**: Detailed system information
- **📋 Logs**: System logs and activity monitoring

## REST API Endpoints

### Node Information
```http
GET /api/info
```
Returns node capabilities, hardware info, and status.

### Engine Discovery
```http
GET /api/inference/engines
```

Returns discovered engine types, display metadata, availability, and dependency errors. Engines are created and loaded when a pipeline starts; there is no standalone `/api/engine/load` endpoint.

### Model Upload
```http
POST /api/models/upload
Content-Type: multipart/form-data

file: [model_file]
engine_type: "onnx"
name: "My model"
description: "Optional description"
```

### Pipeline Creation
```http
POST /api/pipeline/create
Content-Type: application/json

{
    "name": "Camera pipeline",
    "frame_source": {
        "capture_type": "webcam",
        "config": {"source": 0}
    },
    "model": {
        "id": "uploaded_model_id",
        "engine_type": "onnx",
        "device": "cpu"
    },
    "destinations": [
        {"type": "null", "config": {}, "enabled": true}
    ]
}
```

Start the saved pipeline with `POST /api/pipeline/<pipeline_id>/start`. Inference is frame-source driven; the current node does not expose a standalone `/api/inference` image endpoint.

### Publisher Configuration
```http
POST /api/publisher/configure
Content-Type: application/json

{
    "type": "mqtt",
    "config": {
        "broker": "localhost",
        "port": 1883,
        "topic": "inference/results"
    }
}
```

### Telemetry Control
```http
POST /api/telemetry/start
POST /api/telemetry/stop
```

## Configuration

### Command Line Arguments

1. **Port** (optional): TCP port for web interface (default: 5000)
2. **Name** (optional): Node name (default: auto-generated)
3. **Discovery** (optional): Enable network discovery (default: true)
4. **Telemetry** (optional): Enable telemetry publishing (default: false)

### Environment Variables

The node respects the following environment variables:
- `FLASK_DEBUG`: Enable Flask debug mode
- `INFERENCE_NODE_PORT`: Default port if not specified
- `INFERENCE_NODE_NAME`: Default node name

## Integration with Discovery Server

When discovery is enabled (default), the node will:
1. Announce itself on the network via UDP broadcasts
2. Respond to discovery requests from other nodes
3. Appear automatically in the Discovery Server dashboard
4. Enable remote management through the Discovery Server

## Hardware Detection

The node automatically detects and reports:
- **CPU**: Core count and architecture
- **Memory**: Total system RAM
- **GPU**: NVIDIA GPU detection with VRAM info
- **Platform**: Operating system and version

## Dependencies

Required packages (automatically installed in virtual environment):
- `flask`: Web framework
- `psutil`: System monitoring
- `pynvml`: NVIDIA GPU detection (optional)
- `requests`: HTTP client for publishing

## File Structure

```
InferenceNode/
├── inference_node.py      # Main node implementation
├── start_node.bat         # Interactive startup script
├── discovery.py           # Network discovery service
├── telemetry.py          # System monitoring
└── __init__.py           # Package initialization

Templates and static files are loaded from:
├── ../templates/         # Feature-rich web interface templates
└── ../static/           # CSS, JavaScript, and assets
```

## Advanced Usage

### Custom Engine Integration

To add support for new AI engines:

1. Add a module under `InferenceEngine/engines/`.
2. Define a concrete subclass of `BaseInferenceEngine` with a lightweight `**kwargs` constructor.
3. Implement model validation/loading, preprocessing, inference, postprocessing, drawing, and result conversion.
4. Restart the node so `InferenceEngineFactory` discovers the class.
5. Confirm it appears in `GET /api/inference/engines`.
6. Upload a model with the matching engine type and use it in Pipeline Builder.

For the exact output schema and full workflow, see [`../CUSTOM_ENGINE_GUIDE.md`](../CUSTOM_ENGINE_GUIDE.md). In particular, `result_to_json(results, "dict")` must return a Python dictionary whose `predictions` value is a list.

### Custom Publishers

To add new result publishing destinations:

1. Extend the `ResultDestination` class
2. Implement destination-specific publishing logic
3. Register with the `ResultPublisher`

### Telemetry Extensions

To add custom metrics:

1. Extend the `NodeTelemetry` class
2. Add metric collection methods
3. Configure publishing intervals

## Troubleshooting

### Common Issues

**"No module named" errors**
- Ensure virtual environment is activated
- Run from correct directory
- Check Python path configuration

**Web interface not loading**
- Verify port is not in use: `netstat -an | findstr :[port]`
- Check firewall settings
- Ensure templates/static directories exist

**Discovery not working**
- Check UDP port 8888 availability
- Verify network connectivity
- Review firewall UDP rules

**GPU not detected**
- Install NVIDIA drivers
- Install `pynvml`: `pip install pynvml`
- Check CUDA installation

### Debug Mode

Run with Flask debug mode for detailed error messages:
```cmd
set FLASK_DEBUG=1
python inference_node.py
```

### Logging

Logs are written to console with timestamps. For file logging, modify the logging configuration in `inference_node.py`.

## Performance Optimization

- **CPU**: Use appropriate thread counts for inference
- **Memory**: Monitor memory usage for large models
- **GPU**: Enable GPU acceleration when available
- **Network**: Optimize discovery intervals for network load

## Security Considerations

- **Network Access**: Limit node access to trusted networks
- **API Security**: Consider adding authentication for production
- **File Uploads**: Validate uploaded model files
- **Secrets**: Change default Flask secret key in production

## License

This InferenceNode implementation is part of the InferNode project and follows the same licensing terms.
