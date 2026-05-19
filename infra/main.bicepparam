using './main.bicep'

// Naming
param baseName = 'genesys-aa'

// Image — leave empty for the first deployment; run `az acr build` afterwards
// and then `az containerapp update --image <acr>/genesys-agent-assist:latest`.
param containerImage = ''

// ───────── Existing dependencies — fill in with your IDs / names ─────────
param azureOpenAIAccountId = '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<aoai-name>'
param foundryAccountId      = '/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<foundry-name>'
param cosmosAccountName     = '<cosmos-account>'
param cosmosResourceGroup   = '<cosmos-rg>'

// ───────── App settings ─────────
param azureOpenAIEndpoint  = 'https://<aoai>.openai.azure.com'
param projectEndpoint      = 'https://<foundry>.services.ai.azure.com/api/projects/<project>'
param agentName            = 'my-agent'
param allowedAgentNames    = ''
param sttLanguage          = 'es'
param conversationCategories = 'Invoices,Products,Support,Billing'

// Optional shared secret for AudioHook upgrades (X-API-KEY header).
param audiohookApiKey      = ''
