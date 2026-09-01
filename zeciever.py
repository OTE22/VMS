from strongsort.strong_sort import StrongSORT
import torch

tracker = StrongSORT(
    model_weights="osnet_x0_25",   # ✅ REQUIRED
    device="cuda" if torch.cuda.is_available() else "cpu",
    fp16=False
)

print("StrongSORT initialized successfully")
