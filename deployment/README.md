# CI/CD deployment

Production deployment has three required layers:

1. GitHub Actions assumes `AivleBigProjectGitHubDeployRole` through GitHub OIDC.
2. Images are pushed to ECR with the full Git commit SHA as the tag.
3. A restricted SSM document invokes the deployment runner on the EC2 instance.

No long-lived AWS access key or SSH private key is stored in GitHub.

## Deployment targets

|Repository|Branch|Compose service|Tag variable|
|---|---|---|---|
|kt-aivle-big-proj-frontend|develop|frontend|FRONTEND_TAG|
|kt-aivle-big-proj-backend|main|backend-ai, backend|BACKEND_AI_TAG, BACKEND_TAG|
|kt-aivle-big-proj-ai-infer|main|ai-infer|AI_INFER_TAG|
|kt-aivle-big-proj-vlm|develop|vlm|VLM_TAG|
|kt-aivle-big-proj-infra|main|running services|Compose bundle|

## Runtime files

- `/usr/local/bin/battery-deploy-service` updates one image tag, waits for Compose, and rolls back on failure.
- `/usr/local/bin/battery-deploy-infra` validates an immutable S3 bundle, preserves `.env` and `compose.cpu.yaml`, and rolls back Compose files on failure.
- `/usr/local/bin/battery-post-deploy-benchmark` runs service-specific observational benchmarks after deployment. AI Infer uses 3 fixed CT and 3 fixed RGB requests; VLM uses 2 daily and 2 individual reports. It stores schema-v2 JSON under `/var/lib/battery/benchmarks` and `s3://kt-aivle-big-proj-kks/deploy/benchmarks/<instance-id>/`.
- AI Infer results separate download, quality-classifier, conditional defect-detector, pipeline, and request wall time. VLM results separate body generation, hallucination critic, retry count, token counts, and report end-to-end time.
- Frontend and Backend deployments skip AI benchmarks. AI Infer and VLM deployments run only their own suite; Infra deployments run both suites sequentially so the services do not contend for the GPU.
- The `Run production AI benchmark` workflow can manually run `all`, `inference`, or `vlm` through the restricted `AivleBigProjectRunBenchmark` SSM document.
- Both runners share `/var/lock/battery-deploy.lock`, so production deployments cannot overlap.
- GPU reservations are enabled only when `nvidia-smi -L` succeeds. CPU hosts use `compose.cpu.yaml` instead.
- Successful production service deployments publish the complete deployed tag set to the encrypted `/kt-aivle-big-proj/prod/runtime-env` parameter.
- A successful production infra deployment publishes its immutable bundle key to `s3://kt-aivle-big-proj-kks/deploy/infra/latest`.

## AWS resources

- IAM role: `AivleBigProjectGitHubDeployRole`
- SSM documents: `AivleBigProjectDeployService`, `AivleBigProjectDeployInfra`
- Production EC2 instance: `i-0562ca896665be441` (`g4dn.xlarge`, Tesla T4, always running)
- Fast-test EC2 instance: `i-0f243b999a4840674` (`g6e.xlarge`, NVIDIA L40S, start/stop on demand)
- Region: `ap-northeast-2`
- Infra bundles: `s3://kt-aivle-big-proj-kks/deploy/infra/<git-sha>.tar.gz`
- Benchmark fixtures: `s3://kt-aivle-big-proj-kks/models/ai-infer/onnx-20260809-01/fixtures/benchmark-v1/`
- Benchmark results: `s3://kt-aivle-big-proj-kks/deploy/benchmarks/<instance-id>/<timestamp>-<suite>.json`

Benchmarks are observational until an instance baseline and thresholds are approved. A benchmark failure is reported in the deployment log but does not roll back an otherwise healthy release.

The JSON files in `deployment/aws` are the checked-in source for the IAM policies and SSM documents.

## High-performance GPU test instance

The `g6e.xlarge` test host is separate from the always-on production `g4dn.xlarge`.
Its encrypted 150 GiB gp3 root volume retains all ECR image layers, the AI Infer
ONNX bundle, and the Hugging Face VLM cache while the instance is stopped.
Application pushes never start the test instance. On its next manual start,
`battery-fast-test.service` reads the latest successful production infra pointer and
encrypted tag set, pulls all accumulated images once, and only then starts services.

```powershell
# State and the current URL. The public IP changes after a stop/start cycle.
.\scripts\manage-fast-test-instance.ps1 -Action status

# Start, wait for EC2/SSM and HTTP readiness, then print the URL.
.\scripts\manage-fast-test-instance.ps1 -Action start -Execute

# Gracefully stop Compose, then stop only the g6e test instance.
.\scripts\manage-fast-test-instance.ps1 -Action stop -Execute
```

The management script refuses to act unless all test-instance identity tags match,
and it separately verifies that the production `g4dn.xlarge` remains running.
`scripts/prepare-fast-test-host.sh` performs the one-time disk preparation and
installs `battery-fast-test.service`, which starts the cached Compose stack after boot.
