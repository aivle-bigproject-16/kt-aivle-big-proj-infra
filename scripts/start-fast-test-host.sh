#!/usr/bin/env bash

set -Eeuo pipefail

readonly EXPECTED_INSTANCE_ID="i-0f243b999a4840674"
readonly EXPECTED_INSTANCE_TYPE="g6e.xlarge"
readonly DEFAULT_REGION="ap-northeast-2"
readonly DEFAULT_BUCKET="kt-aivle-big-proj-kks"
readonly DEFAULT_LATEST_KEY="deploy/infra/latest"
readonly DEFAULT_RUNTIME_PARAMETER="/kt-aivle-big-proj/prod/runtime-env"
readonly DEFAULT_DEPLOY_DIR="/opt/battery/infra"
readonly DEFAULT_MODEL_DIR="/opt/ai-infer/models"
readonly STATE_DIR="/var/lib/battery-fast-test"
readonly LOCK_FILE="/var/lock/battery-fast-test-start.lock"

log() {
  printf '[fast-test-start] %s\n' "$*"
}

die() {
  printf '[fast-test-start] ERROR: %s\n' "$*" >&2
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
    [[ "$entry" != /* ]] || die "archive contains absolute path: ${entry}"
    [[ ! "$entry" =~ (^|/)\.\.(/|$) ]] || die "archive contains parent path: ${entry}"
  done < <(tar -tzf "$archive")
}

replace_env_value() {
  local key="$1"
  local value="$2"
  local env_file="$3"
  local temp_file

  temp_file="$(mktemp "${env_file}.tmp.XXXXXX")"
  awk -F= -v key="$key" -v value="$value" '
    $1 == key { print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$env_file" > "$temp_file"
  chmod 0600 "$temp_file"
  mv -f -- "$temp_file" "$env_file"
}

env_value() {
  local key="$1"
  local env_file="$2"
  awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; found=1; exit } END { if (!found) exit 1 }' "$env_file"
}

install_release() {
  local release_dir="$1"
  local deploy_dir="$2"

  local required
  for required in compose.yaml compose.gpu.yaml; do
    [[ -f "${release_dir}/${required}" ]] || die "bundle missing ${required}"
  done

  install -d -o root -g root -m 0755 "$deploy_dir"
  install -o root -g root -m 0644 \
    "${release_dir}/compose.yaml" "${deploy_dir}/compose.yaml"
  install -o root -g root -m 0644 \
    "${release_dir}/compose.gpu.yaml" "${deploy_dir}/compose.gpu.yaml"

  if [[ -d "${release_dir}/scripts" ]]; then
    install -d -o root -g root -m 0755 "${deploy_dir}/scripts"
    cp -a -- "${release_dir}/scripts/." "${deploy_dir}/scripts/"
  fi
}

write_state() {
  local bundle_key="$1"
  local env_file="$2"

  install -d -o root -g root -m 0755 "$STATE_DIR"
  BUNDLE_KEY="$bundle_key" ENV_FILE="$env_file" python3 > "${STATE_DIR}/synced-release.json" <<'PY'
import json
import os
from datetime import datetime, timezone

wanted = {
    "FRONTEND_TAG",
    "BACKEND_TAG",
    "BACKEND_AI_TAG",
    "AI_INFER_TAG",
    "VLM_TAG",
}
tags = {}
with open(os.environ["ENV_FILE"], encoding="utf-8") as stream:
    for line in stream:
        key, separator, value = line.rstrip("\n").partition("=")
        if separator and key in wanted:
            tags[key] = value

missing = sorted(wanted - tags.keys())
if missing:
    raise RuntimeError(f"missing image tags: {missing}")

print(json.dumps({
    "schemaVersion": 1,
    "syncedAt": datetime.now(timezone.utc).isoformat(),
    "infraBundleKey": os.environ["BUNDLE_KEY"],
    "imageTags": tags,
}, indent=2, sort_keys=True))
PY
  chmod 0644 "${STATE_DIR}/synced-release.json"
}

main() {
  [[ "${EUID}" -eq 0 ]] || die "run as root"

  local region="${AWS_REGION:-$DEFAULT_REGION}"
  local bucket="${DEPLOY_BUCKET:-$DEFAULT_BUCKET}"
  local latest_key="${LATEST_BUNDLE_KEY:-$DEFAULT_LATEST_KEY}"
  local runtime_parameter="${RUNTIME_ENV_PARAMETER:-$DEFAULT_RUNTIME_PARAMETER}"
  local deploy_dir="${DEPLOY_DIR:-$DEFAULT_DEPLOY_DIR}"
  local model_dir="${MODEL_DIR:-$DEFAULT_MODEL_DIR}"

  for tool in aws curl docker flock python3 tar; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool missing: ${tool}"
  done
  docker compose version >/dev/null 2>&1 || die "Docker Compose plugin is unavailable"

  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another test startup is already running"

  IMDS_TOKEN="$(curl -fsS -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' \
    http://169.254.169.254/latest/api/token)"
  export IMDS_TOKEN

  local instance_id
  local instance_type
  instance_id="$(metadata instance-id)"
  instance_type="$(metadata instance-type)"
  [[ "$instance_id" == "$EXPECTED_INSTANCE_ID" ]] \
    || die "refusing to run on unexpected instance ${instance_id}"
  [[ "$instance_type" == "$EXPECTED_INSTANCE_TYPE" ]] \
    || die "expected ${EXPECTED_INSTANCE_TYPE}, got ${instance_type}"
  nvidia-smi -L | grep -q 'NVIDIA L40S' || die "NVIDIA L40S was not detected"

  local work_dir
  local pointer_file
  local archive
  local release_dir
  local next_env
  work_dir="$(mktemp -d /tmp/battery-fast-test-start.XXXXXX)"
  pointer_file="${work_dir}/latest"
  archive="${work_dir}/infra.tar.gz"
  release_dir="${work_dir}/release"
  next_env="${work_dir}/runtime.env"
  mkdir -p "$release_dir"
  trap "rm -rf -- '$work_dir'" EXIT

  log "reading latest successful production infra release"
  aws s3 cp --only-show-errors --region "$region" \
    "s3://${bucket}/${latest_key}" "$pointer_file"
  local bundle_key
  bundle_key="$(tr -d '\r\n' < "$pointer_file")"
  [[ "$bundle_key" =~ ^deploy/infra/[0-9a-f]{40}\.tar\.gz$ ]] \
    || die "invalid latest bundle pointer: ${bundle_key}"

  log "installing ${bundle_key}"
  aws s3 cp --only-show-errors --region "$region" \
    "s3://${bucket}/${bundle_key}" "$archive"
  validate_archive_paths "$archive"
  tar -xzf "$archive" -C "$release_dir"
  install_release "$release_dir" "$deploy_dir"

  log "reading latest successfully deployed production image tags"
  aws ssm get-parameter \
    --region "$region" \
    --name "$runtime_parameter" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text > "$next_env"
  chmod 0600 "$next_env"
  replace_env_value COMPOSE_PROFILES app,ai "$next_env"
  replace_env_value MODELS_DIR "$model_dir" "$next_env"
  for key in ECR_REGISTRY FRONTEND_TAG BACKEND_TAG BACKEND_AI_TAG AI_INFER_TAG VLM_TAG; do
    env_value "$key" "$next_env" >/dev/null || die "${key} is missing from runtime release"
  done

  [[ -f "${model_dir}/model-manifest.json" ]] \
    || die "prepared model bundle is missing from ${model_dir}"
  install -o root -g root -m 0600 "$next_env" "${deploy_dir}/.env"

  local registry
  registry="$(env_value ECR_REGISTRY "${deploy_dir}/.env")"
  log "pulling the accumulated image releases from ${registry}"
  aws ecr get-login-password --region "$region" \
    | docker login --username AWS --password-stdin "$registry" >/dev/null

  cd "$deploy_dir"
  COMPOSE=(docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai)
  "${COMPOSE[@]}" config --quiet
  "${COMPOSE[@]}" pull
  "${COMPOSE[@]}" up -d --wait --wait-timeout 900

  curl -fsS --retry 12 --retry-delay 5 --retry-connrefused \
    http://127.0.0.1/ >/dev/null
  docker inspect --format '{{.State.Health.Status}}' battery-ai-infer | grep -qx healthy
  docker inspect --format '{{.State.Health.Status}}' battery-vlm | grep -qx healthy
  for container in battery-frontend battery-backend battery-backend-ai battery-redis; do
    docker inspect --format '{{.State.Status}}' "$container" | grep -qx running
  done

  write_state "$bundle_key" "${deploy_dir}/.env"

  if [[ -f "${release_dir}/scripts/start-fast-test-host.sh" ]]; then
    install -o root -g root -m 0755 \
      "${release_dir}/scripts/start-fast-test-host.sh" \
      /usr/local/bin/battery-fast-test-start.next
    mv -f -- /usr/local/bin/battery-fast-test-start.next \
      /usr/local/bin/battery-fast-test-start
  fi

  log "latest accumulated release is ready"
  cat "${STATE_DIR}/synced-release.json"
}

main "$@"
