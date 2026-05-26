# Clark Email Tool — Workplan
## Sending Email from clark@willcrestpartners.com via Claude Cowork

---

## Who This Is For

This workplan assumes:
- **You and one associate** are learning to build with AI assistance ("vibe coding") — no
  prior coding experience required. Claude guides you through every step.
- **Two other team members** are non-technical. Once Clark is set up, they interact
  entirely through Claude Cowork — no terminals, no code, no credentials.
- **Nobody** is a professional developer. Every step in this document is written
  accordingly.

---

## How Vibe Coding Works Here

You are not writing code from scratch. Instead:
1. You describe what you want in plain English (in Claude Code or Claude Cowork)
2. Claude writes the code
3. You review what was written (Claude explains each piece)
4. You approve it and Claude runs or deploys it

Think of Claude as a contractor who builds exactly what you specify, explains every
decision, and asks before doing anything risky. Your job is to understand what's
being built well enough to make good decisions — not to write the code yourself.

---

## Decisions Recorded

| Question | Your Answer |
|----------|-------------|
| Who can send email? | All 4 team members |
| Access model | Per-individual grant — admin controls who is active |
| Sent folder | Yes — appears in Gmail Sent folder |
| Daily limit | Default 20/day per user, configurable |
| Recipient restriction | None — any email address allowed |
| Confirmation required | Yes — always show preview, require approval before sending |
| Where to use it | Claude Cowork (primary) and Claude Code |
| Hosting | AWS ECS Express Mode (using existing AWS account) |

---

## Team Members

| Name | Email | Role | Active |
|------|-------|------|--------|
| Bret Forster | bforster@willcrest.com | admin | yes |
| Dominic Naas | dnaas@willcrest.com | user | yes |
| Patricia Villegas | pvillegas@willcrest.com | user | yes |
| Kristin Osborn | kosborn@willcrest.com | user | **no — see note below** |

### Kristin Osborn — Account Inactive

Kristin's account (kosborn@willcrest.com) was set to `active: false` at initial
deployment because her email address was not yet set up.

**To activate her when ready:**
1. Go to AWS Console → ECS → Clusters → default → clark-email-service
2. Click the service → Update service → Environment variables
3. Find APP_CONFIG_JSON and change `"active":false` to `"active":true` for kosborn@willcrest.com
4. Save and redeploy

Alternatively, ask Claude Code to update APP_CONFIG_JSON and redeploy.

---

## Plain-English Architecture (Final)

```
You or a team member (in Claude Cowork)
        │
        │  "Send an email to ..."
        ▼
   Claude AI (claude.ai/cowork)
        │
        │  calls MCP tool: send_email(...)
        ▼
   Clark MCP Server  ◄─── running on AWS ECS
   (server.py)             (container from ECR)
        │
        ├── checks access_control.py  (who's allowed, daily limits)
        ├── writes to audit_log.py    (CloudWatch + local file)
        │
        │  if confirmed=True:
        ▼
   Gmail API  ──►  sends from clark@willcrestpartners.com
                   copies to Sent folder
```

---

## Security Guardrails Built In

1. **Access control** — only named users can trigger sends
2. **Daily limits** — per-user cap (default 20), configurable per person
3. **Confirmation gate** — every send requires explicit human approval
4. **Audit trail** — every attempt (success or failure) logged permanently to CloudWatch
5. **Minimum Gmail scope** — service account can only send, not read or delete mail
6. **No credentials in GitHub** — all secrets stored as environment variables in AWS
7. **Admin-only dashboard** — only Bret can see usage stats and user settings

---

## Phase 1: Google Cloud Setup (COMPLETE)

### 1.1 Create Google Cloud Project
- Project name: `willcrest-clark-email`
- Project ID: `willcrest-clark-email`
- Organization: willcrest.com

### 1.2 Enable Gmail API
- Go to APIs & Services → Library → search "Gmail API" → Enable

### 1.3 Create Service Account
- Name: `clark-email-sender`
- Email: `clark-email-sender@willcrest-clark-email.iam.gserviceaccount.com`
- Key: downloaded as JSON, stored in willcrest-secrets folder (NOT in GitHub)
- Org policy override required: iam.disableServiceAccountKeyCreation set to
  "Not enforced" at project level, then re-enabled after key download

### 1.4 Configure Domain-Wide Delegation
- Client ID: `110661416084731877070`
- Scope granted in Google Workspace Admin: `https://www.googleapis.com/auth/gmail.send`
- This allows the service account to send email as clark@willcrestpartners.com

---

## Phase 2: AWS Setup (COMPLETE)

### IAM User
- Username: `clark-email-admin`
- Permissions: AmazonEC2ContainerRegistryFullAccess, AmazonECS_FullAccess
- Access keys: stored as GitHub secrets (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)

### ECR Repository
- URI: `626928146978.dkr.ecr.us-east-1.amazonaws.com/willcrestpartners/clark-email-send`
- Region: us-east-1

---

## Phase 3: CI/CD Pipeline (COMPLETE)

GitHub Actions workflow (`.github/workflows/deploy.yml`) triggers on every push
to `main`. It:
1. Builds a Docker image from the Dockerfile
2. Pushes it to ECR with the tag `:latest`

First successful build confirmed. Future code changes auto-deploy when pushed to main.

---

## Phase 4: ECS Deployment (IN PROGRESS)

### ECS Express Mode settings
- Image URI: `626928146978.dkr.ecr.us-east-1.amazonaws.com/willcrestpartners/clark-email-send:latest`
- Service name: `clark-email-service`
- Container port: `8080`
- CPU: `0.25 vCPU`
- Memory: `0.5 GB`
- Max tasks: `2`

### Environment variables set in ECS
| Key | Value |
|-----|-------|
| SENDER_EMAIL | clark@willcrestpartners.com |
| GOOGLE_SERVICE_ACCOUNT_JSON | (full service account JSON) |
| APP_CONFIG_JSON | (full config JSON with all 4 users) |
| PORT | 8080 |

---

## Phase 5: Connect to Claude Cowork (PENDING)

Once ECS is running and you have the service URL:
1. Open Claude Cowork
2. Go to project settings → MCP Servers → Add server
3. Enter the ECS service URL
4. Test with: "Clark, check my access"

---

## Glossary

| Term | Plain-English Meaning |
|------|----------------------|
| MCP Server | A small program that gives Claude new abilities (tools) |
| FastMCP | The Python library used to build MCP servers quickly |
| Service Account | A Google "robot account" the app uses to send email |
| Domain-wide delegation | Permission for the robot account to act as clark@willcrestpartners.com |
| ECR | Amazon's private storage for Docker container images |
| ECS | Amazon's service for running containers in the cloud |
| Docker | Packages the app + all its dependencies into one portable unit |
| GitHub Actions | Automated pipeline: push code → build Docker image → push to ECR |
| Environment variable | A secret setting passed to the app at runtime, never stored in code |
| CloudWatch | AWS's logging service — captures everything the app prints |
| APP_CONFIG_JSON | The user access list and global settings, stored as an env var |
