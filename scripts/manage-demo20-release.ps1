param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$InstanceId = 'i-0f243b999a4840674'
$ReleaseId = 'demo20-20260817-v2'
$ReleaseBase = "s3://kt-aivle-big-proj-kks/deploy/demo20/20260817-v2"
$Region = 'ap-northeast-2'
$AllowedInstanceTypes = @('g6.xlarge')

function Get-DemoInstance {
    $instance = aws ec2 describe-instances `
        --region $Region `
        --instance-ids $InstanceId `
        --query 'Reservations[0].Instances[0].{Id:InstanceId,State:State.Name,Type:InstanceType,Name:Tags[?Key==`Name`]|[0].Value,Purpose:Tags[?Key==`Purpose`]|[0].Value,Ip:PublicIpAddress,HopLimit:MetadataOptions.HttpPutResponseHopLimit}' `
        --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to describe the G6 demo instance.'
    }
    if ($instance.Id -ne $InstanceId -or
        $instance.Name -ne 'big-project-gpu-fast-test' -or
        $instance.Purpose -ne 'high-performance-gpu-test' -or
        $instance.Type -notin $AllowedInstanceTypes -or
        $instance.HopLimit -ne 2) {
        throw "G6 demo target safety check failed for $InstanceId."
    }
    return $instance
}

function Wait-SsmOnline {
    $deadline = (Get-Date).AddMinutes(5)
    do {
        $ping = aws ssm describe-instance-information `
            --region $Region `
            --filters "Key=InstanceIds,Values=$InstanceId" `
            --query 'InstanceInformationList[0].PingStatus' `
            --output text
        if ($LASTEXITCODE -eq 0 -and $ping -eq 'Online') {
            return
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    throw 'G6 demo instance did not become SSM Online within 5 minutes.'
}

if ($Action -eq 'stop') {
    $target = Get-DemoInstance
    if ($target.State -eq 'stopped') {
        Write-Output "Already stopped: $InstanceId"
        exit 0
    }
    if ($target.State -ne 'running') {
        throw "Cannot stop G6 demo instance from state $($target.State)."
    }
    aws ec2 stop-instances --region $Region --instance-ids $InstanceId --output json | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to stop the G6 demo instance.'
    }
    aws ec2 wait instance-stopped --region $Region --instance-ids $InstanceId
    if ($LASTEXITCODE -ne 0) {
        throw 'Timed out waiting for the G6 demo instance to stop.'
    }
    Write-Output "Stopped: $InstanceId"
    exit 0
}

if ($Action -eq 'status') {
    Get-DemoInstance | Select-Object Id, State, Type, Ip | Format-Table
    exit 0
}

$target = Get-DemoInstance
if ($target.State -eq 'stopped') {
    aws ec2 start-instances --region $Region --instance-ids $InstanceId --output json | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to start the G6 demo instance.'
    }
    aws ec2 wait instance-status-ok --region $Region --instance-ids $InstanceId
    if ($LASTEXITCODE -ne 0) {
        throw 'Timed out waiting for the G6 demo instance to become healthy.'
    }
}
elseif ($target.State -ne 'running') {
    throw "Cannot start G6 demo instance from state $($target.State)."
}
Wait-SsmOnline

$commands = @(
    'set -eu',
    'cd /opt/battery/infra',
    "aws s3 cp --only-show-errors $ReleaseBase/compose.yaml compose.yaml",
    "aws s3 cp --only-show-errors $ReleaseBase/compose.gpu.yaml compose.gpu.yaml",
    "aws s3 cp --only-show-errors $ReleaseBase/runtime.env.release runtime.env.release",
    'while IFS= read -r line; do case "$line" in ""|\#*) continue;; esac; key=${line%%=*}; sed -i "/^${key}=/d" .env; printf "%s\n" "$line" >> .env; done < runtime.env.release',
    'docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai config --quiet',
    'docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai pull',
    'docker compose --env-file .env -f compose.yaml -f compose.gpu.yaml --profile app --profile ai up -d',
    'for step in $(seq 1 120); do ai=$(docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" battery-ai-infer 2>/dev/null || true); vlm=$(docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" battery-vlm 2>/dev/null || true); [ "$ai" = healthy ] && [ "$vlm" = healthy ] && break; sleep 5; done',
    '[ "$ai" = healthy ] && [ "$vlm" = healthy ]',
    'test "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/)" = "200"',
    'test "$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1/api/sim)" = "401"',
    "echo RELEASE_READY $ReleaseId",
    'docker inspect --format "{{.Name}}|{{.Config.Image}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}" battery-frontend battery-backend battery-backend-ai battery-ai-infer battery-vlm'
)
$parameters = @{ commands = $commands; executionTimeout = @('1800') } | ConvertTo-Json -Compress
$commandId = aws ssm send-command `
    --instance-ids $InstanceId `
    --document-name AWS-RunShellScript `
    --parameters $parameters `
    --query 'Command.CommandId' `
    --output text

$deadline = (Get-Date).AddMinutes(30)
do {
    Start-Sleep -Seconds 5
    $result = aws ssm get-command-invocation `
        --command-id $commandId `
        --instance-id $InstanceId `
        --output json | ConvertFrom-Json
    if ($result.Status -in @('Success', 'Cancelled', 'TimedOut', 'Failed', 'Cancelling')) {
        break
    }
} while ((Get-Date) -lt $deadline)

if ($result.Status -notin @('Success', 'Cancelled', 'TimedOut', 'Failed', 'Cancelling')) {
    throw "release activation did not finish within 30 minutes: $($result.Status)"
}

Write-Output $result.StandardOutputContent
if ($result.Status -ne 'Success') {
    Write-Error $result.StandardErrorContent
    throw "release activation failed: $($result.Status)"
}
