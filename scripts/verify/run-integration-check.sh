#!/usr/bin/env bash

set -euo pipefail

SCRIPT_NAME=$(basename "$0")
readonly SCRIPT_NAME
readonly BASE_URL="${VERIFY_BASE_URL:-http://localhost}"
readonly WS_HOST="${VERIFY_WS_HOST:-localhost}"
readonly WS_PORT="${VERIFY_WS_PORT:-80}"
readonly WS_PATH="${VERIFY_WS_PATH:-/ws/sim}"
readonly WS_HOLD_SECONDS="${VERIFY_WS_HOLD_SECONDS:-3}"
readonly REPORT_LOG_SINCE="${VERIFY_REPORT_LOG_SINCE:-30m}"
readonly REDIS_RESPONSE_LIMIT="${VERIFY_REDIS_RESPONSE_SECONDS:-1}"
readonly -a EXPECTED_SERVICES=(frontend backend backend-ai ai-infer vlm redis)
readonly -a CHECK_NAMES=(
  "컨테이너 6종 기동"
  "VLM 모델 적재"
  "화면 접속"
  "/api 프록시"
  "/ws 프록시"
  "Supabase 접속"
  "module-api → module-ai"
  "S3 서명 URL 왕복"
  "판정 정상 동작"
  "Redis 장애 격리"
)

declare -a CHECK_STATUS=()
declare -a CHECK_MESSAGE=()
ENABLED_SERVICES=""
FAIL_COUNT=0
INCLUDE_DESTRUCTIVE=0
REDIS_STOPPED=0

usage() {
  cat <<EOF
사용법: bash scripts/verify/${SCRIPT_NAME} [--include-destructive]

  --include-destructive  Redis를 일시 중지하는 10번 검증을 실행합니다.
  -h, --help             이 도움말을 출력합니다.
EOF
}

