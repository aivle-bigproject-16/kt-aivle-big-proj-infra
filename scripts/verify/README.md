# Docker Compose 배포 검증 가이드

이 하네스는 이미 기동된 통합 스택의 컨테이너 상태와 서비스 간 연결을 검증합니다. 호스트에는 Windows Git Bash의 Bash 5 또는 Linux Bash, GNU coreutils, curl, Docker CLI와 Docker Compose 플러그인이 필요합니다. 호스트의 Python과 jq는 필요하지 않습니다. 내부 포트 검증에는 각 Python 서비스 이미지에 포함된 Python 표준 라이브러리를 사용합니다.

## 스택 기동

저장소 루트에서 다음 명령으로 스택을 먼저 기동합니다.

```bash
docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d
```

일반적인 준비 시간은 redis와 frontend가 거의 즉시, backend와 backend-ai가 약 30초, ai-infer가 약 120초, vlm이 약 300초입니다. 첫 모델 다운로드나 GPU 초기화가 있으면 더 오래 걸릴 수 있습니다.

## 실행 방법

`scripts/verify` 디렉터리에서 전체 검증을 실행합니다.

```bash
cd scripts/verify
bash run-all.sh
```

특정 번호만 실행하려면 `bash run-all.sh --only 1,3,4`를 사용하고, 특정 번호를 제외하려면 `bash run-all.sh --skip 8,9`를 사용합니다. 준비 상태 폴링을 생략하려면 `--no-wait`를 사용합니다. Redis를 실제로 중지하는 10번 검증은 `bash run-all.sh --allow-mutate`처럼 명시적으로 허용해야 합니다. 하네스는 종료 신호나 중간 실패가 발생해도 EXIT 트랩에서 redis를 다시 시작하고 healthy 상태를 확인합니다.

각 검증 스크립트는 `scripts/verify`를 현재 디렉터리로 두고 `bash checks/01-containers.sh` 형식으로 직접 실행할 수도 있습니다. 10번을 직접 실행할 때는 `VERIFY_ALLOW_MUTATE=1 bash checks/10-redis-isolation.sh`처럼 명시적으로 허용해야 합니다.

8번 검증에는 유효한 이미지 presigned URL이 필요합니다. 서명이 셸 기록에 남지 않도록 대화형으로 입력한 뒤 환경 변수로 전달하는 방식을 권장합니다.

```bash
read -rsp 'Presigned URL: ' VERIFY_PRESIGNED_URL && echo
export VERIFY_PRESIGNED_URL
bash run-all.sh --only 8
unset VERIFY_PRESIGNED_URL
```

하네스는 URL의 쿼리 문자열을 출력하지 않습니다. URL이 없으면 S3 왕복을 증명할 수 없으므로 8번은 FAIL합니다.

## 판정 회귀 픽스처

9번 검증에는 결함이 없는 정상 배터리 셀의 CT 이미지가 필요합니다. 저장소에는 사용할 수 있는 실제 이미지가 없으므로 운영 모델의 검증 데이터에서 정상으로 확정된 이미지를 `scripts/verify/fixtures/normal-ct.png`에 둡니다. 파일 이름이나 위치를 바꾸려면 `VERIFY_NORMAL_CT_FIXTURE`에 경로를 지정합니다. 파일이 없거나 비어 있으면 9번은 명확한 안내와 함께 FAIL하며, 정상 이미지가 REJECT 또는 FAIL로 판정되어도 FAIL합니다.

## 결과와 종료 코드

각 체크는 마지막 표준 출력 한 줄에 결과를 기록하며, 전체 실행은 같은 내용을 마크다운 파이프 테이블로 출력합니다. 상세 진단은 표준 오류에 기록되고 `reports/verify-<UTC timestamp>.md`에도 함께 저장됩니다.

|종료 코드|결과|의미|
|---|---|---|
|0|PASS|검증이 통과했습니다.|
|1|FAIL|검증이 실패했습니다.|
|2|SKIP|조건이나 명시적 선택 때문에 실행하지 않았습니다.|
|3|XFAIL|현재 코드에서 알려진 실패가 재현되었습니다.|

전체 실행은 FAIL이 하나라도 있으면 1로 끝나고, PASS와 SKIP 및 XFAIL만 있으면 0으로 끝납니다.

