#!/bin/sh
# MinIO 버킷 + 프리픽스 구조 초기화 (아키텍처 v2 §8).
# S3에는 실제 디렉터리가 없음 — .keep 마커로 구조 가시화. 실 객체는 boto3 put 시 프리픽스 생성.
set -e

mc alias set local "http://minio:9000" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

# 버킷 생성(있으면 무시) + 비공개 유지(presigned = BE 일원화, 아키텍처 v2 §2 결정 12)
mc mb --ignore-existing "local/$S3_BUCKET"
mc anonymous set none "local/$S3_BUCKET"

# 프리픽스 마커
#   pool/{ct,rgb}/{normal,defect}/  검증셋+증강 사전적재 (read-only, eviction 제외)
#   defects/                        REJECT 이미지 보관 (BE FIFO eviction)
#   models/                         학습 가중치(.pt/.onnx) — FastAPI 기동 시 로드
for p in \
  pool/ct/normal pool/ct/defect \
  pool/rgb/normal pool/rgb/defect \
  defects models; do
  echo "keep" | mc pipe "local/$S3_BUCKET/$p/.keep"
done

echo "=== bucket layout ==="
mc ls -r "local/$S3_BUCKET"
echo "=== minio-init done ==="
