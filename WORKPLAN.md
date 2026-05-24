# Email Application Workplan
## Sending Email from clark@willcrestpartners.com via Claude Code & Cowork

---

## Decisions Recorded

| Question | Your Answer |
|----------|-------------|
| Who can send? | All 4 team members (2 coding, 2 non-coding) |
| Access model | Per-individual grant — admin controls who is active |
| Sent folder | Yes — sent emails appear in Gmail Sent folder |
| Daily limit | Default 20/day, configurable per user |
| Recipient restriction | None — any email address allowed |
| Confirmation required | Yes — always show preview, require "yes" before sending |
| Where to use it | Claude Cowork (AI project interface) AND Claude Code |

---

## What We're Building

A **Clark email tool** that team members can invoke inside a Claude Cowork project
or Claude Code session. Clark (your AI assistant) can compose and send emails from
`clark@willcrestpartners.com` with human confirmation at every step.

An **admin dashboard** accessible to authorized admins shows:
- Who has access and whether they are active
- Per-user daily limits and how many emails they've sent today
- An audit log of every send attempt (success or failure, with reason)
- All configurable settings in one place

---

## The Plain-English Architecture

This is the updated architecture. The key addition is the **MCP Server** — explained below.

```
Team member (in Claude Cowork project or Claude Code)
      │
      │  "Clark, email this to John"  OR  /send-email
      ▼
┌──────────────────────────────────────┐
│         Clark (Claude AI)             │  ← your AI assistant in Cowork
│   knows about the email tool and      │
│   asks you the confirmation questions │
└──────────────┬───────────────────────┘
               │  calls tool via MCP
               ▼
┌──────────────────────────────────────┐
│         MCP Server (Python)           │  ← always-running small program
│   Tools exposed:                      │     hosted on a server (see Phase 1)
│   • send_email                        │
│   • show_dashboard (admin only)       │
│   • check_my_access                   │
└──────────────┬───────────────────────┘
               │  reads config + writes logs
               ▼
┌──────────────────────────────────────┐
│   config.json  +  audit_log.jsonl    │  ← who has access, limits, all settings,
│   (on the server)                    │     complete history of every email
└──────────────┬───────────────────────┘
               │  authenticates with Google
               ▼
┌──────────────────────────────────────┐
│   Gmail API (Service Account)         │  ← sends from clark@willcrestpartners.com
│                                       │     adds to Gmail Sent folder
└──────────────────────────────────────┘
```

---

## New Concept: What is an MCP Server?

**MCP stands for Model Context Protocol.** It is an open standard (created by Anthropic)
that lets Claude talk to external tools and services.

Think of it like a **plugin system**. When you add an MCP server to a Claude Cowork
project, Claude gains new abilities — in this case, the ability to send email.

The MCP server is a small Python program that:
1. Runs continuously on a server (like a small web service)
2. Exposes "tools" that Claude can call (send_email, show_dashboard, etc.)
3. Handles authentication so Claude never touches your credentials directly

**Without an MCP server**, this tool only works when you manually run a script from
a terminal. **With an MCP server**, Clark can use it seamlessly inside a Cowork
project conversation.

---

## New Concept: Service Account vs. Personal OAuth

In the first draft, each user would have logged in with their own Google account.
That approach doesn't scale well across four people.

Instead, we will use a **Google Service Account** — a special Google identity that
belongs to the application (not to any person). Think of it like a staff badge issued
to "Clark the AI Assistant" rather than to any individual employee.

The service account gets one permission: send email on behalf of
`clark@willcrestpartners.com`. No inbox access, no deletions.

**Security improvement:** The service account credential file lives only on the MCP
server. Team members never handle Google credentials at all. Access is controlled by
our own user list, not by Google.

---

## New Concept: The Admin Dashboard

Instead of a separate web app, the dashboard is built into the MCP server as a tool.
When you (as admin) ask Clark "show me the email dashboard," Clark calls the
`show_dashboard` tool and displays something like:

