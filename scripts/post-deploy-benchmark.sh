#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_BUCKET="kt-aivle-big-proj-kks"
readonly DEFAULT_REGION="ap-northeast-2"
readonly DEFAULT_FIXTURE_PREFIX="models/ai-infer/onnx-20260809-01/fixtures/benchmark-v1"
readonly DEFAULT_RESULT_PREFIX="deploy/benchmarks"
readonly DEFAULT_RESULT_DIR="/var/lib/battery/benchmarks"

log() {
  printf '[benchmark] %s\n' "$*"
}

die() {
  printf '[benchmark] ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  post-deploy-benchmark.sh --check
  post-deploy-benchmark.sh --trigger NAME

The benchmark performs one unmeasured warm-up, then measures:
  - CT inference:  20 fixed images
  - RGB inference: 20 fixed images
  - VLM report:     3 fixed individual-report requests
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is not installed"
}

container_ready() {
  local container="$1"
  local state
  local health

  state="$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null)" || return 1
  [[ "$state" == "running" ]] || return 1
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")"
  [[ "$health" == "none" || "$health" == "healthy" ]]
}

check_prerequisites() {
  require_command aws
  require_command base64
  require_command curl
  require_command docker
  require_command jq
  require_command nvidia-smi
  container_ready battery-ai-infer || die "battery-ai-infer is not ready"
  container_ready battery-vlm || die "battery-vlm is not ready"
  nvidia-smi -L >/dev/null 2>&1 || die "NVIDIA GPU is not ready"
}

list_fixture_keys() {
  local modality="$1"
  local bucket="$2"
  local prefix="$3"
  local region="$4"

  aws s3api list-objects-v2 \
    --bucket "$bucket" \
    --prefix "${prefix}/${modality}/" \
    --region "$region" \
    --query 'Contents[].Key' \
    --output text \
    | tr '\t' '\n' \
    | sed '/^None$/d; /^$/d' \
    | sort
}

