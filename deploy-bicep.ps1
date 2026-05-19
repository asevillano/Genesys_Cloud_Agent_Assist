<#
Deploys the Genesys Cloud Agent Assist Simulator to Azure with Bicep.

Flow:
  1. Deploy infra/main.bicep (ACR + UAMI + ACA env + Container App + Cosmos
     database/container + RBAC) using infra/main.bicepparam.
  2. Build & push the image to the freshly created ACR with `az acr build`.
  3. Update the container app to point at the new image.

Pre-requisites:
  - az login
  - infra/main.bicepparam edited with your AOAI / Foundry / Cosmos values.

Usage:
  ./deploy-bicep.ps1 -ResourceGroup rg-genesys-aa -Location westeurope
#>

param(
    [Parameter(Mandatory=$true)] [string] $ResourceGroup,
    [Parameter(Mandatory=$true)] [string] $Location,
    [string] $ParamFile = "infra/main.bicepparam",
    [string] $ImageTag = "latest"
)

$ErrorActionPreference = "Stop"

Write-Host "→ Ensuring resource group" -ForegroundColor Cyan
az group create -n $ResourceGroup -l $Location | Out-Null

Write-Host "→ Deploying infra (Bicep)" -ForegroundColor Cyan
$deployment = az deployment group create `
    -g $ResourceGroup `
    -f infra/main.bicep `
    -p $ParamFile `
    --query "properties.outputs" -o json | ConvertFrom-Json

$acrName       = $deployment.acrName.value
$acrLoginServer= $deployment.acrLoginServer.value
$appName       = $deployment.containerAppName.value
$fqdn          = $deployment.containerAppFqdn.value
$image         = "$acrLoginServer/genesys-agent-assist:$ImageTag"

Write-Host "→ Building image $image with ACR Tasks" -ForegroundColor Cyan
az acr build -r $acrName -t "genesys-agent-assist:$ImageTag" . | Out-Null

Write-Host "→ Updating container app with new image" -ForegroundColor Cyan
az containerapp update -n $appName -g $ResourceGroup --image $image | Out-Null

Write-Host ""
Write-Host "✓ Deployed: https://$fqdn" -ForegroundColor Green
