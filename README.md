# Genesys Cloud Agent Assist (Simulator)

End-to-end **Agent Assist** demo that mimics how the *Active Listening* solution
would integrate with **Genesys Cloud**, but without needing a Genesys license:
a built-in web simulator plays the role of the Genesys agent desktop and the
**AudioHook v2** audio connector.

## What it does

1. The browser captures the microphone, downsamples to **8 kHz µ-law** and sends
   **interleaved binary frames** (channel `external` = customer, `internal` = agent)
   over a WebSocket implementing the **AudioHook v2** protocol.
2. The backend deinterleaves, upsamples per channel to **PCM16 24 kHz** and feeds
   each channel into a dedicated **Azure OpenAI Realtime** session
   (`gpt-4o-mini-transcribe`).
3. Final **customer** transcriptions are sent to an **Azure AI Foundry Agent**
   (`runs.stream`); deltas are pushed live to the Agent Assist UI.
4. On call wrap-up, an Azure OpenAI chat model produces a **summary** plus a
   list of detected **categories**.
5. Every turn, suggestion and the final summary are persisted in **Cosmos DB**
   (partition key `/conversationId`).

```
┌────────────────────────────────────────────────────────────────────────┐
│  Browser (agent desktop simulator)                                     │
│  ┌─ left: call panel + push-to-talk ──┐  ┌─ right: <iframe> Agent ──┐ │
│  │  /  served by FastAPI              │  │ Assist (Client App sim)  │ │
│  └────────────────┬───────────────────┘  └────────────┬─────────────┘ │
│   binary µ-law 8k │ AudioHook v2 (WS)                 │ WS subscribe  │
└───────────────────┼───────────────────────────────────┼───────────────┘
                    ▼                                   ▼
            /ws/audiohook                       /ws/assist/{convId}
                    │                                   ▲
                    ▼                                   │
        ┌──────────────────────┐    deltas              │
        │ FastAPI session mgr  │ ──────────────────────►│
        │  • RealtimeSTT × 2   │
        │  • Foundry runs.stream                       
        │  • Cosmos persistence                        
        └──────────────────────┘
```

## Project layout

```
backend/
├── audiohook.py        µ-law + AudioHook v2 helpers
├── config.py           env-driven settings
├── cosmos_store.py     persistence (no-op if not configured)
├── foundry_agent.py    Azure AI Foundry wrapper
├── main.py             FastAPI app (REST + WebSockets)
├── session_manager.py  per-conversation state
├── stt_realtime.py     per-channel Azure OpenAI Realtime STT
└── summary.py          wrap-up summary
frontend/
├── simulator.html      Genesys agent desktop simulator
├── simulator.js        getUserMedia → µ-law → AudioHook
├── agent-assist.html   Agent Assist iframe UI
├── agent-assist.js     WS subscriber, renders transcript + suggestions
└── styles.css
Dockerfile
deploy-aca.ps1          Azure Container Apps deployment
.env.sample             template environment variables
requirements.txt
```

## Setup

```powershell
cd 'C:\Angel\AI GBB\Demos\Genesys_Cloud_Agent_Assist'
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.sample .env   # edit with your values
az login                     # required for DefaultAzureCredential (Foundry)
uvicorn backend.main:app --reload --port 8000
```

Then open <http://localhost:8000>:

1. Pick the **mic** and the **Foundry agent**. The Agent Assist panels
   (**Live transcript** and **Suggestions & summary**) are visible from the
   start with a placeholder message until a call begins.
2. Click **▶ Start call**. The button changes to **⏳ Connecting call…**
   while the AudioHook session is being established, and to
   **● Connected** once the backend has confirmed the `opened` handshake.
   At that point the iframe rebinds to the freshly minted `conversationId`.
3. Hold **🎤 Customer** while you speak as the customer; hold **🎤 Agent**
   while you speak as the human agent.
4. Suggestions stream into the right panel as soon as a customer turn is final.
5. Click **📝 Generate summary** to produce the wrap-up.