```
CLARK EMAIL TOOL — ADMIN DASHBOARD
====================================
Sender:       clark@willcrestpartners.com
Global limit: 20 emails/user/day

AUTHORIZED USERS
─────────────────────────────────────────────────────────
User                        Role    Limit  Sent Today  Active
bforster@willcrest.com      admin   20     3           ✓
developer@willcrest.com     user    20     1           ✓
user2@willcrest.com         user    10     0           ✓
user3@willcrest.com         user    10     0           ✗  (suspended)

RECENT ACTIVITY (last 10)
─────────────────────────────────────────────────────────
2026-05-24 14:32  bforster  → john@example.com         ✓ sent
2026-05-24 11:15  user2     → mary@example.com         ✓ sent
2026-05-23 16:44  user3     → external@co.com          ✗ FAILED: user suspended

SETTINGS
─────────────────────────────────────────────────────────
Confirmation required:  yes
Recipient restriction:  none
Sent folder:            yes
```

This means you never need to edit a config file manually — you can ask Clark to make
changes and it updates the config on your behalf.

---

## Software You Need (Updated)

| What | Cost | Why You Need It |
|------|------|-----------------|
| **Google Cloud Console** (free) | Free | Issues the Service Account credential |
| **Python 3** | Free | The language the MCP server is written in |
| **A hosting service** | ~$5–7/month | Keeps the MCP server running 24/7 |
| **Claude Code** | Already have | For developers |
| **Claude Cowork/Teams** | Already have | For all 4 users |
| **GitHub** | Already have | Stores the code |

### Hosting Options (You Need to Choose One)

The MCP server needs to run somewhere that is always on and reachable by Claude.ai.
Here are your options from simplest to most complex:

| Option | Cost | Complexity | Notes |
|--------|------|------------|-------|
| **Railway.app** | ~$5/mo | Very simple | Recommended for getting started. Connects to GitHub, deploys automatically. |
| **Render.com** | Free–$7/mo | Simple | Free tier spins down when idle (causes slow first connection) |
| **Google Cloud Run** | Pay per use | Moderate | Integrates naturally with Google APIs; good long-term choice |
| **A team member's computer** | Free | None | Only works when that computer is on. Not recommended for business use. |

**Recommendation:** Start with Railway. It is the fastest path from zero to running.
You connect it to your GitHub account, point it at this repo, and it handles the rest.
When you're comfortable, migrating to Google Cloud Run later is straightforward.

> **Hosting decision needed before Phase 2 coding begins.** See the open question at
> the bottom of this document.

---

## Security Guardrails (Updated)

| Guardrail | Status | Description |
|-----------|--------|-------------|
| Credentials never in code | ✓ Built in | Service account key is an environment variable on the server |
| Minimum permissions | ✓ Built in | `gmail.send` scope only + `gmail.readonly` to write to Sent folder |
| Per-user access control | ✓ Built in | Each user must be in the authorized list with `active: true` |
| Daily send limit | ✓ Built in | Per-user configurable, default 20 |
| Confirmation before send | ✓ Built in | Always shown, always required |
| Full audit log | ✓ Built in | Every attempt logged with user, recipient, timestamp, result |
| Admin-only dashboard | ✓ Built in | `show_dashboard` checks that the caller has admin role |
| No mass email | ✓ Built in | Tool only accepts a single recipient (no CC/BCC lists for now) |
| Sent folder copy | ✓ Built in | Sent emails appear in Gmail for accountability |

---

## The User Config File (Plain English)