build_infer_input() {
  local bucket="$1"
  local prefix="$2"
  local region="$3"
  local modality
  local key
  local url
  local payload='{"ct":[],"rgb":[]}'
  local -a keys=()

  for modality in ct rgb; do
    mapfile -t keys < <(list_fixture_keys "$modality" "$bucket" "$prefix" "$region")
    [[ ${#keys[@]} -eq 20 ]] || die "expected 20 ${modality^^} fixtures, found ${#keys[@]}"

    for key in "${keys[@]}"; do
      url="$(aws s3 presign "s3://${bucket}/${key}" --region "$region" --expires-in 3600)"
      payload="$(jq -c \
        --arg modality "$modality" \
        --arg key "$key" \
        --arg url "$url" \
        '.[$modality] += [{key: $key, url: $url}]' \
        <<<"$payload")"
    done
  done

  printf '%s' "$payload"
}

run_infer_benchmark() {
  local input_json="$1"
  local input_b64
  input_b64="$(printf '%s' "$input_json" | base64 -w 0)"

  docker exec -i -e BENCH_INPUT_B64="$input_b64" battery-ai-infer python - <<'PY'
import base64
import json
import math
import os
import time
import urllib.error
import urllib.request

fixtures = json.loads(base64.b64decode(os.environ["BENCH_INPUT_B64"]))


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def request(modality, item, inspection_id):
    body = json.dumps({
        "inspection_id": inspection_id,
        "image_key": item["key"],
        "image_url": item["url"],
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:8000/infer/{modality}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as response:
        payload = json.load(response)
    wall_ms = round((time.perf_counter() - started) * 1000)
    if payload.get("label") not in {"PASS", "REJECT", "FAIL"}:
        raise RuntimeError(f"unexpected inference response: {payload}")
    return int(payload["latency_ms"]), wall_ms


result = {}
for modality in ("ct", "rgb"):
    items = fixtures[modality]
    request(modality, items[0], 900000)
    service_values = []
    wall_values = []
    failures = []
    for index, item in enumerate(items, start=1):
        try:
            service_ms, wall_ms = request(modality, item, 900000 + index)
            service_values.append(service_ms)
            wall_values.append(wall_ms)
        except Exception as exc:
            failures.append({"key": item["key"], "error": str(exc)[:300]})

    summary = {
        "requested": len(items),
        "succeeded": len(service_values),
        "failed": len(failures),
        "failures": failures,
    }
    if service_values:
        summary.update({
            "avg_ms": round(sum(service_values) / len(service_values), 2),
            "p50_ms": percentile(service_values, 0.50),
            "p95_ms": percentile(service_values, 0.95),
            "min_ms": min(service_values),
            "max_ms": max(service_values),
            "wall_avg_ms": round(sum(wall_values) / len(wall_values), 2),
        })
    result[modality] = summary

print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
if any(result[name]["failed"] for name in result):
    raise SystemExit(1)
PY
}

run_vlm_benchmark() {
  docker exec -i battery-vlm python - <<'PY'
import json
import math
import time
import urllib.request

payload = {
    "cellSerialNo": "QA-BENCHMARK-CELL",
    "inspectionId": 900000,
    "totalImages": 40,
    "cellSize": None,
    "pointGroups": [],
    "ctVoidRatio": 0.014,
    "rgbDefectRate": 0.025,
    "defectInfo": [
        {"imageType": "CT", "defectType": ["MICRO_DEFECT"]},
        {"imageType": "RGB", "defectType": ["CRACK", "SPOT"]},
    ],
}


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def generate():
    req = urllib.request.Request(
        "http://127.0.0.1:8001/vlm/reports/individual",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as response:
        body = json.load(response)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    if body.get("status") != "COMPLETED":
        raise RuntimeError(body.get("failureReason") or f"unexpected response: {body}")
    return elapsed_ms


generate()
values = []
failures = []
for index in range(1, 4):
    try:
        values.append(generate())
    except Exception as exc:
        failures.append({"iteration": index, "error": str(exc)[:300]})

result = {
    "requested": 3,
    "succeeded": len(values),
    "failed": len(failures),
    "failures": failures,
}
if values:
    result.update({
        "avg_ms": round(sum(values) / len(values), 2),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "min_ms": min(values),
        "max_ms": max(values),
    })

print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
if failures:
    raise SystemExit(1)
PY
}

instance_id() {
  local token
  token="$(curl -fsS --max-time 2 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)" || return 1
  curl -fsS --max-time 2 \
    -H "X-aws-ec2-metadata-token: ${token}" \
    http://169.254.169.254/latest/meta-data/instance-id
}

main() {
  if [[ "${1:-}" == "--check" ]]; then
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    check_prerequisites
    log "benchmark prerequisites are valid"
    exit 0
  fi

  [[ $# -eq 2 && "$1" == "--trigger" ]] || { usage >&2; exit 2; }
  local trigger="$2"
  [[ "$trigger" =~ ^[A-Za-z0-9_.@/-]{1,180}$ ]] || die "invalid trigger: ${trigger}"

  check_prerequisites

  local bucket="${BENCHMARK_BUCKET:-$DEFAULT_BUCKET}"
  local region="${AWS_REGION:-$DEFAULT_REGION}"
  local fixture_prefix="${BENCHMARK_FIXTURE_PREFIX:-$DEFAULT_FIXTURE_PREFIX}"
  local result_prefix="${BENCHMARK_RESULT_PREFIX:-$DEFAULT_RESULT_PREFIX}"
  local result_dir="${BENCHMARK_RESULT_DIR:-$DEFAULT_RESULT_DIR}"
  local timestamp
  local node_id
  local work_dir
  local gpu_log
  local sampler_pid=''
  local infer_json='{}'
  local vlm_json='{}'
  local infer_status=0
  local vlm_status=0

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  node_id="$(instance_id)" || die "failed to resolve EC2 instance id"
  work_dir="$(mktemp -d /tmp/battery-benchmark.XXXXXX)"
  gpu_log="${work_dir}/gpu.csv"
  mkdir -p "$result_dir"

  cleanup() {
    if [[ -n "$sampler_pid" ]]; then
      kill "$sampler_pid" >/dev/null 2>&1 || true
      wait "$sampler_pid" >/dev/null 2>&1 || true
    fi
    rm -rf -- "$work_dir"
  }
  trap cleanup EXIT

  log "starting benchmark for ${trigger} on ${node_id}"
  nvidia-smi \
    --query-gpu=timestamp,name,driver_version,memory.used,memory.total,utilization.gpu,power.draw \
    --format=csv,noheader,nounits \
    -lms 500 >"$gpu_log" 2>/dev/null &
  sampler_pid=$!

  local infer_input
  infer_input="$(build_infer_input "$bucket" "$fixture_prefix" "$region")"
  set +e
  infer_json="$(run_infer_benchmark "$infer_input")"
  infer_status=$?
  vlm_json="$(run_vlm_benchmark)"
  vlm_status=$?
  set -e
  [[ -n "$infer_json" ]] || infer_json='{}'
  [[ -n "$vlm_json" ]] || vlm_json='{}'

  kill "$sampler_pid" >/dev/null 2>&1 || true
  wait "$sampler_pid" >/dev/null 2>&1 || true
  sampler_pid=''

  local gpu_json
  gpu_json="$(awk -F',' '
    function trim(v) { gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); return v }
    NR == 1 { name=trim($2); driver=trim($3); total=trim($5) + 0 }
    { memory=trim($4) + 0; util=trim($6) + 0; power=trim($7) + 0
      if (memory > max_memory) max_memory=memory
      if (util > max_util) max_util=util
      if (power > max_power) max_power=power }
    END { printf "{\"name\":\"%s\",\"driver\":\"%s\",\"memory_total_mib\":%d,\"memory_peak_mib\":%d,\"utilization_peak_pct\":%d,\"power_peak_w\":%.2f}", name, driver, total, max_memory, max_util, max_power }
  ' "$gpu_log")"

  local result_file="${result_dir}/${timestamp}-${node_id}.json"
  jq -n \
    --arg schema_version "1" \
    --arg measured_at "$timestamp" \
    --arg instance_id "$node_id" \
    --arg trigger "$trigger" \
    --arg fixture_prefix "s3://${bucket}/${fixture_prefix}" \
    --argjson inference "$infer_json" \
    --argjson vlm "$vlm_json" \
    --argjson gpu "$gpu_json" \
    --argjson inference_exit "$infer_status" \
    --argjson vlm_exit "$vlm_status" \
    '{
      schema_version: ($schema_version | tonumber),
      measured_at: $measured_at,
      instance_id: $instance_id,
      trigger: $trigger,
      fixture_prefix: $fixture_prefix,
      inference: $inference,
      vlm: $vlm,
      gpu: $gpu,
      exit_codes: {inference: $inference_exit, vlm: $vlm_exit}
    }' >"$result_file"

  cp -f -- "$result_file" "${result_dir}/latest.json"
  local result_key="${result_prefix}/${node_id}/${timestamp}.json"
  aws s3 cp --only-show-errors --region "$region" "$result_file" "s3://${bucket}/${result_key}"

  log "result: s3://${bucket}/${result_key}"
  jq -c . "$result_file"

  local final_status=0
  [[ "$infer_status" -eq 0 && "$vlm_status" -eq 0 ]] || final_status=1
  trap - EXIT
  cleanup
  return "$final_status"
}

main "$@"