The UI uses a **Genesys Cloud light theme** (white panels, navy top bar,
Genesys orange accent) so it visually matches the real agent desktop the
Client App would be embedded into.

## Environment variables

See [.env.sample](.env.sample). Highlights:

| Variable | Purpose |
|---|---|
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` | Realtime STT + summary |
| `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT` | e.g. `gpt-4o-mini-transcribe` |
| `AZURE_OPENAI_SUMMARY_DEPLOYMENT` | e.g. `gpt-4.1-mini` |
| `PROJECT_ENDPOINT` / `AGENT_NAME` | Azure AI Foundry project + default agent (v2, name-based) |
| `ALLOWED_AGENT_NAMES` | optional whitelist for the agent selector |
| `COSMOS_ENDPOINT` / `COSMOS_KEY` | leave empty to disable persistence |
| `STT_LANGUAGE` | BCP-47 hint (`es`, `en`, …) |
| `CONVERSATION_CATEGORIES` | comma-separated, used by the summary |
| `AUDIOHOOK_API_KEY` | optional shared secret for `/ws/audiohook` |

### Entra ID authentication (recommended)

When `AZURE_OPENAI_API_KEY` and/or `COSMOS_KEY` are left **empty**, the backend
falls back to `DefaultAzureCredential` and authenticates with Entra ID. This is
also the **only** option if the target Azure OpenAI / Cosmos accounts have
local authentication disabled (`disableLocalAuth = true` on Cosmos, or "Disable
local auth" on AOAI).

To use this mode locally, run `az login` and grant your user (or the managed
identity used in Container Apps) the following data-plane RBAC roles on the
resources referenced from `.env`:

| Resource | Role | Role definition id |
|---|---|---|
| Azure AI Foundry project | `Azure AI User` | n/a (built-in) |
| Azure OpenAI resource | `Cognitive Services OpenAI User` | n/a (built-in) |
| Cosmos DB account | `Cosmos DB Built-in Data Contributor` | `00000000-0000-0000-0000-000000000002` |

> ⚠️ The Cosmos role above is a **data-plane** role and is *not* assigned via
> `az role assignment create`. Use `az cosmosdb sql role assignment create`:
>
> ```powershell
> $pid = az ad signed-in-user show --query id -o tsv
> az cosmosdb sql role assignment create `
>     --account-name <cosmos-account> `
>     --resource-group <cosmos-rg> `
>     --scope "/" `
>     --principal-id $pid `
>     --role-definition-id 00000000-0000-0000-0000-000000000002
> ```
>
> Without this role you will see `401 Unauthorized — Local Authorization is
> disabled. Use an AAD token to authorize all requests.` (substatus `5202`)
> at startup and persistence will be disabled.

#### Cosmos DB account firewall

If the Cosmos account restricts network access (`ipRules` populated or
`publicNetworkAccess = "Disabled"`), the developer machine / Container Apps
outbound IP must be on the allowlist or the request is rejected with:

```
(Forbidden) Request originated from IP <addr> through public internet.
This is blocked by your Cosmos DB account firewall settings.
```

Add your IP (the command **replaces** the full list, so include the existing
entries):

```powershell
$cur = (az cosmosdb show -n <cosmos-account> -g <cosmos-rg> `
          --query "ipRules[].ipAddressOrRange" -o tsv) -join ","
$new = if ($cur) { "$cur,<your-ip>" } else { "<your-ip>" }
az cosmosdb update -n <cosmos-account> -g <cosmos-rg> --ip-range-filter "$new"
```

Firewall changes take **2–5 minutes** to propagate before requests are
accepted.

#### Pre-create database and container (development)

The built-in `Cosmos DB Built-in Data Contributor` role only covers the
**data plane** (read/write items). Creating the database or container is a
**control-plane** action and will fail with:

```
(Forbidden) ... does not have required RBAC permissions to perform action
[Microsoft.DocumentDB/databaseAccounts/sqlDatabases/write] on any scope.
```

For **local development**, pre-create the database and container once and
leave only the data-plane role on your `az login` user. The code in
[backend/cosmos_store.py](backend/cosmos_store.py) calls
`create_database_if_not_exists` / `create_container_if_not_exists`, so if
they already exist the app just opens them:

```powershell
az cosmosdb sql database create `
    -a <cosmos-account> -g <cosmos-rg> -n agentassist

az cosmosdb sql container create `
    -a <cosmos-account> -g <cosmos-rg> -d agentassist `
    -n conversations --partition-key-path "/conversationId"
```

