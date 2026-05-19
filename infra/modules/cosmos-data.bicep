/*
  Cosmos DB SQL database + container + data-plane role assignment.
  Scoped to the resource group of the existing Cosmos account.
*/
@description('Existing Cosmos DB account name (in this resource group scope).')
param cosmosAccountName string

param databaseName string
param containerName string
param principalId string
param dataContributorRoleId string

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosAccountName
}

resource db 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: { id: databaseName }
  }
}

resource container 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: db
  name: containerName
  properties: {
    resource: {
      id: containerName
      partitionKey: {
        paths: [ '/conversationId' ]
        kind: 'Hash'
      }
    }
  }
}

resource roleAssign 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: account
  name: guid(account.id, principalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: '${account.id}/sqlRoleDefinitions/${dataContributorRoleId}'
    principalId: principalId
    scope: account.id
  }
}
