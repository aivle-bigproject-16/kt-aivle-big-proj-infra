#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

if [ -z "${VERIFY_PRESIGNED_URL:-}" ]; then
    result FAIL "set VERIFY_PRESIGNED_URL to a valid presigned image URL before running check 8"
fi

case "$VERIFY_PRESIGNED_URL" in
    http://*|https://*) ;;
    *) result FAIL "VERIFY_PRESIGNED_URL must be an HTTP or HTTPS URL" ;;
esac

response=$(printf '%s\n' "$VERIFY_PRESIGNED_URL" | compose exec -T ai-infer python -c '
import json
import sys
import urllib.request

image_url = sys.stdin.readline().rstrip("\n")
payload = json.dumps({
    "inspection_id": 900008,
    "image_key": "verify/presigned-round-trip",
    "image_url": image_url,
}).encode()
request = urllib.request.Request(
    "http://localhost:8000/infer/ct",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=120) as result:
        print(result.read().decode())
        raise SystemExit(0 if result.status == 200 else 1)
except Exception as exc:
    print("REQUEST_FAILED " + type(exc).__name__)
    raise SystemExit(1)
' 2>/dev/null) || {
    log_err "ai-infer could not complete the presigned image request"
    result FAIL "ai-infer failed to GET or infer the presigned image"
}

if ! printf '%s\n' "$response" | grep -Eq '"label"[[:space:]]*:[[:space:]]*"(PASS|REJECT|FAIL)"'; then
    log_err "ai-infer response did not contain a recognized judgement"
    printf '%s\n' "$response" | sed -E 's#(https?://[^?" ]+)\?[^" ]*#\1?<redacted>#g' >&2
    result FAIL "presigned image request returned an invalid inference response"
fi

result PASS "ai-infer downloaded the presigned image and returned a judgement"
