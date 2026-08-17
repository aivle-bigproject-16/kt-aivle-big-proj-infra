#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_DEPLOY_DIR="/opt/battery/infra"
readonly DEFAULT_REPLAY_FIXTURE_URI="s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.8/replay/demo20-pass15-reject3-fail2-v1/wave-01-demo20-pass15-reject3-fail2.json"
readonly DEFAULT_REPLAY_FIXTURE_SHA256="12ebaee4e34a781f2e1534c4f0e4cf4626acbd855e9713100188bce26c99b8fd"
readonly DEFAULT_REPORT_FIXTURE_URI="s3://kt-aivle-big-proj-kks/simulations/server-simulation-v1.8/reports/demo20-20260817-v1/run-20-report-qa.json"
readonly DEFAULT_REPORT_FIXTURE_SHA256="76c8ea296df6bd60ff2cb17dd7d6ceb8ede7b439a9f55bcbab960a7936b241cb"

log() {
  printf '[serving-mode] %s\n' "$*"
}

die() {
  printf '[serving-mode] ERROR: %s\n' "$*" >&2
  exit 1
}

env_value() {
  local key="$1"
  local env_file="$2"
  awk -F= -v wanted="$key" '
    $0 !~ /^[[:space:]]*#/ && $1 == wanted {
      sub(/^[^=]*=/, "")
      value=$0
    }
    END {
      if (value == "") exit 1
      print value
    }
  ' "$env_file"
}

upsert_env() {
  local key="$1"
  local value="$2"
  local env_file="$3"
  local temp_file
  temp_file="$(mktemp "${env_file}.update.XXXXXX")"
  awk -F= -v wanted="$key" '$1 != wanted { print }' "$env_file" > "$temp_file"
  printf '%s=%s\n' "$key" "$value" >> "$temp_file"
  chmod --reference="$env_file" "$temp_file"
  mv -- "$temp_file" "$env_file"
}

container_healthy() {
  local name="$1"
  [[ "$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null || true)" == "running" ]] \
    && [[ "$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || true)" == "healthy" ]]
}

main() {
  [[ $# -eq 1 ]] || die "usage: switch-serving-mode.sh live|replay"
  local mode="${1,,}"
  [[ "$mode" == "live" || "$mode" == "replay" ]] || die "mode must be live or replay"

  local deploy_dir="${DEPLOY_DIR:-$DEFAULT_DEPLOY_DIR}"
  local env_file="${ENV_FILE:-${deploy_dir}/.env}"
  [[ -f "$deploy_dir/compose.yaml" ]] || die "missing ${deploy_dir}/compose.yaml"
  [[ -f "$env_file" ]] || die "missing ${env_file}"
  command -v docker >/dev/null 2>&1 || die "docker is not installed"

  local backup_file
  backup_file="$(mktemp "${env_file}.serving-mode.XXXXXX")"
  cp -a -- "$env_file" "$backup_file"

  local -a compose=(docker compose --env-file "$env_file" -f "$deploy_dir/compose.yaml")

  rollback() {
    local exit_code=$?
    trap - ERR
    set +e
    log "switch failed; restoring previous environment"
    cp -a -- "$backup_file" "$env_file"
    "${compose[@]}" up -d --no-deps backend-ai >/dev/null 2>&1
    rm -f -- "$backup_file"
    exit "$exit_code"
  }
  trap rollback ERR

  if [[ "$mode" == "replay" ]]; then
    env_value AI_INTERNAL_API_KEY "$env_file" >/dev/null \
      || die "AI_INTERNAL_API_KEY is missing"
    env_value BACKEND_CALLBACK_URL "$env_file" >/dev/null \
      || die "BACKEND_CALLBACK_URL is missing"
    if ! env_value REPLAY_FIXTURE_URI "$env_file" >/dev/null; then
      upsert_env REPLAY_FIXTURE_URI "$DEFAULT_REPLAY_FIXTURE_URI" "$env_file"
    fi
    if ! env_value REPLAY_FIXTURE_SHA256 "$env_file" >/dev/null; then
      upsert_env REPLAY_FIXTURE_SHA256 "$DEFAULT_REPLAY_FIXTURE_SHA256" "$env_file"
    fi
    if ! env_value REPLAY_REPORT_FIXTURE_URI "$env_file" >/dev/null; then
      upsert_env REPLAY_REPORT_FIXTURE_URI "$DEFAULT_REPORT_FIXTURE_URI" "$env_file"
    fi
    if ! env_value REPLAY_REPORT_FIXTURE_SHA256 "$env_file" >/dev/null; then
      upsert_env REPLAY_REPORT_FIXTURE_SHA256 "$DEFAULT_REPORT_FIXTURE_SHA256" "$env_file"
    fi

    upsert_env SERVING_MODE replay "$env_file"
    upsert_env AI_SERVER_URL http://replay:8000 "$env_file"
    upsert_env LLM_SERVER_URL http://replay:8000 "$env_file"

    "${compose[@]}" up -d --no-deps --build --wait --wait-timeout 180 replay \
      || rollback
    container_healthy battery-replay || rollback
    "${compose[@]}" up -d --no-deps --wait --wait-timeout 180 backend-ai \
      || rollback
    log "REPLAY active; ai-infer and vlm are no longer request targets"
  else
    container_healthy battery-ai-infer || die "battery-ai-infer is not healthy"
    container_healthy battery-vlm || die "battery-vlm is not healthy"

    upsert_env SERVING_MODE live "$env_file"
    upsert_env AI_SERVER_URL http://ai-infer:8000 "$env_file"
    upsert_env LLM_SERVER_URL http://vlm:8001 "$env_file"

    "${compose[@]}" up -d --no-deps --wait --wait-timeout 180 backend-ai \
      || rollback
    "${compose[@]}" stop replay >/dev/null 2>&1 || true
    log "LIVE active"
  fi

  trap - ERR
  rm -f -- "$backup_file"
}

main "$@"