The access control lives in a file called `config.json` on the server. It looks like this:

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
      "name": "Your Name",
      "role": "admin",
      "daily_limit": 20,
      "active": true
    },
    "developer@willcrest.com": {
      "name": "Developer",
      "role": "user",
      "daily_limit": 20,
      "active": true
    }
  }
}
```

To suspend someone's access, change `"active": false`. To change their limit, change
the number. You can do this by asking Clark directly in a Cowork project ("suspend
access for user3") or by editing the file manually on the server.

---

## Audit Log (Plain English)

Every email attempt — successful or not — is written to a log file:

```
{"time": "2026-05-24T14:32:01", "user": "bforster@willcrest.com", "to": "john@example.com", "subject": "Project update", "status": "sent"}
{"time": "2026-05-24T11:15:44", "user": "user3@willcrest.com", "to": "x@y.com", "subject": "Test", "status": "failed", "reason": "user suspended"}
```

If someone asks "why didn't my email send?" you can look here for the exact reason.

---

## Phase-by-Phase Implementation Plan (Updated)

---

### Phase 1 — Hosting & Google Cloud Setup (~1.5 Hours)
**You and/or your developer do this in a browser.**

**Step 1.1 — Choose and sign up for Railway**
- Go to `railway.app` and sign up with your GitHub account
- This links Railway to your GitHub repos

**Step 1.2 — Google Cloud Project + Service Account**
- Go to `console.cloud.google.com`
- Create a project called `willcrest-email-tool`
- Enable the **Gmail API**
- Create a **Service Account** (not OAuth — this is the change from the original plan)
  - Name it something like `clark-email-sender`
  - Download the JSON key file — store it safely, never upload to GitHub
- In Google Workspace Admin (`admin.google.com`):
  - Go to Security → API Controls → Domain-wide Delegation
  - Add the service account with scope: `https://www.googleapis.com/auth/gmail.send`
  - This is the step that says "this service account is allowed to send as clark@"

**Step 1.3 — Configure the MCP Server's environment variables on Railway**
- In Railway, add these environment variables (not in code):
  - `GOOGLE_SERVICE_ACCOUNT_JSON` — paste the entire contents of the key JSON file
  - `SENDER_EMAIL` — `clark@willcrestpartners.com`

---

### Phase 2 — MCP Server Code (~3–4 Hours)
**Your developer writes this.**

Files to build:
```
email_tool/
├── server.py          ← the MCP server (replaces the old simple script)
├── gmail_client.py    ← handles Gmail API calls
├── access_control.py  ← checks user permissions and daily limits
├── audit_log.py       ← writes every attempt to the log
├── config.py          ← reads and updates config.json
├── config.json        ← the user access list (not in GitHub — server only)
├── requirements.txt   ← updated with MCP library
└── Procfile           ← tells Railway how to start the server
```

---

### Phase 3 — Connect to Claude Cowork (~30 Minutes)
**You do this in your Claude Cowork project settings.**

- In your Cowork project, go to Settings → Tools/Integrations
- Add a new MCP server
- Enter the URL of your Railway deployment
- Claude (Clark) will now have access to the email tools

---

### Phase 4 — Team Onboarding (~30 Minutes)
**Each user does a one-time setup.**

Non-coding users need only:
1. Access to the Claude Cowork project where Clark lives
2. Their email address added to `config.json` by the admin

That's it. No Python, no credentials, no terminal.

---

### Phase 5 — Testing Checklist

- [ ] Admin can see the dashboard
- [ ] Admin can send a test email (appears in Gmail Sent folder)
- [ ] Non-coding user can send a test email
- [ ] Suspended user cannot send (sees clear error)
- [ ] Daily limit blocks sends after limit is hit
- [ ] Failed attempts appear in audit log with correct reason
- [ ] Confirmation preview shows before every send

---

## Open Question — Hosting Decision

**Before your developer starts Phase 2, decide:** Railway.app or another option?

Railway is recommended. If you have a strong preference for another platform or are
already paying for a cloud service, let us know and we can target that instead.

---

## Glossary (Additions)

- **MCP Server** — A small always-running program that exposes tools to Claude.
  Think of it as Clark's "hands" — without it, Clark can talk but can't act.
- **Service Account** — A Google identity for an application rather than a person.
  It has a key file (JSON) that the app uses to authenticate.
- **Domain-wide Delegation** — A Google Workspace setting that says "this service
  account is allowed to act on behalf of users in this domain."
- **Environment Variable (on a server)** — Same concept as on your laptop, but stored
  in Railway's secure settings panel instead of a file on your computer.
- **Audit Log** — A running record of every action taken, stored as a file. Like a
  bank statement for your email tool.
- **Railway / Render** — Hosting platforms that make it simple to run a small web
  service. They watch your GitHub repo and automatically redeploy when code changes.
