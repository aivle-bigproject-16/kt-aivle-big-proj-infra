# CPU replay service

GPU에서 기록한 승인 fixture를 기존 ai-infer 및 VLM HTTP 계약으로 재생한다. 모델, ONNX 런타임 및 이미지 다운로드를 포함하지 않는다.

필수 환경변수:

- `REPLAY_FIXTURE_URI`
- `REPLAY_FIXTURE_SHA256`
- `AI_INTERNAL_API_KEY`
- `BACKEND_CALLBACK_URL`

선택적 리포트 재생은 `REPLAY_REPORT_FIXTURE_URI`와 `REPLAY_REPORT_FIXTURE_SHA256`을 함께 설정한다.

선택 설정:

- `REPLAY_CELL_POOL` (기본 `true`): 셀 풀 폴백
- `REPLAY_DELAY_MS` (기본 `800`), `REPLAY_MAX_PENDING` (기본 `64`)

## 셀 풀 폴백

fixture는 승인된 20셀만 담고 있으므로 지문이 정확히 일치하는 요청만 재생하면 21번째 셀부터 404가 된다. `REPLAY_CELL_POOL=true`이면 지문 miss가 발생한 셀을 녹화된 셀 그룹에 배정한다.

- 배정은 비복원 추출이다. 한 셀 시리얼은 녹화 셀 하나를 점유하고, 그룹의 20개를 모두 소진하면 사용 집합을 리셋한 뒤 fixture 순서대로 다시 배정한다. 따라서 판정 믹스가 20셀 주기로 반복된다.
- 같은 셀 시리얼의 재요청과 재촬영은 항상 같은 슬롯을 사용한다. 재촬영은 녹화된 attempt 2가 있으면 그것을 재생한다.
- CT/RGB는 각각 같은 슬롯의 해당 모달리티 녹화본에 매핑되므로 셀 단위 판정이 녹화 당시와 같게 유지된다.
- 개별 리포트도 같은 배정표를 따르며, 본문의 녹화 셀 시리얼과 검사 ID는 현재 요청 값으로 치환된다.
- `REPLAY_CELL_POOL=false`면 종전대로 미기록 셀은 404로 fail-closed 된다.

`GET /health`의 `cellPool`이 그룹 크기, 배정된 셀 수, 리셋 횟수를 보고한다.

```bash
docker build -t battery-replay ./replay
docker run --rm -p 8000:8000 --env-file .env battery-replay
```

`GET /health`가 `mode=REPLAY`를 반환해야 준비된 상태다. 어떤 경우에도 LIVE 추론으로 자동 폴백하지 않는다. `REPLAY_CELL_POOL=false`이면 fixture miss는 404이고, 기본값 `true`이면 미기록 셀은 위의 셀 풀 폴백으로 재생된다. 리포트 fixture schema v2는 20셀 개별 VLM 녹화본을 셀 시리얼별로 보관하고 현재 대표·출처 검사 ID로 치환한다. 일일 리포트는 녹화 당시의 전역 집계가 섞이지 않도록 현재 요청의 확정 집계를 결정적 Markdown으로 렌더링한다.
