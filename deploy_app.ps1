<#
.SYNOPSIS
    Ships app.py/requirements-server.txt/templates to the EC2 instance and
    restarts the imageapp service. No CI/CD -- just scp + ssh, matching the
    "necessary minimum" scope of this deployment.

.EXAMPLE
    $env:IMAGEAPP_HOST = "203.0.113.50"   # the Elastic IP from the CDK output
    .\deploy_app.ps1
#>
param(
    [string]$AppHost = $env:IMAGEAPP_HOST,
    [string]$Key = "infra/keys/local-image-classifier-key.pem",
    [string]$RemoteUser = "ec2-user"
)

if (-not $AppHost) {
    Write-Error "Set `$env:IMAGEAPP_HOST or pass -AppHost <ip>. See CDK output InstancePublicIp."
    exit 1
}
if (-not (Test-Path $Key)) {
    Write-Error "SSH key not found at $Key. Fetch it once with:`n  aws ssm get-parameter --name /ec2/keypair/<key-id> --with-decryption --query Parameter.Value --output text > $Key"
    exit 1
}

Write-Host "Deploying to $RemoteUser@$AppHost ..."

ssh -i $Key "$RemoteUser@$AppHost" "sudo mkdir -p /opt/imageapp/app/templates && sudo chown -R ${RemoteUser}:${RemoteUser} /opt/imageapp/app"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

scp -i $Key app.py requirements-server.txt "${RemoteUser}@${AppHost}:/opt/imageapp/app/"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

scp -i $Key -r templates "${RemoteUser}@${AppHost}:/opt/imageapp/app/"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

ssh -i $Key "$RemoteUser@$AppHost" @'
sudo chown -R imageapp:imageapp /opt/imageapp/app
sudo -u imageapp /opt/imageapp/venv/bin/pip install -q -r /opt/imageapp/app/requirements-server.txt
sudo systemctl restart imageapp
sleep 1
sudo systemctl status imageapp --no-pager
'@
