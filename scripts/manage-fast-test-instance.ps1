param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("status", "start", "stop", "sync")]
    [string]$Action,

    [switch]$Execute,
    [string]$InstanceId = "i-0f243b999a4840674",
    [string]$ProductionInstanceId = "i-0562ca896665be441",
    [string]$Profile = "default",
    [string]$Region = "ap-northeast-2",
    [int]$ReadyTimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"

$ExpectedName = "big-project-gpu-fast-test"
$ExpectedPurpose = "high-performance-gpu-test"
$ExpectedType = "g6e.xlarge"
$ExpectedProductionName = "big-project-gpu-serving"
$ExpectedProductionType = "g4dn.xlarge"

function Invoke-Aws {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    $base = @()
    if ($Profile) {
        $base += @("--profile", $Profile)
    }

    & aws @base @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "AWS CLI command failed: aws $($Arguments -join ' ')"
    }
}

function Get-Instance {
    param([string]$Id)

    $json = Invoke-Aws ec2 describe-instances `
        --region $Region `
        --instance-ids $Id `
        --query "Reservations[0].Instances[0].{Id:InstanceId,State:State.Name,Type:InstanceType,Name:Tags[?Key=='Name']|[0].Value,Purpose:Tags[?Key=='Purpose']|[0].Value,ProductionInstanceId:Tags[?Key=='ProductionInstanceId']|[0].Value,PublicIp:PublicIpAddress,PrivateIp:PrivateIpAddress,HopLimit:MetadataOptions.HttpPutResponseHopLimit}" `
        --output json
    return $json | ConvertFrom-Json
}

function Assert-SafeTargets {
    $production = Get-Instance -Id $ProductionInstanceId
    if ($production.Name -ne $ExpectedProductionName -or
        $production.Type -ne $ExpectedProductionType -or
        $production.State -ne "running") {
        throw "Production protection check failed. Expected running $ExpectedProductionName/$ExpectedProductionType."
    }

    if ($InstanceId -eq $ProductionInstanceId) {
        throw "Refusing to manage the production instance."
    }

    $target = Get-Instance -Id $InstanceId
    if ($target.Name -ne $ExpectedName -or
        $target.Purpose -ne $ExpectedPurpose -or
        $target.Type -ne $ExpectedType -or
        $target.ProductionInstanceId -ne $ProductionInstanceId) {
        throw "Target safety check failed for $InstanceId."
    }
    if ($target.HopLimit -ne 2) {
        throw "Target IMDS hop limit must be 2 for container credentials."
    }

    return $target
}

function Wait-SsmOnline {
    param([string]$Id)

    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    do {
        $ping = Invoke-Aws ssm describe-instance-information `
            --region $Region `
            --filters "Key=InstanceIds,Values=$Id" `
            --query "InstanceInformationList[0].PingStatus" `
            --output text
        if ($ping -eq "Online") {
            return
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    throw "SSM did not become online within $ReadyTimeoutSeconds seconds."
}

function Wait-HttpReady {
    param([string]$PublicIp)

    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest `
                -Uri "http://$PublicIp/" `
                -UseBasicParsing `
                -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                return
            }
        }
        catch {
            # The systemd service is still loading GPU models.
        }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    throw "HTTP service did not become ready within $ReadyTimeoutSeconds seconds."
}

function Invoke-SsmDocument {
    param(
        [string]$Id,
        [string]$DocumentName,
        [string]$Comment,
        [string]$Parameters,
        [string]$FailureMessage
    )

    Wait-SsmOnline -Id $Id

    $arguments = @(
        "ssm", "send-command",
        "--region", $Region,
        "--instance-ids", $Id,
        "--document-name", $DocumentName,
        "--comment", $Comment
    )
    if ($Parameters) {
        $arguments += @("--parameters", $Parameters)
    }
    $arguments += @("--query", "Command.CommandId", "--output", "text")

    $commandId = Invoke-Aws @arguments

    # The CLI waiter gives up after 20 checks, which is shorter than a full image
    # pull, so a timed-out wait is retried rather than reported as a failure.
    do {
        $waited = $true
        try {
            Invoke-Aws ssm wait command-executed `
                --region $Region `
                --command-id $commandId `
                --instance-id $Id | Out-Null
        }
        catch {
            $waited = $false
        }
    } while (-not $waited)

    $status = Invoke-Aws ssm get-command-invocation `
        --region $Region `
        --command-id $commandId `
        --instance-id $Id `
        --query "Status" `
        --output text
    if ($status -ne "Success") {
        $output = Invoke-Aws ssm get-command-invocation `
            --region $Region `
            --command-id $commandId `
            --instance-id $Id `
            --query "StandardErrorContent" `
            --output text
        throw "$FailureMessage`: $status`n$output"
    }
}

