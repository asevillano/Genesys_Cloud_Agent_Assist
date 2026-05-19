/*
  Genesys Cloud Agent Assist (Simulator) — Azure infrastructure.

  Provisions:
    - Log Analytics workspace (required by Container Apps environment)
    - Azure Container Registry (admin disabled; image pull via managed identity)
    - User-assigned managed identity (used by the container app)
    - Azure Container Apps environment + Container App (HTTPS ingress on port 8000)
    - Cosmos DB SQL database `agentassist` + container `conversations`
      (partition key `/conversationId`) on an EXISTING Cosmos account, plus
      a data-plane role assignment for the UAMI
    - Data-plane role assignments for the UAMI on EXISTING Azure OpenAI and
      Foundry project resources

  This file does NOT build or push the container image. Build it with
  `az acr build` (see `deploy-bicep.ps1`) before/after running this template.
*/

@description('Base name used to derive resource names (3-15 chars, lowercase letters / digits).')
@minLength(3)
@maxLength(15)
param baseName string

@description('Azure region for all new resources.')
param location string = resourceGroup().location

@description('Container image reference (e.g. <acr>.azurecr.io/genesys-agent-assist:latest). If empty, a placeholder image is used and you must run `az containerapp update --image` after `az acr build`.')
param containerImage string = ''

// ───────── Existing dependencies (passed in as IDs / names) ─────────
@description('Resource ID of the existing Azure OpenAI account used for Realtime STT + summary.')
param azureOpenAIAccountId string

@description('Resource ID of the existing Azure AI Foundry project (Microsoft.CognitiveServices/accounts).')
param foundryAccountId string

@description('Name of the existing Cosmos DB account used for persistence.')
param cosmosAccountName string

@description('Resource group of the existing Cosmos DB account (defaults to current RG).')
param cosmosResourceGroup string = resourceGroup().name

// ───────── App settings (forwarded to the container) ─────────
@description('Azure OpenAI endpoint, e.g. https://<aoai>.openai.azure.com')
param azureOpenAIEndpoint string

@description('Azure OpenAI API version.')
param azureOpenAIApiVersion string = '2024-10-01-preview'

@description('Realtime transcribe deployment name.')
param transcribeDeployment string = 'gpt-4o-mini-transcribe'

@description('Chat deployment used for wrap-up summary.')
param summaryDeployment string = 'gpt-4.1-mini'

@description('Foundry project endpoint, e.g. https://<foundry>.services.ai.azure.com/api/projects/<project>')
param projectEndpoint string

@description('Default Foundry agent name.')
param agentName string

@description('Optional comma-separated whitelist of agent names exposed in the UI.')
param allowedAgentNames string = ''

@description('STT language hint (BCP-47).')
param sttLanguage string = 'es'

@description('Comma-separated conversation categories used by the summary.')
param conversationCategories string = 'Invoices,Products,Support,Billing'

@description('Optional shared secret for AudioHook upgrades (X-API-KEY header).')
@secure()
param audiohookApiKey string = ''

@description('Cosmos DB SQL database name.')
param cosmosDatabaseName string = 'agentassist'

@description('Cosmos DB SQL container name.')
param cosmosContainerName string = 'conversations'

// ───────── Naming ─────────
var suffix = toLower(uniqueString(resourceGroup().id, baseName))
var uamiName = '${baseName}-uami'
var acrName = toLower(replace('${baseName}acr${suffix}', '-', ''))
var lawName = '${baseName}-law'
var envName = '${baseName}-env'
var appName = '${baseName}-app'

// Built-in role definition IDs
var roleIds = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  // Cognitive Services OpenAI User (data plane for AOAI)
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  // Azure AI User (data plane for Foundry projects)
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  // Cosmos DB Built-in Data Contributor (SQL data plane — assigned via
  // sqlRoleAssignments, NOT roleAssignments).
  cosmosDataContributor: '00000000-0000-0000-0000-000000000002'
}

// ───────── Identity ─────────
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
}

// ───────── Log Analytics + ACA env ─────────
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: lawName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// ───────── ACR (admin disabled — pull via UAMI) ─────────
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, roleIds.acrPull)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleIds.acrPull)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ───────── Cosmos data plane: db + container + role assignment ─────────
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosAccountName
  scope: resourceGroup(cosmosResourceGroup)
}

module cosmosData 'modules/cosmos-data.bicep' = {
  name: 'cosmos-data'
  scope: resourceGroup(cosmosResourceGroup)
  params: {
    cosmosAccountName: cosmosAccountName
    databaseName: cosmosDatabaseName
    containerName: cosmosContainerName
    principalId: uami.properties.principalId
    dataContributorRoleId: roleIds.cosmosDataContributor
  }
}

// ───────── Data-plane RBAC on existing AOAI + Foundry ─────────
module aoaiRole 'modules/role-assignment.bicep' = {
  name: 'aoai-role'
  scope: resourceGroup(split(azureOpenAIAccountId, '/')[4])
  params: {
    accountName: last(split(azureOpenAIAccountId, '/'))
    principalId: uami.properties.principalId
    roleDefinitionId: roleIds.cognitiveServicesOpenAIUser
  }
}

module foundryRole 'modules/role-assignment.bicep' = {
  name: 'foundry-role'
  scope: resourceGroup(split(foundryAccountId, '/')[4])
  params: {
    accountName: last(split(foundryAccountId, '/'))
    principalId: uami.properties.principalId
    roleDefinitionId: roleIds.azureAIUser
  }
}

// ───────── Container App ─────────
var effectiveImage = empty(containerImage)
  ? 'mcr.microsoft.com/k8se/quickstart:latest'
  : containerImage

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uami.id}': {}
    }
  }
  dependsOn: [
    acrPull
  ]
  properties: {
    managedEnvironmentId: acaEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: uami.id
        }
      ]
      secrets: empty(audiohookApiKey) ? [] : [
        {
          name: 'audiohook-api-key'
          value: audiohookApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'app'
          image: effectiveImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: concat(
            [
              { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
              { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAIEndpoint }
              { name: 'AZURE_OPENAI_API_VERSION', value: azureOpenAIApiVersion }
              { name: 'AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT', value: transcribeDeployment }
              { name: 'AZURE_OPENAI_SUMMARY_DEPLOYMENT', value: summaryDeployment }
              { name: 'PROJECT_ENDPOINT', value: projectEndpoint }
              { name: 'AGENT_NAME', value: agentName }
              { name: 'ALLOWED_AGENT_NAMES', value: allowedAgentNames }
              { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
              { name: 'COSMOS_DATABASE', value: cosmosDatabaseName }
              { name: 'COSMOS_CONTAINER', value: cosmosContainerName }
              { name: 'STT_LANGUAGE', value: sttLanguage }
              { name: 'CONVERSATION_CATEGORIES', value: conversationCategories }
              { name: 'PORT', value: '8000' }
            ],
            empty(audiohookApiKey) ? [] : [
              { name: 'AUDIOHOOK_API_KEY', secretRef: 'audiohook-api-key' }
            ]
          )
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

// ───────── Outputs ─────────
output containerAppFqdn string = app.properties.configuration.ingress.fqdn
output containerAppName string = app.name
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output uamiClientId string = uami.properties.clientId
output uamiPrincipalId string = uami.properties.principalId
output resourceGroupLocation string = location
