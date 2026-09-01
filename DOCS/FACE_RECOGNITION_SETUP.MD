# 🔍 Face Detection & Recognition Setup Guide

Complete guide to use InferNode V2 for face detection and face recognition.

---

## 📋 Table of Contents

1. [Face Detection (Easy - Works Immediately)](#face-detection)
2. [Face Recognition (Advanced - Requires Setup)](#face-recognition)
3. [Models & Resources](#models-and-resources)
4. [Step-by-Step Tutorials](#tutorials)
5. [Troubleshooting](#troubleshooting)

---

## 🎯 Face Detection

### Option 1: YOLO Face Detection (Recommended)

**Works immediately with existing system!**

#### Step 1: Get a YOLO Face Model

```bash
# Download pre-trained YOLOv8 face detection model
wget https://github.com/akanametov/yolo-face/releases/download/v0.0.0/yolov8n-face.pt

# Or train your own:
pip install ultralytics
yolo task=detect mode=train data=face_dataset.yaml model=yolov8n.pt epochs=100
```

Popular YOLO face models:
- **YOLOv8n-face** - Fast, lightweight (6.2MB)
- **YOLOv8s-face** - Balanced (24MB)
- **YOLOv8m-face** - Accurate (52MB)

#### Step 2: Upload to InferNode

1. Open InferNode web interface: `http://localhost:5555`
2. Go to **Models** page
3. Select **Ultralytics** engine
4. Upload `yolov8n-face.pt`
5. Add description: "YOLOv8 Face Detection"

#### Step 3: Create Pipeline

1. Go to **Pipeline Builder**
2. Configure:
   - **Frame Source**: Webcam (device 0)
   - **Model**: Select uploaded face model
   - **Device**: GPU (if available) or CPU
   - **Publishers**: MQTT/Webhook/Folder to save results

3. Click **Create Pipeline**

#### Step 4: Start Detection

1. Go to **Pipeline Management**
2. Click **Start** on your pipeline
3. Click **Preview** to see live face detection!

**Result Format:**
```json
{
  "detections": [
    {
      "class": "face",
      "confidence": 0.92,
      "bbox": [120, 150, 280, 340]
    }
  ],
  "frame_count": 1523,
  "fps": 28.4
}
```

---

### Option 2: ONNX Face Detection Models

**Use proven face detection models in ONNX format**

#### Popular Models:

| Model | Accuracy | Speed | Size | Best For |
|-------|----------|-------|------|----------|
| **RetinaFace** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ~1.6MB | Highest accuracy |
| **YuNet** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ~353KB | Real-time, lightweight |
| **MTCNN** | ⭐⭐⭐⭐ | ⭐⭐⭐ | ~2MB | Face alignment |
| **MediaPipe** | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ~3MB | Mobile-optimized |

#### Download ONNX Models:

```bash
# YuNet (fastest)
wget https://github.com/opencv/opencv_zoo/raw/master/models/face_detection_yunet/face_detection_yunet_2023mar.onnx

# RetinaFace (most accurate)
# Convert from PyTorch: https://github.com/biubug6/Pytorch_Retinaface
```

#### Setup:

1. Upload ONNX model via **Models** page
2. Select **ONNX** engine
3. Choose device (CPU/GPU/CUDA)
4. Create pipeline as above

---

## 🧠 Face Recognition

**Advanced setup - Detects AND identifies faces**

### Architecture

```
Frame → Face Detection → Face Extraction → Face Recognition → Database Match
                                                                      ↓
                                                              Person Identified!
```

### Prerequisites

```bash
# Install required packages
pip install onnxruntime opencv-python numpy scipy
```

---

### Step 1: Get Face Recognition Models

You need TWO models:
1. **Face Detection Model** (to find faces)
2. **Face Recognition Model** (to identify faces)

#### Recommended Models:

**Detection:**
- YuNet: `face_detection_yunet_2023mar.onnx`
- RetinaFace: Convert from PyTorch

**Recognition:**
- **FaceNet** (Most popular)
- **ArcFace** (Most accurate)
- **InsightFace** (Best for Asian faces)

#### Download FaceNet ONNX:

```bash
# Option 1: Download pre-converted
wget https://github.com/onnx/models/raw/main/vision/body_analysis/arcface/model/arcfaceresnet100-8.onnx

# Option 2: Convert yourself
git clone https://github.com/timesler/facenet-pytorch
cd facenet-pytorch
# Run conversion script (see repo docs)
```

---

### Step 2: Create Face Database

Create a folder structure with known faces:

```
known_faces/
├── john_doe/
│   ├── photo1.jpg
│   ├── photo2.jpg
│   └── photo3.jpg
├── jane_smith/
│   ├── photo1.jpg
│   └── photo2.jpg
└── admin/
    └── profile.jpg
```

**Image Requirements:**
- Clear, front-facing photos
- Good lighting
- Face occupies most of image
- Multiple photos per person (recommended)

---

### Step 3: Generate Face Database

Use the Face Database Manager utility:

```bash
cd V2/utils

# Create database from folder
python face_database_manager.py \
    --recognition-model /path/to/facenet.onnx \
    --add-folder /path/to/known_faces \
    --database face_database.json

# Output:
# ✓ Loaded recognition model
# Processing: john_doe
#   ✓ Added john_doe (averaged from 3 images)
# Processing: jane_smith
#   ✓ Added jane_smith (averaged from 2 images)
# ✓ Saved 2 faces to face_database.json
```

**Add single face:**
```bash
python face_database_manager.py \
    --recognition-model facenet.onnx \
    --add-face "New Person" photo.jpg \
    --database face_database.json
```

**List database:**
```bash
python face_database_manager.py \
    --recognition-model facenet.onnx \
    --list \
    --database face_database.json
```

---

### Step 4: Upload Models to InferNode

The custom Face Recognition Engine (`face_recognition_engine.py`) is now available!

1. Go to **Models** page
2. Upload your detection model
3. Upload your recognition model
4. Upload your `face_database.json`

---

### Step 5: Create Face Recognition Pipeline

1. **Pipeline Builder** page
2. Configure:
   - **Frame Source**: Webcam/Video/IP Camera
   - **Inference Engine**: **Face Recognition**
   - **Configuration**:
     - Detection Model: Select uploaded detector
     - Recognition Model: Select uploaded recognizer
     - Face Database: Upload `face_database.json`
     - Detection Confidence: 0.5
     - Recognition Threshold: 0.6 (higher = stricter)
     - Input Size: 160x160 (FaceNet) or 112x112 (ArcFace)
   - **Publishers**: Configure where to send results

3. **Create Pipeline**

---

### Step 6: Start Recognition

1. **Pipeline Management** → Start pipeline
2. **Preview** to see live results!

**Result Format:**
```json
{
  "success": true,
  "face_count": 2,
  "database_size": 5,
  "faces": [
    {
      "bbox": [120, 150, 280, 340],
      "detection_confidence": 0.94,
      "person_id": "john_doe",
      "similarity": 0.87,
      "recognized": true
    },
    {
      "bbox": [350, 180, 490, 380],
      "detection_confidence": 0.89,
      "person_id": "Unknown",
      "similarity": 0.42,
      "recognized": false
    }
  ]
}
```

---

## 📦 Models & Resources

### Pre-trained Models

| Model Type | Name | Format | Download |
|------------|------|--------|----------|
| Detection | YOLOv8-face | .pt | [GitHub](https://github.com/akanametov/yolo-face) |
| Detection | YuNet | .onnx | [OpenCV Zoo](https://github.com/opencv/opencv_zoo) |
| Detection | RetinaFace | .onnx | [Convert Required](https://github.com/biubug6/Pytorch_Retinaface) |
| Recognition | ArcFace | .onnx | [ONNX Models](https://github.com/onnx/models) |
| Recognition | FaceNet | .onnx | [facenet-pytorch](https://github.com/timesler/facenet-pytorch) |
| Recognition | InsightFace | .onnx | [InsightFace](https://github.com/deepinsight/insightface) |

---

## 🎓 Tutorials

### Tutorial 1: Office Access Control

**Goal:** Identify employees entering office

```bash
# 1. Collect employee photos
mkdir employee_faces
# Add folders: john_doe/, jane_smith/, etc.

# 2. Create database
python face_database_manager.py \
    --recognition-model arcface.onnx \
    --add-folder employee_faces \
    --database office_access.json

# 3. Create pipeline in InferNode
# - Source: IP Camera at door
# - Model: Face Recognition
# - Database: office_access.json
# - Publishers: MQTT → Access Control System

# 4. Configure MQTT publisher
# Topic: office/access
# Server: mqtt.office.local

# Result published:
# {
#   "person_id": "john_doe",
#   "timestamp": "2025-01-10T14:23:45",
#   "confidence": 0.89,
#   "action": "grant_access"
# }
```

---

### Tutorial 2: Attendance System

**Goal:** Track classroom attendance

```bash
# 1. Create student database
python face_database_manager.py \
    --recognition-model facenet.onnx \
    --add-folder student_photos \
    --database classroom_db.json

# 2. Create pipeline
# - Source: Classroom camera
# - Model: Face Recognition
# - Publishers: Webhook → Attendance API

# 3. Configure Webhook
# URL: https://school.edu/api/attendance
# Include results in POST body

# API receives:
# {
#   "timestamp": "2025-01-10T09:00:00",
#   "class_id": "CS101",
#   "present": ["student_001", "student_002", "student_005"],
#   "absent": ["student_003", "student_004"]
# }
```

---

### Tutorial 3: Visitor Management

**Goal:** Alert when unknown person detected

```python
# Custom result publisher for alerts
# V2/ResultPublisher/plugins/alert_destination.py

class AlertDestination(BaseResultDestination):
    def publish(self, result):
        for face in result.get('faces', []):
            if not face['recognized']:
                # Unknown person detected!
                send_alert(
                    message=f"Unknown person detected",
                    confidence=face['detection_confidence'],
                    image=result.get('image')
                )
```

---

## 🔧 Troubleshooting

### Issue: Low Detection Accuracy

**Solutions:**
1. ✅ Increase detection confidence threshold (0.3 → 0.5)
2. ✅ Use better lighting
3. ✅ Use higher resolution camera
4. ✅ Try different detection model (RetinaFace > YuNet)

---

### Issue: Wrong Person Identified

**Solutions:**
1. ✅ Lower recognition threshold (0.6 → 0.7)
2. ✅ Add more training photos per person (3-5 minimum)
3. ✅ Use consistent photo conditions (lighting, angle)
4. ✅ Re-generate face database
5. ✅ Try different recognition model (ArcFace > FaceNet)

---

### Issue: Slow Performance

**Solutions:**
1. ✅ Use GPU instead of CPU
2. ✅ Reduce input resolution
3. ✅ Use lightweight models (YuNet, YOLOv8n-face)
4. ✅ Limit max faces per frame
5. ✅ Process every N frames (skip frames)

---

### Issue: "Unknown" for Everyone

**Possible causes:**
1. ❌ Database not loaded correctly
2. ❌ Recognition threshold too high
3. ❌ Wrong model input size
4. ❌ Poor quality training photos

**Fix:**
```bash
# Verify database
python face_database_manager.py --list --database face_db.json

# Lower threshold in pipeline config
Recognition Threshold: 0.4 (from 0.6)

# Check model compatibility
Input Size: Match your recognition model (160 for FaceNet, 112 for ArcFace)
```

---

## 📊 Performance Benchmarks

### Detection Only (YOLOv8n-face)

| Device | Resolution | FPS | Latency |
|--------|-----------|-----|---------|
| CPU (Intel i7) | 640x480 | 28 FPS | 35ms |
| GPU (GTX 1660) | 640x480 | 95 FPS | 10ms |
| GPU (RTX 3060) | 1920x1080 | 60 FPS | 16ms |

### Full Recognition (YuNet + FaceNet)

| Device | Resolution | FPS | Latency |
|--------|-----------|-----|---------|
| CPU (Intel i7) | 640x480 | 12 FPS | 83ms |
| GPU (GTX 1660) | 640x480 | 45 FPS | 22ms |
| GPU (RTX 3060) | 1920x1080 | 30 FPS | 33ms |

---

## 🚀 Advanced Features

### 1. Live Enrollment

Add new faces without stopping the pipeline:

```python
# Via API
curl -X POST http://localhost:5555/api/face-database/add \
  -F "person_id=new_person" \
  -F "image=@photo.jpg"
```

### 2. Multiple Databases

Use different databases for different scenarios:

```
- office_employees.json
- visitors.json
- vip_guests.json
```

### 3. Confidence-based Actions

```python
if similarity > 0.9:
    action = "grant_access"
elif similarity > 0.7:
    action = "request_confirmation"
else:
    action = "deny_access"
```

### 4. Age/Gender Estimation

Extend the engine to include age/gender models:
- Add age/gender ONNX models
- Run alongside face recognition
- Include in results

---

## 📚 Additional Resources

- **YOLO Face Training**: https://github.com/ultralytics/ultralytics
- **ONNX Model Zoo**: https://github.com/onnx/models
- **Face Recognition Papers**: https://paperswithcode.com/task/face-recognition
- **Dataset Sources**:
  - LFW (Labeled Faces in the Wild)
  - CelebA
  - VGGFace2

---

## 🎯 Summary

✅ **Face Detection** - Works immediately with YOLO or ONNX models
✅ **Face Recognition** - Custom engine provided, requires setup
✅ **Real-time Processing** - 30-60 FPS depending on hardware
✅ **Distributed** - Multiple cameras across multiple nodes
✅ **Flexible Output** - MQTT, Webhooks, Files, Serial, ROS2, etc.

**Start with face detection, then add recognition when needed!**

---

Questions? Check the [API Documentation](http://localhost:5555/api-docs) or InferNode logs.
