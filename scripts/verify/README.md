# 로컬 통합 기동 검증

이 디렉터리의 `run-integration-check.sh`는 EC2 기동 전에 로컬 Docker Compose 형상을 확인하는 진입점이다. 스크립트는 각 검증을 `PASS`, `FAIL`, `SKIP`으로 출력하고 마지막에 10개 항목의 요약 표를 출력한다. 하나라도 실패하면 종료 코드는 1이며, 실패가 없으면 일부 항목이 입력이나 프로파일 부족으로 생략되어도 종료 코드는 0이다.

## 실행 전 준비

Bash, Docker Compose v2, `curl`, `jq`가 필요하다. 스크립트는 `set -euo pipefail`을 사용하며 Linux의 Bash와 Windows Git Bash에서 실행할 수 있다. 서비스 이미지를 준비하고 프로젝트 루트의 `.env`에 Compose가 요구하는 값을 설정해야 한다. 시크릿은 이 문서나 스크립트에 기록하지 않는다.

전체 형상을 검증하려면 `app`과 `ai` 프로파일을 모두 활성화해야 한다. 다음 명령은 프로젝트 루트에서 실행한다.

```bash
COMPOSE_PROFILES=app,ai bash scripts/verify/run-integration-check.sh
```

10번 Redis 장애 검증까지 실행하려면 명시적으로 파괴적 검증 플래그를 추가한다.

```bash
COMPOSE_PROFILES=app,ai bash scripts/verify/run-integration-check.sh \
  --include-destructive
```

스크립트는 스택을 기동하지 않는다. 실행 전에 별도로 Docker Compose 형상을 기동해야 한다. GPU 장치 설정이 필요한 환경에서는 `COMPOSE_FILE`에 `compose.yaml`과 `compose.gpu.yaml`을 함께 지정한 상태에서 실행한다.

## 외부 입력 환경변수

`VERIFY_NORMAL_CT_PRESIGNED_URL`에는 정상 CT 이미지의 유효한 presigned GET URL을 넣는다. 이 값이 없으면 8번과 9번은 `SKIP`이다. 스크립트는 이 값을 파일에 쓰거나 화면에 출력하지 않으며, 한 번의 `/infer/ct` 호출 결과를 8번과 9번이 함께 사용한다.

다음 환경변수는 검증 환경에 맞게 선택적으로 바꿀 수 있다.

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `VERIFY_BASE_URL` | `http://localhost` | SPA와 REST 프록시를 호출할 외부 기준 URL이다. |
| `VERIFY_WS_HOST` | `localhost` | WebSocket TCP 연결 대상 호스트이다. |
| `VERIFY_WS_PORT` | `80` | WebSocket TCP 연결 대상 포트이다. |
| `VERIFY_WS_PATH` | `/ws/sim` | WebSocket 업그레이드 경로이다. |
| `VERIFY_WS_HOLD_SECONDS` | `3` | STOMP 연결 후 하트비트를 보내기 전 유지 시간이다. |
| `VERIFY_REPORT_LOG_SINCE` | `30m` | 7번에서 최근 리포트 요청 로그를 찾을 기간이다. |
| `VERIFY_REDIS_RESPONSE_SECONDS` | `1` | 10번의 연결 및 전체 응답 시간 상한이다. |

환경변수 값에 공백이 있거나 URL에 `&`가 포함되면 셸 확장을 막기 위해 값을 인용해야 한다. 예를 들어 presigned URL은 `VERIFY_NORMAL_CT_PRESIGNED_URL='값'` 형태로 현재 명령의 환경에만 주입한다.

## 자동 판정 범위

| 번호 | 항목 | 자동 판정 방법 |
| --- | --- | --- |
| 1 | 컨테이너 6종 기동 | `frontend`, `backend`, `backend-ai`, `ai-infer`, `vlm`, `redis`가 Compose 프로파일에 포함되었는지 확인한 뒤, `docker compose ps`로 실행 컨테이너를 찾고 정의된 헬스체크가 `healthy`인지 확인한다. 필요한 프로파일이 빠지면 실패가 아니라 `SKIP`이다. |
| 2 | VLM 모델 적재 | 컨테이너 내부의 `/health`가 `status=ok`인지 확인하고, 컨테이너 로그에 `모델 적재 완료 —`가 함께 존재하는지 확인한다. |
| 3 | 화면 접속 | 외부 루트 URL이 HTTP 200을 반환하고 HTML에 `id="root"`인 SPA 루트 엘리먼트가 있는지 확인한다. |
| 4 | `/api` 프록시 | 회원가입에는 빈 JSON 객체를, 로그인에는 파싱할 수 없는 JSON을 보낸다. 두 요청이 모두 backend의 HTTP 400을 반환해야 통과하며, 어느 요청도 DB를 변경하지 않는다. |
| 5 | `/ws` 프록시 | Bash TCP 소켓으로 WebSocket 업그레이드의 HTTP 101을 확인하고, 마스킹한 STOMP `CONNECT` 프레임을 보내 `CONNECTED`를 확인한다. 지정한 유지 시간이 지난 뒤 하트비트 프레임도 전송한다. |
| 6 | Supabase 접속 | `backend`와 `backend-ai` 각각의 로그에서 HikariCP의 `Start completed.`와 Spring의 기동 완료 로그를 함께 확인한다. |
| 7 | module-api에서 module-ai 호출 | 데이터 변경 요청은 만들지 않는다. 지정한 최근 로그 구간에서 backend의 실패 로그 또는 backend-ai의 내부 리포트 요청 수신 로그를 찾아 판정한다. 판정할 최근 요청이 없으면 `SKIP`이다. |
| 8 | S3 서명 URL 왕복 | `ai-infer` 컨테이너 내부에서 `/infer/ct`를 한 번 호출하고 HTTP 200 및 요청 식별자 왕복을 확인한다. `/ai/cells/analyze`는 사용하지 않는다. |
| 9 | 판정 정상 동작 | 8번과 같은 응답에서 정상 CT 이미지의 `label`이 `PASS`인지 확인한다. |
| 10 | Redis 장애 격리 | `--include-destructive`가 있을 때만 Redis를 중지하고 화면이 사용하는 `/api/sim`이 제한 시간 안에 HTTP 200을 반환하는지 확인한다. 정상 종료와 인터럽트 모두에서 종료 트랩이 Redis 재시작을 시도한다. |

