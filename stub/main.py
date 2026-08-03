"""ai-infer 스텁 — GPU 없는 FE·BE 개발자용 고정 응답 서버.

계약 = 아키텍처 설계 v2 §9 (BE ↔ AI).
실제 추론은 하지 않는다. 스키마·상태코드·지연만 흉내낸다.

  POST /infer/ct   { inspection_id, image_key, image_url }
  POST /infer/rgb  { inspection_id, image_key, image_url }
  GET  /health     200 (모델 로드 완료) / 503 (미로드)

image_url = BE 가 발급한 presigned URL (2026-07-10 BE 합의).
AI 서버는 S3 자격증명 없이 이 URL 로 이미지를 GET 한다.
"""

import os
import random
import time

from fastapi import FastAPI
from pydantic import BaseModel

LATENCY_MS = int(os.getenv("STUB_LATENCY_MS", "800"))
REJECT_RATE = float(os.getenv("STUB_REJECT_RATE", "0.3"))

# 아키텍처 v2 §2 결정 4 — 결함 유형 4종 고정
DEFECT_TYPES = ["부풀음", "오점", "미세결함", "갈라짐"]

app = FastAPI(title="ai-infer-stub")


class InferRequest(BaseModel):
    inspection_id: int
    image_key: str
    # BE 가 발급한 presigned URL. 스텁은 실제로 내려받지 않는다.
    image_url: str | None = None


def _infer(req: InferRequest) -> dict:
    time.sleep(LATENCY_MS / 1000)
    is_reject = random.random() < REJECT_RATE
    return {
        "inspection_id": req.inspection_id,
        "label": "REJECT" if is_reject else "PASS",
        "confidence": round(random.uniform(0.75, 0.99), 4),
        "defect_type": random.choice(DEFECT_TYPES) if is_reject else None,
        "bbox": {"x": 120, "y": 80, "width": 240, "height": 160} if is_reject else None,
        "latency_ms": LATENCY_MS,
    }


@app.post("/infer/ct")
def infer_ct(req: InferRequest) -> dict:
    return _infer(req)


@app.post("/infer/rgb")
def infer_rgb(req: InferRequest) -> dict:
    return _infer(req)


@app.get("/health")
def health() -> dict:
    # 실제 ai-infer 는 CT·RGB 두 모델 로드를 확인하고 미로드 시 503 을 반환한다.
    # 스텁은 항상 로드된 것으로 취급한다.
    return {"status": "ok", "models": {"ct": True, "rgb": True}}
