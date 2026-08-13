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

json_escape() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    printf '%s' "$value"
}

login_cookie_jar() {
    local cookie_file=$1 email password escaped_email escaped_password body_file code

    email=${VERIFY_LOGIN_EMAIL:-}
    password=${VERIFY_LOGIN_PASSWORD:-}
    if [ -z "$email" ] || [ -z "$password" ]; then
        log_err "VERIFY_LOGIN_EMAIL and VERIFY_LOGIN_PASSWORD are required for authenticated checks"
        return 2
    fi

    escaped_email=$(json_escape "$email")
    escaped_password=$(json_escape "$password")
    body_file=$(mktemp) || return 1

    code=$(printf '{"email":"%s","password":"%s"}' "$escaped_email" "$escaped_password" | \
        curl -sS --max-time 15 -o "$body_file" -w '%{http_code}' \
            -c "$cookie_file" \
            -H 'Content-Type: application/json' \
            --data-binary @- \
            "$BASE_URL/api/auth/login" 2>/dev/null || true)

    rm -f "$body_file"
    case "$code" in
        2??) ;;
        *)
            log_err "verification login returned HTTP ${code:-000}"
            return 1
            ;;
    esac

    if ! grep -q $'\taccess_token\t' "$cookie_file" 2>/dev/null; then
        log_err "verification login succeeded without an access_token cookie"
        return 1
    fi
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
export -f compose svc_state wait_for logs_since http_code json_escape login_cookie_jar result redact log_info log_warn log_err
