# Clark Email Tool

Clark is an MCP (Model Context Protocol) server that lets Willcrest Partners team members send email from **clark@willcrestpartners.com** directly through Claude Cowork. A team member asks Claude to send an email, reviews a preview, confirms, and the email goes out — no switching apps, no separate email client.

## What It Does

Clark exposes three tools to Claude:

| Tool | Who Can Use It | What It Does |
|---|---|---|
| `send_email` | Any authorized user | Compose and send an email. Always shows a preview first — the user must confirm before anything is sent. |
| `check_my_access` | Any authorized user | Check your own access status and remaining sends for the day. |
| `show_dashboard` | Admins only | View all users, their limits, today's usage, and recent send activity. |

---

## Inbound (email command bus)

In addition to sending, Clark can act as the **inbound gateway** of an email→AI
command bus (Phase 1). It polls clark@willcrestpartners.com, applies deterministic
gating, and forwards qualifying messages to the **Clark** destination, which does
the intent classification. **This gateway runs NO LLM and holds NO Anthropic key**
— it is a low-privilege transport that does gating + relay only. (See Clark's
`specs/email-gateway-integration.md` for the destination side.)

**New Gmail scope:** `https://www.googleapis.com/auth/gmail.readonly` must be added
to the service account's Domain-wide Delegation in Google Workspace Admin (alongside
the existing `gmail.send` / `gmail.modify`) so the poller can read mail.

