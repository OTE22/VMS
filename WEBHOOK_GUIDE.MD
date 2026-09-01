# Webhook Guide - Sending Face Detection Results & Images

This guide shows you how to configure webhooks to send face recognition results and images to your own endpoint.

---

## 📡 What Gets Sent?

The webhook can send:
- **Detection Results** (JSON) - Face bounding boxes, identities, match scores
- **Original Image** (Base64 encoded) - The input frame
- **Result Image** (Base64 encoded) - Frame with drawn bounding boxes and labels

---

## 🔧 Configuration Options

### Basic Configuration

In the **Pipeline Builder**, when configuring your Result Publisher:

```json
{
  "type": "webhook",
  "url": "http://your-server.com/api/detections",
  "timeout": 30,
  "rate_limit": 1.0,
  "include_image_data": true,
  "include_result_image": true,
  "headers": {
    "Content-Type": "application/json",
    "Authorization": "Bearer your-api-key-here"
  }
}
```

### Configuration Parameters

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `url` | string | HTTP endpoint URL (supports variables) | **Required** |
| `timeout` | number | Request timeout in seconds | 30 |
| `rate_limit` | number | Min seconds between requests (0 = unlimited) | None |
| `max_frames` | number | Max frames to send before auto-pause (0 = unlimited) | None |
| `include_image_data` | boolean | Send original frame as Base64 | false |
| `include_result_image` | boolean | Send annotated frame as Base64 | false |
| `headers` | object | Custom HTTP headers | `{"Content-Type": "application/json"}` |

---

## 📨 Webhook Payload Structure

### Without Images (Default)

```json
{
  "node_id": "8283ed97-c506-4a61-ae75-ea71fa7bca09",
  "timestamp": "2025-12-13T10:30:45.123456Z",
  "results": {
    "predictions": [
      {
        "bbox": [100, 120, 250, 350],
        "confidence": 0.95,
        "identity": "John Doe",
        "match_score": 0.87,
        "recognized": true
      },
      {
        "bbox": [400, 150, 520, 320],
        "confidence": 0.92,
        "identity": "Unknown",
        "match_score": 0.23,
        "recognized": false
      }
    ],
    "count": 2,
    "engine": "face_recognition",
    "version": "1.0.0"
  }
}
```

### With Images Included

```json
{
  "node_id": "8283ed97-c506-4a61-ae75-ea71fa7bca09",
  "timestamp": "2025-12-13T10:30:45.123456Z",
  "results": {
    "predictions": [...],
    "count": 2,
    "engine": "face_recognition",
    "version": "1.0.0"
  },
  "image": "/9j/4AAQSkZJRgABAQAAAQABAAD...",  // Base64 encoded JPG (original frame)
  "result_image": "/9j/4AAQSkZJRgABAQAAAQA..."   // Base64 encoded JPG (with boxes drawn)
}
```

---

## 🎯 URL Variable Substitution

You can use variables in your webhook URL for dynamic routing:

### Supported Variables

```
{pipeline_id}   - Pipeline UUID
{node_id}       - Node identifier
{model_name}    - Model being used
{hostname}      - System hostname
{timestamp}     - Current timestamp (ISO format)
{date}          - Current date (YYYY-MM-DD)
{time}          - Current time (HH:MM:SS)
{unix_time}     - Unix timestamp
```

### Examples

```
# Route by pipeline
http://api.example.com/detections/{pipeline_id}

# Route with timestamp
http://api.example.com/faces/{date}/{time}/results

# Complex routing
http://api.example.com/node/{node_id}/pipeline/{pipeline_id}/detections
```

---

## 💻 Example Server Implementation

### Flask Server (Python)