프로파일 때문에 대상 서비스가 Compose 설정에 포함되지 않은 항목은 `SKIP`이다. 7번처럼 안전하게 자동 요청을 만들 수 없거나 8번과 9번처럼 필수 외부 입력이 없는 항목도 `SKIP`으로 남긴다. 실행 대상이 프로파일에 포함되었지만 컨테이너 상태, 응답 또는 로그가 통과 기준을 충족하지 않으면 `FAIL`이다.

## 검증 전 이미 알려진 결함

`ReportService.java` 116행과 153행은 `AI_GATEWAY_URL`을 사용하지 않고 `http://localhost:8081`을 하드코딩한다. 컨테이너에서 localhost는 module-api 자신이므로 7번이 실패하며, 호출 예외를 삼키기 때문에 API 응답의 리포트 상태는 `PENDING`으로 남는다. 스크립트는 backend 로그의 `Failed to trigger LLM generation`을 발견하면 이 원인을 포함해 7번을 `FAIL`로 판정한다.

`/ai/cells/analyze`의 입력 계약은 presigned URL이 아니라 `bucket_name`과 오브젝트 키의 조합이다. 이 계약은 `ai-infer/app/schemas.py`의 `CellImageRequest`에 정의되어 있다. 따라서 8번과 9번은 presigned URL을 직접 받는 `/infer/ct`로 설계했으며, 이는 결함이 아니라 현재 설계 사실이다.

`SimulationSnapshotStore.find()`에는 Redis 예외 격리와 DB fallback이 없다. Redis가 중지되면 `RedisConnectionFailureException`이 그대로 전파될 수 있으므로 10번은 현재 구현에서 실패할 가능성이 크다. 이때 스크립트는 `/api/sim`의 비정상 HTTP 상태와 함께 원인을 안내하고 Redis를 복구한다.

## 결과 해석

`PASS`는 해당 실행에서 자동 통과 기준을 모두 관측했다는 뜻이다. `FAIL`은 필요한 프로파일과 입력이 있는데도 관측 결과가 기준에 미달했다는 뜻이다. `SKIP`은 프로파일, 외부 입력, 안전한 최근 로그 증거 또는 파괴적 실행 동의가 부족해 판정하지 않았다는 뜻이다.

7번은 최근 로그에 의존하므로 실제 리포트 흐름을 수동으로 실행한 직후 검증 스크립트를 다시 실행해야 자동 판정할 수 있다. 스크립트 자체는 Supabase 데이터를 바꾸는 리포트 생성 요청을 만들지 않는다.

## 구현 시 둔 가정

`frontend`, `backend`, `backend-ai`, `ai-infer`, `vlm`, `redis`가 전체 통합 검증의 6개 서비스라고 가정한다. `frontend`, `backend`, `backend-ai`처럼 Compose 헬스체크가 정의되지 않은 서비스는 `running` 상태만 검사하고, 헬스체크가 정의된 서비스는 추가로 `healthy` 상태를 요구한다.

WebSocket 검증은 Spring이 보내는 STOMP `CONNECTED` 프레임이 125바이트 미만인 일반적인 응답이라고 가정한다. 프레임이 확장 길이 형식을 사용하면 스크립트는 추측해서 통과시키지 않고 5번을 `FAIL`로 판정한다.

4번은 Spring MVC가 빈 회원가입 DTO와 파싱 불가능한 로그인 JSON을 모두 HTTP 400으로 거부한다고 가정한다. 이는 컨트롤러의 정상 비즈니스 로직과 DB 쓰기 작업에 진입하지 않기 위한 선택이다.

10번의 “화면 응답”은 정적 SPA HTML이 아니라 화면이 상태 복구에 사용하는 `/api/sim` 응답이라고 해석한다. 정적 HTML만 확인하면 Redis 예외 격리 결함을 관측할 수 없기 때문이다.
