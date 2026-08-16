#!/usr/bin/env bash

set -Eeuo pipefail

readonly DEFAULT_BUCKET="kt-aivle-big-proj-kks"
readonly DEFAULT_REGION="ap-northeast-2"
readonly DEFAULT_FIXTURE_PREFIX="models/ai-infer/onnx-20260809-01/fixtures/benchmark-v1"
readonly DEFAULT_RESULT_PREFIX="deploy/benchmarks"
readonly DEFAULT_RESULT_DIR="/var/lib/battery/benchmarks"
readonly INFER_SAMPLE_COUNT=3
readonly VLM_SAMPLE_COUNT=2

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
  post-deploy-benchmark.sh --trigger NAME [--suite all|inference|vlm]

Suites:
  inference  3 fixed CT requests and 3 fixed RGB requests
  vlm        2 daily reports and 2 individual reports
  all        inference followed by VLM (default)

There is no extra warm-up request. The first request is retained separately in
the result. Measurements are observational and never trigger a rollback.
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
  local suite="$1"

  require_command aws
  require_command curl
  require_command docker
  require_command jq
  require_command nvidia-smi

  if [[ "$suite" == "all" || "$suite" == "inference" ]]; then
    require_command base64
    container_ready battery-ai-infer || die "battery-ai-infer is not ready"
  fi
  if [[ "$suite" == "all" || "$suite" == "vlm" ]]; then
    container_ready battery-vlm || die "battery-vlm is not ready"
  fi
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
  local etag
  local url
  local payload='{"ct":[],"rgb":[]}'
  local -a all_keys=()
  local -a keys=()

  for modality in ct rgb; do
    mapfile -t all_keys < <(list_fixture_keys "$modality" "$bucket" "$prefix" "$region")
    [[ ${#all_keys[@]} -ge $INFER_SAMPLE_COUNT ]] \
      || die "expected at least ${INFER_SAMPLE_COUNT} ${modality^^} fixtures, found ${#all_keys[@]}"
    keys=("${all_keys[@]:0:$INFER_SAMPLE_COUNT}")

    for key in "${keys[@]}"; do
      etag="$(aws s3api head-object \
        --bucket "$bucket" \
        --key "$key" \
        --region "$region" \
        --query ETag \
        --output text | tr -d '"')"
      url="$(aws s3 presign "s3://${bucket}/${key}" --region "$region" --expires-in 3600)"
      payload="$(jq -c \
        --arg modality "$modality" \
        --arg key "$key" \
        --arg etag "$etag" \
        --arg url "$url" \
        '.[$modality] += [{key: $key, etag: $etag, url: $url}]' \
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
import os
import statistics
import time
import urllib.request

fixtures = json.loads(base64.b64decode(os.environ["BENCH_INPUT_B64"]))


def summarize(values):
    if not values:
        return None
    subsequent = values[1:]
    return {
        "samples": values,
        "count": len(values),
        "avg": round(sum(values) / len(values), 2),
        "median": round(statistics.median(values), 2),
        "min": min(values),
        "max": max(values),
        "first_request": values[0],
        "subsequent_avg": (
            round(sum(subsequent) / len(subsequent), 2)
            if subsequent else None
        ),
    }


def parse_server_timing(value):
    timings = {}
    for item in (value or "").split(","):
        name, separator, duration = item.strip().partition(";dur=")
        if separator:
            timings[f"{name}_ms"] = round(float(duration), 2)
    return timings


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
        server_timing = response.headers.get("Server-Timing")
    wall_ms = round((time.perf_counter() - started) * 1000)
    if payload.get("label") not in {"PASS", "REJECT", "FAIL"}:
        raise RuntimeError(f"unexpected inference response: {payload}")
    timings = parse_server_timing(server_timing)
    timings.setdefault("total_ms", int(payload["latency_ms"]))
    return {
        "key": item["key"],
        "etag": item["etag"],
        "label": payload["label"],
        "defect_count": len(payload.get("defects", [])),
        "wall_ms": wall_ms,
        **timings,
    }


result = {}
for modality in ("ct", "rgb"):
    samples = []
    failures = []
    for index, item in enumerate(fixtures[modality], start=1):
        try:
            samples.append(request(modality, item, 900000 + index))
        except Exception as exc:
            failures.append({"key": item["key"], "error": str(exc)[:300]})

    metrics = {}
    for metric in (
        "download_ms",
        "quality_ms",
        "defect_ms",
        "pipeline_ms",
        "total_ms",
        "wall_ms",
    ):
        summary = summarize([
            sample[metric] for sample in samples if metric in sample
        ])
        if summary is not None:
            metrics[metric] = summary

    result[modality] = {
        "requested": len(fixtures[modality]),
        "succeeded": len(samples),
        "failed": len(failures),
        "failures": failures,
        "samples": samples,
        "metrics": metrics,
        "defect_stage_samples": sum("defect_ms" in item for item in samples),
    }

print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
if any(result[name]["failed"] for name in result):
    raise SystemExit(1)
PY
}

run_vlm_benchmark() {
  docker exec -i battery-vlm python - <<'PY'
import json
import statistics
import time
import urllib.request

payloads = {
    "daily": {
        "daily_data": {
            "reportDate": "2026-08-14",
            "summaryData": {
                "totalCount": 120,
                "passCount": 104,
                "rejectCount": 14,
                "failedCount": 2,
                "prevTotalCount": 118,
                "prevRejectCount": 12,
                "defects": [
                    {"defectType": "MICRO_DEFECT", "count": 5},
                    {"defectType": "CRACK", "count": 2},
                ],
            },
        },
    },
    "individual": {
        "cellSerialNo": "QA-BENCHMARK-CELL",
        "inspectionId": 900000,
        "totalImages": 6,
        "cellSize": None,
        "pointGroups": [],
        "ctVoidRatio": 0.014,
        "rgbDefectRate": 0.025,
        "defectInfo": [
            {"imageType": "CT", "defectType": ["MICRO_DEFECT"]},
            {"imageType": "RGB", "defectType": ["CRACK", "SPOT"]},
        ],
    },
}


def summarize(values):
    if not values:
        return None
    subsequent = values[1:]
    return {
        "samples": values,
        "count": len(values),
        "avg": round(sum(values) / len(values), 2),
        "median": round(statistics.median(values), 2),
        "min": min(values),
        "max": max(values),
        "first_request": values[0],
        "subsequent_avg": (
            round(sum(subsequent) / len(subsequent), 2)
            if subsequent else None
        ),
    }


def generate(kind, iteration):
    req = urllib.request.Request(
        f"http://127.0.0.1:8001/vlm/reports/{kind}",
        data=json.dumps(payloads[kind]).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as response:
        body = json.load(response)
        timing_header = response.headers.get("X-VLM-Timings")
    wall_ms = round((time.perf_counter() - started) * 1000)
    if body.get("status") != "COMPLETED":
        raise RuntimeError(body.get("failureReason") or f"unexpected response: {body}")
    timing = json.loads(timing_header) if timing_header else {}
    return {
        "iteration": iteration,
        "wall_ms": wall_ms,
        "e2e_ms": timing.get("total_ms", wall_ms),
        "retry_count": timing.get("retry_count"),
        "calls": timing.get("calls", []),
    }


result = {}
for kind in ("daily", "individual"):
    samples = []
    failures = []
    for iteration in range(1, 3):
        try:
            samples.append(generate(kind, iteration))
        except Exception as exc:
            failures.append({"iteration": iteration, "error": str(exc)[:300]})

    operations = {}
    operation_names = sorted({
        call["operation"]
        for sample in samples
        for call in sample["calls"]
    })
    for operation in operation_names:
        calls = [
            call
            for sample in samples
            for call in sample["calls"]
            if call["operation"] == operation
        ]
        operations[operation] = {
            metric: summary
            for metric in (
                "preprocess_ms",
                "generate_ms",
                "decode_ms",
                "total_ms",
                "input_tokens",
                "output_tokens",
            )
            if (summary := summarize([
                call[metric] for call in calls if metric in call
            ])) is not None
        }

    result[kind] = {
        "requested": 2,
        "succeeded": len(samples),
        "failed": len(failures),
        "failures": failures,
        "samples": samples,
        "e2e_ms": summarize([sample["e2e_ms"] for sample in samples]),
        "wall_ms": summarize([sample["wall_ms"] for sample in samples]),
        "operations": operations,
    }

print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
if any(result[name]["failed"] for name in result):
    raise SystemExit(1)
PY
}

metadata_value() {
  local path="$1"
  local token="$2"
  curl -fsS --max-time 2 \
    -H "X-aws-ec2-metadata-token: ${token}" \
    "http://169.254.169.254/latest/meta-data/${path}"
}

container_metadata() {
  local container="$1"
  local setting_pattern="$2"

  docker inspect "$container" | jq -c --arg pattern "$setting_pattern" '
    .[0] | {
      image_ref: .Config.Image,
      image_id: .Image,
      settings: (([
        .Config.Env[]?
        | select(test($pattern))
        | capture("^(?<key>[^=]+)=(?<value>.*)$")
        | {(.key): .value}
      ] | add) // {})
    }
  '
}

main() {
  if [[ "${1:-}" == "--check" ]]; then
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    check_prerequisites all
    log "benchmark prerequisites are valid"
    exit 0
  fi

  [[ $# -eq 2 || $# -eq 4 ]] || { usage >&2; exit 2; }
  [[ "$1" == "--trigger" ]] || { usage >&2; exit 2; }
  local trigger="$2"
  local suite="all"
  if [[ $# -eq 4 ]]; then
    [[ "$3" == "--suite" ]] || { usage >&2; exit 2; }
    suite="$4"
  fi
  [[ "$suite" == "all" || "$suite" == "inference" || "$suite" == "vlm" ]] \
    || die "invalid suite: ${suite}"
  [[ "$trigger" =~ ^[A-Za-z0-9_.@/-]{1,180}$ ]] || die "invalid trigger: ${trigger}"

  check_prerequisites "$suite"

  local bucket="${BENCHMARK_BUCKET:-$DEFAULT_BUCKET}"
  local region="${AWS_REGION:-$DEFAULT_REGION}"
  local fixture_prefix="${BENCHMARK_FIXTURE_PREFIX:-$DEFAULT_FIXTURE_PREFIX}"
  local result_prefix="${BENCHMARK_RESULT_PREFIX:-$DEFAULT_RESULT_PREFIX}"
  local result_dir="${BENCHMARK_RESULT_DIR:-$DEFAULT_RESULT_DIR}"
  local timestamp
  local imds_token
  local node_id
  local instance_type
  local work_dir
  local gpu_log
  local sampler_pid=''
  local infer_json='null'
  local vlm_json='null'
  local infer_status=0
  local vlm_status=0
  local ai_metadata='null'
  local vlm_metadata='null'

  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  imds_token="$(curl -fsS --max-time 2 -X PUT \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)" \
    || die "failed to obtain EC2 metadata token"
  node_id="$(metadata_value instance-id "$imds_token")" \
    || die "failed to resolve EC2 instance id"
  instance_type="$(metadata_value instance-type "$imds_token")" \
    || die "failed to resolve EC2 instance type"
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

  log "starting ${suite} benchmark for ${trigger} on ${node_id}"
  nvidia-smi \
    --query-gpu=timestamp,name,driver_version,memory.used,memory.total,utilization.gpu,power.draw \
    --format=csv,noheader,nounits \
    -lms 500 >"$gpu_log" 2>/dev/null &
  sampler_pid=$!

  set +e
  if [[ "$suite" == "all" || "$suite" == "inference" ]]; then
    local infer_input
    infer_input="$(build_infer_input "$bucket" "$fixture_prefix" "$region")"
    infer_json="$(run_infer_benchmark "$infer_input")"
    infer_status=$?
    ai_metadata="$(container_metadata \
      battery-ai-infer \
      '^(INFERENCE_MODE|ONNX_DEVICE|CT_POSTPROCESS_TYPE|CT_POSTPROCESS_MATCH_METRIC|CT_POSTPROCESS_MATCH_THRESHOLD|CT_DEFECT_CONF_THRESHOLD|CT_QUALITY_THRESHOLD|RGB_QUALITY_FAIL_THRESHOLD|CELL_MIN_VALID_COVERAGE|RGB_CELL_REJECT_RATE_THRESHOLD|CT_QUALITY_GATE_MODE|CELL_ANALYSIS_WORKERS|CELL_ANALYSIS_QUEUE_SIZE)=')"
  fi
  if [[ "$suite" == "all" || "$suite" == "vlm" ]]; then
    vlm_json="$(run_vlm_benchmark)"
    vlm_status=$?
    vlm_metadata="$(container_metadata \
      battery-vlm \
      '^(VLM_MODEL_ID|DEVICE|VLM_DTYPE|VLM_QUANTIZATION)=')"
  fi
  set -e
  [[ -n "$infer_json" ]] || infer_json='{}'
  [[ -n "$vlm_json" ]] || vlm_json='{}'
  [[ -n "$ai_metadata" ]] || ai_metadata='null'
  [[ -n "$vlm_metadata" ]] || vlm_metadata='null'

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
    --arg schema_version "2" \
    --arg measured_at "$timestamp" \
    --arg instance_id "$node_id" \
    --arg instance_type "$instance_type" \
    --arg trigger "$trigger" \
    --arg suite "$suite" \
    --arg fixture_prefix "s3://${bucket}/${fixture_prefix}" \
    --argjson inference "$infer_json" \
    --argjson vlm "$vlm_json" \
    --argjson ai_metadata "$ai_metadata" \
    --argjson vlm_metadata "$vlm_metadata" \
    --argjson gpu "$gpu_json" \
    --argjson inference_exit "$infer_status" \
    --argjson vlm_exit "$vlm_status" \
    '{
      schema_version: ($schema_version | tonumber),
      measured_at: $measured_at,
      instance: {id: $instance_id, type: $instance_type},
      trigger: $trigger,
      suite: $suite,
      fixture_prefix: $fixture_prefix,
      services: {ai_infer: $ai_metadata, vlm: $vlm_metadata},
      inference: $inference,
      vlm: $vlm,
      gpu: $gpu,
      exit_codes: {inference: $inference_exit, vlm: $vlm_exit}
    }' >"$result_file"

  cp -f -- "$result_file" "${result_dir}/latest.json"
  local result_key="${result_prefix}/${node_id}/${timestamp}-${suite}.json"
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