```python
from flask import Flask, request, jsonify
import base64
import cv2
import numpy as np

app = Flask(__name__)

@app.route('/api/detections', methods=['POST'])
def handle_detections():
    data = request.json

    # Extract detection results
    results = data.get('results', {})
    predictions = results.get('predictions', [])

    print(f"Received {len(predictions)} face detections")

    # Process each detection
    for pred in predictions:
        identity = pred['identity']
        confidence = pred['confidence']
        match_score = pred['match_score']
        recognized = pred['recognized']
        bbox = pred['bbox']

        print(f"  - {identity} (conf: {confidence:.2f}, match: {match_score:.2f})")

    # Decode and save images if included
    if 'image' in data:
        # Decode original image
        img_data = base64.b64decode(data['image'])
        nparr = np.frombuffer(img_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        # Save to file
        cv2.imwrite(f"detections/original_{data['timestamp']}.jpg", img)

    if 'result_image' in data:
        # Decode result image (with bounding boxes)
        img_data = base64.b64decode(data['result_image'])
        nparr = np.frombuffer(img_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        # Save to file
        cv2.imwrite(f"detections/result_{data['timestamp']}.jpg", img)

    return jsonify({"status": "success", "processed": len(predictions)}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
```

### Express Server (Node.js)

```javascript
const express = require('express');
const fs = require('fs');
const path = require('path');

const app = express();
app.use(express.json({limit: '50mb'})); // Increase limit for images

app.post('/api/detections', (req, res) => {
    const data = req.body;
    const results = data.results || {};
    const predictions = results.predictions || [];

    console.log(`Received ${predictions.length} face detections`);

    // Process detections
    predictions.forEach(pred => {
        console.log(`  - ${pred.identity} (conf: ${pred.confidence.toFixed(2)}, match: ${pred.match_score.toFixed(2)})`);
    });

    // Save images if included
    if (data.image) {
        const imgBuffer = Buffer.from(data.image, 'base64');
        fs.writeFileSync(`detections/original_${Date.now()}.jpg`, imgBuffer);
    }

    if (data.result_image) {
        const imgBuffer = Buffer.from(data.result_image, 'base64');
        fs.writeFileSync(`detections/result_${Date.now()}.jpg`, imgBuffer);
    }

    res.json({ status: 'success', processed: predictions.length });
});

app.listen(5000, () => {
    console.log('Webhook server listening on port 5000');
});
```

### FastAPI Server (Python - Async)

```python
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import List, Optional
import base64
import cv2
import numpy as np

app = FastAPI()

class Prediction(BaseModel):
    bbox: List[float]
    confidence: float
    identity: str
    match_score: float
    recognized: bool

class DetectionData(BaseModel):
    node_id: str
    timestamp: str
    results: dict
    image: Optional[str] = None
    result_image: Optional[str] = None

@app.post("/api/detections")
async def handle_detections(data: DetectionData):
    predictions = data.results.get('predictions', [])

    print(f"Received {len(predictions)} face detections")

    # Process detections
    for pred in predictions:
        print(f"  - {pred['identity']} (conf: {pred['confidence']:.2f}, match: {pred['match_score']:.2f})")

    # Decode images
    if data.image:
        img_data = base64.b64decode(data.image)
        nparr = np.frombuffer(img_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        cv2.imwrite(f"detections/original_{data.timestamp}.jpg", img)

    if data.result_image:
        img_data = base64.b64decode(data.result_image)
        nparr = np.frombuffer(img_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        cv2.imwrite(f"detections/result_{data.timestamp}.jpg", img)

    return {"status": "success", "processed": len(predictions)}

# Run with: uvicorn server:app --host 0.0.0.0 --port 5000
```

---

## 🚀 Configuration via ArmyEye Web UI

1. **Navigate to Pipeline Builder**
2. **Select your Pipeline**
3. **Add Result Publisher**
4. **Choose "Webhook" destination**
5. **Configure**:
   - URL: `http://your-server:5000/api/detections`
   - Rate Limit: `1.0` (1 detection per second)
   - Include Image Data: ✅ (check to send original frames)
   - Include Result Image: ✅ (check to send annotated frames)
   - Custom Headers:
     ```
     Authorization: Bearer your-secret-token
     X-API-Key: your-api-key
     ```