**Poll loop:** the poller runs one sweep per invocation via `poller.poll_once()`.
In production this is the `clark-email-poller` Lambda, fired every minute by an
EventBridge schedule (`rate(1 minute)`, reserved concurrency 1) — it replaces the
old always-on asyncio loop. Each sweep lists unread inbox mail and, per message:
dedupes by RFC822 Message-ID (DynamoDB in prod, SQLite locally), drops unknown
senders (allow-list fetched from Clark, cached 300s) and unhealthy mail
(auto-replies, bulk/list mail, bounces, self-loops — all deterministic), then POSTs
an **envelope v1** to Clark's inbound webhook with an `X-Clark-Signature:
sha256=<hmac>` header over the raw body. On a 2xx ack the message is marked read;
otherwise it is left unread for retry.

**Outbound `/send` relay:** Clark POSTs replies back to `POST {gateway}/send` with the
same HMAC signature. The gateway verifies it and sends from clark@ **threaded**
(In-Reply-To / References from the payload). Returns 200 on send, 401 on bad
signature, 5xx on send failure.

**New MCP tools:**

| Tool | Who | What |
|---|---|---|
| `poll_inbox` | Admins | Trigger one poll sweep now and return a summary. |
| `verify_sender` | Any user | Report whether an address is on the cached allow-list. |
| `send_approval_notification` | Admins | Manually relay a (threaded) reply from clark@. |

**Connector-backed tools** (CIM intake + the Cowork mobile/voice UI). Thin,
HMAC-signed proxies over Clark's `/api/connector/*` routes — `caller_email` is
passed through and Clark enforces the authorized-user allow-list and app-layer
permissions server-side. All reuse `CLARK_INBOUND_HMAC_SECRET` +
`CLARK_CONNECTOR_BASE_URL`; no new infra or auth model.

| Tool | Who | What |
|---|---|---|
| `search_companies` | Clark users | Find existing companies/deals so a CIM isn't duplicated. |
| `analyze_cim` | Clark users | Extract facts from a CIM in a Dropbox deal folder (server-side). |
| `submit_cim_intake` | Clark users | Submit confirmed CIM data → one approval with one-tap links. |
| `search_contacts` | Clark users | Look up a person or build a shortlist ("brokers in Dallas"). |
| `get_contact` | Clark users | Full contact record by id (call-prep). |
| `get_company` | Clark users | Full company/deal record by id. |
| `submit_contact` | Clark users | Add a person (+ optional company/activity) → approval with one-tap links. |
| `submit_activity` | Clark users | Log/re-code a call or meeting note → approval with one-tap links. |
| `sync_granola` | Clark users | Import recent Granola team-folder notes as activities. |
| `list_pending_approvals` | Clark users | Show what's waiting for the user's approval. |
| `act_on_approval` | Clark users | Approve/reject a pending request by id (High-risk rejected server-side). |

`/health` now returns JSON including `inbound_enabled` and `last_successful_poll`.

**New environment variables** (see `.env.example`):

| Variable | Purpose |
|---|---|
| `CLARK_INBOUND_HMAC_SECRET` | Shared HMAC secret; signs/verifies all bus traffic. |
| `CLARK_WEBHOOK_URL` | Clark inbound webhook (or set per-mailbox in config). Prod → `https://clark.willcrestpartners.com/api/email/inbound`. |
| `CLARK_AUTHORIZED_SENDERS_URL` | Clark allow-list endpoint (or per-mailbox in config). Prod → `https://clark.willcrestpartners.com/api/email/authorized-senders`. |
| `INBOUND_DB_PATH` | SQLite path for idempotency/audit (default `/app/inbound.db`); local dev only. |
| `GATEWAY_TABLE` | DynamoDB table (`clark-email-gateway`) for dedup + daily send counts. When set (prod/Lambda) it replaces SQLite + in-memory state; unset locally. |
| `GATEWAY_SECRETS_ARN` | Secrets Manager ARN (`clark/email-gateway`); `bootstrap.py` loads its JSON keys into env at cold start. |
| `MCP_STATELESS_HTTP` | `true` in the Lambda web function (stateless MCP over the Function URL). |
| `PORT` | Port uvicorn listens on. `8080` locally/ECS and under Lambda (the LWA forwards Function URL requests to uvicorn on this port). |

**New config block** (`inbound` in APP_CONFIG_JSON / config.json):

```json
"inbound": {
  "enabled": true,
  "poll_seconds": 60,
  "mailboxes": [
    {
      "address": "clark@willcrestpartners.com",
      "destination": {
        "name": "clark-os",
        "webhook_url": "<or env CLARK_WEBHOOK_URL>",
        "authorized_senders_url": "<or env CLARK_AUTHORIZED_SENDERS_URL>"
      }
    }
  ]
}
```

In production this block lives in `APP_CONFIG_JSON` inside the Secrets Manager
secret. `poll_seconds` is retained for local dev; under Lambda the real cadence
is the EventBridge `rate(1 minute)` schedule on `clark-email-poller`.

---

## System Architecture

The gateway runs on **AWS Lambda** (SAM template `infra/template.yaml`): one
build artifact, two functions.

```
Claude Cowork (claude.ai)                Clark (clark.willcrestpartners.com)
        │                                        ▲   │
        │  MCP over HTTPS                envelope │   │ /send reply
        ▼                                (HMAC)   │   ▼  (HMAC)
Lambda Function URL ─► clark-email-web (uvicorn + Starlette under LWA: /mcp /send /health)
                                    │
EventBridge rate(1 min) ─► clark-email-poller (poller.poll_once, concurrency 1)
                                    │
        DynamoDB clark-email-gateway (dedup + daily counts)
        Secrets Manager clark/email-gateway (SA key, HMAC, APP_CONFIG_JSON)
                                    │  Gmail API (domain-wide delegation)
                                    ▼
                          Google Gmail (sends as clark@willcrestpartners.com)
```

**Component details:**

- **Claude Cowork** — Team members interact with Clark here. The Clark Email Tool connector must be enabled in each user's personal settings at claude.ai.
- **GitHub** (`WillcrestPartners/clark-email-send`) — Source of truth for all code.
- **AWS Lambda `clark-email-web`** — The Starlette app (MCP `/mcp` + `/send` relay + `/health`) run as a persistent **uvicorn** server (`python -m uvicorn lambda_web:app`) under the **AWS Lambda Web Adapter (LWA)**, exposed via a **Lambda Function URL**. MCP runs stateless (`MCP_STATELESS_HTTP=true`). The handler is `run.sh`; LWA is enabled via the adapter layer (`AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap`, `AWS_LWA_INVOKE_MODE=buffered`, `PORT=8080`). Mangum was dropped because it re-runs the ASGI lifespan per invocation and re-enters the MCP `StreamableHTTPSessionManager` (only enterable once), which broke every route after the first request.
- **AWS Lambda `clark-email-poller`** — Runs `poller.poll_once()` once per invocation, fired by an **EventBridge schedule** (`rate(1 minute)`), reserved concurrency 1. Handler `lambda_poll.handler`. The schedule is gated by the `PollerEnabled` CloudFormation parameter (deploy `false`, flip to `true` at cutover).
- **DynamoDB `clark-email-gateway`** (PK `pk`, GSI `rfc822-index`, TTL `ttl`) — Inbound Message-ID dedup (replaces SQLite) and per-user daily send counts (replaces in-memory). Selected automatically when `GATEWAY_TABLE` is set; local dev leaves it unset and keeps SQLite + in-memory.
- **AWS Secrets Manager `clark/email-gateway`** — JSON with `GOOGLE_SERVICE_ACCOUNT_JSON`, `CLARK_INBOUND_HMAC_SECRET`, `APP_CONFIG_JSON`, referenced by `GATEWAY_SECRETS_ARN`; `bootstrap.py` loads them into env at cold start. The HMAC value equals Clark's `clark/app` `CLARK_INBOUND_HMAC_SECRET` so signatures match.
- **AWS SAM** (`infra/template.yaml`) — Defines both functions, the Function URL, the EventBridge rule, the DynamoDB table, and IAM. `ClarkBaseUrl` parameter sets the Clark base URL (prod `https://clark.willcrestpartners.com`); deploy/cutover runbook in `notes/DEPLOY-LAMBDA.md`.
- **Google Cloud** (`willcrest-clark-email` project) — Hosts the `clark-email-sender` service account with domain-wide delegation enabled.
- **Google Workspace Admin** — Authorizes the service account client ID (`110661416084731877070`) to impersonate `clark@willcrestpartners.com` with Gmail scopes.

> **Legacy (pre-migration):** the gateway previously ran as a single always-on
> **AWS ECS Express** service (`clark-email-service`, cluster `default`) built from a
> Docker image in ECR via GitHub Actions on every push to `main`, doing all three
> jobs (MCP, `/send`, and the asyncio poll loop) in one container. Superseded by the
> Lambda architecture above.

---

## Adding a New User (e.g. Kristin)

Two things are required:

### 1. Add them to APP_CONFIG_JSON

Edit the `APP_CONFIG_JSON` value inside the Secrets Manager secret
`clark/email-gateway` and add an entry to the `users` object:

```json
"kristin@willcrest.com": {
  "name": "Kristin",
  "role": "user",
  "daily_limit": 20,
  "active": true
}
```

The functions read the secret at cold start, so the change takes effect on the
next cold start (or force one by redeploying the stack). *(Legacy ECS: this value
lived in the `clark-email-service` task definition and was applied by incrementing
`REDEPLOY`.)*

### 2. Enable the connector in their Claude account

The new user must go to **claude.ai → Settings → Connectors** and enable the **Clark Email Tool** connector for their account. The connector URL is the gateway's `/mcp` path:

```
https://cl-874b2f3a18c5475dbfbd921b886e8153.ecs.us-east-1.on.aws/mcp   (legacy ECS)
```

> **⚠️ Changing at cutover:** after the Lambda migration this becomes the new
> Lambda **Function URL** + `/mcp` (i.e. `<FunctionUrl>mcp`). Update the Cowork
> connector to the new URL once the stack is live.

After enabling, they should start a **new conversation** in Claude Cowork to load the tools.

---

## Configured Settings

### Environment Variables

Under Lambda, `GOOGLE_SERVICE_ACCOUNT_JSON` and `APP_CONFIG_JSON` come from the
Secrets Manager secret `clark/email-gateway` (loaded by `bootstrap.py` at cold
start); non-secret vars (`GATEWAY_TABLE`, `GATEWAY_SECRETS_ARN`, `ClarkBaseUrl`
→ `CLARK_*`, `MCP_STATELESS_HTTP`) are set by the SAM template.

*Legacy ECS environment variables (superseded):*

| Variable | Value | Notes |
|---|---|---|
| `SENDER_EMAIL` | `clark@willcrestpartners.com` | The Gmail address emails are sent from |
| `PORT` | `8080` | Port the MCP server listens on |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | *(service account key JSON)* | Full JSON key — now in Secrets Manager |
| `APP_CONFIG_JSON` | *(see below)* | User list and global settings — now in Secrets Manager |
| `REDEPLOY` | `1`, `2`, `3`… | Increment to force a new ECS deployment |

### APP_CONFIG_JSON Structure

```json
{
  "global": {
    "sender_email": "clark@willcrestpartners.com",
    "default_daily_limit": 20,
    "confirmation_required": true,
    "copy_to_sent_folder": true
  },
  "users": {
    "bforster@willcrest.com": {
      "name": "Bret Forster",
      "role": "admin",
      "daily_limit": 20,
      "active": true
    }
  }
}
```

| Setting | Current Value | Description |
|---|---|---|
| `default_daily_limit` | `20` | Max emails per user per day (resets at midnight or server restart) |
| `confirmation_required` | `true` | Claude always shows a preview before sending |
| `copy_to_sent_folder` | `true` | Sent emails are copied to clark's Gmail Sent folder |

### Google Service Account

| Setting | Value |
|---|---|
| Project | `willcrest-clark-email` |
| Service account | `clark-email-sender@willcrest-clark-email.iam.gserviceaccount.com` |
| Client ID | `110661416084731877070` |
| Authorized scopes | `https://www.googleapis.com/auth/gmail.send`, `https://www.googleapis.com/auth/gmail.modify`, `https://www.googleapis.com/auth/gmail.readonly` |
| Delegation authorized in | Google Workspace Admin → Security → API Controls → Domain-wide Delegation |
