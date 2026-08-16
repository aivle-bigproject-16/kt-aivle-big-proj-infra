#!/usr/bin/env python3
"""Build and activate local QA images on the dedicated G6 host via SSM."""

import argparse
import shlex
import time

import boto3
from botocore.exceptions import ClientError


BUILD_SCRIPT = r'''set -Eeuo pipefail
build=/opt/battery/qa-build/20260816-v17
test ! -e "$build"
mkdir -p "$build/ai-infer" "$build/backend" "$build/infra"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/ai-infer.tar.gz "$build/ai-infer.tar.gz"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/backend.tar.gz "$build/backend.tar.gz"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/infra.tar.gz "$build/infra.tar.gz"
tar -xzf "$build/ai-infer.tar.gz" -C "$build/ai-infer"
tar -xzf "$build/backend.tar.gz" -C "$build/backend"
tar -xzf "$build/infra.tar.gz" -C "$build/infra"
registry=$(awk -F= '$1=="ECR_REGISTRY"{print $2}' /opt/battery/infra/.env)
docker build -f "$build/ai-infer/docker/Dockerfile.gpu-onnx" -t "$registry/kt-aivle-big-proj-ai-infer:qa-v17-20260816" "$build/ai-infer"
docker build --build-arg MODULE=module-api -t "$registry/kt-aivle-big-proj-backend:qa-v17-20260816" "$build/backend"
cp /opt/battery/infra/.env "$build/runtime.env.before"
install -m 0644 "$build/infra/compose.yaml" /opt/battery/infra/compose.yaml
cp -a "$build/infra/scripts/." /opt/battery/infra/scripts/
sed -i 's/^AI_INFER_TAG=.*/AI_INFER_TAG=qa-v17-20260816/' /opt/battery/infra/.env
sed -i 's/^BACKEND_TAG=.*/BACKEND_TAG=qa-v17-20260816/' /opt/battery/infra/.env
sed -i '/^CELL_MIN_VALID_COVERAGE=/d; /^RGB_CELL_REJECT_RATE_THRESHOLD=/d; /^CT_QUALITY_GATE_MODE=/d' /opt/battery/infra/.env
printf 'CELL_MIN_VALID_COVERAGE=0.8\nRGB_CELL_REJECT_RATE_THRESHOLD=0.7\nCT_QUALITY_GATE_MODE=shadow\n' >> /opt/battery/infra/.env
cd /opt/battery/infra
docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai config --quiet
docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d --no-deps backend ai-infer
docker inspect --format '{{.Name}} {{.Config.Image}} {{.State.Status}}' battery-backend battery-ai-infer
'''

BACKEND_V2_SCRIPT = r'''set -Eeuo pipefail
build=/opt/battery/qa-build/20260816-v17
test -d "$build/backend"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/backend-v2.tar.gz "$build/backend-v2.tar.gz"
tar -xzf "$build/backend-v2.tar.gz" -C "$build/backend"
registry=$(awk -F= '$1=="ECR_REGISTRY"{print $2}' /opt/battery/infra/.env)
docker build --build-arg MODULE=module-api -t "$registry/kt-aivle-big-proj-backend:qa-v17-20260816-2" "$build/backend"
sed -i 's/^BACKEND_TAG=.*/BACKEND_TAG=qa-v17-20260816-2/' /opt/battery/infra/.env
cd /opt/battery/infra
docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d --no-deps backend
docker inspect --format '{{.Name}} {{.Config.Image}} {{.State.Status}}' battery-backend
'''

