#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

if ! compose exec -T backend-ai sh -c '[ "$LLM_SERVER_URL" = "http://vlm:8001" ]' >/dev/null 2>&1; then
    result FAIL "backend-ai LLM_SERVER_URL is not http://vlm:8001"
fi

body_file=$(mktemp)
cookie_file=$(mktemp)
trap 'rm -f "$body_file" "$cookie_file"' EXIT HUP INT TERM

if ! login_cookie_jar "$cookie_file"; then
    result FAIL "could not log in with the verification account"
fi

started=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
report_date=$(date -u '+%Y-%m-%d')

code=$(curl -sS --max-time 15 -o "$body_file" -w '%{http_code}' \
    -b "$cookie_file" \
    -H 'Content-Type: application/json' \
    --data "{\"reportDate\":\"$report_date\"}" \
    "$BASE_URL/api/reports/daily" 2>/dev/null || true)

case "$code" in
    2??) ;;
    *)
        log_err "daily report trigger returned HTTP ${code:-000}"
        sed -n '1,20p' "$body_file" | while IFS= read -r line; do redact "$line" >&2; done
        result FAIL "report request could not trigger the module-api to module-ai path"
        ;;
esac

wait_for 10 "logs_since backend '$started' | grep -Fq 'Failed to trigger LLM generation' || logs_since backend-ai '$started' | grep -Fq 'Received internal request to generate daily report'" || true
recent_logs=$(logs_since backend "$started")
ai_logs=$(logs_since backend-ai "$started")

if printf '%s\n' "$ai_logs" | grep -Fq 'Received internal request to generate daily report'; then
    result PASS "backend-ai received the report trigger and LLM_SERVER_URL is http://vlm:8001"
fi

# Backend commit 0824846 (BE PR #15) replaced the hardcoded http://localhost:8081 in
# ReportService with the injected aiGatewayRestClient, which reads ai-gateway.base-url
# from AI_GATEWAY_URL. Compose supplies http://backend-ai:8081, so this call is expected
# to succeed now. A trigger failure is therefore no longer an accepted known defect --
# treating it as XFAIL would hide a genuine wiring or auth problem.
if printf '%s\n' "$recent_logs" | grep -Fq 'Failed to trigger LLM generation'; then
    printf '%s\n' "$recent_logs" | grep -F 'Failed to trigger LLM generation' | tail -n 5 >&2
    if [ "${BACKEND_PRE_0824846:-0}" = "1" ]; then
        result XFAIL "pre-0824846 backend still hardcodes localhost:8081 while backend-ai LLM_SERVER_URL is correct"
    fi
    result FAIL "backend 0824846 should reach backend-ai:8081 but the trigger failed -- check AI_GATEWAY_URL and AI_INTERNAL_API_KEY"
fi

result FAIL "report request returned success but backend-ai receipt was not found in logs"
