#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=deploy-service.sh
source "${SCRIPT_DIR}/deploy-service.sh"

assert_env_value() {
  local expected="$1"
  local key="$2"
  local env_file="$3"
  local actual

  actual="$(env_value "$key" "$env_file")"
  [[ "$actual" == "$expected" ]] || {
    printf 'expected %s=%s, got %s\n' "$key" "$expected" "$actual" >&2
    return 1
  }
}

new_env_file() {
  local path="$1"
  printf '%s\n' \
    'ECR_REGISTRY=example.invalid' \
    'FRONTEND_TAG=frontend-old' \
    'BACKEND_TAG=backend-old' \
    'BACKEND_AI_TAG=backend-ai-old' \
    'AI_INFER_TAG=ai-old' \
    'VLM_TAG=vlm-old' > "$path"
}

test_backend_stack_updates_both_tags() {
  local work_dir
  local env_file
  work_dir="$(mktemp -d)"
  env_file="${work_dir}/.env"
  new_env_file "$env_file"

  (
    deploy_backend_stack_once() { return 0; }
    finish_deployment() { return 0; }
    deploy_backend_stack "$env_file" example.invalid ap-northeast-2 release-new
  )

  assert_env_value release-new BACKEND_TAG "$env_file"
  assert_env_value release-new BACKEND_AI_TAG "$env_file"
  rm -rf -- "$work_dir"
}

test_backend_stack_rolls_back_both_tags() {
  local work_dir
  local env_file
  work_dir="$(mktemp -d)"
  env_file="${work_dir}/.env"
  new_env_file "$env_file"

  if (
    attempt=0
    deploy_backend_stack_once() {
      attempt=$((attempt + 1))
      [[ "$attempt" -gt 1 ]]
    }
    finish_deployment() { return 0; }
    deploy_backend_stack "$env_file" example.invalid ap-northeast-2 release-new
  ); then
    printf 'expected the failed deployment to report failure after rollback\n' >&2
    return 1
  fi

  assert_env_value backend-old BACKEND_TAG "$env_file"
  assert_env_value backend-ai-old BACKEND_AI_TAG "$env_file"
  rm -rf -- "$work_dir"
}

test_backend_stack_updates_both_tags
test_backend_stack_rolls_back_both_tags
printf 'deploy-service tests passed\n'