CALLBACK_HOTFIX_SCRIPT = r'''set -Eeuo pipefail
build=/opt/battery/qa-build/20260816-v17-hotfix1
test ! -e "$build"
mkdir -p "$build/ai-infer" "$build/backend"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/ai-infer-hotfix1.tar.gz "$build/ai-infer.tar.gz"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/backend-hotfix1.tar.gz "$build/backend.tar.gz"
tar -xzf "$build/ai-infer.tar.gz" -C "$build/ai-infer"
tar -xzf "$build/backend.tar.gz" -C "$build/backend"
registry=$(awk -F= '$1=="ECR_REGISTRY"{print $2}' /opt/battery/infra/.env)
docker build -f "$build/ai-infer/docker/Dockerfile.gpu-onnx" -t "$registry/kt-aivle-big-proj-ai-infer:qa-v17-20260816-hotfix1" "$build/ai-infer"
docker build --build-arg MODULE=module-api -t "$registry/kt-aivle-big-proj-backend:qa-v17-20260816-hotfix1" "$build/backend"
sed -i 's/^AI_INFER_TAG=.*/AI_INFER_TAG=qa-v17-20260816-hotfix1/' /opt/battery/infra/.env
sed -i 's/^BACKEND_TAG=.*/BACKEND_TAG=qa-v17-20260816-hotfix1/' /opt/battery/infra/.env
cd /opt/battery/infra
docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d --no-deps backend ai-infer
docker inspect --format '{{.Name}} {{.Config.Image}} {{.State.Status}}' battery-backend battery-ai-infer
'''

SHADOW_HOTFIX_SCRIPT = r'''set -Eeuo pipefail
build=/opt/battery/qa-build/20260816-v17-hotfix2
test ! -e "$build"
mkdir -p "$build/ai-infer" "$build/backend"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/ai-infer-hotfix2.tar.gz "$build/ai-infer.tar.gz"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/backend-hotfix2.tar.gz "$build/backend.tar.gz"
tar -xzf "$build/ai-infer.tar.gz" -C "$build/ai-infer"
tar -xzf "$build/backend.tar.gz" -C "$build/backend"
registry=$(awk -F= '$1=="ECR_REGISTRY"{print $2}' /opt/battery/infra/.env)
docker build -f "$build/ai-infer/docker/Dockerfile.gpu-onnx" -t "$registry/kt-aivle-big-proj-ai-infer:qa-v17-20260816-hotfix2" "$build/ai-infer"
docker build --build-arg MODULE=module-api -t "$registry/kt-aivle-big-proj-backend:qa-v17-20260816-hotfix2" "$build/backend"
sed -i 's/^AI_INFER_TAG=.*/AI_INFER_TAG=qa-v17-20260816-hotfix2/' /opt/battery/infra/.env
sed -i 's/^BACKEND_TAG=.*/BACKEND_TAG=qa-v17-20260816-hotfix2/' /opt/battery/infra/.env
cd /opt/battery/infra
docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d --no-deps backend ai-infer
docker inspect --format '{{.Name}} {{.Config.Image}} {{.State.Status}}' battery-backend battery-ai-infer
'''

REPORT_HOTFIX_SCRIPT = r'''set -Eeuo pipefail
build=/opt/battery/qa-build/20260816-report-hotfix1
test ! -e "$build"
mkdir -p "$build/backend"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/backend-report-hotfix1.tar.gz "$build/backend.tar.gz"
tar -xzf "$build/backend.tar.gz" -C "$build/backend"
registry=$(awk -F= '$1=="ECR_REGISTRY"{print $2}' /opt/battery/infra/.env)
docker build --build-arg MODULE=module-ai -t "$registry/kt-aivle-big-proj-backend-ai:qa-v17-20260816-report-hotfix1" "$build/backend"
sed -i 's/^BACKEND_AI_TAG=.*/BACKEND_AI_TAG=qa-v17-20260816-report-hotfix1/' /opt/battery/infra/.env
cd /opt/battery/infra
docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d --no-deps backend-ai
docker inspect --format '{{.Name}} {{.Config.Image}} {{.State.Status}}' battery-backend-ai
'''