7번은 backend `cf0dbbc`까지 XFAIL이었으나 `0824846`(BE PR #15)에서 해소되어 이제 PASS를 기대합니다. `ReportService`가 하드코딩된 `http://localhost:8081` 대신 `aiGatewayRestClient`를 주입받고, 그 빈이 `ai-gateway.base-url`을 통해 `AI_GATEWAY_URL`을 읽습니다. Compose가 `http://backend-ai:8081`을 주입하므로 호출이 성립합니다. 따라서 `Failed to trigger LLM generation` 로그가 보이면 그것은 알려진 결함이 아니라 실제 배선 또는 인증 문제이며, 검증은 이를 FAIL로 판정합니다. `cf0dbbc` 이전 이미지를 일부러 검증할 때만 `BACKEND_PRE_0824846=1`을 주어 XFAIL로 되돌릴 수 있습니다.

10번도 현재 알려진 XFAIL입니다. `SimulationSnapshotStore.find()`가 Redis 예외를 처리하지 않아 `RedisConnectionFailureException`이 전파되고 데이터베이스 대체 경로가 없습니다. 이 검증은 `--allow-mutate`가 없으면 `requires --allow-mutate` 사유로 SKIP합니다.

## 8번이 필요로 하는 서명 URL

8번은 ai-infer가 S3 presigned URL로 이미지를 직접 내려받는 경로를 검증합니다. 서명은 요청자가 만들어 주어야 하므로 `VERIFY_PRESIGNED_URL` 환경변수로 넘깁니다. 모델 번들과 함께 올려 둔 CT 픽스처를 그대로 쓰면 됩니다.

```bash
export VERIFY_PRESIGNED_URL="$(aws s3 presign \
  s3://kt-aivle-big-proj-kks/models/ai-infer/onnx-20260809-01/fixtures/ct-2f749661c58cee23a6a8cecf5ea195647e86c33411e34649e37035dbd6c2d97f \
  --expires-in 900 --region ap-northeast-2 --profile admin)"
bash run-all.sh
```

이 검증은 판정 결과가 무엇인지 보지 않습니다. 이미지를 받아 와 해석 가능한 판정을 돌려주기만 하면 통과입니다. 판정의 옳고 옳지 않음은 9번이 봅니다. 서명 URL은 어떤 경우에도 출력하지 않으며, 로그에 섞여 나올 때는 물음표 뒤가 가려집니다.

## 9번 픽스처의 출처

`scripts/verify/fixtures/normal-ct.png`는 전처리 산출물 `battery_v41_output`에서 가져온 정상 셀 CT 이미지입니다. 파일 이름은 하네스의 기본 경로 계약을 따라 `.png`이지만 내용은 원본 그대로의 JPEG이며, CT 어댑터가 바이트를 직접 해석하므로 확장자는 판정에 영향을 주지 않습니다. 어떤 원본에서 왔고 무엇을 근거로 정상이라고 판단했는지는 `fixtures/README.md`에 적어 두었습니다. 요약하면 전처리 매니페스트의 `original_is_normal`과 `original_defect_count=0`, 라벨 JSON의 `is_normal: true`와 `defects: null`, 그리고 비어 있는 detection 라벨 파일 세 가지가 서로 일치합니다.

2026-08-13 기준으로 이 픽스처는 `PASS` 판정을 받아 9번이 통과합니다.

S3 모델 번들에 함께 들어 있는 `models/ai-infer/onnx-20260809-01/fixtures/` 아래의 CT 이미지 두 개는 이 검증에 쓰지 않습니다. 정상 셀이라는 라벨 근거가 없고, 실제로 넣어 보면 아래와 같이 어느 쪽도 PASS가 아니기 때문입니다. 8번의 서명 URL 왕복처럼 판정 내용을 보지 않는 검증에는 그대로 써도 됩니다.

| 픽스처 | 해상도 | 판정 |
|---|---|---|
| `ct-2f749661...` | 562x1152 | `FAIL` (신뢰도 0.981, 결함 없음. 품질 게이트 탈락) |
| `ct-85e374e1...` | 562x4000 | `REJECT` (신뢰도 0.260, `MICRO_DEFECT` 폭 3픽셀) |

픽스처 파일이 없거나 비어 있으면 9번은 조용히 건너뛰지 않고 명시적으로 FAIL합니다. 그것은 의도한 동작입니다.
