#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_DEPLOY_DIR="/opt/battery/infra"
readonly DEFAULT_AWS_REGION="ap-northeast-2"
readonly DEFAULT_RUNTIME_PARAMETER="/kt-aivle-big-proj/prod/runtime-env"
readonly PRODUCTION_INSTANCE_ID="i-0562ca896665be441"
readonly LOCK_FILE="/var/lock/battery-deploy.lock"

# publish_runtime_env() returns this when the host is deliberately not allowed to
# own the published runtime release. That is an expected outcome on the GPU test
# host, so the caller must not treat it as a deployment failure.
readonly RUNTIME_PUBLISH_SKIPPED=3

log() {
  printf '[deploy] %s\n' "$*"
}

die() {
  printf '[deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  deploy-service.sh --check
  deploy-service.sh <service> <image-tag>

Services:
  frontend | backend | backend-ai | ai-infer | vlm

Environment:
  DEPLOY_DIR  Compose directory (default: /opt/battery/infra)
  AWS_REGION  ECR region (default: ap-northeast-2)
  RUNTIME_ENV_PARAMETER  SecureString published after a successful production deploy
EOF
}

instance_id() {
  local token
  token="$(curl -fsS --connect-timeout 2 --max-time 5 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)" || return 1
  curl -fsS --connect-timeout 2 --max-time 5 \
    -H "X-aws-ec2-metadata-token: ${token}" \
    http://169.254.169.254/latest/meta-data/instance-id
}

publish_runtime_env() {
  local env_file="$1"
  local region="$2"
  local parameter="${RUNTIME_ENV_PARAMETER:-$DEFAULT_RUNTIME_PARAMETER}"
  local current_instance_id

  current_instance_id="$(instance_id)" || {
    log "unable to determine instance ID; runtime release was not published"
    return 1
  }
  if [[ "$current_instance_id" != "$PRODUCTION_INSTANCE_ID" ]]; then
    log "refusing to publish runtime release from ${current_instance_id}"
    return "$RUNTIME_PUBLISH_SKIPPED"
  fi

  log "publishing deployed tag set to ${parameter}"
  aws ssm put-parameter \
    --region "$region" \
    --name "$parameter" \
    --type SecureString \
    --value "file://${env_file}" \
    --overwrite >/dev/null
}

finish_deployment() {
  local env_file="$1"
  local region="$2"
  local trigger="$3"
  local status=0

  publish_runtime_env "$env_file" "$region" || status=$?
  case "$status" in
    0)
      ;;
    "$RUNTIME_PUBLISH_SKIPPED")
      log "the published runtime release is owned by production; continuing"
      ;;
    *)
      die "service is deployed but the runtime release was not published"
      ;;
  esac

  run_post_deploy_benchmark "$trigger" "$BENCHMARK_SUITE"
}

run_post_deploy_benchmark() {
  local trigger="$1"
  local suite="$2"

  if [[ "$suite" == "none" ]]; then
    log "no AI benchmark is associated with ${trigger}; skipping"
    return 0
  fi

  if [[ ! -x /usr/local/bin/battery-post-deploy-benchmark ]]; then
    log "benchmark runner is not installed; skipping"
    return 0
  fi

  if /usr/local/bin/battery-post-deploy-benchmark \
    --trigger "$trigger" --suite "$suite"; then
    log "post-deploy benchmark succeeded"
  else
    log "WARNING: post-deploy benchmark failed; deployment remains active"
  fi
}

env_value() {
  local key="$1"
  local env_file="$2"

  awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; found=1; exit } END { if (!found) exit 1 }' "$env_file"
}

replace_env_value() {
  local key="$1"
  local value="$2"
  local env_file="$3"
  local temp_file

  temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
  if ! awk -F= -v key="$key" -v value="$value" '
    $1 == key { print key "=" value; found=1; next }
    { print }
    END { if (!found) exit 42 }
  ' "$env_file" > "$temp_file"; then
    rm -f -- "$temp_file"
    die "${key} is missing from ${env_file}"
  fi

  chmod --reference="$env_file" "$temp_file"
  chown --reference="$env_file" "$temp_file"
  mv -f -- "$temp_file" "$env_file"
}

service_config() {
  local service="$1"

  case "$service" in
    frontend)
      TAG_KEY="FRONTEND_TAG"
      CONTAINER_NAME="battery-frontend"
      WAIT_TIMEOUT=180
      BENCHMARK_SUITE="none"
      ;;
    backend)
      TAG_KEY="BACKEND_TAG"
      CONTAINER_NAME="battery-backend"
      WAIT_TIMEOUT=240
      BENCHMARK_SUITE="none"
      ;;
    backend-ai)
      TAG_KEY="BACKEND_AI_TAG"
      CONTAINER_NAME="battery-backend-ai"
      WAIT_TIMEOUT=240
      BENCHMARK_SUITE="none"
      ;;
    ai-infer)
      TAG_KEY="AI_INFER_TAG"
      CONTAINER_NAME="battery-ai-infer"
      WAIT_TIMEOUT=600
      BENCHMARK_SUITE="inference"
      ;;
    vlm)
      TAG_KEY="VLM_TAG"
      CONTAINER_NAME="battery-vlm"
      WAIT_TIMEOUT=900
      BENCHMARK_SUITE="vlm"
      ;;
    *)
      die "unsupported service: ${service}"
      ;;
  esac
}

