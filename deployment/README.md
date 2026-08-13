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
- Both runners share `/var/lock/battery-deploy.lock`, so production deployments cannot overlap.
- GPU reservations are enabled only when `nvidia-smi -L` succeeds. CPU hosts use `compose.cpu.yaml` instead.

## AWS resources

- IAM role: `AivleBigProjectGitHubDeployRole`
- SSM documents: `AivleBigProjectDeployService`, `AivleBigProjectDeployInfra`
- EC2 instance: `i-0562ca896665be441` (`g4dn.xlarge`, Tesla T4)
- Region: `ap-northeast-2`
- Infra bundles: `s3://kt-aivle-big-proj-kks/deploy/infra/<git-sha>.tar.gz`

The JSON files in `deployment/aws` are the checked-in source for the IAM policies and SSM documents.
