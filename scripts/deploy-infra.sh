#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_DEPLOY_DIR="/opt/battery/infra"
readonly DEFAULT_BUCKET="kt-aivle-big-proj-kks"
readonly DEFAULT_AWS_REGION="ap-northeast-2"
readonly LOCK_FILE="/var/lock/battery-deploy.lock"

log() {
  printf '[infra-deploy] %s\n' "$*"
}

die() {
  printf '[infra-deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

build_compose_command() {
  local deploy_dir="$1"
  local env_file="$2"
  local base_file="$3"
  local gpu_file="$4"

  COMPOSE=(docker compose --env-file "$env_file" -f "$base_file")

  if [[ -f "$gpu_file" ]] \
    && command -v nvidia-smi >/dev/null 2>&1 \
    && nvidia-smi -L >/dev/null 2>&1; then
    COMPOSE+=(-f "$gpu_file")
  elif [[ -f "$deploy_dir/compose.cpu.yaml" ]]; then
    COMPOSE+=(-f "$deploy_dir/compose.cpu.yaml")
  fi
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

main() {
  [[ $# -eq 1 ]] || die "usage: deploy-infra.sh deploy/infra/<git-sha>.tar.gz"

  local bundle_key="$1"
  local deploy_dir="${DEPLOY_DIR:-$DEFAULT_DEPLOY_DIR}"
  local bucket="${DEPLOY_BUCKET:-$DEFAULT_BUCKET}"
  local region="${AWS_REGION:-$DEFAULT_AWS_REGION}"
  local env_file="${deploy_dir}/.env"

  [[ "$bundle_key" =~ ^deploy/infra/[0-9a-f]{40}\.tar\.gz$ ]] || die "invalid bundle key: ${bundle_key}"
  [[ -f "$env_file" ]] || die "missing ${env_file}"
  command -v aws >/dev/null 2>&1 || die "aws CLI is not installed"
  command -v docker >/dev/null 2>&1 || die "docker is not installed"
  command -v flock >/dev/null 2>&1 || die "flock is not installed"
  command -v tar >/dev/null 2>&1 || die "tar is not installed"

  exec 9>"$LOCK_FILE"
  flock -w 900 9 || die "another deployment still holds ${LOCK_FILE}"

  local work_dir
  local archive
  local release_dir
  local backup_dir
  work_dir="$(mktemp -d /tmp/battery-infra-deploy.XXXXXX)"
  archive="${work_dir}/infra.tar.gz"
  release_dir="${work_dir}/release"
  backup_dir="${work_dir}/backup"
  mkdir -p "$release_dir" "$backup_dir"
  trap 'rm -rf -- "$work_dir"' EXIT

  log "downloading s3://${bucket}/${bundle_key}"
  aws s3 cp --only-show-errors --region "$region" "s3://${bucket}/${bundle_key}" "$archive"
  validate_archive_paths "$archive"
  tar -xzf "$archive" -C "$release_dir"

  local required_file
  for required_file in compose.yaml compose.gpu.yaml scripts/deploy-service.sh scripts/deploy-infra.sh scripts/post-deploy-benchmark.sh; do
    [[ -f "$release_dir/$required_file" ]] || die "bundle is missing ${required_file}"
  done

  bash -n "$release_dir/scripts/deploy-service.sh"
  bash -n "$release_dir/scripts/deploy-infra.sh"
  bash -n "$release_dir/scripts/post-deploy-benchmark.sh"

  build_compose_command \
    "$deploy_dir" \
    "$env_file" \
    "$release_dir/compose.yaml" \
    "$release_dir/compose.gpu.yaml"
  "${COMPOSE[@]}" config --quiet

  local -a running_services=()
  build_compose_command \
    "$deploy_dir" \
    "$env_file" \
    "$deploy_dir/compose.yaml" \
    "$deploy_dir/compose.gpu.yaml"
  mapfile -t running_services < <("${COMPOSE[@]}" ps --services --status running)
  [[ ${#running_services[@]} -gt 0 ]] || die "no running Compose services were detected"

  cp -a -- "$deploy_dir/compose.yaml" "$backup_dir/compose.yaml"
  cp -a -- "$deploy_dir/compose.gpu.yaml" "$backup_dir/compose.gpu.yaml"
  cp -a -- /usr/local/bin/battery-deploy-service "$backup_dir/battery-deploy-service"
  if [[ -f /usr/local/bin/battery-post-deploy-benchmark ]]; then
    cp -a -- /usr/local/bin/battery-post-deploy-benchmark "$backup_dir/battery-post-deploy-benchmark"
  fi

  install -o root -g root -m 0644 "$release_dir/compose.yaml" "$deploy_dir/compose.yaml"
  install -o root -g root -m 0644 "$release_dir/compose.gpu.yaml" "$deploy_dir/compose.gpu.yaml"
  install -o root -g root -m 0755 "$release_dir/scripts/deploy-service.sh" /usr/local/bin/battery-deploy-service
  install -o root -g root -m 0755 "$release_dir/scripts/deploy-infra.sh" /usr/local/bin/battery-deploy-infra
  install -o root -g root -m 0755 "$release_dir/scripts/post-deploy-benchmark.sh" /usr/local/bin/battery-post-deploy-benchmark

  if [[ -d "$release_dir/stub" ]]; then
    mkdir -p "$deploy_dir/stub"
    cp -a -- "$release_dir/stub/." "$deploy_dir/stub/"
  fi

  build_compose_command \
    "$deploy_dir" \
    "$env_file" \
    "$deploy_dir/compose.yaml" \
    "$deploy_dir/compose.gpu.yaml"

  log "reconciling: ${running_services[*]}"
  if "${COMPOSE[@]}" up -d --no-deps --wait --wait-timeout 900 "${running_services[@]}"; then
    log "infra deployment succeeded: ${bundle_key}"
    if /usr/local/bin/battery-post-deploy-benchmark --trigger "infra@${bundle_key#deploy/infra/}"; then
      log "post-deploy benchmark succeeded"
    else
      log "WARNING: post-deploy benchmark failed; deployment remains active"
    fi
    exit 0
  fi

  log "infra deployment failed; restoring previous Compose files"
  install -o root -g root -m 0644 "$backup_dir/compose.yaml" "$deploy_dir/compose.yaml"
  install -o root -g root -m 0644 "$backup_dir/compose.gpu.yaml" "$deploy_dir/compose.gpu.yaml"
  install -o root -g root -m 0755 "$backup_dir/battery-deploy-service" /usr/local/bin/battery-deploy-service
  if [[ -f "$backup_dir/battery-post-deploy-benchmark" ]]; then
    install -o root -g root -m 0755 "$backup_dir/battery-post-deploy-benchmark" /usr/local/bin/battery-post-deploy-benchmark
  fi

  build_compose_command \
    "$deploy_dir" \
    "$env_file" \
    "$deploy_dir/compose.yaml" \
    "$deploy_dir/compose.gpu.yaml"

  if "${COMPOSE[@]}" up -d --no-deps --wait --wait-timeout 900 "${running_services[@]}"; then
    die "infra deployment failed and rollback succeeded"
  fi

  die "infra deployment and rollback both failed; inspect the Compose project immediately"
}

main "$@"
