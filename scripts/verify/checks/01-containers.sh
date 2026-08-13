#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

services="frontend backend backend-ai ai-infer vlm redis"
health_services="ai-infer vlm redis"
timeout=${VERIFY_CONTAINER_TIMEOUT:-360}

if [ "${VERIFY_NO_WAIT:-0}" = 1 ]; then
    timeout=0
fi

all_ready() {
    local service state
    for service in $services; do
        state=$(svc_state "$service")
        case " $health_services " in
            *" $service ") [ "$state" = running-healthy ] || return 1 ;;
            *) [ "$state" = running ] || [ "$state" = running-healthy ] || return 1 ;;
        esac
    done
    return 0
}

if ! wait_for "$timeout" all_ready; then
    failed=""
    for service in $services; do
        state=$(svc_state "$service")
        log_err "$service state is $state"
        failed="$failed $service=$state"
    done
    result FAIL "container readiness failed:$failed"
fi

result PASS "all six services are running and declared healthchecks are healthy"
