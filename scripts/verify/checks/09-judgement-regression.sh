#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

fixture=${VERIFY_NORMAL_CT_FIXTURE:-fixtures/normal-ct.png}

if [ ! -f "$fixture" ]; then
    log_err "missing known-good CT fixture: $fixture"
    log_err "place a normal cell CT image at scripts/verify/fixtures/normal-ct.png"
    result FAIL "missing normal CT fixture at scripts/verify/fixtures/normal-ct.png"
fi

if [ ! -s "$fixture" ]; then
    result FAIL "normal CT fixture exists but is empty"
fi

response=$(compose exec -T ai-infer python -c '
import json
import sys
from app.adapters.factory import build_adapter
from app.settings import load_settings

adapter = build_adapter("ct", load_settings())
print(json.dumps(adapter.predict(sys.stdin.buffer.read())))
' < "$fixture" 2>/dev/null) || {
    log_err "CT adapter could not evaluate the supplied fixture"
    result FAIL "normal CT fixture inference failed"
}

judgement=$(printf '%s\n' "$response" | grep -Eo '"label"[[:space:]]*:[[:space:]]*"(PASS|REJECT|FAIL)"' | tail -n 1 || true)
if printf '%s\n' "$judgement" | grep -Eq '"PASS"'; then
    result PASS "known-good normal CT fixture was judged PASS"
fi

log_err "normal CT regression fixture was not judged PASS"
printf '%s\n' "$response" | tail -n 5 >&2
result FAIL "known-good normal CT fixture was judged REJECT or FAIL"
