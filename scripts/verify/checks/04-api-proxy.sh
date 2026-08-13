#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

body_file=$(mktemp)
header_file=$(mktemp)
trap 'rm -f "$body_file" "$header_file"' EXIT HUP INT TERM

code=$(curl -sS --max-time 10 \
    -D "$header_file" -o "$body_file" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    --data '{}' "$BASE_URL/api/auth/signup" 2>/dev/null || true)

case "$code" in
    502|504|000|"")
        log_err "nginx /api proxy returned HTTP ${code:-000}"
        result FAIL "/api proxy did not reach backend"
        ;;
    2??)
        result PASS "/api/auth/signup reached Spring and returned HTTP $code"
        ;;
    4??)
        if grep -Eiq '^content-type:[[:space:]]*application/(problem\+)?json|^[[:space:]]*\{' "$header_file" "$body_file"; then
            log_info "Spring validation response was HTTP $code"
            sed -n '1,20p' "$body_file" | while IFS= read -r line; do redact "$line" >&2; done
            result PASS "/api proxy reached Spring validation and returned HTTP $code"
        fi
        ;;
esac

log_err "unexpected response through /api proxy: HTTP ${code:-000}"
sed -n '1,20p' "$body_file" | while IFS= read -r line; do redact "$line" >&2; done
result FAIL "/api proxy did not produce a Spring response"
