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
| Hosting | AWS App Runner (using existing AWS account) |

---

## Plain-English Architecture (Final)

```
You or a team member (in Claude Cowork)
        │
        │  "Clark, send this email to John"
        ▼
┌───────────────────────────────────────┐
│          Clark (Claude AI)             │  Your AI assistant inside Cowork.
│  Asks questions, shows preview,        │  Understands natural language.
│  waits for your "yes" before acting.   │
└───────────────┬───────────────────────┘
                │  calls the email tool via MCP
                │  (secure internet connection)
                ▼
┌───────────────────────────────────────┐
│      MCP Server on AWS App Runner      │  A small always-running program
│  • Checks: are you authorized?         │  in your AWS account.
│  • Checks: daily limit OK?             │  AWS App Runner keeps it running
│  • Logs the attempt                    │  and gives it a secure web address.
│  • Calls Gmail on your behalf          │
└───────────────┬───────────────────────┘
                │  reads config, writes logs
                ▼
┌───────────────────────────────────────┐
│  config.json — your user access list   │  Who can send, what limits, settings.
│  audit_log.jsonl — the paper trail     │  Every attempt recorded here.
└───────────────┬───────────────────────┘
                │  authenticates with Google
                ▼
┌───────────────────────────────────────┐
│  Gmail API (Google Service Account)    │  Sends from clark@willcrestpartners.com
│                                        │  Copies to Gmail Sent folder.
└───────────────────────────────────────┘
```

---

## What is AWS App Runner?

AWS App Runner is Amazon's simplest hosting service. You give it your GitHub
repository and it handles everything else:
- Builds your application automatically
- Gives it a secure `https://` web address
- Keeps it running 24/7
- Automatically redeploys when you push new code to GitHub

**Cost:** Approximately $5–10/month for this project's traffic level.

You already have an AWS account, so there's no new signup needed. The setup
involves clicking through the AWS web console — no command-line work.

---

## Security Guardrails Summary

| Guardrail | What It Prevents |
|-----------|-----------------|
| IAM user with limited permissions | A mistake in this project can't affect other things in your AWS account |
| Billing alert at $20 | You get an email if costs unexpectedly spike |
| Service Account (not your password) | Google credentials are never exposed to team members |
| config.json not in GitHub | User access list never accidentally published |
| Per-user active flag | Suspend anyone's access instantly without touching Google |
| Daily send limit | Accidental loops or mistakes can't send hundreds of emails |
| Confirmation gate | Clark always shows a preview and waits for "yes" |
| Audit log | Every attempt recorded — always know who sent what and why it failed |
| Single recipient only | No mass-email capability — tool only accepts one `to` address |
| `gmail.send` scope only | The service account cannot read, delete, or manage your Gmail inbox |

---

## Phase 0 — AWS Safety Setup
### Time: ~30 minutes | Who: You | Where: AWS website (browser)

Do this before anything else. It protects your AWS account from surprise costs
and limits the blast radius if anything goes wrong.

---

**Step 0.1 — Set Up a Billing Alert**

This emails you if your AWS bill exceeds a threshold. Takes 5 minutes.

1. Log in to `console.aws.amazon.com`
2. In the top search bar, type **Billing** and click "Billing and Cost Management"
3. In the left sidebar, click **Budgets**
4. Click the orange **Create budget** button
5. Choose **Use a template** → select **Monthly cost budget**
6. Set the budget amount to **$20**
7. Enter your email address for notifications
8. Click **Create budget**

You will now receive an email if your AWS charges approach $20 in a month.
For this project, your bill should be well under $10/month.

---

**Step 0.2 — Create an IAM User for This Project**

Your AWS root account (the one you signed up with) has unlimited power.
We create a limited sub-account ("IAM user") for this project so that a mistake
here cannot affect anything else in your AWS account.

1. In the AWS search bar, type **IAM** and click "IAM"
2. In the left sidebar, click **Users**
3. Click the orange **Create user** button
4. Username: `clark-email-tool`
5. Check the box: **Provide user access to the AWS Management Console** → No
   (this user is for programmatic access only)
6. Click **Next**
7. On the permissions page, choose **Attach policies directly**
8. Search for and check: `AWSAppRunnerFullAccess`
9. Search for and check: `AmazonEC2ContainerRegistryFullAccess`
10. Click **Next** → **Create user**
11. Click on the new user → **Security credentials** tab → **Create access key**
12. Choose **Application running outside AWS**
13. Download the CSV file — store it safely. This is the user's credential.
    **Do not upload this to GitHub.**

> **Why this matters:** If you ever need to revoke this user's access (e.g., if
> the credentials were accidentally exposed), you can delete it in IAM without
> affecting your main AWS account or any other services.

---

## Phase 1 — Google Cloud Setup
### Time: ~45 minutes | Who: You | Where: Google Cloud website (browser)

