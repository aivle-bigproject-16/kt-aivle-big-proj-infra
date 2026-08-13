#!/usr/bin/env bash
set -u
. "$(dirname "$0")/../lib/common.sh"

if ! state=$(svc_state backend) || { [ "$state" != running ] && [ "$state" != running-healthy ]; }; then
    result FAIL "backend is not running, so Supabase startup cannot be verified"
fi

backend_logs=$(compose logs --no-color --tail 3000 backend 2>&1)

if ! printf '%s\n' "$backend_logs" | grep -Eq 'HikariPool-[0-9]+.*Start completed'; then
    log_err "Hikari pool start marker was not found in backend logs"
    result FAIL "backend logs do not show a started Hikari pool"
fi

if printf '%s\n' "$backend_logs" | grep -Eiq 'Schema-validation:|SchemaManagementException|missing (table|column)|wrong column type'; then
    log_err "JPA schema validation failure found in backend logs"
    printf '%s\n' "$backend_logs" | grep -Ei 'Schema-validation:|SchemaManagementException|missing (table|column)|wrong column type' >&2
    result FAIL "JPA ddl-auto validation reported a schema mismatch"
fi

if ! printf '%s\n' "$backend_logs" | grep -Eq 'Started ApiApplication'; then
    log_err "ApiApplication startup completion marker was not found"
    result FAIL "Hikari started but application startup did not complete after schema validation"
fi

result PASS "Hikari started and ApiApplication completed ddl-auto validation without mismatch"
