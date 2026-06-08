# Deployment Runbook (manual, step-by-step)

You run every command yourself. Each step says **what it does**, the **command**,
and **how to verify** before moving on. Nothing here is destructive except the
final teardown step.

> **Current phase:** personal AWS account, personal GitHub (`jaycp30`), Firefox.
> Sandbox target: account `441342223857`, region `ap-northeast-1` (Tokyo).
> Your AWS CLI default region is `ap-southeast-1`, so we pass region explicitly.

**Split of responsibilities:** the **backend** (Lambda, HTTP API, Cognito,
Guardrail) is deployed with **AWS SAM**. The **frontend** is hosted by **AWS
Amplify Hosting**, which builds and deploys it on every git push.

---

## Phase 0 — Understand what gets created

The SAM stack (`bedrock-monitor`) creates the **backend** only:

| Resource | Why |
|----------|-----|
| Lambda function | Runs `backend/app.py` — reads invocation logs, pricing, Cost Explorer; serves `/usage` + `/ask` |
| HTTP API (API Gateway) | Public HTTPS endpoint, guarded by a Cognito JWT authorizer |
| Cognito User Pool + App Client + Hosted UI domain | Login. Email, admin-created users only |
| Bedrock Guardrail (+ version) | Scopes the `/ask` agent to usage/cost topics |

The **frontend is NOT in this stack** — Amplify Hosting handles it (Phases 6–8).

Cost at personal scale: a few cents/month. Cost Explorer calls are $0.01 each
(cached). Nothing here is in the AWS free-tier danger zone.

---

## Phase 1 — Set the admin email

The first login user is seeded from the `AdminEmail` parameter. Edit
`samconfig.toml` → `[sandbox.deploy.parameters]` → `parameter_overrides` →
`AdminEmail`. This is where Cognito sends the temporary password.

---

## Phase 1.5 — Enable invocation logging in each region (REQUIRED)

The dashboard reads Bedrock **model-invocation logs** — the most precise token
source — so logging must be ON in every region you monitor. Logs only capture
invocations made *after* logging is enabled. (Your **Tokyo sandbox is already
set up** from the earlier handoff session.)

Easiest path — reuse the `bedrock-lens` CLI you already have, once per region:

```bash
bedrock-lens --setup --region us-east-1
bedrock-lens --setup --region eu-west-1
bedrock-lens --setup --region us-west-2
bedrock-lens --setup --region ca-central-1
bedrock-lens --setup --region ap-southeast-2
```

Each run creates the `/aws/bedrock/model-invocations` log group + the IAM role
Bedrock uses to write to it, and turns on model-invocation logging.

**Verify** (per region):

```bash
aws bedrock get-model-invocation-logging-configuration --region us-east-1
```

You should see `loggingConfig` with a `cloudWatchConfig.logGroupName` of
`/aws/bedrock/model-invocations`. Regions where you skip this will simply show
zero usage (billed cost from Cost Explorer still appears).

---

## Phase 2 — Build the Lambda artifact

**What:** SAM installs the Lambda's Python deps and stages a deployable bundle
under `.aws-sam/build/`. Uses Docker for a Lambda-compatible build.

```bash
cd /Users/jaycpantinople/Claude-test/bedrock-monitor
sam build
```

**Verify:** ends with `Build Succeeded`. (Read-only locally — touches no AWS.)

---

## Phase 3 — Deploy the backend stack

**What:** uploads the artifact and creates/updates the CloudFormation stack.
Parameters (region, `AdminEmail`, `Regions`, `FrontendUrl`, tags) all come from
`samconfig.toml`, so no flags are needed. This is the step that **creates AWS
resources**.

```bash
sam deploy --config-env sandbox
```

`confirm_changeset = true` means it shows you the full resource list and **pauses**
— read it, confirm the **region is `ap-northeast-1`**, then type `y`.

> First time only: if you'd rather walk the prompts interactively, use
> `sam deploy --guided --config-env sandbox` — but note it can rewrite
> `samconfig.toml`. The plain command above is preferred since the config is set.

**Verify:** ends with `Successfully created/updated stack`, then prints the
**Outputs** table (`ApiUrl`, `CognitoHostedUiDomain`, `UserPoolClientId`,
`UserPoolId`, `GuardrailId`).

> Note: the Cognito Hosted UI domain is `bedrock-monitor-<accountid>` and must be
> globally unique. If it collides, change `Domain` in `template.yaml`.

