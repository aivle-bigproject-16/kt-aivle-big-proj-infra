#!/usr/bin/env bash

REPO_ROOT=${REPO_ROOT:-$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)}
ENV_FILE=${ENV_FILE:-"$REPO_ROOT/.env"}
BASE_URL=${BASE_URL:-http://localhost}

compose() {
    (
        cd "$REPO_ROOT" || exit 1
        docker compose --env-file "$ENV_FILE" \
            -f compose.yaml -f compose.gpu.yaml \
            --profile app --profile ai "$@"
    )
}

svc_state() {
    local service=$1 cid state health

    cid=$(compose ps --all -q "$service" 2>/dev/null | head -n 1)
    if [ -z "$cid" ]; then
        printf '%s\n' missing
        return 0
    fi

    state=$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null) || {
        printf '%s\n' missing
        return 0
    }

    if [ "$state" = running ]; then
        health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)
        if [ "$health" = healthy ]; then
            printf '%s\n' running-healthy
        else
            printf '%s\n' running
        fi
    else
        printf '%s\n' exited
    fi
}

wait_for() {
    local timeout=$1 command started now
    shift
    command=$*
    started=$(date +%s)

    while :; do
        if eval "$command" >/dev/null 2>&1; then
            return 0
        fi
        now=$(date +%s)
        if [ $((now - started)) -ge "$timeout" ]; then
            return 1
        fi
        sleep 2
    done
}

logs_since() {
    local service=$1 duration=$2
    compose logs --no-color --since "$duration" "$service" 2>&1
}

http_code() {
    local url=$1
    shift
    curl -sS -o /dev/null -w '%{http_code}' "$@" "$url"
}

result() {
    local status=$1 reason code
    shift
    reason=$*
    reason=${reason//$'\n'/ }
    reason=${reason//$'\r'/ }
    reason=${reason//|//}

    case "$status" in
        PASS) code=0 ;;
        FAIL) code=1 ;;
        SKIP) code=2 ;;
        XFAIL) code=3 ;;
        *) status=FAIL; code=1; reason="invalid result status" ;;
    esac

    printf 'RESULT=%s|%s\n' "$status" "$reason"
    exit "$code"
}

redact() {
    local value=$1
    case "$value" in
        *\?*) printf '%s?<redacted>\n' "${value%%\?*}" ;;
        *) printf '%s\n' "$value" ;;
    esac
}

log_info() {
    printf 'INFO: %s\n' "$*" >&2
}

log_warn() {
    printf 'WARN: %s\n' "$*" >&2
}

log_err() {
    printf 'ERROR: %s\n' "$*" >&2
}

export REPO_ROOT ENV_FILE BASE_URL
export -f compose svc_state wait_for logs_since http_code result redact log_info log_warn log_err
