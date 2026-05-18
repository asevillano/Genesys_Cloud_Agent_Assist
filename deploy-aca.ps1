<#
Deploys the Genesys Cloud Agent Assist Simulator to Azure Container Apps.

Pre-requisites:
  - Azure CLI logged in (az login)
  - An ACR (will be created if missing)
  - The Container Apps Environment (will be created if missing)
  - A user-assigned managed identity OR allow the container app's system-assigned
    identity to access Foundry / Cosmos (recommended). For simplicity this script
    uses environment variables for keys.

Usage:
  ./deploy-aca.ps1 -ResourceGroup rg-genesys-aa -Location westeurope `
                   -AcrName genesysaaacr -AppName genesys-agent-assist `
                   -EnvName genesys-aa-env
#>

param(
    [Parameter(Mandatory=$true)] [string] $ResourceGroup,
    [Parameter(Mandatory=$true)] [string] $Location,
    [Parameter(Mandatory=$true)] [string] $AcrName,
    [Parameter(Mandatory=$true)] [string] $AppName,
    [Parameter(Mandatory=$true)] [string] $EnvName,
    [string] $ImageTag = "latest",
    [string] $EnvFile = ".env"
)

$ErrorActionPreference = "Stop"

Write-Host "→ Ensuring resource group" -ForegroundColor Cyan
az group create -n $ResourceGroup -l $Location | Out-Null

Write-Host "→ Ensuring Azure Container Registry" -ForegroundColor Cyan
az acr create -g $ResourceGroup -n $AcrName --sku Basic --admin-enabled true | Out-Null
$acrLogin = (az acr show -n $AcrName --query loginServer -o tsv)
$image = "$acrLogin/genesys-agent-assist:$ImageTag"

Write-Host "→ Building image $image with ACR Tasks" -ForegroundColor Cyan
az acr build -r $AcrName -t "genesys-agent-assist:$ImageTag" . | Out-Null

Write-Host "→ Ensuring Container Apps Environment" -ForegroundColor Cyan
$envExists = az containerapp env show -n $EnvName -g $ResourceGroup 2>$null
if (-not $envExists) {
    az containerapp env create -n $EnvName -g $ResourceGroup -l $Location | Out-Null
}

# Load .env into --env-vars format (KEY=VALUE pairs, skipping comments/blank lines)
$envVars = @()
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $envVars += $line
    }
} else {
    Write-Warning "$EnvFile not found — deploying without app settings"
}

$acrUser = (az acr credential show -n $AcrName --query username -o tsv)
$acrPass = (az acr credential show -n $AcrName --query "passwords[0].value" -o tsv)

Write-Host "→ Deploying container app" -ForegroundColor Cyan
$appExists = az containerapp show -n $AppName -g $ResourceGroup 2>$null
if ($appExists) {
    az containerapp update `
        -n $AppName -g $ResourceGroup `
        --image $image `
        --set-env-vars @envVars | Out-Null
} else {
    az containerapp create `
        -n $AppName -g $ResourceGroup `
        --environment $EnvName `
        --image $image `
        --registry-server $acrLogin `
        --registry-username $acrUser `
        --registry-password $acrPass `
        --target-port 8000 `
        --ingress external `
        --transport auto `
        --min-replicas 1 --max-replicas 3 `
        --cpu 1.0 --memory 2Gi `
        --env-vars @envVars | Out-Null
}

$fqdn = (az containerapp show -n $AppName -g $ResourceGroup --query properties.configuration.ingress.fqdn -o tsv)
Write-Host ""
Write-Host "✓ Deployed: https://$fqdn" -ForegroundColor Green
