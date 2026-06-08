# Bedrock Usage Monitor

A serverless, multi-region dashboard for AWS Bedrock model usage and cost. Shows
per-model / per-region token usage (including prompt-cache tokens), invocations,
**estimated cost** (from live AWS prices) and **actual billed cost** (Cost
Explorer) — plus a Guardrailed natural-language **Ask** agent.

> **Current phase:** built and deployed on a **personal AWS account** as a
> learning exercise → personal GitHub (`jaycp30`). The UI uses a client brand
> theme, but that's just styling — it only becomes client work if/when
> deployed into the client's own AWS account.

## Architecture

The **backend** is AWS SAM; the **frontend** is hosted separately by **AWS
Amplify Hosting** (git-push CI/CD). Cognito auth lives in the SAM backend.

```
Browser ──► AWS Amplify Hosting              [React SPA, deploys on git push]
   │  (Cognito Hosted UI login → JWT)
   ▼
API Gateway (HTTP API, Cognito JWT authorizer) ──► Lambda (Python)   [SAM]
   │
   ├─ GET  /usage  per region:
   │     ├─► CloudWatch Logs  (/aws/bedrock/model-invocations) — exact tokens + cache
   │     ├─► AWS Price List API — live per-model prices (regional + global)
   │     └─► Cost Explorer — billed $, cached 6h
   │
   └─ POST /ask  → Bedrock Converse (tool-use agent) + Bedrock Guardrail
         └─► calls /usage internally as a tool → grounded answers
```

The SAM stack provisions Lambda, the HTTP API, **Cognito** (user pool + hosted
UI), and the **Bedrock Guardrail**. Amplify Hosting builds and serves
`frontend/`. The `FrontendUrl` SAM parameter wires the Cognito callback/logout
URLs and the API's CORS to the Amplify app URL.

### Data approach (ported from [bedrock-lens](https://github.com/OmarCodes022/bedrock-lens), MIT)

- **Usage = CloudWatch model-invocation LOGS**, not metrics. Logs carry exact
  per-call `inputTokenCount` / `outputTokenCount` **and** `cacheRead/Write` token
  counts — the most precise source. Read via `filter_log_events` (no per-GB scan
  charge). See `backend/bedrock_logs.py`.
- **Estimated cost = live AWS Price List API** (`backend/pricing.py`): per-model
  regional and global-profile rates incl. cache read/write, cached 24h. No
  hand-maintained price file.
- **Billed cost = Cost Explorer**, grouped by region, filtered to Amazon Bedrock,
  cached 6h (CE charges $0.01/call).
- **Regions** are config-driven via the `Regions` Lambda env var.

> ⚠️ **Prerequisite — per-region logging.** The logs source only contains data
> from the moment **Bedrock model-invocation logging is enabled in that region**.
> Enable it in every region you monitor (see DEPLOY.md). Regions without logging
> show zero usage (but billed cost from Cost Explorer still appears).

### The "Ask" agent

`backend/ask.py` is a ~20-line agent loop on the Bedrock **Converse API**. It
offers one tool (`get_usage`) that calls the dashboard's own data function, so
answers are grounded in real numbers. Three layers keep it on-task:

1. **Tool set** — bounds what data it can reach.
2. **System prompt** — guides it to usage/cost topics.
3. **Bedrock Guardrail** (`AWS::Bedrock::Guardrail` in `template.yaml`) — enforces
   the boundary, blocking off-topic prompts and prompt-injection.

## Prerequisites

AWS CLI (configured creds), SAM CLI, Node 18+, Python 3.12, Docker (for `sam
build`), and a GitHub repo (for Amplify Hosting).

## Deploy

Full manual, step-by-step runbook: **[DEPLOY.md](DEPLOY.md)**. In short:

1. **Backend (SAM)** — set `AdminEmail` in [`samconfig.toml`](samconfig.toml), then:
   ```bash
   sam build
   sam deploy --config-env sandbox     # Tokyo (ap-northeast-1)
   # client profile = us-east-1, eu-west-1, us-west-2, ca-central-1, ap-southeast-2
   ```
   Note the stack **Outputs** (`ApiUrl`, `CognitoHostedUiDomain`, `UserPoolClientId`).
2. **Frontend (Amplify Hosting)** — push the repo to GitHub, create an Amplify app
   from it (it auto-detects [`amplify.yml`](amplify.yml), app root `frontend/`),
   and set these env vars in the Amplify console:
   ```
   VITE_API_URL        = <ApiUrl>
   VITE_COGNITO_DOMAIN = <CognitoHostedUiDomain>
   VITE_CLIENT_ID      = <UserPoolClientId>
   VITE_REDIRECT_URI   = https://<your-amplify-app>.amplifyapp.com/
   ```
   Add an SPA rewrite: source `/<*>` → target `/index.html`, type **200**.
3. **Reconnect** — set `FrontendUrl` in `samconfig.toml` to the Amplify URL and
   re-run `sam deploy --config-env sandbox` so Cognito callbacks + CORS allow it.

Cognito emails a temporary password to `AdminEmail` on first deploy.

### Two frontends

- **`frontend/`** — the experimentation playground (try any palette/design).
- **`frontend-gk/`** — a **locked** copy holding the client brand
  design. Point a separate Amplify app (or branch) at this folder for the
  client-ready build. Don't experiment in it.

## Local development

Backend, against real logs/pricing/CE (read-only, no deploy):

```bash
cd backend
python app.py ap-northeast-1 30d           # regions, range: today|7d|30d|90d
python ask.py "summarize my usage this week" ap-northeast-1
```

Frontend in demo mode (bundled sample data, no backend/login):

```bash
cd frontend && npm install && npm run dev
```

Against a deployed backend: create `frontend/.env.local` with the same `VITE_*`
keys you set in Amplify.

## Cost notes

- CloudWatch `filter_log_events`: standard API calls, no per-GB scan charge.
- Cost Explorer: **$0.01 per request** → cached.
- Price List API: free.
- Bedrock `/ask`: each question costs Bedrock tokens (Haiku by default → tiny).
- Lambda + API Gateway + Cognito + Amplify Hosting at personal scale: a few
  cents/month.

## Files

| Path | What |
|------|------|
| `backend/app.py` | Lambda: routes `/usage` + `/ask`, multi-region aggregation |
| `backend/bedrock_logs.py` | Model-invocation log reader (ported from bedrock-lens) |
| `backend/pricing.py` | Live AWS Price List pricing (ported from bedrock-lens) |
| `backend/ask.py` | Guardrailed Bedrock Converse agent |
| `template.yaml` | SAM backend: Lambda, HTTP API + Cognito, Bedrock Guardrail |
| `samconfig.toml` | `sandbox` and `client` deploy profiles (incl. `FrontendUrl`) |
| `amplify.yml` | Amplify Hosting build spec (builds `frontend/`) |
| `frontend/` | React + Vite + Recharts SPA (experimentation) |
| `frontend-gk/` | Locked client-brand copy of the frontend |
| `frontend/src/theme.css` | Design tokens (brand palette) |

## Credits

Usage-log reading and live-pricing logic ported from
[OmarCodes022/bedrock-lens](https://github.com/OmarCodes022/bedrock-lens) (MIT).
