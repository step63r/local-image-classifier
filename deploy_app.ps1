<#
.SYNOPSIS
    Ships app.py/requirements-server.txt/templates to the EC2 instance and
    restarts the imageapp service, via S3 + SSM Run Command (no SSH/scp --
    the instance has no open SSH port or key pair; see infra/README.md).

.EXAMPLE
    $env:IMAGEAPP_INSTANCE_ID = "i-0123456789abcdef0"   # CDK output InstanceId
    $env:IMAGEAPP_BUCKET = "imageclassifierappstack-mediabucket-xxxx"  # CDK output MediaBucketName
    .\deploy_app.ps1
#>
param(
    [string]$InstanceId = $env:IMAGEAPP_INSTANCE_ID,
    [string]$Bucket = $env:IMAGEAPP_BUCKET
)

if (-not $InstanceId) {
    Write-Error "Set `$env:IMAGEAPP_INSTANCE_ID or pass -InstanceId <id>. See CDK output InstanceId."
    exit 1
}
if (-not $Bucket) {
    Write-Error "Set `$env:IMAGEAPP_BUCKET or pass -Bucket <name>. See CDK output MediaBucketName."
    exit 1
}

$archive = Join-Path $env:TEMP "imageapp-deploy.tar.gz"
if (Test-Path $archive) { Remove-Item $archive }

Write-Host "Packaging app.py, requirements-server.txt, templates/ ..."
tar -czf $archive app.py requirements-server.txt templates
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$s3Key = "deploy/app.tar.gz"
Write-Host "Uploading to s3://$Bucket/$s3Key ..."
aws s3 cp $archive "s3://$Bucket/$s3Key"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Remove-Item $archive

$remoteScript = @"
set -euo pipefail
sudo -u imageapp aws s3 cp s3://$Bucket/$s3Key /tmp/imageapp-deploy.tar.gz
sudo -u imageapp mkdir -p /opt/imageapp/app
sudo -u imageapp tar -xzf /tmp/imageapp-deploy.tar.gz -C /opt/imageapp/app
sudo -u imageapp /opt/imageapp/venv/bin/pip install -q -r /opt/imageapp/app/requirements-server.txt
sudo systemctl restart imageapp
sleep 1
sudo systemctl status imageapp --no-pager
"@

Write-Host "Sending deploy command via SSM to $InstanceId ..."
$paramsFile = Join-Path $env:TEMP "imageapp-ssm-params.json"
@{ commands = @($remoteScript) } | ConvertTo-Json -Depth 3 | Set-Content -Path $paramsFile -Encoding utf8

$commandId = aws ssm send-command `
    --instance-ids $InstanceId `
    --document-name "AWS-RunShellScript" `
    --parameters file://$paramsFile `
    --query "Command.CommandId" --output text
Remove-Item $paramsFile

if (-not $commandId) {
    Write-Error "Failed to send SSM command."
    exit 1
}

Write-Host "Command $commandId sent, waiting for completion..."
do {
    Start-Sleep -Seconds 2
    $status = aws ssm get-command-invocation --command-id $commandId --instance-id $InstanceId --query "Status" --output text
} while ($status -eq "InProgress" -or $status -eq "Pending")

aws ssm get-command-invocation --command-id $commandId --instance-id $InstanceId `
    --query "{Status:Status,Stdout:StandardOutputContent,Stderr:StandardErrorContent}" --output json

if ($status -ne "Success") {
    Write-Error "Deploy command finished with status: $status"
    exit 1
}
Write-Host "Deploy complete."
