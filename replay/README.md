# CPU replay service

GPU에서 기록한 승인 fixture를 기존 ai-infer 및 VLM HTTP 계약으로 재생한다. 모델, ONNX 런타임 및 이미지 다운로드를 포함하지 않는다.

필수 환경변수:

- `REPLAY_FIXTURE_URI`
- `REPLAY_FIXTURE_SHA256`
- `AI_INTERNAL_API_KEY`
- `BACKEND_CALLBACK_URL`

선택적 리포트 재생은 `REPLAY_REPORT_FIXTURE_URI`와 `REPLAY_REPORT_FIXTURE_SHA256`을 함께 설정한다.

```bash
docker build -t battery-replay ./replay
docker run --rm -p 8000:8000 --env-file .env battery-replay
```

`GET /health`가 `mode=REPLAY`를 반환해야 준비된 상태다. fixture miss는 404이며 LIVE 추론으로 자동 폴백하지 않는다. 리포트 fixture schema v2는 20셀 개별 VLM 녹화본을 셀 시리얼별로 보관하고 현재 대표·출처 검사 ID로 치환한다. 일일 리포트는 녹화 당시의 전역 집계가 섞이지 않도록 현재 요청의 확정 집계를 결정적 Markdown으로 렌더링한다.
