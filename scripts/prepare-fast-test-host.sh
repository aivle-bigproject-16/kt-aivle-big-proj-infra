#!/usr/bin/env bash

set -Eeuo pipefail

readonly EXPECTED_INSTANCE_TYPE="g6e.xlarge"
readonly DEFAULT_REGION="ap-northeast-2"
readonly DEFAULT_BUCKET="kt-aivle-big-proj-kks"
readonly DEFAULT_BUNDLE_KEY="deploy/infra/f1c4c8c8e5e56bb3696f33c1af9d5f856e60c9ae.tar.gz"
readonly DEFAULT_RUNTIME_PARAMETER="/kt-aivle-big-proj/prod/runtime-env"
readonly DEFAULT_DEPLOY_DIR="/opt/battery/infra"
readonly DEFAULT_MODEL_DIR="/opt/ai-infer/models"
readonly MODEL_PREFIX="models/ai-infer/onnx-20260809-01"
readonly PREPARED_DIR="/var/lib/battery-fast-test"
readonly LOCK_FILE="/var/lock/battery-fast-test-prepare.lock"

log() {
  printf '[fast-test-prepare] %s\n' "$*"
}

die() {
  printf '[fast-test-prepare] ERROR: %s\n' "$*" >&2
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
  if ! awk -F= -v key="$key" -v value="$value" '
    $1 == key { print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$env_file" > "$temp_file"; then
    rm -f -- "$temp_file"
    die "failed to set ${key}"
  fi
  chmod 0600 "$temp_file"
  mv -f -- "$temp_file" "$env_file"
}

verify_models() {
  local model_dir="$1"

  MODEL_DIR="$model_dir" python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["MODEL_DIR"]).resolve()
manifest = json.loads((root / "model-manifest.json").read_text(encoding="utf-8"))

for artifact in manifest["artifacts"]:
    relative = Path(artifact["localPath"])
    if relative.parts[0] == "models":
        relative = Path(*relative.parts[1:])
    path = (root / relative).resolve()
    if root not in path.parents:
        raise RuntimeError(f"artifact escaped model directory: {relative}")
    if path.stat().st_size != artifact["sizeBytes"]:
        raise RuntimeError(f"size mismatch: {relative}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != artifact["sha256"]:
        raise RuntimeError(f"sha256 mismatch: {relative}")

print(f"verified {len(manifest['artifacts'])} model artifacts")
PY
}

install_startup_service() {
  local deploy_dir="$1"

  [[ -f "${deploy_dir}/scripts/start-fast-test-host.sh" ]] \
    || die "missing ${deploy_dir}/scripts/start-fast-test-host.sh"
  install -o root -g root -m 0755 \
    "${deploy_dir}/scripts/start-fast-test-host.sh" \
    /usr/local/bin/battery-fast-test-start

  cat > /usr/local/bin/battery-fast-test-compose <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
cd "${deploy_dir}"
exec docker compose \\
  --env-file .env \\
  -f compose.yaml \\
  -f compose.gpu.yaml \\
  --profile app \\
  --profile ai \\
  "\$@"
EOF
  chmod 0755 /usr/local/bin/battery-fast-test-compose

  cat > /etc/systemd/system/battery-fast-test.service <<'EOF'
[Unit]
Description=Battery high-performance GPU test stack
Requires=docker.service
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/battery-fast-test-start
ExecStop=/usr/local/bin/battery-fast-test-compose stop -t 60
TimeoutStartSec=1800
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable battery-fast-test.service
}

main() {
  [[ "${EUID}" -eq 0 ]] || die "run as root"

  local region="${AWS_REGION:-$DEFAULT_REGION}"
  local bucket="${DEPLOY_BUCKET:-$DEFAULT_BUCKET}"
  local bundle_key="${DEPLOY_BUNDLE_KEY:-$DEFAULT_BUNDLE_KEY}"
  local runtime_parameter="${RUNTIME_ENV_PARAMETER:-$DEFAULT_RUNTIME_PARAMETER}"
  local deploy_dir="${DEPLOY_DIR:-$DEFAULT_DEPLOY_DIR}"
  local model_dir="${MODEL_DIR:-$DEFAULT_MODEL_DIR}"

  for tool in aws curl docker flock python3 sha256sum tar; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool missing: ${tool}"
  done
  docker compose version >/dev/null 2>&1 || die "Docker Compose plugin is unavailable"

  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another preparation is already running"

  IMDS_TOKEN="$(curl -fsS -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 21600' \
    http://169.254.169.254/latest/api/token)"
  export IMDS_TOKEN

  local instance_id
  local instance_type
  instance_id="$(metadata instance-id)"
  instance_type="$(metadata instance-type)"
  [[ "$instance_type" == "$EXPECTED_INSTANCE_TYPE" ]] \
    || die "refusing to prepare ${instance_id}: expected ${EXPECTED_INSTANCE_TYPE}, got ${instance_type}"

  nvidia-smi -L | grep -q 'NVIDIA L40S' \
    || die "NVIDIA L40S was not detected"

  local work_dir
  local archive
  local release_dir
  work_dir="$(mktemp -d /tmp/battery-fast-test.XXXXXX)"
  archive="${work_dir}/infra.tar.gz"
  release_dir="${work_dir}/release"
  mkdir -p "$release_dir"
  trap "rm -rf -- '$work_dir'" EXIT

  log "installing infra bundle s3://${bucket}/${bundle_key}"
  aws s3 cp --only-show-errors --region "$region" \
    "s3://${bucket}/${bundle_key}" "$archive"
  validate_archive_paths "$archive"
  tar -xzf "$archive" -C "$release_dir"

  local required
  for required in compose.yaml compose.gpu.yaml; do
    [[ -f "${release_dir}/${required}" ]] || die "bundle missing ${required}"
  done

  install -d -o root -g root -m 0755 "$deploy_dir"
  install -o root -g root -m 0644 "${release_dir}/compose.yaml" "${deploy_dir}/compose.yaml"
  install -o root -g root -m 0644 "${release_dir}/compose.gpu.yaml" "${deploy_dir}/compose.gpu.yaml"

  if [[ -d "${release_dir}/scripts" ]]; then
    install -d -o root -g root -m 0755 "${deploy_dir}/scripts"
    cp -a -- "${release_dir}/scripts/." "${deploy_dir}/scripts/"
  fi

  log "loading encrypted runtime environment from ${runtime_parameter}"
  aws ssm get-parameter \
    --region "$region" \
    --name "$runtime_parameter" \
    --with-decryption \
    --query 'Parameter.Value' \
    --output text > "${deploy_dir}/.env"
  chmod 0600 "${deploy_dir}/.env"
  replace_env_value COMPOSE_PROFILES app,ai "${deploy_dir}/.env"
  replace_env_value MODELS_DIR "$model_dir" "${deploy_dir}/.env"

  log "downloading and verifying model bundle"
  install -d -o root -g root -m 0755 "$model_dir"
  aws s3 cp --only-show-errors --recursive --region "$region" \
    --exclude 'fixtures/*' \
    "s3://${bucket}/${MODEL_PREFIX}/" "${model_dir}/"
  if [[ -f "${model_dir}/defect_ct.onnx" ]]; then
    mv -f -- "${model_dir}/defect_ct.onnx" "${model_dir}/defect_ct.torch211.onnx"
  fi
  verify_models "$model_dir"

  local registry
  registry="$(awk -F= '$1=="ECR_REGISTRY"{sub(/^[^=]*=/, ""); print; exit}' "${deploy_dir}/.env")"
  [[ -n "$registry" ]] || die "ECR_REGISTRY is missing"
  log "authenticating to ${registry} and caching all service images"
  aws ecr get-login-password --region "$region" \
    | docker login --username AWS --password-stdin "$registry" >/dev/null

  cd "$deploy_dir"
  docker compose \
    --env-file .env \
    -f compose.yaml \
    -f compose.gpu.yaml \
    --profile app \
    --profile ai \
    config --quiet
  docker compose \
    --env-file .env \
    -f compose.yaml \
    -f compose.gpu.yaml \
    --profile app \
    --profile ai \
    pull

  install_startup_service "$deploy_dir"

  log "starting services and warming model caches"
  systemctl start battery-fast-test.service

  curl -fsS --retry 12 --retry-delay 5 --retry-connrefused \
    http://127.0.0.1/ >/dev/null
  docker inspect --format '{{.State.Health.Status}}' battery-ai-infer | grep -qx healthy
  docker inspect --format '{{.State.Health.Status}}' battery-vlm | grep -qx healthy
  for container in battery-frontend battery-backend battery-backend-ai battery-redis; do
    docker inspect --format '{{.State.Status}}' "$container" | grep -qx running
  done
  sleep 5
  for container in battery-frontend battery-backend battery-backend-ai battery-redis; do
    docker inspect --format '{{.State.Status}}' "$container" | grep -qx running
  done

  install -d -o root -g root -m 0755 "$PREPARED_DIR"
  python3 - "$instance_id" "$instance_type" > "${PREPARED_DIR}/prepared.json" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone

containers = subprocess.check_output(
    ["docker", "ps", "--format", "{{.Names}}"], text=True
).splitlines()
print(json.dumps({
    "schemaVersion": 1,
    "instanceId": sys.argv[1],
    "instanceType": sys.argv[2],
    "preparedAt": datetime.now(timezone.utc).isoformat(),
    "containers": sorted(containers),
}, indent=2))
PY
  chmod 0644 "${PREPARED_DIR}/prepared.json"

  log "preparation succeeded for ${instance_id}"
  cat "${PREPARED_DIR}/prepared.json"
}

main "$@"