The names (`agentassist` / `conversations`) and partition key
(`/conversationId`) must match `COSMOS_DATABASE`, `COSMOS_CONTAINER` and the
code in [backend/cosmos_store.py](backend/cosmos_store.py).

For **production / ACA deployments**, provision the database, container and
role assignment declaratively with Bicep — see the section below.

## Deploy to Azure Container Apps

```powershell
./deploy-aca.ps1 `
    -ResourceGroup rg-genesys-aa `
    -Location westeurope `
    -AcrName genesysaaacr `
    -AppName genesys-agent-assist `
    -EnvName genesys-aa-env
```

The script builds the image with ACR Tasks, creates the ACA environment if
needed, and pushes every `KEY=VALUE` line from `.env` as application settings.
WebSocket transport works out of the box on Container Apps.

> For production use:
> - Move secrets out of plain env-vars (Container Apps secrets or Key Vault).
> - Use a **user-assigned managed identity** with RBAC on Foundry / Cosmos
>   instead of keys; `DefaultAzureCredential` will pick it up automatically.
> - Add Application Insights for latency/RU tracking.

### Provisioning Cosmos for production (IaC)

The recommended flow for ACA is to provision the Cosmos database, container
and data-plane role assignment **declaratively** with Bicep, so the container
app's managed identity only ever has data-plane (read/write items) access and
the app never needs control-plane permissions at runtime.

Minimal `infra/cosmos.bicep`:

```bicep
param cosmosAccountName string
param databaseName string = 'agentassist'
param containerName string = 'conversations'
@description('Object id of the container app managed identity that needs data-plane access.')
param appPrincipalId string

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

// Cosmos DB Built-in Data Contributor (data-plane only).
var dataContributorRoleId = '${account.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'

resource roleAssign 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: account
  // GUID must be deterministic per (principal, scope).
  name: guid(account.id, appPrincipalId, dataContributorRoleId)
  properties: {
    roleDefinitionId: dataContributorRoleId
    principalId: appPrincipalId
    scope: account.id
  }
}
```

Deploy it from the same pipeline that runs `deploy-aca.ps1`:

```powershell
# After the container app exists, capture its managed identity object id.
$appPrincipalId = az containerapp show `
    -g rg-genesys-aa -n genesys-agent-assist `
    --query identity.principalId -o tsv

az deployment group create `
    -g rg-genesys-aa `
    -f infra/cosmos.bicep `
    -p cosmosAccountName=<cosmos-account> appPrincipalId=$appPrincipalId
```

The deployment is idempotent: re-running it on an existing DB/container is a
no-op, and the role assignment GUID is derived from `(principal, scope)` so
it is reused across runs.

At that point the container app starts cleanly with only the data-plane role
and the `create_*_if_not_exists` calls in `cosmos_store.py` just open the
existing database and container.

## Notes on the simulation

- The AudioHook frames really are µ-law 8 kHz interleaved with two channels,
  so the same `/ws/audiohook` endpoint *should* accept a real Genesys
  AudioHook Monitor with minimal protocol tweaks (HMAC validation, full
  message handling). What the browser does is exactly what Genesys Edge would
  do, only with a synthetic two-channel mix.
- Push-to-talk is the simulator's UX trick to keep a single mic but produce
  two clean channels. Releasing a button sends silence on that channel; the
  Realtime STT VAD handles end-of-turn detection naturally.
