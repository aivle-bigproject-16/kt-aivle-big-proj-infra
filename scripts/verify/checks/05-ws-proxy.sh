#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

header_file=$(mktemp)
error_file=$(mktemp)
trap 'rm -f "$header_file" "$error_file"' EXIT HUP INT TERM

started=$(date +%s)
code=$(curl -sS --http1.1 --max-time 6 \
    -D "$header_file" -o /dev/null -w '%{http_code}' \
    -H 'Connection: Upgrade' \
    -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    -H 'Sec-WebSocket-Version: 13' \
    -H 'Origin: http://localhost' \
    "$BASE_URL/ws/sim" 2>"$error_file" || true)
elapsed=$(( $(date +%s) - started ))

if [ "$code" = 101 ] && [ "$elapsed" -ge 5 ]; then
    result PASS "/ws/sim returned HTTP 101 and remained open for ${elapsed}s"
fi

log_err "WebSocket probe returned HTTP ${code:-000} after ${elapsed}s"
sed -n '1,20p' "$header_file" | while IFS= read -r line; do redact "$line" >&2; done
sed -n '1,20p' "$error_file" | while IFS= read -r line; do redact "$line" >&2; done
result FAIL "/ws proxy did not sustain a WebSocket upgrade for 5 seconds"