REPORT_COMMIT_HOTFIX_SCRIPT = r'''set -Eeuo pipefail
build=/opt/battery/qa-build/20260816-report-hotfix2
test ! -e "$build"
mkdir -p "$build/backend"
aws s3 cp --only-show-errors s3://kt-aivle-big-proj-kks/deploy/qa-source/20260816/backend-report-hotfix2.tar.gz "$build/backend.tar.gz"
tar -xzf "$build/backend.tar.gz" -C "$build/backend"
registry=$(awk -F= '$1=="ECR_REGISTRY"{print $2}' /opt/battery/infra/.env)
docker build --build-arg MODULE=module-api -t "$registry/kt-aivle-big-proj-backend:qa-v17-20260816-report-hotfix2" "$build/backend"
docker build --build-arg MODULE=module-ai -t "$registry/kt-aivle-big-proj-backend-ai:qa-v17-20260816-report-hotfix2" "$build/backend"
sed -i 's/^BACKEND_TAG=.*/BACKEND_TAG=qa-v17-20260816-report-hotfix2/' /opt/battery/infra/.env
sed -i 's/^BACKEND_AI_TAG=.*/BACKEND_AI_TAG=qa-v17-20260816-report-hotfix2/' /opt/battery/infra/.env
cd /opt/battery/infra
docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d --no-deps backend backend-ai
docker inspect --format '{{.Name}} {{.Config.Image}} {{.State.Status}}' battery-backend battery-backend-ai
'''

VERIFY_SCRIPT = r'''set -Eeuo pipefail
for step in $(seq 1 60); do
  health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' battery-ai-infer)
  [ "$health" = healthy ] && break
  sleep 5
done
[ "$health" = healthy ]
docker exec battery-ai-infer python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
docker exec battery-ai-infer env | grep -E '^(CELL_MIN_VALID_COVERAGE|RGB_CELL_REJECT_RATE_THRESHOLD|CT_QUALITY_GATE_MODE)=' | sort
curl -fsS http://127.0.0.1/ >/dev/null
docker inspect --format '{{.Name}} {{.Config.Image}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' battery-backend battery-ai-infer
'''

SMOKE_SCRIPT = r'''set -Eeuo pipefail
bash /opt/battery/infra/scripts/post-deploy-benchmark.sh --trigger qa-v17-preflight --suite inference
'''


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--instance-id",
        default="i-0f243b999a4840674",
    )
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument(
        "--phase",
        choices=(
            "full",
            "backend-v2",
            "callback-hotfix",
            "shadow-hotfix",
            "report-hotfix",
            "report-commit-hotfix",
            "verify",
            "smoke",
        ),
        default="full",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    ssm = boto3.client("ssm", region_name=args.region)
    response = ssm.send_command(
        InstanceIds=[args.instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={
            "commands": ["bash -lc " + shlex.quote(
                BUILD_SCRIPT if args.phase == "full"
                else BACKEND_V2_SCRIPT if args.phase == "backend-v2"
                else CALLBACK_HOTFIX_SCRIPT if args.phase == "callback-hotfix"
                else SHADOW_HOTFIX_SCRIPT if args.phase == "shadow-hotfix"
                else REPORT_HOTFIX_SCRIPT if args.phase == "report-hotfix"
                else REPORT_COMMIT_HOTFIX_SCRIPT \
                if args.phase == "report-commit-hotfix"
                else VERIFY_SCRIPT if args.phase == "verify"
                else SMOKE_SCRIPT
            )],
            "executionTimeout": ["7200"],
        },
        TimeoutSeconds=30,
    )
    command_id = response["Command"]["CommandId"]
    print(f"commandId={command_id}", flush=True)

    while True:
        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=args.instance_id,
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") \
                    != "InvocationDoesNotExist":
                raise
            time.sleep(2)
            continue
        status = invocation["Status"]
        if status in {
            "Success",
            "Cancelled",
            "TimedOut",
            "Failed",
            "Cancelling",
        }:
            print(invocation.get("StandardOutputContent", ""))
            error = invocation.get("StandardErrorContent", "")
            if error:
                print(error)
            if status != "Success":
                raise SystemExit(f"deployment failed: {status}")
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
