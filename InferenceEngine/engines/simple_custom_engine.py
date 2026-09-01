#!/usr/bin/env python3
"""
Simple Custom Engine Example
A minimal working example you can customize for your own engine.
"""

import numpy as np
from typing import Any, Optional
import json

try:
    from .base_engine import BaseInferenceEngine
except ImportError:
    from base_engine import BaseInferenceEngine


class SimpleCustomEngine(BaseInferenceEngine):
    """
    Simple custom inference engine example.
    Replace this with your actual model loading and inference logic.
    """
    
    # REQUIRED: User-friendly display name
    display_name = "Simple Custom Engine"
    
    def __init__(self, **kwargs):
        """Initialize your engine"""
        super().__init__(**kwargs)
        self.model = None
        # Add any custom parameters here
        self.confidence_threshold = kwargs.get('confidence_threshold', 0.5)
    
    def _load_model(self, model_file: str, device: str) -> bool:
        """
        Load your model from file.
        
        Args:
            model_file: Path to model file
            device: Target device ('cpu', 'gpu', etc.)
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            self.logger.info(f"Loading model from {model_file} on device {device}")
            
            # TODO: Replace with your actual model loading code
            # Example for different frameworks:
            #
            # TensorFlow:
            #   import tensorflow as tf
            #   self.model = tf.keras.models.load_model(model_file)
            #
            # PyTorch:
            #   import torch
            #   self.model = torch.load(model_file, map_location=device)
            #   self.model.eval()
            #
            # ONNX Runtime:
            #   import onnxruntime as ort
            #   self.model = ort.InferenceSession(model_file)
            #
            # For this example, we'll just mark as loaded
            self.model = {"loaded": True, "path": model_file, "device": device}
            self.is_loaded = True
            
            self.logger.info("Model loaded successfully")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to load model: {e}")
            return False
    
    def check_valid_model(self, model_file: str) -> bool:
        """
        Check if the model file is valid for this engine.
        
        Args:
            model_file: Path to model file
            
        Returns:
            bool: True if valid, False otherwise
        """
        import os
        
        # Check if file exists
        if not os.path.exists(model_file):
            return False
        
        # Check file extension (customize for your model format)
        valid_extensions = ('.pt', '.pth', '.onnx', '.pb', '.h5', '.tflite', '.model')
        return model_file.lower().endswith(valid_extensions)
    
    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        """
        Preprocess input image for inference.
        
        Args:
            image: Input image as numpy array (BGR format from OpenCV)
            
        Returns:
            np.ndarray: Preprocessed image
        """
        if not isinstance(image, np.ndarray):
            raise TypeError("Input image must be a numpy array")
        
        # TODO: Add your preprocessing logic
        # Examples:
        # - Resize to model input size
        # - Normalize pixel values (0-1 or -1 to 1)
        # - Convert BGR to RGB
        # - Apply mean/std normalization
        
        # For this example, just return the image as-is
        return image
    
    def _infer(self, preprocessed_input: np.ndarray) -> Any:
        """
        Run inference on preprocessed input.
        
        Args:
            preprocessed_input: Preprocessed image
            
        Returns:
            Any: Raw inference results
        """
        if self.model is None:
            return None
        
        # TODO: Replace with your actual inference code
        # Example for different frameworks:
        #
        # TensorFlow:
        #   predictions = self.model.predict(preprocessed_input)
        #
        # PyTorch:
        #   with torch.no_grad():
        #       input_tensor = torch.from_numpy(preprocessed_input)
        #       predictions = self.model(input_tensor)
        #
        # ONNX Runtime:
        #   input_name = self.model.get_inputs()[0].name
        #   predictions = self.model.run(None, {input_name: preprocessed_input})
        
        # For this example, return dummy results
        # In real implementation, return actual model predictions
        return {
            "raw_predictions": "Your model output here"
        }
    
    def _postprocess(self, raw_output: Any) -> Any:
        """
        Postprocess raw inference results.
        
        Args:
            raw_output: Raw results from _infer()
            
        Returns:
            Any: Processed results
        """
        # TODO: Add your postprocessing logic
        # Examples:
        # - Apply Non-Maximum Suppression (NMS)
        # - Filter by confidence threshold
        # - Convert coordinates to image space
        # - Format results for output
        
        return raw_output
    
    def draw(self, image: np.ndarray, results: Any) -> np.ndarray:
        """
        Draw inference results on the image.
        
        Args:
            image: Original input image
            results: Processed inference results
            
        Returns:
            np.ndarray: Image with annotations drawn
        """
        import cv2
        
        annotated_image = image.copy()
        
        # TODO: Add your drawing logic
        # Example:
        # if results and "detections" in results:
        #     for det in results["detections"]:
        #         bbox = det["bbox"]
        #         class_name = det["class_name"]
        #         confidence = det["confidence"]
        #         
        #         # Draw bounding box
        #         cv2.rectangle(annotated_image, 
        #                      (int(bbox[0]), int(bbox[1])), 
        #                      (int(bbox[2]), int(bbox[3])), 
        #                      (0, 255, 0), 2)
        #         
        #         # Draw label
        #         label = f"{class_name}: {confidence:.2f}"
        #         cv2.putText(annotated_image, label,
        #                    (int(bbox[0]), int(bbox[1]) - 10),
        #                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        
        return annotated_image
    
    def result_to_json(self, results: Any, output_format: str = "dict") -> str:
        """
        Convert inference results to JSON format.
        
        IMPORTANT: The pipeline expects results in this format:
        {
            "task_type": "detection",
            "num_detections": N,
            "predictions": [
                {
                    "class_id": 0,
                    "class_name": "person",
                    "confidence": 0.95,
                    "bbox": [x1, y1, x2, y2],
                    "track_id": 1  # Optional, for tracking
                }
            ]
        }
        
        Args:
            results: Processed inference results
            output_format: Output format ('dict' or 'json')
            
        Returns:
            str: JSON string representation
        """
        # TODO: Convert your model's output format to the expected format
        
        # Example conversion (customize based on your model's output):
        detections = []
        
        # If your model returns results in a different format, convert them here
        # For example:
        # if isinstance(results, dict) and "boxes" in results:
        #     for i, box in enumerate(results["boxes"]):
        #         detections.append({
        #             "class_id": int(results["classes"][i]),
        #             "class_name": results["class_names"][i],
        #             "confidence": float(results["scores"][i]),
        #             "bbox": box.tolist() if hasattr(box, 'tolist') else list(box),
        #             "track_id": None  # Add if you have tracking
        #         })
        
        # Format results for pipeline
        json_results = {
            "task_type": "detection",  # or "segmentation", "pose", etc.
            "num_detections": len(detections),
            "predictions": detections  # Pipeline expects "predictions" key
        }
        
        if output_format == "dict":
            return json.dumps(json_results, default=str)
        else:
            return json.dumps(json_results, default=str)


# Example usage
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    
    # Create engine instance
    engine = SimpleCustomEngine(confidence_threshold=0.6)
    
    # Test model validation
    test_model = "test_model.pt"
    if engine.check_valid_model(test_model):
        print(f"✓ Model file {test_model} is valid")
    else:
        print(f"✗ Model file {test_model} is invalid")
    
    # Test loading (will fail if file doesn't exist, but shows workflow)
    if engine.load(test_model):
        print("✓ Model loaded successfully")
        
        # Test inference with dummy image
        dummy_image = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        results = engine.infer(dummy_image)
        print(f"✓ Inference completed: {results}")
        
        # Test JSON conversion
        json_output = engine.result_to_json(results)
        print(f"✓ JSON output: {json_output[:100]}...")
    else:
        print("✗ Failed to load model")