- PII masking has been intentionally removed (per project scope).
- **Foundry agent versions are immutable.** `responses.create(..., agent_reference={"name": AGENT_NAME})`
  invokes the *latest published version* of the agent. If you edit the agent
  in the Foundry portal (instructions, knowledge / file search, tools) you
  must **Save as new version** for the change to be served by the Responses
  API. Until then the Playground will show your draft while the simulator
  keeps serving the previous version.

---

## Integrating with real Genesys Cloud

The demo is architected around the two contracts Genesys Cloud actually
exposes for Agent Assist scenarios:

1. **AudioHook Monitor** — Genesys Edge streams the live call audio (both
   participants) to a third-party WebSocket. This is what feeds the STT and
   the Foundry agent.
2. **Client App (Interaction Widget)** — a Premium/Custom Client App that
   Genesys embeds **inside the agent desktop as an iframe**. This is the
   Agent Assist UI the agent sees during the call.

Both pieces are already implemented in the simulator (`/ws/audiohook` and
`/agent-assist`). To go from the simulator to a real Genesys integration you
need to (a) make the AudioHook endpoint Genesys-compliant, (b) register the
Client App with the right URL template, and (c) wire OAuth so the iframe can
read interaction context. The next sections detail each step.

### 1. AudioHook Monitor integration

#### Prerequisites in Genesys Cloud
- An **OAuth client** of type *Client Credentials* with the
  `audiohook:integration` scope.
- A **Generic Audio Connector** integration of category *Audio Connector*,
  configured with:
  - **Channel** = `audiohook`
  - **Connection URI** = `wss://<your-aca-host>/ws/audiohook`
  - **API key** = shared secret (sent as `X-API-KEY` header).
  - **Client secret** = the HMAC secret used to sign every WS upgrade request.
  - **Outbound** = enabled, **Audio format** = `PCMU/8000/stereo` (the
    AudioHook Monitor default).
- A **Trigger** (Architect Data Action / Genesys Cloud trigger or a queue
  routing rule) that activates the AudioHook integration on the calls you
  want to listen to.

#### What the backend already implements
- Binary frames are decoded as **µ-law 8 kHz, 2 channels interleaved**, with
  channel `external` = customer and channel `internal` = agent. Genesys uses
  the same convention.
- JSON protocol messages handled: `open`, `ping`/`pong`, `close`, `closed`,
  with `seq`/`serverseq` accounting and an `opened` reply listing the
  channels we accept.
- Optional shared-secret auth via the `X-API-KEY` header (`AUDIOHOOK_API_KEY`).

#### What you still have to add for production AudioHook
Genesys requires you to validate **every WebSocket upgrade** using an HMAC
signature; the simulator skips this on purpose. To make the endpoint
production-ready you need to:

1. **Verify the `Signature` / `Signature-Input` headers** sent by Genesys
   Edge on the upgrade request. The signature covers the request line plus
   selected headers (date, host, audiohook-organization-id,
   audiohook-session-id, audiohook-correlation-id, x-api-key). Use the
   *Client secret* of the integration as the HMAC-SHA256 key and reject the
   upgrade with `401` if it doesn't match. Microsoft's docs and the Genesys
   `audiohook-reference-implementation` (Node sample) both cover the exact
   header layout.
2. **Honour the full `open` parameters Genesys sends** — in particular:
   - `organizationId`, `conversationId`, `participant.id`, `media[]`,
     `customConfig`. The current code already picks `conversationId` from
     `parameters.conversationId`; the rest is available in `m["parameters"]`
     if you need it (for example to look up the agent name from
     `customConfig.agentName`).
   - Reply with the matching `opened` message including the `media` array
     the integration negotiated. The simulator already does this with
     `audiohook.build_opened(...)`.
3. **Implement `pause` / `resume` / `discarded` / `paused` / `resumed` /
   `error` messages** so disconnects and quality events are handled
   gracefully.
4. **Expose the endpoint over WSS with a valid TLS certificate** — Genesys
   Edge will not connect to plain `ws://` or self-signed certs. Azure
   Container Apps provides this automatically with the built-in FQDN.

