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

**Poll loop:** when `inbound.enabled` is true, a background asyncio task runs every
`inbound.poll_seconds` (default 300). Each sweep lists unread inbox mail and, per
message: dedupes by RFC822 Message-ID (SQLite), drops unknown senders (allow-list
fetched from Clark, cached 300s) and unhealthy mail (auto-replies, bulk/list mail,
bounces, self-loops — all deterministic), then POSTs an **envelope v1** to Clark's
inbound webhook with an `X-Clark-Signature: sha256=<hmac>` header over the raw body.
On a 2xx ack the message is marked read; otherwise it is left unread for retry.

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

`/health` now returns JSON including `inbound_enabled` and `last_successful_poll`.

**New environment variables** (see `.env.example`):

| Variable | Purpose |
|---|---|
| `CLARK_INBOUND_HMAC_SECRET` | Shared HMAC secret; signs/verifies all bus traffic. |
| `CLARK_WEBHOOK_URL` | Clark inbound webhook (or set per-mailbox in config). |
| `CLARK_AUTHORIZED_SENDERS_URL` | Clark allow-list endpoint (or per-mailbox in config). |
| `INBOUND_DB_PATH` | SQLite path for idempotency/audit (default `/app/inbound.db`). |

**New config block** (`inbound` in APP_CONFIG_JSON / config.json):

```json
"inbound": {
  "enabled": true,
  "poll_seconds": 300,
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

---

## System Architecture

```
Claude Cowork (claude.ai)
        │
        │  MCP over HTTPS
        ▼
AWS ECS Express  ←──── GitHub Actions (builds & pushes image on every push to main)
(clark-email-service)        │
        │                    └── AWS ECR (Docker image registry)
        │  Gmail API (domain-wide delegation)
        ▼
Google Gmail
(sends as clark@willcrestpartners.com)
```

**Component details:**

- **Claude Cowork** — Team members interact with Clark here. The Clark Email Tool connector must be enabled in each user's personal settings at claude.ai.
- **GitHub** (`WillcrestPartners/clark-email-send`) — Source of truth for all code. Every push to `main` triggers a GitHub Actions build.
- **GitHub Actions** (`.github/workflows/deploy.yml`) — Builds the Docker image and pushes it to AWS ECR automatically.
- **AWS ECR** — Stores the Docker image at `626928146978.dkr.ecr.us-east-1.amazonaws.com/willcrestpartners/clark-email-send:latest`.
- **AWS ECS Express** (`clark-email-service`) — Runs the server container. To deploy a new image, increment the `REDEPLOY` environment variable to force a task replacement.
- **Google Cloud** (`willcrest-clark-email` project) — Hosts the `clark-email-sender` service account with domain-wide delegation enabled.
- **Google Workspace Admin** — Authorizes the service account client ID (`110661416084731877070`) to impersonate `clark@willcrestpartners.com` with Gmail scopes.

---

## Adding a New User (e.g. Kristin)

Two things are required:

### 1. Add them to APP_CONFIG_JSON in ECS

In the AWS ECS console, go to the `clark-email-service` → Update service → Environment variables → edit the `APP_CONFIG_JSON` value. Add an entry to the `users` object:

```json
"kristin@willcrest.com": {
  "name": "Kristin",
  "role": "user",
  "daily_limit": 20,
  "active": true
}
```

Save and increment `REDEPLOY` to apply the change.

### 2. Enable the connector in their Claude account

The new user must go to **claude.ai → Settings → Connectors** and enable the **Clark Email Tool** connector for their account. The connector URL is:

```
https://cl-874b2f3a18c5475dbfbd921b886e8153.ecs.us-east-1.on.aws/mcp
```

After enabling, they should start a **new conversation** in Claude Cowork to load the tools.

---

## Configured Settings

### ECS Environment Variables

| Variable | Value | Notes |
|---|---|---|
| `SENDER_EMAIL` | `clark@willcrestpartners.com` | The Gmail address emails are sent from |
| `PORT` | `8080` | Port the MCP server listens on |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | *(service account key JSON)* | Full JSON key — stored securely in ECS, never in GitHub |
| `APP_CONFIG_JSON` | *(see below)* | User list and global settings |
| `REDEPLOY` | `1`, `2`, `3`… | Increment to force a new deployment |

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