---

**Step 1.1 — Create a Google Cloud Project**

1. Go to `console.cloud.google.com`
2. Sign in with your **Google Workspace admin account**
   (the account that manages `willcrestpartners.com` — not your personal Gmail)
3. At the top of the page, click the project dropdown (it may say "Select a project")
4. Click **New Project**
5. Project name: `willcrest-clark-email`
6. Click **Create**
7. Wait a few seconds, then click the dropdown again and select your new project

---

**Step 1.2 — Enable the Gmail API**

1. In the search bar at the top, type **Gmail API**
2. Click the result that says "Gmail API" under APIs & Services
3. Click the blue **Enable** button
4. Wait for it to activate (takes about 10 seconds)

---

**Step 1.3 — Create a Service Account**

A service account is an identity for your application — not a person.
Think of it as a staff badge issued to "Clark" rather than to any employee.

1. In the left sidebar, click **APIs & Services** → **Credentials**
2. Click **+ Create Credentials** → **Service account**
3. Service account name: `clark-email-sender`
4. Service account ID: leave as auto-filled
5. Description: `Sends email from clark@willcrestpartners.com on behalf of the team`
6. Click **Create and continue**
7. Skip the optional role/access steps — click **Done**
8. You'll see your new service account listed. Click on its email address.
9. Click the **Keys** tab → **Add Key** → **Create new key**
10. Choose **JSON** → **Create**
11. A JSON file downloads automatically — this is your service account key.
    **Store it safely. Never upload it to GitHub.**

---

**Step 1.4 — Note the Service Account's "Client ID"**

1. While still on the service account page, click the **Details** tab
2. Copy the number shown as **Unique ID** — you'll need it in the next step.
   It looks something like: `108234567890123456789`

---

**Step 1.5 — Grant Gmail Send Permission in Google Workspace Admin**

This is the step that tells Google: "Clark's service account is allowed to
send email as clark@willcrestpartners.com."

1. Open a new browser tab and go to `admin.google.com`
2. Sign in with your Google Workspace admin account
3. Click **Security** → **Access and data control** → **API controls**
4. Scroll down to **Domain-wide delegation** → click **Manage domain-wide delegation**
5. Click **Add new**
6. Client ID: paste the Unique ID you copied in Step 1.4
7. OAuth scopes: `https://www.googleapis.com/auth/gmail.send`
8. Click **Authorize**

> **What you just did:** You told Google Workspace that Clark's service account
> is authorized to send email on behalf of users in your domain, but only to send —
> not to read, delete, or do anything else.

---

**Step 1.6 — Save the Service Account JSON Somewhere Safe**

Before moving on, make sure you know where the JSON key file is on your computer.
You'll need it in Phase 3 when you configure the AWS App Runner environment.

A safe place: a folder called `willcrest-secrets` in a location that is NOT
inside this GitHub repository folder. Do not put it in the `Test` folder.

---

## Phase 2 — Deploy to AWS App Runner
### Time: ~1.5 hours | Who: You (with Claude guiding) | Where: AWS + Claude Code

---

**Step 2.1 — Add a Dockerfile to the Repository**

A Dockerfile is a recipe that tells AWS how to build and run the application.
Claude has already created this file in the repository. You don't need to edit it.

---

**Step 2.2 — Create the App Runner Service**

