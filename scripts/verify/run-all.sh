#!/usr/bin/env bash
set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
only_list=""
skip_list=""
allow_mutate=0
no_wait=0

usage() {
    printf 'Usage: bash run-all.sh [--only N[,N...]] [--skip N[,N...]] [--allow-mutate] [--no-wait]\n' >&2
}

valid_list() {
    [[ $1 =~ ^([1-9]|10)(,([1-9]|10))*$ ]]
}

contains_check() {
    case ",$1," in
        *",$2,"*) return 0 ;;
        *) return 1 ;;
    esac
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --only)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            only_list=$2
            valid_list "$only_list" || { usage; exit 2; }
            shift 2
            ;;
        --skip)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            skip_list=$2
            valid_list "$skip_list" || { usage; exit 2; }
            shift 2
            ;;
        --allow-mutate)
            allow_mutate=1
            shift
            ;;
        --no-wait)
            no_wait=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

check_files=(
    "01-containers.sh"
    "02-vlm-model-load.sh"
    "03-spa.sh"
    "04-api-proxy.sh"
    "05-ws-proxy.sh"
    "06-supabase.sh"
    "07-module-ai-wiring.sh"
    "08-s3-presigned.sh"
    "09-judgement-regression.sh"
    "10-redis-isolation.sh"
)
check_names=(
    "컨테이너 생존 및 헬스"
    "vlm 모델 적재"
    "SPA 접근"
    "/api 프록시"
    "/ws 프록시"
    "Supabase 연결"
    "module-api와 module-ai 연결"
    "S3 presigned URL 왕복"
    "판정 정확성 회귀"
    "Redis 장애 격리"
)

declare -a statuses reasons
all_diagnostics=$(mktemp)
table_file=$(mktemp)
check_diag=$(mktemp)
trap 'rm -f "$all_diagnostics" "$table_file" "$check_diag"' EXIT HUP INT TERM
has_fail=0

for index in "${!check_files[@]}"; do
    number=$((index + 1))
    padded=$(printf '%02d' "$number")
    status=""
    reason=""

    if [ -n "$only_list" ] && ! contains_check "$only_list" "$number"; then
        status=SKIP
        reason="not selected by --only"
    elif [ -n "$skip_list" ] && contains_check "$skip_list" "$number"; then
        status=SKIP
        reason="excluded by --skip"
    else
        : > "$check_diag"
        stdout=$(cd "$SCRIPT_DIR" && \
            VERIFY_ALLOW_MUTATE="$allow_mutate" \
            VERIFY_NO_WAIT="$no_wait" \
            bash "checks/${check_files[$index]}" 2>"$check_diag")
        rc=$?

        summary_count=$(printf '%s\n' "$stdout" | grep -c '^RESULT=' || true)
        line_count=$(printf '%s\n' "$stdout" | grep -c '.' || true)
        summary=$(printf '%s\n' "$stdout" | tail -n 1)

        if [ "$summary_count" -ne 1 ] || [ "$line_count" -ne 1 ] || [[ $summary != RESULT=*\|* ]]; then
            status=FAIL
            reason="check violated the one-line RESULT contract"
            {
                printf '\n## Check %s %s\n\n' "$padded" "${check_names[$index]}"
                printf 'Invalid stdout was suppressed by the runner.\n'
                sed 's/^/    /' "$check_diag"
            } >> "$all_diagnostics"
        else
            payload=${summary#RESULT=}
            status=${payload%%|*}
            reason=${payload#*|}
            reason=${reason//|//}

            case "$status:$rc" in
                PASS:0|FAIL:1|SKIP:2|XFAIL:3) ;;
                *)
                    status=FAIL
                    reason="result status and exit code disagree"
                    ;;
            esac

            if [ -s "$check_diag" ]; then
                {
                    printf '\n## Check %s %s\n\n' "$padded" "${check_names[$index]}"
                    sed 's/^/    /' "$check_diag"
                } >> "$all_diagnostics"
            fi
        fi
    fi

    statuses[$index]=$status
    reasons[$index]=$reason
    if [ "$status" = FAIL ]; then
        has_fail=1
    fi
    printf '[%s/10] %s: %s - %s\n' "$padded" "${check_names[$index]}" "$status" "$reason" >&2
done

{
    printf '| # | 검증 | 결과 | 근거 |\n'
    printf '|---|---|---|---|\n'
    for index in "${!check_files[@]}"; do
        printf '|%d|%s|%s|%s|\n' \
            "$((index + 1))" "${check_names[$index]}" "${statuses[$index]}" "${reasons[$index]}"
    done
} > "$table_file"

timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
report_path="$SCRIPT_DIR/reports/verify-$timestamp.md"
if ! mkdir -p "$SCRIPT_DIR/reports" || ! {
        printf '# 배포 검증 보고서\n\n'
        cat "$table_file"
        printf '\n# 진단 출력\n'
        if [ -s "$all_diagnostics" ]; then
            cat "$all_diagnostics"
        else
            printf '\n진단 출력이 없습니다.\n'
        fi
    } > "$report_path"; then
    printf 'ERROR: could not write report: %s\n' "$report_path" >&2
    has_fail=1
fi

cat "$table_file"
printf 'Report: %s\n' "$report_path" >&2
exit "$has_fail"
