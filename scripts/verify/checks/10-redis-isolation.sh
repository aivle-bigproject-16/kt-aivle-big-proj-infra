#!/usr/bin/env bash
# Redis 장애 정책은 Option B(완결된 실패 응답)다.
# 완전 중단 시 Docker DNS 실패 감지에 약 5초가 걸리는 것이 확인되어,
# BE가 전체 호출 시간 예산을 결정할 때까지 임시 응답 상한을 10초로 둔다.
# 근거는 BE origin/main의 26c801f(PR #16, 변경 커밋 ccd9ae3)에서 확인한 실제 코드다.
# SimulationSnapshotStore.find()는 RedisConnectionFailureException을 캐시 미스로 처리하지 않고
# HTTP 503 ResponseStatusException으로 변환하며, DB 또는 빈 스냅샷 대체 경로는 추가하지 않았다.
# gh 인증 토큰이 유효하지 않아 PR 본문과 댓글은 확인하지 못했으므로 이 판정은 BE main diff에만 근거한다.
set -u
. "$(dirname "$0")/../lib/common.sh"

response_cap=10

if [ "${VERIFY_ALLOW_MUTATE:-0}" != 1 ]; then
    result SKIP "requires --allow-mutate"
fi

cookie_file=$(mktemp)
redis_stopped=0
cleanup() {
    rm -f "$cookie_file"
    if [ "$redis_stopped" = 1 ]; then
        log_info "restarting redis"
        if compose start redis >&2 && wait_for 60 '[ "$(svc_state redis)" = running-healthy ]'; then
            redis_stopped=0
            log_info "redis restarted and is healthy"
            return 0
        fi
        log_err "redis restart did not become healthy"
        return 1
    fi
    return 0
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

started=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
initial_state=$(svc_state redis)
if [ "$initial_state" != running ] && [ "$initial_state" != running-healthy ]; then
    result FAIL "redis is not running before the fault-isolation probe"
fi

if ! login_cookie_jar "$cookie_file"; then
    result FAIL "could not log in with the verification account"
fi

redis_stopped=1
if ! compose stop redis >&2; then
    result FAIL "could not stop redis for the fault-isolation probe"
fi

if ! wait_for 30 '[ "$(svc_state redis)" = exited ]'; then
    result FAIL "redis did not stop within 30 seconds"
fi

probe=$(curl -sS -o /dev/null \
    -b "$cookie_file" \
    --connect-timeout 1 \
    --max-time "$response_cap" \
    -w '%{http_code}|%{time_total}' \
    "$BASE_URL/api/sim")
curl_status=$?

code=${probe%%|*}
elapsed=${probe#*|}

if ! cleanup; then
    result FAIL "redis fault probe completed but redis could not be restarted"
fi

if [ "$curl_status" -ne 0 ]; then
    result FAIL "/api/sim hung, timed out, or dropped the connection while redis was stopped (curl exit $curl_status, ${elapsed:-unknown}s)"
fi

case "$code" in
    5??) ;;
    *) result FAIL "/api/sim returned HTTP $code instead of a failure response (HTTP 5xx) while redis was stopped (${elapsed}s)" ;;
esac

if ! awk -v elapsed="$elapsed" -v cap="$response_cap" 'BEGIN { exit !(elapsed + 0 <= cap) }'; then
    result FAIL "/api/sim returned HTTP $code after ${elapsed}s, exceeding the ${response_cap}-second temporary cap"
fi

result PASS "/api/sim returned HTTP $code in ${elapsed}s while redis was stopped; the response completed without a hang or connection drop"
