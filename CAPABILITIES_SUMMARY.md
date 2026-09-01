# 🎯 ArmyEye - Complete Capabilities Summary

## 📋 Table of Contents
1. [Administrative Features](#administrative-features)
2. [User Interface & Web Dashboard](#user-interface--web-dashboard)
3. [Face Detection & Recognition](#face-detection--recognition)
4. [Object Detection & Tracking](#object-detection--tracking)
5. [Inference Engines](#inference-engines)
6. [Frame Sources](#frame-sources)
7. [Result Publishing Destinations](#result-publishing-destinations)
8. [REST API Capabilities](#rest-api-capabilities)
9. [Network & Discovery](#network--discovery)
10. [Telemetry & Monitoring](#telemetry--monitoring)
11. [Hardware Support](#hardware-support)
12. [Pipeline Management](#pipeline-management)

---

## 🛠️ Administrative Features

### Node Management
- **Multi-node Architecture**: Deploy and manage multiple inference nodes across networks
- **Auto-discovery**: Automatic network discovery of nodes via UDP broadcasts
- **Remote Management**: Control nodes remotely through Discovery Server
- **Node Naming**: Custom node names with auto-generation fallback
- **Port Configuration**: Flexible port configuration (default: 5555)
- **Service Control**: Start/stop nodes with discovery and telemetry options

### Configuration Management
- **Command-line Configuration**: Configure via command-line arguments
- **Web UI Configuration**: Full configuration through web interface
- **REST API Configuration**: Programmatic configuration via API
- **Environment Variables**: Support for environment-based configuration
- **Settings Persistence**: Node settings saved in JSON format

### Security & Access
- **Webhook Receiver**: Built-in endpoint to receive webhook POST requests (`/webhook/<webhook_id>`)
- **Dynamic Port Detection**: Automatic port detection for webhook URLs (80 for HTTP, 443 for HTTPS)
- **Nginx Proxy Support**: Optimized for nginx reverse proxy setups
- **Health Check Endpoint**: `/health` endpoint for Docker and monitoring

---

## 🌐 User Interface & Web Dashboard

### Web Pages
1. **📊 Dashboard** (`/`)
   - System overview and hardware information
   - Quick actions and status indicators
   - Real-time system metrics

2. **⚙️ Models** (`/models`)
   - AI model management and upload
   - Model repository browsing
   - Model loading and configuration
   - Support for multiple model formats (.pt, .onnx, etc.)

3. **🔧 Pipeline Builder** (`/pipeline-builder`)
   - Visual pipeline configuration
   - Frame source selection
   - Model assignment
   - Publisher configuration
   - Real-time preview setup

4. **📋 Pipeline Management** (`/pipeline-management`)
   - View all pipelines
   - Start/stop/pause pipelines
   - Live preview of pipeline output
   - Pipeline statistics and metrics
   - Pipeline deletion

5. **📤 Publishers** (`/publisher`)
   - Configure result publishing destinations
   - Multiple destination support per pipeline
   - Rate limiting configuration
   - Destination testing

6. **📈 Telemetry** (`/telemetry`)
   - System monitoring dashboard
   - Performance metrics visualization
   - Real-time telemetry data
   - MQTT telemetry configuration

7. **📚 API Docs** (`/api-docs`)
   - Complete REST API documentation
   - Interactive API testing
   - Endpoint examples and schemas

8. **ℹ️ Node Info** (`/node-info`)
   - Detailed system information
   - Hardware capabilities
   - Node configuration details

9. **📋 Logs** (`/logs`)
   - System logs viewer
   - Log filtering by level, component, search term
   - Log statistics
   - Real-time log streaming

10. **🔍 Node Discovery** (`/node-discovery`)
    - Network node discovery interface
    - Discovered nodes listing
    - Node status and capabilities

### UI Features
- **Bootstrap 5**: Modern, responsive design
- **Real-time Updates**: Live data refresh
- **Interactive Testing**: Built-in API testing tools
- **Responsive Design**: Works on desktop, tablet, and mobile
- **Dark/Light Theme**: Customizable styling

---

## 👤 Face Detection & Recognition

### Face Detection
**Supported Methods:**
1. **YOLO Face Detection** (Recommended)
   - YOLOv8n-face: Fast, lightweight (6.2MB)
   - YOLOv8s-face: Balanced (24MB)
   - YOLOv8m-face: Accurate (52MB)
   - Works immediately with existing system

2. **ONNX Face Detection Models**
   - **RetinaFace**: Highest accuracy (~1.6MB)
   - **YuNet**: Real-time, lightweight (~353KB)
   - **MTCNN**: Face alignment (~2MB)
   - **MediaPipe**: Mobile-optimized (~3MB)

3. **OpenCV DNN Face Detection**
   - Built-in OpenCV DNN support
   - Haar Cascade fallback
   - Configurable confidence thresholds

### Face Recognition (Advanced)
**Architecture:**
```
Frame → Face Detection → Face Extraction → Face Recognition → Database Match → Person Identified!
```

**Supported Models:**
- **FaceNet**: Most popular
- **ArcFace**: Most accurate
- **InsightFace**: Best for Asian faces

**Features:**
- Face database management
- Live enrollment (add faces without stopping pipeline)
- Multiple databases support
- Confidence-based actions
- Similarity threshold configuration
- Person identification with confidence scores

**Use Cases:**
- Office access control
- Visitor management
- Security monitoring
- VIP guest recognition

### Face Detection Integration
- **Person + Face Detection**: Optional face detection requirement for person detections
- **Quality-based Detection**: Collects multiple detections and sends best quality
- **Region-based Detection**: Face detection within person bounding boxes
- **Configurable Thresholds**: Adjustable confidence levels

---

## 🎯 Object Detection & Tracking

### Object Detection
**Supported Models:**
- **YOLOv8**: Latest YOLO models (nano, small, medium, large, xlarge)
- **YOLOv11**: Next-generation YOLO models
- **YOLOv12**: Cutting-edge YOLO models
- **Custom YOLO Models**: Train and deploy your own
- **ONNX Models**: Any ONNX-compatible detection model
- **Geti Models**: Intel Geti platform models

**Detection Features:**
- Real-time object detection
- Multi-class detection
- Confidence scoring
- Bounding box coordinates
- Batch processing support
- Custom class filtering

### Object Tracking
**Tracking Algorithms:**
1. **Bot-SORT** (Default - StrongSORT-like)
   - Most powerful tracker in Ultralytics
   - Combines ReID + Kalman filter
   - Python 3.12 compatible
   - Performance: Bot-SORT ≈ StrongSORT > OCSORT > ByteTrack

2. **DeepSORT Integration**
   - Multi-object tracking
   - Track ID assignment
   - Track persistence across frames

**Tracked Object Classes:**
- Person
- Vehicles: car, truck, bus, motorcycle, bicycle, train
- Face
- Custom classes (configurable)

**Tracking Features:**
- **Track IDs**: Unique IDs for each tracked object
- **20-minute Cooldown**: Prevents duplicate detections of same object
- **Quality Buffer**: Collects detections over 4 seconds, sends best quality
- **Deduplication**: IOU-based duplicate detection prevention
- **Track TTL**: 600 seconds (20 minutes) cooldown period
- **Confidence Thresholds**: Separate thresholds for tracked vs. new objects

**Tracking Statistics:**
- Frame count per track
- First seen timestamp
- Best detection quality
- Track duration

---

## 🧠 Inference Engines

### Supported Engines

1. **Ultralytics YOLO Engine**
   - YOLOv8, YOLOv11, YOLOv12 support
   - Object detection, segmentation, classification
   - Built-in Bot-SORT tracking
   - GPU/CPU/OpenVINO acceleration
   - Model formats: .pt (PyTorch)

2. **ONNX Runtime Engine**
   - Cross-platform ML inference
   - CPU, OpenVINO, CUDA acceleration
   - Model formats: .onnx
   - Supports any ONNX-compatible model

3. **Geti Engine**
   - Intel Geti platform integration
   - Computer vision models
   - Requires Python 3.10-3.13

4. **Pass-through Engine**
   - Testing and development
   - No actual inference
   - Useful for pipeline testing

5. **Custom Engine**
   - Extensible framework
   - Implement your own inference logic
   - Template provided for easy implementation

### Engine Features
- **Device Selection**: CPU, GPU (CUDA), OpenVINO
- **Model Upload**: Web interface and API support
- **Model Repository**: Centralized model storage
- **Model Metadata**: Description, version, tags
- **Dynamic Loading**: Load/unload models on demand
- **Multi-model Support**: Multiple models per node
- **Model Versioning**: Track model versions

---

## 📹 Frame Sources

### Supported Sources
1. **Webcam**
   - USB webcams
   - Built-in cameras
   - Device ID selection
   - Resolution configuration

2. **Video Files**
   - MP4, AVI, MOV, MKV support
   - Local file paths
   - Network file paths
   - Frame rate control

3. **IP Cameras**
   - RTSP streams
   - HTTP streams
   - ONVIF support (via FrameSource library)
   - Authentication support

4. **Network Streams**
   - RTSP URLs
   - HTTP/HTTPS streams
   - UDP streams
   - Custom stream URLs

5. **Image Files**
   - Single image inference
   - Batch image processing
   - Directory scanning

### Frame Source Features
- **FrameSource Library Integration**: Full FrameSource[full] support
- **Auto-reconnection**: Automatic reconnection on stream loss
- **Frame Rate Control**: Configurable FPS limits
- **Resolution Control**: Resize and crop options
- **Format Conversion**: Automatic format handling
- **Error Handling**: Graceful error recovery

---

## 📤 Result Publishing Destinations

### Available Destinations

1. **MQTT** (Primary)
   - Publish to MQTT brokers
   - Topic-based routing
   - QoS levels
   - Authentication support
   - Variable substitution in topics

2. **Webhook** (Primary)
   - HTTP POST requests
   - Custom endpoints
   - Dynamic port detection (80/443)
   - Nginx proxy support
   - Variable substitution in URLs
   - Custom headers

3. **Serial Port** (Primary)
   - RS-232 communication
   - USB serial devices
   - Configurable baud rates
   - Custom protocols

4. **Folder/File** (Primary)
   - Save to local filesystem
   - Network filesystem support
   - JSON file format
   - Image saving (optional)
   - Timestamped filenames

5. **ZeroMQ**
   - High-performance messaging
   - PUB/SUB pattern
   - Request/Reply pattern

6. **OPC UA**
   - Industrial automation protocol
   - Node-based publishing
   - Security policies
   - Authentication

7. **ROS2**
   - Robot Operating System 2
   - Topic publishing
   - Message types

8. **Roboflow**
   - Roboflow platform integration
   - Image upload
   - Dataset management

9. **Geti**
   - Geti platform integration
   - Model training data
   - Annotation support

10. **Null Destination**
    - Discard results (testing)
    - No-op destination

### Publishing Features
- **Rate Limiting**: Configurable rate limits per destination
- **Max Frames**: Limit number of frames published
- **Image Inclusion**: Optional original/result image inclusion
- **Variable Substitution**: Dynamic values in URLs/topics
  - `{pipeline_id}`, `{model_name}`, `{node_id}`
  - `{port}`, `{api_port}` (auto-detected)
  - `{timestamp}`, `{date}`, `{time}`
  - `{hostname}`, `{unix_time}`
- **Multiple Destinations**: Multiple publishers per pipeline
- **Async Publishing**: Non-blocking result publishing
- **Error Handling**: Graceful failure handling
- **Retry Logic**: Automatic retry on failures

---

## 🔌 REST API Capabilities

### Node Information
- `GET /api/info` - Node capabilities and status
- `GET /health` - Health check endpoint
- `GET /api/logs` - System logs with filtering

### Engine Management
- `POST /api/engine/load` - Load inference engine
- `POST /api/engine/upload` - Upload model file
- `GET /api/engines` - List available engines
- `GET /api/engines/<engine_id>` - Get engine details
- `DELETE /api/engines/<engine_id>` - Unload engine

### Model Management
- `GET /api/models` - List all models
- `POST /api/models/upload` - Upload new model
- `GET /api/models/<model_id>` - Get model details
- `DELETE /api/models/<model_id>` - Delete model

### Inference
- `POST /api/inference` - Run inference on image
- `POST /api/inference/batch` - Batch inference

### Pipeline Management
- `GET /api/pipelines` - List all pipelines
- `POST /api/pipelines` - Create new pipeline
- `GET /api/pipeline/<pipeline_id>` - Get pipeline details
- `PUT /api/pipeline/<pipeline_id>` - Update pipeline
- `DELETE /api/pipeline/<pipeline_id>` - Delete pipeline
- `POST /api/pipeline/<pipeline_id>/start` - Start pipeline
- `POST /api/pipeline/<pipeline_id>/stop` - Stop pipeline
- `POST /api/pipeline/<pipeline_id>/pause` - Pause pipeline
- `GET /api/pipeline/<pipeline_id>/preview` - Get preview stream
- `GET /api/pipelines/metrics` - Get pipeline metrics
- `GET /api/pipelines/summary` - Get pipeline summary

### Publisher Configuration
- `POST /api/publisher/configure` - Configure publisher
- `GET /api/publisher/destinations` - List available destinations
- `POST /api/publisher/test` - Test destination

### Telemetry
- `POST /api/telemetry/start` - Start telemetry
- `POST /api/telemetry/stop` - Stop telemetry
- `GET /api/telemetry/status` - Get telemetry status

### Webhook Receiver
- `POST /webhook/<webhook_id>` - Receive webhook POST requests
- Returns JSON with success status and received data keys

### API Features
- **JSON Responses**: All endpoints return JSON
- **Error Handling**: Comprehensive error messages
- **Authentication**: Ready for authentication integration
- **CORS Support**: Cross-origin resource sharing
- **Rate Limiting**: Built-in rate limiting (configurable)

---

## 🌐 Network & Discovery

### Network Discovery
- **UDP Broadcasts**: Automatic node announcement
- **mDNS/Bonjour**: Zero-configuration networking
- **Discovery Server**: Centralized node management
- **Auto-discovery**: Nodes discover each other automatically
- **Manual Discovery**: Manual node discovery via API

### Discovery Features
- **Node Announcement**: Automatic network presence
- **Capability Exchange**: Share node capabilities
- **Status Updates**: Real-time status broadcasting
- **Network Topology**: Visual network representation
- **Remote Management**: Control nodes remotely

### Network Configuration
- **Port Configuration**: Flexible port assignment
- **Network Interface Selection**: Choose network interface
- **Firewall Friendly**: Works through firewalls (with configuration)
- **IPv4/IPv6 Support**: Both protocol versions

---

## 📊 Telemetry & Monitoring

### System Monitoring
- **CPU Metrics**: Usage percentage, frequency, core count
- **Memory Metrics**: Usage percentage, total/available memory
- **Disk Metrics**: Usage, read/write speeds
- **Network Metrics**: Bandwidth, packet statistics
- **GPU Metrics**: NVIDIA GPU information (if available)
  - GPU usage, memory, temperature
  - Multiple GPU support

### Inference Metrics
- **FPS**: Frames per second
- **Latency**: Inference latency
- **Throughput**: Processing throughput
- **Error Rates**: Success/failure rates
- **Queue Depth**: Processing queue status

### Telemetry Publishing
- **MQTT Publishing**: Publish metrics to MQTT
- **Configurable Interval**: Adjustable update frequency
- **JSON Format**: Structured JSON telemetry data
- **Real-time Updates**: Live metric streaming

### Monitoring Features
- **Dashboard Visualization**: Real-time charts and graphs
- **Historical Data**: Metric history (if configured)
- **Alerts**: Configurable alert thresholds
- **Export**: Export metrics data

---

## 💻 Hardware Support

### CPU Support
- **Intel Processors**: Full support
- **AMD Processors**: Basic support (limited testing)
- **Multi-core**: Automatic multi-core utilization
- **OpenVINO**: Intel OpenVINO acceleration

### GPU Support
- **NVIDIA GPUs**: Full CUDA support
  - CUDA 12.x support
  - cuDNN integration
  - Multi-GPU support
  - GPU monitoring (nvidia-ml-py)
- **AMD GPUs**: Limited support (on roadmap)
- **Intel GPUs**: OpenVINO support

### Device Selection
- **CPU**: Default, always available
- **CUDA**: NVIDIA GPU acceleration
- **OpenVINO**: Intel hardware acceleration
- **Auto-detection**: Automatic best device selection

### Hardware Detection
- **Automatic Detection**: Detects available hardware
- **Capability Reporting**: Reports hardware capabilities
- **Performance Optimization**: Optimizes for available hardware

---

## 🔄 Pipeline Management

### Pipeline Features
- **Visual Builder**: Web-based pipeline configuration
- **Multiple Pipelines**: Run multiple pipelines simultaneously
- **Pipeline Templates**: Save and reuse configurations
- **Pipeline Cloning**: Duplicate existing pipelines
- **Pipeline Export/Import**: Share pipeline configurations

### Pipeline Configuration
- **Frame Source**: Select input source
- **Inference Engine**: Choose AI engine
- **Model Selection**: Assign models to pipelines
- **Publishers**: Configure result destinations
- **Advanced Settings**: Fine-tune pipeline behavior

### Pipeline Control
- **Start/Stop**: Control pipeline execution
- **Pause/Resume**: Temporarily pause processing
- **Preview**: Live preview of pipeline output
- **Statistics**: Real-time pipeline statistics
- **Error Handling**: Automatic error recovery

### Pipeline Monitoring
- **Status Tracking**: Real-time status updates
- **Performance Metrics**: FPS, latency, throughput
- **Resource Usage**: CPU/GPU/memory usage per pipeline
- **Error Logging**: Comprehensive error tracking

---

## 🎨 Additional Features

### Quality Assurance
- **Best Quality Detection**: Collects multiple detections, sends best
- **Confidence Filtering**: Configurable confidence thresholds
- **Deduplication**: Prevents duplicate detections
- **Quality Buffer**: 4-second buffer for best detection selection

### Performance Optimization
- **Async Processing**: Non-blocking operations
- **Thread Pool**: Efficient task distribution
- **Memory Management**: Optimized memory usage
- **Frame Skipping**: Optional frame skipping for performance

### Extensibility
- **Custom Engines**: Implement custom inference engines
- **Custom Destinations**: Create custom result publishers
- **Plugin System**: Modular plugin architecture
- **API Extensions**: Extend API with custom endpoints

### Development Features
- **Logging**: Comprehensive logging system
- **Debug Mode**: Detailed debug information
- **Error Reporting**: Detailed error messages
- **Testing Tools**: Built-in testing utilities

---

## 📝 Summary

**ArmyEye** is a comprehensive, enterprise-grade AI inference platform that provides:

✅ **Multi-engine AI inference** (YOLO, ONNX, Geti, Custom)  
✅ **Face detection and recognition** with database matching  
✅ **Advanced object tracking** (Bot-SORT, DeepSORT)  
✅ **Multiple frame sources** (Webcam, Video, IP Camera, RTSP)  
✅ **10+ result publishing destinations** (MQTT, Webhook, Serial, OPC UA, ROS2, etc.)  
✅ **Full web dashboard** with 10+ management pages  
✅ **Complete REST API** with 30+ endpoints  
✅ **Network discovery** and multi-node management  
✅ **Real-time telemetry** and monitoring  
✅ **Hardware acceleration** (CPU, GPU, OpenVINO)  
✅ **Pipeline management** with visual builder  
✅ **Production-ready** with nginx proxy support  

**Perfect for:**
- Security and surveillance systems
- Access control and face recognition
- Traffic monitoring and ANPR
- Industrial automation
- Robotics and autonomous systems
- Research and development
- Edge AI deployments

---

*Last Updated: December 2024*