function Stop-ServicesGracefully {
    param([string]$Id)

    Invoke-SsmDocument `
        -Id $Id `
        -DocumentName "AWS-RunShellScript" `
        -Comment "Gracefully stop separate GPU test services" `
        -Parameters 'commands=["sudo systemctl stop battery-fast-test.service"]' `
        -FailureMessage "Graceful service stop failed"
}

function Sync-LatestRelease {
    param([string]$Id)

    Invoke-SsmDocument `
        -Id $Id `
        -DocumentName "AivleBigProjectSyncFastTest" `
        -Comment "Resynchronise the GPU test host with the latest production release" `
        -FailureMessage "Test host synchronisation failed"
}

$target = Assert-SafeTargets

Write-Host ""
Write-Host "High-performance GPU test instance" -ForegroundColor Cyan
Write-Host "Instance:   $($target.Id)"
Write-Host "State:      $($target.State)"
Write-Host "Type:       $($target.Type)"
Write-Host "Public IP:  $($target.PublicIp)"
Write-Host "Production: $ProductionInstanceId (protected and running)"

switch ($Action) {
    "status" {
        if ($target.State -eq "running" -and $target.PublicIp) {
            Write-Host "URL:        http://$($target.PublicIp)"
        }
        exit 0
    }

    "start" {
        if (-not $Execute) {
            Write-Host "DRY RUN: use -Execute to start the test instance." -ForegroundColor Yellow
            exit 0
        }

        if ($target.State -eq "stopped") {
            Invoke-Aws ec2 start-instances `
                --region $Region `
                --instance-ids $InstanceId `
                --output json | Out-Null
            Invoke-Aws ec2 wait instance-status-ok `
                --region $Region `
                --instance-ids $InstanceId | Out-Null
        }
        elseif ($target.State -ne "running") {
            throw "Cannot start from state $($target.State)."
        }

        Wait-SsmOnline -Id $InstanceId
        $target = Assert-SafeTargets
        Wait-HttpReady -PublicIp $target.PublicIp

        Write-Host "Ready: http://$($target.PublicIp)" -ForegroundColor Green
        exit 0
    }

    "sync" {
        if (-not $Execute) {
            Write-Host "DRY RUN: use -Execute to resynchronise the test instance." -ForegroundColor Yellow
            exit 0
        }

        if ($target.State -ne "running") {
            throw "Cannot synchronise from state $($target.State). Start the instance first."
        }

        Sync-LatestRelease -Id $InstanceId
        $target = Assert-SafeTargets
        Wait-HttpReady -PublicIp $target.PublicIp

        Write-Host "Synchronised: http://$($target.PublicIp)" -ForegroundColor Green
        exit 0
    }

    "stop" {
        if (-not $Execute) {
            Write-Host "DRY RUN: use -Execute to stop the test instance." -ForegroundColor Yellow
            exit 0
        }

        if ($target.State -eq "stopped") {
            Write-Host "Test instance is already stopped." -ForegroundColor Green
            exit 0
        }
        if ($target.State -ne "running") {
            throw "Cannot stop from state $($target.State)."
        }

        Stop-ServicesGracefully -Id $InstanceId
        Invoke-Aws ec2 stop-instances `
            --region $Region `
            --instance-ids $InstanceId `
            --output json | Out-Null
        Invoke-Aws ec2 wait instance-stopped `
            --region $Region `
            --instance-ids $InstanceId | Out-Null

        Write-Host "Stopped: $InstanceId" -ForegroundColor Green
        exit 0
    }
}