Suggested code touch-points: extend `backend/audiohook.py` with a
`verify_signature(headers, secret, method, target)` helper and call it from
`@app.websocket("/ws/audiohook")` **before** `websocket.accept()`. FastAPI
exposes the raw upgrade headers via `websocket.headers`.

### 2. Agent Assist UI as a Genesys Client App

The `/agent-assist` route is designed to be embedded as an iframe inside the
Genesys agent desktop using the **Premium/Custom Client App** integration
type (also called *Interaction Widget* for conversation-scoped widgets).

Steps:

1. In **Admin → Integrations**, install a **Premium Client App** (or
   *Interaction Widget*) and set the **Application URL** template to:

   ```
   https://<your-aca-host>/agent-assist?convId={{gcConversationId}}&lang={{gcLangTag}}
   ```

   `{{gcConversationId}}` is the placeholder Genesys substitutes with the
   live conversation id; `{{gcLangTag}}` provides the agent UI language.

2. Set **Group filters** so only agents on the right queues see the widget,
   and choose **Communication type filter = call** so it only opens during
   voice interactions.

3. (Optional but recommended) Add a second **OAuth client** of type
   *Implicit Grant* or *Code Authorization* with the scopes
   `conversations`, `analytics` and `users` so the iframe can fetch
   interaction metadata. The Client App receives the agent's access token
   via the **Genesys Cloud Client App SDK** (`purecloud-client-app-sdk`,
   served from `https://apps.<region>.pure.cloud/client-apps-sdk/...`).

4. In `frontend/agent-assist.js` add a small bootstrap that:
   - Loads the Client App SDK.
   - Calls `clientApp.lifecycle.addStopListener` to detect when the call
     ends (and trigger `POST /api/wrapup/{convId}`).
   - Optionally calls `clientApp.alerting.showToast(...)` to surface
     suggestions through native Genesys notifications.

The matching `conversationId` is the link between AudioHook and Client App:
both will receive the same Genesys interaction id, so the WS pub/sub in
`backend/session_manager.py` works as-is — no changes required there.

### 3. Replacing the in-browser microphone

The simulator captures audio in the agent's browser to imitate AudioHook.
In a real deployment that path **goes away**: Genesys Edge is the only audio
source. Practical consequences:

- Serve `/agent-assist` (and only that route) from the public URL Genesys
  embeds. `/` (the simulator) should be disabled or protected behind admin
  auth in production.
- The `simulator.js` push-to-talk logic and `getUserMedia` are no longer
  used; you can keep the file for QA but it must not be linked from
  `agent-assist.html`.
- The same `ConversationSession` registry keyed by `conversationId` works
  for both modes, so no backend refactor is needed.

### 4. Security checklist for production

- [ ] AudioHook **HMAC signature verification** enabled on the upgrade.
- [ ] `AUDIOHOOK_API_KEY` rotated and stored as a Container Apps secret.
- [ ] AOAI and Cosmos accessed through a **user-assigned managed identity**
      with RBAC (`Cognitive Services OpenAI User`,
      `Cosmos DB Built-in Data Contributor`); no keys in env vars.
- [ ] WSS-only endpoints, with HSTS at the ingress.
- [ ] Genesys OAuth scopes limited to the minimum the Client App needs.
- [ ] PII handling reviewed — re-enable Azure AI Language **PII Detection**
      on transcripts before persisting if your contract requires it.
- [ ] Per-tenant isolation: Cosmos partition key is already
      `/conversationId`; add an `organizationId` claim from the AudioHook
      `open` message into the persisted document if you host multiple
      Genesys orgs.

### Reference

- Genesys Cloud — [AudioHook Monitor protocol](https://developer.genesys.cloud/devapps/audiohook/) (`audiohook` channel,
  µ-law/stereo, HMAC signature spec).
- Genesys Cloud — [Custom Client Apps](https://developer.genesys.cloud/api/client-apps/) and the
  [`purecloud-client-app-sdk`](https://github.com/MyPureCloud/client-app-sdk).
- Microsoft Foundry — [Agents v2 / Responses API](https://learn.microsoft.com/azure/foundry/agents/concepts/runtime-components).
