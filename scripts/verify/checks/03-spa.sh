#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

body_file=$(mktemp)
trap 'rm -f "$body_file"' EXIT HUP INT TERM

code=$(curl -sS --max-time 5 -o "$body_file" -w '%{http_code}' "$BASE_URL" 2>/dev/null || true)
if [ "$code" != 200 ]; then
    log_err "SPA request returned HTTP ${code:-000}"
    result FAIL "frontend root did not return HTTP 200"
fi

if ! grep -Eiq '<html|<!doctype' "$body_file" || \
   ! grep -Eiq "<div[^>]+id=['\"]root['\"]|<script[^>]+src=" "$body_file"; then
    log_err "response body does not look like a React SPA shell"
    sed -n '1,20p' "$body_file" | while IFS= read -r line; do redact "$line" >&2; done
    result FAIL "frontend returned 200 but body is not a recognizable SPA shell"
fi

result PASS "frontend root returned HTTP 200 with an SPA shell"