while (($# > 0)); do
  case "$1" in
    --include-destructive)
      INCLUDE_DESTRUCTIVE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '알 수 없는 인자입니다: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

compose() {
  docker compose "$@"
}

cleanup() {
  local exit_code=$?

  if ((REDIS_STOPPED == 1)); then
    printf '\n[복구] 중단된 Redis를 다시 시작합니다.\n' >&2
    if compose start redis >/dev/null 2>&1; then
      REDIS_STOPPED=0
    else
      printf '[경고] Redis 자동 복구에 실패했습니다. docker compose start redis를 실행하십시오.\n' >&2
    fi
  fi

  return "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT TERM HUP

record_result() {
  local number=$1
  local status=$2
  local message=$3

  CHECK_STATUS[$number]=$status
  CHECK_MESSAGE[$number]=$message
  if [[ $status == "FAIL" ]]; then
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
  printf '[%s] %2d. %s — %s\n' \
    "$status" \
    "$number" \
    "${CHECK_NAMES[$((number - 1))]}" \
    "$message"
}

print_summary() {
  local number

  printf '\n%-4s | %-24s | %-4s | %s\n' \
    "번호" \
    "검증 항목" \
    "결과" \
    "메시지"
  printf '%s\n' \
    '-----+--------------------------+------+----------------------------------------'

  for number in {1..10}; do
    printf '%-4s | %-24s | %-4s | %s\n' \
      "$number" \
      "${CHECK_NAMES[$((number - 1))]}" \
      "${CHECK_STATUS[$number]:-SKIP}" \
      "${CHECK_MESSAGE[$number]:-실행되지 않았습니다.}"
  done

  if ((FAIL_COUNT > 0)); then
    printf '\n결과: FAIL (%d개 실패, 종료 코드 1)\n' "$FAIL_COUNT"
  else
    printf '\n결과: PASS (실패 없음, 종료 코드 0)\n'
  fi
}

skip_unrecorded() {
  local reason=$1
  local number

  for number in {1..10}; do
    if [[ -z ${CHECK_STATUS[$number]:-} ]]; then
      record_result "$number" "SKIP" "$reason"
    fi
  done
}

service_enabled() {
  local wanted=$1
  local service

  while IFS= read -r service; do
    if [[ $service == "$wanted" ]]; then
      return 0
    fi
  done <<<"$ENABLED_SERVICES"

  return 1
}

service_container_id() {
  local service=$1
  compose ps -q "$service" 2>/dev/null | head -n 1
}

service_is_running() {
  local service=$1
  local container_id

  container_id=$(service_container_id "$service")
  [[ -n $container_id ]]
}

join_items() {
  local separator=$1
  shift
  local joined=""
  local item

  for item in "$@"; do
    if [[ -n $joined ]]; then
      joined+="$separator"
    fi
    joined+="$item"
  done

  printf '%s' "$joined"
}

check_prerequisites() {
  local -a missing=()
  local command_name

  for command_name in docker curl jq; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
      missing+=("$command_name")
    fi
  done

  if ((${#missing[@]} > 0)); then
    printf '[FAIL] 사전 점검 — 필수 명령이 없습니다: %s\n' \
      "$(join_items ', ' "${missing[@]}")" >&2
    return 1
  fi

  if ! docker compose version >/dev/null 2>&1; then
    printf '[FAIL] 사전 점검 — Docker Compose v2를 사용할 수 없습니다.\n' >&2
    return 1
  fi

  return 0
}

check_containers() {
  local -a missing_profiles=()
  local -a problems=()
  local service
  local container_id
  local state
  local runtime_status
  local health_status

  for service in "${EXPECTED_SERVICES[@]}"; do
    if ! service_enabled "$service"; then
      missing_profiles+=("$service")
    fi
  done

  if ((${#missing_profiles[@]} > 0)); then
    record_result 1 "SKIP" \
      "프로파일에서 빠진 서비스가 있습니다: $(join_items ', ' "${missing_profiles[@]}")."
    return
  fi

  for service in "${EXPECTED_SERVICES[@]}"; do
    container_id=$(service_container_id "$service")
    if [[ -z $container_id ]]; then
      problems+=("${service}=not-running")
      continue
    fi

    state=$(
      docker inspect \
        --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        "$container_id" 2>/dev/null || true
    )
    runtime_status=${state%%|*}
    health_status=${state#*|}

    if [[ $runtime_status != "running" ]]; then
      problems+=("${service}=${runtime_status:-unknown}")
    elif [[ $health_status != "none" && $health_status != "healthy" ]]; then
      problems+=("${service}=health-${health_status:-unknown}")
    fi
  done

  if ((${#problems[@]} > 0)); then
    record_result 1 "FAIL" \
      "실행 상태 또는 헬스체크가 기준을 충족하지 않습니다: $(join_items ', ' "${problems[@]}")."
  else
    record_result 1 "PASS" \
      "6개 서비스가 running이고 정의된 헬스체크가 모두 healthy입니다."
  fi
}

check_vlm_model() {
  local health_json
  local logs

  if ! service_enabled vlm; then
    record_result 2 "SKIP" \
      "llm 또는 ai 프로파일이 활성화되지 않았습니다."
    return
  fi

  if ! service_is_running vlm; then
    record_result 2 "FAIL" \
      "vlm 컨테이너가 실행 중이 아닙니다."
    return
  fi

  if ! health_json=$(
    compose exec -T vlm python -c \
      'import urllib.request; print(urllib.request.urlopen("http://localhost:8001/health", timeout=5).read().decode())' \
      2>/dev/null
  ); then
    record_result 2 "FAIL" \
      "vlm 컨테이너 내부의 /health 호출에 실패했습니다."
    return
  fi

  if ! jq -e '.status == "ok"' >/dev/null 2>&1 <<<"$health_json"; then
    record_result 2 "FAIL" \
      "vlm /health 응답이 status=ok가 아닙니다."
    return
  fi

  logs=$(compose logs --no-color vlm 2>&1 || true)
  if [[ $logs != *"모델 적재 완료 —"* ]]; then
    record_result 2 "FAIL" \
      "/health는 정상이지만 '모델 적재 완료 —' 로그가 없습니다."
    return
  fi

  record_result 2 "PASS" \
    "/health와 모델 적재 완료 로그를 함께 확인했습니다."
}

check_frontend() {
  local response
  local status_code
  local body

  if ! service_enabled frontend; then
    record_result 3 "SKIP" \
      "app 프로파일이 활성화되지 않았습니다."
    return
  fi

  if ! response=$(
    curl \
      --silent \
      --show-error \
      --connect-timeout 2 \
      --max-time 5 \
      --write-out $'\n%{http_code}' \
      "${BASE_URL%/}/" 2>/dev/null
  ); then
    record_result 3 "FAIL" \
      "${BASE_URL%/}/ 요청에 실패했습니다."
    return
  fi

  status_code=${response##*$'\n'}
  body=${response%$'\n'*}

  if [[ $status_code != "200" ]]; then
    record_result 3 "FAIL" \
      "SPA 루트가 HTTP ${status_code}를 반환했습니다."
  elif [[ $body != *'id="root"'* && $body != *"id='root'"* ]]; then
    record_result 3 "FAIL" \
      "HTTP 200 응답에 SPA 루트 엘리먼트(id=root)가 없습니다."
  else
    record_result 3 "PASS" \
      "HTTP 200과 SPA 루트 엘리먼트를 확인했습니다."
  fi
}

check_api_proxy() {
  local signup_code
  local login_code

  if ! service_enabled frontend || ! service_enabled backend; then
    record_result 4 "SKIP" \
      "app 프로파일의 frontend와 backend가 모두 필요합니다."
    return
  fi

  signup_code=$(
    curl \
      --silent \
      --show-error \
      --output /dev/null \
      --write-out '%{http_code}' \
      --connect-timeout 2 \
      --max-time 5 \
      --header 'Content-Type: application/json' \
      --data '{}' \
      "${BASE_URL%/}/api/auth/signup" 2>/dev/null || true
  )

  login_code=$(
    curl \
      --silent \
      --show-error \
      --output /dev/null \
      --write-out '%{http_code}' \
      --connect-timeout 2 \
      --max-time 5 \
      --header 'Content-Type: application/json' \
      --data '{' \
      "${BASE_URL%/}/api/auth/login" 2>/dev/null || true
  )

  if [[ $signup_code == "400" && $login_code == "400" ]]; then
    record_result 4 "PASS" \
      "DB를 변경하지 않는 실패 페이로드가 회원가입과 로그인에서 각각 HTTP 400을 반환했습니다."
  else
    record_result 4 "FAIL" \
      "backend 전달 기준은 두 요청 모두 HTTP 400이며, 실제 값은 signup=${signup_code:-요청실패}, login=${login_code:-요청실패}입니다."
  fi
}

ws_write_byte() {
  local value=$1
  local escaped

  printf -v escaped '\\%03o' "$value"
  printf '%b' "$escaped" >&9
}

ws_send_text() {
  local payload=$1
  local append_null=$2
  local payload_length=${#payload}
  local -a mask=(18 52 86 120)
  local index
  local character
  local character_code
  local masked_code

  if ((append_null == 1)); then
    payload_length=$((payload_length + 1))
  fi

  if ((payload_length >= 126)); then
    return 1
  fi

  ws_write_byte 129
  ws_write_byte $((128 + payload_length))

  for masked_code in "${mask[@]}"; do
    ws_write_byte "$masked_code"
  done

  for ((index = 0; index < ${#payload}; index++)); do
    character=${payload:index:1}
    printf -v character_code '%d' "'$character"
    masked_code=$((character_code ^ mask[index % 4]))
    ws_write_byte "$masked_code"
  done

  if ((append_null == 1)); then
    masked_code=${mask[index % 4]}
    ws_write_byte "$masked_code"
  fi
}

ws_read_byte() {
  local character

  if ! IFS= read -r -N 1 -t 5 character <&9; then
    return 1
  fi

  printf -v WS_BYTE_VALUE '%d' "'$character"
}

check_websocket_proxy() {
  local status_line=""
  local line
  local headers_complete=0
  local first_byte
  local second_byte
  local opcode
  local payload_length
  local stomp_reply=""
  local stomp_connect

  if ! service_enabled frontend || ! service_enabled backend; then
    record_result 5 "SKIP" \
      "app 프로파일의 frontend와 backend가 모두 필요합니다."
    return
  fi

  if ! exec 9<>"/dev/tcp/${WS_HOST}/${WS_PORT}"; then
    record_result 5 "FAIL" \
      "${WS_HOST}:${WS_PORT} TCP 연결에 실패했습니다."
    return
  fi

  printf \
    'GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nOrigin: http://localhost\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: v12.stomp,v11.stomp,v10.stomp\r\n\r\n' \
    "$WS_PATH" \
    "$WS_HOST" \
    "$WS_PORT" >&9

  while IFS= read -r -t 5 line <&9; do
    line=${line%$'\r'}

    if [[ -z $status_line ]]; then
      status_line=$line
    fi

    if [[ -z $line ]]; then
      headers_complete=1
      break
    fi
  done

  if ((headers_complete == 0)) || [[ $status_line != *" 101 "* ]]; then
    exec 9>&-
    record_result 5 "FAIL" \
      "WebSocket 업그레이드가 HTTP 101을 반환하지 않았습니다 (${status_line:-응답없음})."
    return
  fi

  stomp_connect=$'CONNECT\naccept-version:1.2\nhost:localhost\nheart-beat:10000,10000\n\n'
  if ! ws_send_text "$stomp_connect" 1; then
    exec 9>&-
    record_result 5 "FAIL" \
      "STOMP CONNECT 프레임을 전송하지 못했습니다."
    return
  fi

  if ! ws_read_byte; then
    exec 9>&-
    record_result 5 "FAIL" \
      "STOMP 응답 프레임을 받지 못했습니다."
    return
  fi
  first_byte=$WS_BYTE_VALUE

  if ! ws_read_byte; then
    exec 9>&-
    record_result 5 "FAIL" \
      "WebSocket 프레임 길이를 읽지 못했습니다."
    return
  fi
  second_byte=$WS_BYTE_VALUE

  opcode=$((first_byte & 15))
  payload_length=$((second_byte & 127))

  if ((opcode != 1 || payload_length == 126 || payload_length == 127)); then
    exec 9>&-
    record_result 5 "FAIL" \
      "예상하지 못한 WebSocket 응답 프레임입니다(opcode=${opcode}, length=${payload_length})."
    return
  fi

  if ! IFS= read -r -d '' -t 5 stomp_reply <&9; then
    exec 9>&-
    record_result 5 "FAIL" \
      "STOMP 응답의 종료 바이트를 받지 못했습니다."
    return
  fi

  if [[ $stomp_reply != CONNECTED$'\n'* &&
        $stomp_reply != CONNECTED$'\r\n'* ]]; then
    exec 9>&-
    record_result 5 "FAIL" \
      "STOMP CONNECTED 응답이 아닙니다."
    return
  fi

  sleep "$WS_HOLD_SECONDS"

  if ! ws_send_text $'\n' 0; then
    exec 9>&-
    record_result 5 "FAIL" \
      "연결 유지 시간 이후 WebSocket 하트비트 전송에 실패했습니다."
    return
  fi

  exec 9>&-
  record_result 5 "PASS" \
    "HTTP 101과 STOMP CONNECTED를 확인하고 ${WS_HOLD_SECONDS}초 동안 연결을 유지했습니다."
}

check_supabase() {
  local service
  local logs
  local -a missing_profiles=()
  local -a problems=()

  for service in backend backend-ai; do
    if ! service_enabled "$service"; then
      missing_profiles+=("$service")
      continue
    fi

    if ! service_is_running "$service"; then
      problems+=("${service}=not-running")
      continue
    fi

    logs=$(compose logs --no-color "$service" 2>&1 || true)

    if [[ $logs != *"Start completed."* ]]; then
      problems+=("${service}=Hikari 로그 없음")
    fi

    if [[ ! $logs =~ Started.*in[[:space:]][0-9.]+[[:space:]]seconds ]]; then
      problems+=("${service}=Spring 기동 로그 없음")
    fi
  done

  if ((${#missing_profiles[@]} > 0)); then
    record_result 6 "SKIP" \
      "app 프로파일에서 빠진 서비스가 있습니다: $(join_items ', ' "${missing_profiles[@]}")."
  elif ((${#problems[@]} > 0)); then
    record_result 6 "FAIL" \
      "DB 풀 또는 Spring 기동 완료 근거가 부족합니다: $(join_items ', ' "${problems[@]}")."
  else
    record_result 6 "PASS" \
      "backend와 backend-ai에서 Hikari 'Start completed.' 및 Spring 기동 완료 로그를 확인했습니다."
  fi
}

check_module_api_to_ai() {
  local backend_logs
  local backend_ai_logs

  if ! service_enabled backend || ! service_enabled backend-ai; then
    record_result 7 "SKIP" \
      "app 프로파일의 backend와 backend-ai가 모두 필요합니다."
    return
  fi

  if ! service_is_running backend || ! service_is_running backend-ai; then
    record_result 7 "FAIL" \
      "backend와 backend-ai가 모두 실행 중이어야 최근 전달 로그를 판정할 수 있습니다."
    return
  fi

  backend_logs=$(
    compose logs \
      --no-color \
      --since "$REPORT_LOG_SINCE" \
      backend 2>&1 || true
  )
  backend_ai_logs=$(
    compose logs \
      --no-color \
      --since "$REPORT_LOG_SINCE" \
      backend-ai 2>&1 || true
  )

  if [[ $backend_logs == *"Failed to trigger LLM generation"* ]]; then
    record_result 7 "FAIL" \
      "알려진 결함이 관측되었습니다. ReportService가 localhost:8081을 호출해 module-ai 전달에 실패했습니다."
  elif [[ $backend_ai_logs == *"Received internal request to generate daily report"* ||
          $backend_ai_logs == *"Received internal request to generate individual report"* ]]; then
    record_result 7 "PASS" \
      "최근 ${REPORT_LOG_SINCE} 로그에서 backend-ai의 리포트 생성 요청 수신을 확인했습니다."
  else
    record_result 7 "SKIP" \
      "DB 변경을 피하기 위해 요청을 만들지 않았으며 최근 ${REPORT_LOG_SINCE} 로그에도 판정 가능한 리포트 요청이 없습니다."
  fi
}

run_ct_inference_once() {
  local request_json
  local python_code

  request_json=$(
    jq -nc \
      '{
        inspection_id: 2147483647,
        image_key: "verify/normal-ct",
        image_url: env.VERIFY_NORMAL_CT_PRESIGNED_URL
      }'
  )

  python_code=$'import json, sys, urllib.error, urllib.request\nrequest_body = sys.stdin.buffer.read()\nrequest = urllib.request.Request("http://localhost:8000/infer/ct", data=request_body, headers={"Content-Type": "application/json"}, method="POST")\ntry:\n    with urllib.request.urlopen(request, timeout=120) as response:\n        status = response.status\n        body = response.read().decode("utf-8")\nexcept urllib.error.HTTPError as error:\n    status = error.code\n    body = error.read().decode("utf-8", errors="replace")\nexcept Exception:\n    status = 0\n    body = ""\nprint(json.dumps({"status": status, "body": body}))'

  printf '%s' "$request_json" |
    compose exec -T ai-infer python -c "$python_code" 2>/dev/null
}

check_presigned_and_verdict() {
  local inference_result
  local status_code
  local response_body
  local label

  if ! service_enabled ai-infer; then
    record_result 8 "SKIP" \
      "ai 프로파일이 활성화되지 않았습니다."
    record_result 9 "SKIP" \
      "ai 프로파일이 활성화되지 않았습니다."
    return
  fi

  if [[ -z ${VERIFY_NORMAL_CT_PRESIGNED_URL:-} ]]; then
    record_result 8 "SKIP" \
      "VERIFY_NORMAL_CT_PRESIGNED_URL이 제공되지 않았습니다."
    record_result 9 "SKIP" \
      "VERIFY_NORMAL_CT_PRESIGNED_URL이 제공되지 않았습니다."
    return
  fi

  if ! service_is_running ai-infer; then
    record_result 8 "FAIL" \
      "ai-infer 컨테이너가 실행 중이 아닙니다."
    record_result 9 "FAIL" \
      "ai-infer 컨테이너가 실행 중이 아닙니다."
    return
  fi

  if ! inference_result=$(run_ct_inference_once); then
    record_result 8 "FAIL" \
      "/infer/ct 내부 호출에 실패했습니다. 서명 URL 값은 출력하지 않았습니다."
    record_result 9 "FAIL" \
      "CT 추론 응답을 받지 못했습니다."
    return
  fi

  if ! jq -e '(.status | type) == "number"' \
    >/dev/null 2>&1 <<<"$inference_result"; then
    record_result 8 "FAIL" \
      "/infer/ct 결과 형식을 해석하지 못했습니다. 서명 URL 값은 출력하지 않았습니다."
    record_result 9 "FAIL" \
      "CT 추론 결과 형식을 해석하지 못했습니다."
    return
  fi

  status_code=$(jq -r '.status' <<<"$inference_result")
  response_body=$(jq -r '.body' <<<"$inference_result")

  if [[ $status_code != "200" ]] ||
    ! jq -e '.inspection_id == 2147483647' \
      >/dev/null 2>&1 <<<"$response_body"; then
    if [[ $status_code == "502" ]]; then
      record_result 8 "FAIL" \
        "presigned GET이 실패했습니다. URL 만료, 권한 또는 S3 접근성을 확인하십시오."
    else
      record_result 8 "FAIL" \
        "/infer/ct가 유효한 HTTP 200 응답을 반환하지 않았습니다(status=${status_code})."
    fi

    record_result 9 "FAIL" \
      "정상 판정을 평가할 유효한 CT 추론 결과가 없습니다."
    return
  fi

  record_result 8 "PASS" \
    "presigned URL의 S3 GET과 /infer/ct 왕복이 성공했습니다. URL 값은 출력하지 않았습니다."

  label=$(jq -r '.label // empty' <<<"$response_body")
  if [[ $label == "PASS" ]]; then
    record_result 9 "PASS" \
      "정상 CT 이미지의 판정이 PASS입니다."
  else
    record_result 9 "FAIL" \
      "정상 CT 이미지의 기대 판정은 PASS이지만 실제 판정은 ${label:-없음}입니다."
  fi
}

restore_redis() {
  if compose start redis >/dev/null 2>&1; then
    REDIS_STOPPED=0
    return 0
  fi

  return 1
}

check_redis_isolation() {
  local response
  local status_code
  local elapsed
  local within_limit=0
  local restore_ok=1

  if ((INCLUDE_DESTRUCTIVE == 0)); then
    record_result 10 "SKIP" \
      "--include-destructive 플래그가 없어 Redis 중지 검증을 생략했습니다."
    return
  fi

  if ! service_enabled redis ||
    ! service_enabled frontend ||
    ! service_enabled backend; then
    record_result 10 "SKIP" \
      "redis, frontend, backend가 포함된 app 형상이 필요합니다."
    return
  fi

  if ! service_is_running redis ||
    ! service_is_running frontend ||
    ! service_is_running backend; then
    record_result 10 "FAIL" \
      "Redis 중지 전에 redis, frontend, backend가 모두 실행 중이어야 합니다."
    return
  fi

  if ! compose stop redis >/dev/null 2>&1; then
    record_result 10 "FAIL" \
      "Redis 중지에 실패했으므로 장애 검증을 실행하지 않았습니다."
    return
  fi
  REDIS_STOPPED=1

  response=$(
    curl \
      --silent \
      --show-error \
      --output /dev/null \
      --write-out '%{http_code} %{time_total}' \
      --connect-timeout "$REDIS_RESPONSE_LIMIT" \
      --max-time "$REDIS_RESPONSE_LIMIT" \
      "${BASE_URL%/}/api/sim" 2>/dev/null || true
  )
  status_code=${response%% *}
  elapsed=${response#* }

  if jq -e -n \
    --arg elapsed "$elapsed" \
    --arg limit "$REDIS_RESPONSE_LIMIT" \
    '($elapsed | tonumber? // 999999) <= ($limit | tonumber)' \
    >/dev/null 2>&1; then
    within_limit=1
  fi

  if ! restore_redis; then
    restore_ok=0
  fi

  if ((restore_ok == 0)); then
    record_result 10 "FAIL" \
      "검증 후 Redis 자동 복구에 실패했습니다. 종료 트랩에서 다시 시도합니다."
  elif [[ $status_code != "200" ]]; then
    record_result 10 "FAIL" \
      "Redis 중지 시 /api/sim이 HTTP ${status_code:-요청실패}를 반환했습니다. RedisConnectionFailureException 격리와 DB fallback 부재를 확인하십시오."
  elif ((within_limit == 0)); then
    record_result 10 "FAIL" \
      "Redis 중지 시 /api/sim 응답 시간이 ${elapsed:-측정실패}초로 ${REDIS_RESPONSE_LIMIT}초 기준을 넘었습니다."
  else
    record_result 10 "PASS" \
      "Redis 중지 중 /api/sim이 ${elapsed}초 안에 HTTP 200을 반환했고 Redis를 복구했습니다."
  fi
}

main() {
  if ! check_prerequisites; then
    record_result 1 "FAIL" \
      "필수 의존성 사전 점검이 실패했습니다."
    skip_unrecorded \
      "필수 의존성 사전 점검이 실패해 실행하지 않았습니다."
    print_summary
    exit 1
  fi

  if ! ENABLED_SERVICES=$(compose config --services 2>/dev/null); then
    record_result 1 "FAIL" \
      "docker compose config를 해석하지 못했습니다. .env 필수값과 Compose 설정을 확인하십시오."
    skip_unrecorded \
      "Compose 설정을 해석하지 못해 실행하지 않았습니다."
    print_summary
    exit 1
  fi

  check_containers
  check_vlm_model
  check_frontend
  check_api_proxy
  check_websocket_proxy
  check_supabase
  check_module_api_to_ai
  check_presigned_and_verdict
  check_redis_isolation

  print_summary

  if ((FAIL_COUNT > 0)); then
    exit 1
  fi

  exit 0
}

main