1. Log in to `console.aws.amazon.com`
2. In the search bar, type **App Runner** and click the result
3. Click **Create service**
4. Source: choose **Source code repository**
5. Click **Add new** next to the GitHub connection
   - Authorize AWS to access your GitHub account
   - Select the `WillcrestPartners/Test` repository
   - Branch: `main` (we'll merge our branch before this step)
6. **Build settings:**
   - Configuration file: choose **Use a configuration file** (it will find the
     `apprunner.yaml` file Claude created)
7. Click **Next**
8. Service name: `clark-email-tool`
9. **Environment variables** — click "Add environment variable" for each of these:

   | Key | Value |
   |-----|-------|
   | `SENDER_EMAIL` | `clark@willcrestpartners.com` |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | *paste the entire contents of your JSON key file here* |
   | `DEFAULT_DAILY_LIMIT` | `20` |

   > **How to paste the JSON:** Open the key file in a text editor (Notepad on
   > Windows, TextEdit on Mac). Select all, copy. Paste it as the value for
   > `GOOGLE_SERVICE_ACCOUNT_JSON`. It will look like a long block of text
   > starting with `{` and ending with `}`.

   > **Security note:** AWS stores these as encrypted environment variables.
   > They are never visible in your GitHub repository. This is the safe way to
   > store secrets on a server.

10. Port: `8080`
11. Click **Next** → **Create and deploy**
12. Wait 3–5 minutes for the first deployment to complete (status changes to "Running")
13. Copy the **Default domain** shown — it looks like:
    `https://xxxxxxxxxx.us-east-1.awsapprunner.com`
    This is your MCP server's address.

---

**Step 2.3 — Create Your config.json on the Server**

The `config.json` file stores your team's access list. It is not in GitHub
(protected by `.gitignore`) so it must be set as an environment variable on the server.

1. Open `email_tool/config.json.example` in this repository
2. Make a copy of it and fill in your team's real email addresses and names
3. In AWS App Runner, add one more environment variable:

   | Key | Value |
   |-----|-------|
   | `APP_CONFIG_JSON` | *paste the entire contents of your filled-in config here* |

4. Click **Deploy** to redeploy with the new variable

> Claude will guide you through this step interactively when you reach it.

---

## Phase 3 — Connect Clark to the Email Tool
### Time: ~20 minutes | Who: You | Where: Claude Cowork project settings

---

**Step 3.1 — Add the MCP Server to Your Cowork Project**

1. Open the Claude Cowork project where Clark lives
2. Go to **Project Settings** (gear icon or settings menu)
3. Find the section for **Integrations** or **MCP Servers** or **Tools**
   (the exact label depends on your Cowork version)
4. Click **Add MCP Server**
5. Name: `Clark Email Tool`
6. URL: paste your App Runner URL from Step 2.2, plus `/mcp` at the end:
   `https://xxxxxxxxxx.us-east-1.awsapprunner.com/mcp`
7. Save

Clark will now have the email tool available. You can test it by saying:
"Clark, check my email access" — Clark will call the `check_my_access` tool
and confirm whether your account is active.

---

## Phase 4 — Testing Checklist
### Time: ~30 minutes | Who: Everyone

Work through these in order. Don't skip to the next until the current one passes.

- [ ] Admin asks: "Clark, show me the email dashboard" — dashboard displays
- [ ] Admin sends a test email to themselves — appears in Gmail Sent folder
- [ ] Non-coding team member asks Clark to send a test email — works
- [ ] Admin suspends a user in config, that user tries to send — sees clear error
- [ ] Same user tries again after being re-activated — works again
- [ ] Send 20 emails in a day (can be quick tests to yourself) — 21st is blocked
- [ ] Check the audit log — all 21 attempts are recorded, last one shows "limit reached"

---

## Ongoing: How to Make Changes

Once this is running, here is how you make common changes:

**To add a new user:**
Ask Clark: *"Add sarah@willcrestpartners.com to the email tool with a daily limit of 10."*

**To suspend someone:**
Ask Clark: *"Suspend email access for john@willcrestpartners.com."*

**To see what's been sent:**
Ask Clark: *"Show me the email dashboard."*

**To change someone's daily limit:**
Ask Clark: *"Change the daily limit for sarah to 15."*

**To update the code (e.g., add a feature):**
Open Claude Code, describe what you want, Claude writes and deploys it.

---

## Full File List (What's in the Repository)

```
Test/
├── WORKPLAN.md                    ← this document
├── Dockerfile                     ← recipe for AWS to build the app
├── apprunner.yaml                 ← AWS App Runner configuration
├── Procfile                       ← alternative start instruction
├── .env.example                   ← template for environment variables
├── .gitignore                     ← what GitHub will NOT store (secrets, logs)
└── email_tool/
    ├── server.py                  ← the MCP server (Clark's "hands")
    ├── gmail_client.py            ← Gmail API calls
    ├── access_control.py          ← who can send, daily limits
    ├── audit_log.py               ← records every attempt
    ├── config.json.example        ← template for your user list
    └── requirements.txt           ← Python libraries needed
```

**Files that exist only on the server (never in GitHub):**
- `config.json` — your live user access list
- `audit_log.jsonl` — the running log of all email activity

---

## Glossary

| Term | Plain English |
|------|---------------|
| **AWS App Runner** | Amazon's simplest hosting service. You give it your GitHub repo; it handles building, running, and securing your app. |
| **IAM User** | A limited sub-account inside your main AWS account. Like giving a contractor a keycard that only opens certain doors. |
| **Billing Alert** | An AWS feature that emails you when costs exceed a threshold. |
| **MCP Server** | A small program that gives Claude new abilities — in this case, the ability to send email. |
| **Service Account** | A Google identity for an application rather than a person. Has a key file instead of a password. |
| **Domain-wide Delegation** | A Google Workspace setting that says "this service account may act on behalf of users in this domain." |
| **Environment Variable** | A secret stored in a platform's settings panel rather than in code. Never ends up in GitHub. |
| **Dockerfile** | A recipe file that tells a hosting service how to build and run your application. |
| **Audit Log** | A permanent record of every action taken. One line per attempt. |
| **config.json** | Your live user list — who has access, what limits, all settings. |
| **Vibe coding** | Building software by describing what you want in plain English and letting Claude write the code. You stay in control; Claude handles the technical execution. |