build_compose_command() {
  local deploy_dir="$1"
  local env_file="$2"

  COMPOSE=(docker compose --env-file "$env_file" -f "$deploy_dir/compose.yaml")

  if [[ -f "$deploy_dir/compose.gpu.yaml" ]] \
    && command -v nvidia-smi >/dev/null 2>&1 \
    && nvidia-smi -L >/dev/null 2>&1; then
    COMPOSE+=(-f "$deploy_dir/compose.gpu.yaml")
  elif [[ -f "$deploy_dir/compose.cpu.yaml" ]]; then
    COMPOSE+=(-f "$deploy_dir/compose.cpu.yaml")
  fi
}

check_prerequisites() {
  local deploy_dir="$1"
  local env_file="$2"

  command -v aws >/dev/null 2>&1 || die "aws CLI is not installed"
  command -v curl >/dev/null 2>&1 || die "curl is not installed"
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  command -v flock >/dev/null 2>&1 || die "flock is not installed"
  [[ -f "$deploy_dir/compose.yaml" ]] || die "missing ${deploy_dir}/compose.yaml"
  [[ -f "$env_file" ]] || die "missing ${env_file}"

  local required_key
  for required_key in ECR_REGISTRY FRONTEND_TAG BACKEND_TAG BACKEND_AI_TAG AI_INFER_TAG VLM_TAG; do
    env_value "$required_key" "$env_file" >/dev/null || die "${required_key} is missing from ${env_file}"
  done

  "${COMPOSE[@]}" config --quiet
}

verify_container() {
  local container_name="$1"
  local state
  local health

  state="$(docker inspect --format '{{.State.Status}}' "$container_name" 2>/dev/null)" || return 1
  [[ "$state" == "running" ]] || return 1

  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_name")" || return 1
  [[ "$health" == "none" || "$health" == "healthy" ]] || return 1

  sleep 5
  [[ "$(docker inspect --format '{{.State.Status}}' "$container_name" 2>/dev/null)" == "running" ]]
}

deploy_once() {
  local service="$1"
  local registry="$2"
  local region="$3"

  log "authenticating Docker to ${registry}"
  aws ecr get-login-password --region "$region" \
    | docker login --username AWS --password-stdin "$registry" >/dev/null || return 1

  log "pulling ${service}"
  "${COMPOSE[@]}" config --quiet || return 1
  "${COMPOSE[@]}" pull "$service" || return 1

  log "starting ${service}"
  "${COMPOSE[@]}" up -d --no-deps --wait --wait-timeout "$WAIT_TIMEOUT" "$service" || return 1
  verify_container "$CONTAINER_NAME" || return 1
}

main() {
  local deploy_dir="${DEPLOY_DIR:-$DEFAULT_DEPLOY_DIR}"
  local region="${AWS_REGION:-$DEFAULT_AWS_REGION}"
  local env_file="${deploy_dir}/.env"

  build_compose_command "$deploy_dir" "$env_file"

  if [[ "${1:-}" == "--check" ]]; then
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    check_prerequisites "$deploy_dir" "$env_file"
    log "deployment prerequisites are valid"
    exit 0
  fi

  [[ $# -eq 2 ]] || { usage >&2; exit 2; }

  local service="$1"
  local new_tag="$2"
  [[ "$new_tag" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "invalid OCI image tag: ${new_tag}"

  service_config "$service"
  check_prerequisites "$deploy_dir" "$env_file"

  exec 9>"$LOCK_FILE"
  flock -w 900 9 || die "another deployment still holds ${LOCK_FILE}"

  local registry
  local old_tag
  local backup_file
  registry="$(env_value ECR_REGISTRY "$env_file")"
  old_tag="$(env_value "$TAG_KEY" "$env_file")"

  if [[ "$old_tag" == "$new_tag" ]]; then
    log "${service} already uses ${new_tag}"
    finish_deployment "$env_file" "$region" "${service}@${new_tag}"
    exit 0
  fi

  backup_file="${env_file}.before-${service}-$(date -u +%Y%m%dT%H%M%SZ)"
  cp --preserve=mode,ownership,timestamps -- "$env_file" "$backup_file"

  log "updating ${service}: ${old_tag} -> ${new_tag}"
  replace_env_value "$TAG_KEY" "$new_tag" "$env_file"

  if deploy_once "$service" "$registry" "$region"; then
    log "deployment succeeded: ${service}@${new_tag}"
    finish_deployment "$env_file" "$region" "${service}@${new_tag}"
    exit 0
  fi

  log "deployment failed; restoring ${service}@${old_tag}"
  cp --preserve=mode,ownership,timestamps -- "$backup_file" "$env_file"

  if deploy_once "$service" "$registry" "$region"; then
    die "deployment failed and rollback succeeded: ${service}@${old_tag}"
  fi

  die "deployment and rollback both failed; inspect ${CONTAINER_NAME} immediately"
}

main "$@"