---

## 📊 Advanced Use Cases

### 1. Send Only High-Confidence Detections

Use rate limiting and process server-side:

```python
@app.route('/api/detections', methods=['POST'])
def handle_detections():
    data = request.json
    predictions = data.get('results', {}).get('predictions', [])

    # Filter high confidence only
    high_conf = [p for p in predictions if p['recognized'] and p['match_score'] > 0.75]

    if high_conf:
        # Store in database
        for pred in high_conf:
            db.store_detection(
                identity=pred['identity'],
                confidence=pred['match_score'],
                timestamp=data['timestamp']
            )

    return jsonify({"status": "success"})
```

### 2. Real-time Alerts

Send notifications when specific people are detected:

```python
@app.route('/api/detections', methods=['POST'])
def handle_detections():
    data = request.json
    predictions = data.get('results', {}).get('predictions', [])

    # Alert on VIP detection
    vip_list = ['CEO', 'Manager', 'Security']

    for pred in predictions:
        if pred['identity'] in vip_list and pred['recognized']:
            send_alert(
                f"🚨 {pred['identity']} detected!",
                image=data.get('result_image')
            )

    return jsonify({"status": "success"})
```

### 3. Cloud Storage Upload

```python
import boto3
from datetime import datetime

s3 = boto3.client('s3')

@app.route('/api/detections', methods=['POST'])
def handle_detections():
    data = request.json

    # Upload result image to S3
    if data.get('result_image'):
        img_data = base64.b64decode(data['result_image'])
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        s3.put_object(
            Bucket='face-detections',
            Key=f'detections/{timestamp}.jpg',
            Body=img_data,
            ContentType='image/jpeg'
        )

    return jsonify({"status": "success"})
```

---

## 🔐 Security Best Practices

### 1. Use HTTPS
```json
{
  "url": "https://api.example.com/detections"
}
```

### 2. API Key Authentication
```json
{
  "headers": {
    "Authorization": "Bearer your-secret-api-key",
    "X-API-Key": "your-api-key"
  }
}
```

### 3. Validate Requests on Server

```python
API_KEY = "your-secret-key"

@app.route('/api/detections', methods=['POST'])
def handle_detections():
    # Verify API key
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {API_KEY}":
        return jsonify({"error": "Unauthorized"}), 401

    # Process data
    data = request.json
    # ...

    return jsonify({"status": "success"})
```

---

## 🐛 Troubleshooting

### Webhook Not Receiving Data

1. **Check if webhook is enabled**:
   - View Pipeline → Result Publisher → Webhook status

2. **Verify URL is accessible**:
   ```bash
   curl -X POST http://your-server:5000/api/detections \
     -H "Content-Type: application/json" \
     -d '{"test": "data"}'
   ```

3. **Check ArmyEye logs**:
   - Look for webhook errors in the logs panel

4. **Verify firewall**:
   - Ensure your server accepts connections from ArmyEye

### Images Not Included

Make sure you enabled the options:
- ✅ Include Image Data
- ✅ Include Result Image

### Rate Limiting Issues

If detections are being skipped:
- Increase `rate_limit` value (e.g., from 0.5 to 2.0 seconds)
- Or set to 0 for unlimited

---

## 📝 Summary

**To send detected images to your endpoint:**

1. ✅ Create a webhook receiver (Flask/Express/FastAPI)
2. ✅ Configure webhook in Pipeline Builder
3. ✅ Enable "Include Result Image" option
4. ✅ Start your pipeline
5. ✅ Your endpoint receives Base64 images + detection data!

The complete flow:
```
Face Detected → Engine Processes → Draws Boxes → Encodes to Base64 → Webhook POST → Your Server
```

🎉 You now have face detection results and images sent to your custom endpoint!
