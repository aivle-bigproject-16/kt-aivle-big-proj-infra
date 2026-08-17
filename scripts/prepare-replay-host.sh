#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_EXPECTED_INSTANCE_TYPE="t3.large"
readonly DEFAULT_REGION="ap-northeast-2"
readonly DEFAULT_BUCKET="kt-aivle-big-proj-kks"
readonly DEFAULT_RUNTIME_PARAMETER="/kt-aivle-big-proj/prod/runtime-env"
readonly DEFAULT_DEPLOY_DIR="/opt/battery/infra"
readonly STATE_DIR="/var/lib/battery-replay"
readonly LOCK_FILE="/var/lock/battery-replay-prepare.lock"

log() {
  printf '[replay-prepare] %s\n' "$*"
}

die() {
  printf '[replay-prepare] ERROR: %s\n' "$*" >&2
  exit 1
}

metadata() {
  local path="$1"
  curl -fsS \
    -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
    "http://169.254.169.254/latest/meta-data/${path}"
}

validate_archive_paths() {
  local archive="$1"
  local entry

  while IFS= read -r entry; do
    [[ -n "$entry" ]] || continue
    [[ "$entry" != /* ]] || die "archive contains an absolute path: ${entry}"
    [[ ! "$entry" =~ (^|/)\.\.(/|$) ]] || die "archive contains a parent path: ${entry}"
  done < <(tar -tzf "$archive")
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
  chmod 0600 "$temp_file"
  mv -f -- "$temp_file" "$env_file"
}

main() {
  [[ "${EUID}" -eq 0 ]] || die "run as root"
  [[ $# -eq 1 ]] || die "usage: prepare-replay-host.sh deploy/infra/<sha>.tar.gz"

  local bundle_key="$1"
  local region="${AWS_REGION:-$DEFAULT_REGION}"
  local bucket="${DEPLOY_BUCKET:-$DEFAULT_BUCKET}"
  local runtime_parameter="${RUNTIME_ENV_PARAMETER:-$DEFAULT_RUNTIME_PARAMETER}"
  local deploy_dir="${DEPLOY_DIR:-$DEFAULT_DEPLOY_DIR}"
  local expected_type="${EXPECTED_INSTANCE_TYPE:-$DEFAULT_EXPECTED_INSTANCE_TYPE}"
  local env_file="${deploy_dir}/.env"

  [[ "$bundle_key" =~ ^deploy/infra/[0-9a-f]{40}\.tar\.gz$ ]] \
    || die "invalid bundle key: ${bundle_key}"
  for tool in aws curl docker flock python3 tar; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool missing: ${tool}"
  done
  docker compose version >/dev/null 2>&1 \
    || die "Docker Compose plugin is unavailable"

  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another replay preparation is already running"

  IMDS_TOKEN="$(curl -fsS -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' \
    http://169.254.169.254/latest/api/token)"
  export IMDS_TOKEN

  local instance_id
  local instance_type
  instance_id="$(metadata instance-id)"
  instance_type="$(metadata instance-type)"
  [[ "$instance_type" == "$expected_type" ]] \
    || die "refusing to prepare ${instance_id}: expected ${expected_type}, got ${instance_type}"

  local work_dir
  local archive
  local release_dir
  work_dir="$(mktemp -d /tmp/battery-replay-prepare.XXXXXX)"
  archive="${work_dir}/infra.tar.gz"
  release_dir="${work_dir}/release"
  mkdir -p "$release_dir"
  trap "rm -rf -- '$work_dir'" EXIT

  log "installing s3://${bucket}/${bundle_key}"
  aws s3 cp --only-show-errors --region "$region" \
    "s3://${bucket}/${bundle_key}" "$archive"
  validate_archive_paths "$archive"
  tar -xzf "$archive" -C "$release_dir"

  local required
  for required in compose.yaml scripts/deploy-service.sh scripts/deploy-infra.sh \
    scripts/post-deploy-benchmark.sh scripts/switch-serving-mode.sh \
    replay/Dockerfile; do
    [[ -f "${release_dir}/${required}" ]] || die "bundle missing ${required}"
  done

  install -d -o root -g root -m 0755 "$deploy_dir" "${deploy_dir}/scripts"
  install -o root -g root -m 0644 \
    "${release_dir}/compose.yaml" "${deploy_dir}/compose.yaml"
  if [[ -f "${release_dir}/compose.gpu.yaml" ]]; then
    install -o root -g root -m 0644 \
      "${release_dir}/compose.gpu.yaml" "${deploy_dir}/compose.gpu.yaml"
  fi
  cp -a -- "${release_dir}/replay" "${deploy_dir}/"
  install -o root -g root -m 0755 \
    "${release_dir}/scripts/switch-serving-mode.sh" \
    /usr/local/bin/battery-switch-serving-mode
  install -o root -g root -m 0755 \
    "${release_dir}/scripts/deploy-service.sh" \
    /usr/local/bin/battery-deploy-service
  install -o root -g root -m 0755 \
    "${release_dir}/scripts/deploy-infra.sh" \
    /usr/local/bin/battery-deploy-infra
  install -o root -g root -m 0755 \
    "${release_dir}/scripts/post-deploy-benchmark.sh" \
    /usr/local/bin/battery-post-deploy-benchmark

  log "loading encrypted runtime environment from ${runtime_parameter}"
  aws ssm get-parameter \
    --region "$region" \
    --name "$runtime_parameter" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text > "$env_file"
  chmod 0600 "$env_file"
  upsert_env COMPOSE_PROFILES app,replay "$env_file"
  for key in ECR_REGISTRY FRONTEND_TAG BACKEND_TAG BACKEND_AI_TAG \
    AI_INTERNAL_API_KEY BACKEND_CALLBACK_URL; do
    env_value "$key" "$env_file" >/dev/null || die "${key} is missing"
  done

  local registry
  registry="$(env_value ECR_REGISTRY "$env_file")"
  log "authenticating to ${registry} and pulling CPU application images"
  aws ecr get-login-password --region "$region" \
    | docker login --username AWS --password-stdin "$registry" >/dev/null

  cd "$deploy_dir"
  local -a compose=(docker compose --env-file "$env_file" -f compose.yaml)
  "${compose[@]}" config --quiet
  "${compose[@]}" pull frontend backend backend-ai
  "${compose[@]}" up -d --wait --wait-timeout 300 \
    redis backend backend-ai frontend

  DEPLOY_DIR="$deploy_dir" /usr/local/bin/battery-switch-serving-mode replay

  curl -fsS --retry 12 --retry-delay 5 --retry-connrefused \
    http://127.0.0.1/ >/dev/null
  docker inspect --format '{{.State.Health.Status}}' battery-replay \
    | grep -qx healthy
  for container in battery-frontend battery-backend battery-backend-ai battery-redis; do
    docker inspect --format '{{.State.Status}}' "$container" | grep -qx running
  done

  install -d -o root -g root -m 0755 "$STATE_DIR"
  INSTANCE_ID="$instance_id" INSTANCE_TYPE="$instance_type" \
    BUNDLE_KEY="$bundle_key" python3 > "${STATE_DIR}/prepared.json" <<'PY'
import json
import os
from datetime import datetime, timezone

print(json.dumps({
    "schemaVersion": 1,
    "instanceId": os.environ["INSTANCE_ID"],
    "instanceType": os.environ["INSTANCE_TYPE"],
    "infraBundleKey": os.environ["BUNDLE_KEY"],
    "servingMode": "replay",
    "preparedAt": datetime.now(timezone.utc).isoformat(),
}, indent=2, sort_keys=True))
PY
  chmod 0644 "${STATE_DIR}/prepared.json"
  log "preparation succeeded for ${instance_id}"
  cat "${STATE_DIR}/prepared.json"
}

main "$@"