---

## Phase 4 — Capture the stack outputs

**What:** these values configure the frontend in Amplify.

```bash
aws cloudformation describe-stacks --stack-name bedrock-monitor \
  --region ap-northeast-1 \
  --query 'Stacks[0].Outputs' --output table
```

**Verify:** you see `ApiUrl`, `CognitoHostedUiDomain`, `UserPoolClientId`,
`UserPoolId`, `GuardrailId`. Keep them handy for Phase 7.

---

## Phase 5 — Push the repo to GitHub

**What:** Amplify Hosting deploys from a Git repo. Personal phase → `jaycp30`.

```bash
git init && git add -A
git commit -m "feat: bedrock-monitor (SAM backend + Amplify hosting)"
# create the repo under jaycp30 in the browser (Firefox), then:
git remote add origin git@github.com:jaycp30/bedrock-monitor.git
git branch -M main && git push -u origin main
```

**Verify:** the repo shows up on GitHub with `amplify.yml`, `frontend/`,
`backend/`, `template.yaml`. (`CLAUDE.md` is gitignored — it won't appear.)

---

## Phase 6 — Create the Amplify Hosting app

**What:** stands up a managed CI/CD pipeline + CDN for the frontend.

In the AWS console (region `ap-northeast-1`):

1. **Amplify → Create new app → Deploy from GitHub** → authorize → pick the repo
   + `main` branch.
2. It auto-detects [`amplify.yml`](amplify.yml) (monorepo, app root `frontend/`).
   Accept it.
3. Let the first build run, then **note the app URL**:
   `https://main.dXXXXXX.amplifyapp.com`.

**Verify:** the build succeeds and the URL loads (it'll show the login screen, not
yet wired to the backend — that's Phase 7).

---

## Phase 7 — Configure the frontend + reconnect the backend

**What:** point the frontend at the backend, and allow the Amplify URL in Cognito
+ CORS.

**7a. Set env vars in Amplify** (App settings → Environment variables), using your
Phase 4 outputs and the Phase 6 URL:

```
VITE_API_URL        = <ApiUrl>
VITE_COGNITO_DOMAIN = <CognitoHostedUiDomain>
VITE_CLIENT_ID      = <UserPoolClientId>
VITE_REDIRECT_URI   = https://main.dXXXXXX.amplifyapp.com/
```

Then **Redeploy this version** in Amplify.

**7b. Add the SPA rewrite** (App settings → Rewrites and redirects):
source `/<*>` → target `/index.html` → type **200 (Rewrite)**.

**7c. Reconnect the backend** — set `FrontendUrl` in `samconfig.toml` to the
Amplify URL (no trailing slash), then redeploy:

```bash
sam deploy --config-env sandbox
```

**Verify:** the changeset shows Cognito `UserPoolClient` + the API being updated;
approve it.

---

## Phase 8 — First login

1. Open the **Amplify app URL**.
2. Click **Sign in** → redirected to the Cognito Hosted UI.
3. Username = your `AdminEmail`; password = the temporary one Cognito emailed.
4. You'll set a new password (min 12 chars, upper/lower/number/symbol).
5. Back to the dashboard — now showing **live** data (the "Demo data" pill is gone).

Add more users later:

```bash
aws cognito-idp admin-create-user --user-pool-id <UserPoolId> \
  --region ap-northeast-1 \
  --username someone@example.com \
  --user-attributes Name=email,Value=someone@example.com Name=email_verified,Value=true
```

---

## Updating later

- **Backend code change** → `sam build && sam deploy --config-env sandbox`
- **Frontend change** → just `git push`; Amplify rebuilds and redeploys automatically.

## Deploying the client-branded frontend

Point a **separate Amplify app** (or branch) at the `frontend-gatekeeper/` folder
(set its app root accordingly) so the locked Gatekeeper design ships independently
of your experiments in `frontend/`.

## Deploying to the client's 5 regions

Use the `client` profile (set its `AdminEmail` and `FrontendUrl` first):

```bash
sam deploy --config-env client
```

It deploys the stack in `us-east-1` and the Lambda fans out to all five regions.

## Teardown (DESTRUCTIVE — removes everything)

```bash
# 1. Delete the Amplify app in the console (App settings → General → Delete app).
# 2. Delete the backend stack:
sam delete --stack-name bedrock-monitor --region ap-northeast-1
```
