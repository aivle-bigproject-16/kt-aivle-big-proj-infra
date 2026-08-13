#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

if [ "${VERIFY_ALLOW_MUTATE:-0}" != 1 ]; then
    result SKIP "requires --allow-mutate"
fi

redis_stopped=0
cleanup() {
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

redis_stopped=1
if ! compose stop redis >&2; then
    result FAIL "could not stop redis for the fault-isolation probe"
fi

if ! wait_for 30 '[ "$(svc_state redis)" = exited ]'; then
    result FAIL "redis did not stop within 30 seconds"
fi

code=$(http_code "$BASE_URL/api/sim" --max-time 1 2>/dev/null || true)
wait_for 5 "logs_since backend '$started' | grep -Eq 'RedisConnectionFailureException|RedisConnectionException|Unable to connect to Redis'" || true
recent_logs=$(logs_since backend "$started")

if ! cleanup; then
    result FAIL "redis fault probe completed but redis could not be restarted"
fi

if [ "$code" = 200 ]; then
    result PASS "/api/sim remained available within 1 second while redis was stopped"
fi

if printf '%s\n' "$recent_logs" | grep -Eq 'RedisConnectionFailureException|RedisConnectionException|Unable to connect to Redis'; then
    printf '%s\n' "$recent_logs" | grep -E 'RedisConnectionFailureException|RedisConnectionException|Unable to connect to Redis' | tail -n 10 >&2
    result XFAIL "known SimulationSnapshotStore Redis exception prevented the 1-second UI response"
fi

result FAIL "/api/sim failed during the redis outage without the expected Redis exception evidence"
