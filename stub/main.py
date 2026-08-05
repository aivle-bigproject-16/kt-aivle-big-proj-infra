"""Fixed-response inference server for FE/BE development without a GPU.

The stub keeps the production endpoints and request fields while returning the
wire response defined by Core SSOT sections 6.5 and 12.1.
"""

import os
import random
import time

from fastapi import FastAPI
from pydantic import BaseModel


LATENCY_MS = int(os.getenv("STUB_LATENCY_MS", "800"))
REJECT_RATE = float(os.getenv("STUB_REJECT_RATE", "0.3"))
FAIL_RATE = float(os.getenv("STUB_FAIL_RATE", "0.0"))

if not 0.0 <= REJECT_RATE <= 1.0:
    raise ValueError("STUB_REJECT_RATE must be between 0.0 and 1.0")
if not 0.0 <= FAIL_RATE <= 1.0:
    raise ValueError("STUB_FAIL_RATE must be between 0.0 and 1.0")

# Complete wire-level dictionary. SWELLING has no emitting model at present.
DEFECT_TYPES = ["SWELLING", "SPOT", "MICRO_DEFECT", "CRACK"]
MODAL_DEFECT_TYPES = {
    "ct": ["MICRO_DEFECT"],
    "rgb": ["CRACK", "SPOT"],
}

app = FastAPI(title="ai-infer-stub")


class InferRequest(BaseModel):
    inspection_id: int
    image_key: str
    # Presigned URL issued by BE. The stub does not download it.
    image_url: str | None = None


def _infer(req: InferRequest, modality: str) -> dict:
    time.sleep(LATENCY_MS / 1000)

    if random.random() < FAIL_RATE:
        return {
            "inspection_id": req.inspection_id,
            "label": "FAIL",
            # Core does not yet define FAIL confidence; confirm this with BE.
            "confidence": 0.0,
            "defects": [],
            "latency_ms": LATENCY_MS,
        }

    if random.random() >= REJECT_RATE:
        return {
            "inspection_id": req.inspection_id,
            "label": "PASS",
            # No below-threshold candidates are simulated, so A-4 yields 1.0.
            "confidence": 1.0,
            "defects": [],
            "latency_ms": LATENCY_MS,
        }

    return {
        "inspection_id": req.inspection_id,
        "label": "REJECT",
        "confidence": round(random.uniform(0.75, 0.99), 4),
        "defects": [
            {
                "defectType": random.choice(MODAL_DEFECT_TYPES[modality]),
                "confidence": round(random.uniform(0.75, 0.99), 4),
                "bbox": {
                    "x": 120.0,
                    "y": 80.0,
                    "width": 240.0,
                    "height": 160.0,
                },
            }
        ],
        "latency_ms": LATENCY_MS,
    }


@app.post("/infer/ct")
def infer_ct(req: InferRequest) -> dict:
    return _infer(req, "ct")


@app.post("/infer/rgb")
def infer_rgb(req: InferRequest) -> dict:
    return _infer(req, "rgb")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "models": {"ct": True, "rgb": True}}
