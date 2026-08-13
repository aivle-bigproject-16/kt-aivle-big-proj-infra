#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

timeout=${VERIFY_VLM_TIMEOUT:-330}
if [ "${VERIFY_NO_WAIT:-0}" = 1 ]; then
    timeout=0
fi

probe="compose exec -T vlm python -c \"import urllib.request; response = urllib.request.urlopen('http://localhost:8001/health', timeout=5); raise SystemExit(0 if response.status == 200 else 1)\""

if ! wait_for "$timeout" "$probe"; then
    log_err "vlm did not serve /health within ${timeout}s; raw log tail follows"
    compose logs --no-color --tail 200 vlm >&2 || true
    result FAIL "vlm model load or health request failed"
fi

vlm_logs=$(compose logs --no-color --tail 2000 vlm 2>&1)
if ! printf '%s\n' "$vlm_logs" | grep -Fq '모델 적재 완료'; then
    log_err "vlm /health responded but the model-load completion marker is absent; raw log tail follows"
    compose logs --no-color --tail 200 vlm >&2 || true
    result FAIL "vlm serves health but model load completion was not confirmed in logs"
fi

result PASS "vlm model load marker found and internal /health returned 200"
