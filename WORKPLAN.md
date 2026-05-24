# Email Application Workplan
## Sending Email from claude@willcrestpartners.com via Claude Code

---

## What We're Building

A custom Claude Code **skill** (a slash command you type like `/send-email`) that walks you
through sending an email from `clark@willcrestpartners.com`. When you invoke it, Claude will
ask you:

1. Who to send it to
2. The subject line
3. The message body

Then it sends the email and confirms it was delivered.

---

## The Plain-English Architecture

Think of it as three layers, each doing one job:

```
You (in Claude Code)
      │
      │  type:  /send-email
      ▼
┌─────────────────────┐
│   Claude Code Skill  │  ← the "menu" that asks you questions
│   (a .md file here) │
└─────────┬───────────┘
          │  calls a Python script
          ▼
┌─────────────────────┐
│   Python Script      │  ← the "engine" that composes & sends the email
│   (runs on your      │
│    computer/server)  │
└─────────┬───────────┘
          │  authenticates with Google
          ▼
┌─────────────────────┐
│   Gmail API          │  ← Google's official email-sending door
│   (Google's servers) │
└─────────────────────┘
```

---

## Software You Need (and Why)

| What | Cost | Why You Need It |
|------|------|-----------------|
| **Google Cloud Console** (free tier) | Free | Creates a "key" that lets code log in to Gmail on your behalf |
| **Python 3** | Free | The language the email script is written in |
| **Claude Code** | Already have it | The interface where you type `/send-email` |
| **GitHub** | Already have it | Stores the code so your developer collaborator can access it |

That's the complete list. No additional paid services required for this test.

**What you do NOT need** (keeping it simple):
- A separate email service (SendGrid, Mailgun, etc.) — Gmail handles this directly
- A cloud server — the script runs locally on whoever's machine runs Claude Code
- A database — no data storage needed for a simple send-email tool

---

## Security Guardrails — Read This First

Security risks are real. Here is what we will build in, and what you need to know:

### Guardrail 1: Credentials Never in Code
Google will give you a credential file (a JSON file with a secret key). This file
**must never be stored in GitHub**. We will store it in an environment variable or a
local file that is excluded from the repository via `.gitignore`.

**Risk without this:** Anyone who can read your GitHub repo could send email as you.

### Guardrail 2: Minimum Permissions (Least Privilege)
When we set up the Gmail API, we will request only `gmail.send` permission — the
narrowest scope that lets us send email. We will NOT request permission to read,
delete, or manage your inbox.

**Risk without this:** A bug or compromised script could read all your email.

### Guardrail 3: Recipient Allowlist (for later)
For this test we will accept any recipient. Before using this in production, we
recommend adding an allowlist — a hardcoded list of approved recipient domains or
addresses. This prevents accidentally blasting external parties.

**Risk without this:** A mistake (or someone who borrows your laptop) could send
email to unintended people.

### Guardrail 4: Daily Send Limit
Google's free Gmail API has a built-in cap of 500 emails/day for consumer accounts
and higher for GSuite (Workspace) accounts. We will also add a local counter that
refuses to send more than a configurable daily limit (default: 20).

**Risk without this:** A script bug in a loop could exhaust your quota or look like spam.

### Guardrail 5: Confirmation Before Send
The skill will show you a preview of the email and ask you to type **yes** before
sending. This is your last line of defense against typos and mistakes.

### Guardrail 6: No Password Storage
We will use **OAuth 2.0** (the same "Sign in with Google" system websites use) rather
than storing your Gmail password anywhere. Google issues a time-limited token.

---

## Phase-by-Phase Implementation Plan

---

### Phase 1 — Google Cloud Setup (One-Time, ~45 Minutes)

**You do this yourself in a web browser. No coding.**

**Step 1.1 — Create a Google Cloud Project**
- Go to `https://console.cloud.google.com`
- Sign in with a Google account (your Google Workspace admin account works)
- Click "New Project" → name it something like `willcrest-email-tool`
- This is like creating a folder in Google's system for your app's credentials

**Step 1.2 — Enable the Gmail API**
- Inside your project, go to "APIs & Services" → "Library"
- Search for "Gmail API" → click Enable
- This tells Google: "yes, I want code to be able to use Gmail on my behalf"

**Step 1.3 — Create OAuth 2.0 Credentials**
- Go to "APIs & Services" → "Credentials"
- Click "Create Credentials" → "OAuth client ID"
- Application type: **Desktop app** (for now; we can change this later)
- Download the resulting JSON file — this is your credential file
- **Store this file safely. Do not upload it to GitHub.**

**Step 1.4 — Configure the OAuth Consent Screen**
- Go to "OAuth consent screen"
- Choose "Internal" (only users in your Google Workspace can use this)
- Add your email as a test user
- Add the scope: `https://www.googleapis.com/auth/gmail.send`

> **Why "Internal"?** Because this is a business tool for your team only. "External"
> would allow anyone with a Google account to authorize it, which you don't want.

---

### Phase 2 — Python Script (Your Developer Does This, ~2 Hours)

**Your developer collaborator will write this code. You don't need to understand it
line by line, but here is what each file does:**

```
email-tool/
├── send_email.py         ← the main script that sends the email
├── auth.py               ← handles the Google login flow
├── guardrails.py         ← enforces daily limits and input validation
├── requirements.txt      ← list of Python libraries needed
├── .env.example          ← template showing what environment variables are needed
├── .gitignore            ← tells GitHub what NOT to store (credentials, etc.)
└── .claude/
    └── commands/
        └── send-email.md ← the Claude Code skill definition
```

**What each file does in plain English:**

- **send_email.py** — the "engine." It takes a recipient, subject, and body, then
  calls Google's API to send it. Like a mail clerk who takes your letter and drops
  it in the outgoing mail slot.

- **auth.py** — the "login handler." The first time you run this, it opens a browser
  window asking you to approve the app. After that it saves a token so you don't
  have to log in every time. Like a keycard that you swipe once and then tap quickly
  after that.

- **guardrails.py** — the "safety checker." Validates that the recipient looks like
  a real email address, checks the daily send counter, and rejects obviously bad input.

- **requirements.txt** — a shopping list. Running `pip install -r requirements.txt`
  installs everything the script needs automatically.

- **.env.example** — a template. Your developer fills in the real values in a `.env`
  file (which stays on your machine and never goes to GitHub).

- **.claude/commands/send-email.md** — the skill definition. This is what Claude Code
  reads when you type `/send-email`. It tells Claude: "ask the user for recipient,
  subject, and body, then run the Python script."

---

### Phase 3 — Claude Code Skill Setup (~30 Minutes)

**Your developer creates the skill file. You install it.**

The skill file (`.claude/commands/send-email.md`) contains instructions that tell
Claude how to run the conversation with you. When you type `/send-email`, Claude will:

1. Ask: "Who should this email go to?"
2. Ask: "What is the subject line?"
3. Ask: "What is the message? (type it out)"
4. Show you a preview and ask: "Send this email? (yes/no)"
5. Run the Python script with your answers
6. Report back: "Email sent successfully" or show an error

---

### Phase 4 — Team Setup (~1 Hour)

**Three types of users, three different setups:**

| Role | Who | What They Need |
|------|-----|----------------|
| **Admin/Developer** | You + your developer | Full GitHub access, Python installed, credential file |
| **Non-coding users** | Your 2 other team members | Claude Code installed, credential file shared securely, Python installed |
| **Read-only** | Future — not needed yet | N/A |

**Sharing credentials with non-coding team members:**
- Do NOT email them the credential JSON file
- Use a password manager with secure sharing (1Password, Bitwarden) — or
- Share via Google Drive with access restricted to your Workspace domain

---

### Phase 5 — Testing Checklist

Before declaring this done, test these scenarios:

- [ ] Send a test email to yourself
- [ ] Try sending to an invalid address (should be rejected with a clear error)
- [ ] Try sending twice in quick succession (second should show a warning)
- [ ] Disconnect from the internet and try to send (should fail gracefully)
- [ ] Close the credential file and try to send (should ask you to re-authenticate)

---

## Questions to Answer Before We Write Code

Before your developer writes a single line, you should decide:

1. **Who runs this tool?** Just you on your own computer? Or do the 2 non-coding users
   also send email from `clark@willcrestpartners.com`? (If so, you'll need shared
   OAuth credentials, which has security implications.)

2. **Should sent emails appear in Gmail's Sent folder?** By default, Gmail API sends
   do not show up there unless we explicitly add them. Probably you want them there
   for your records.

3. **What's your daily limit?** We'll default to 20 emails/day as a safety cap.
   Is that enough?

4. **Recipient restriction?** Any email address OK, or should we restrict to
   specific domains (e.g., only `@willcrestpartners.com` or a list of known contacts)?

---

## Timeline Estimate

| Phase | Who Does It | Time |
|-------|-------------|------|
| Phase 1: Google Cloud | You (with Claude guiding) | 45 min |
| Phase 2: Python script | Your developer | 2–3 hours |
| Phase 3: Skill file | Your developer | 30 min |
| Phase 4: Team setup | You + developer | 1 hour |
| Phase 5: Testing | Everyone | 30 min |
| **Total** | | **~5 hours** |

---

## Glossary (Plain English)

- **API** — A door that lets one piece of software talk to another. The Gmail API
  is Google's official door for sending email from code.
- **OAuth 2.0** — A login standard. Instead of giving your password to an app, you
  tell Google "I approve this app" and Google gives the app a temporary pass.
- **Credential / Token** — A secret code that proves your app is allowed to use the API.
- **Scope** — The specific permission you're granting. `gmail.send` means "allowed
  to send email only." Think of it like a hotel key that only opens your room.
- **Environment variable** — A secret stored in your computer's settings, not in the
  code. Like a sticky note on your monitor that the code can read but that doesn't
  get photocopied when you share the code.
- **Skill** — In Claude Code, a custom slash command you define. When you type
  `/send-email`, Claude reads the skill's instructions to know what to do.
- **MCP server** — A more advanced version of a skill that runs as a separate
  process and can expose many tools at once. Not needed for this simple use case.
- **`.gitignore`** — A file that tells GitHub "don't save these files." We use it
  to make sure your credential JSON never accidentally gets uploaded.

---

## What Comes Next (After This Test Works)

Once this simple test works, you'll have the foundation to build more:

- A skill that summarizes documents and emails the summary
- A skill that drafts emails from bullet points
- A skill that schedules emails for later sending
- Adding a second sending address

Each of these builds on exactly the same architecture: a Claude skill → a Python script → Gmail API.

---

*This workplan was created as a foundation document. Update it as decisions are made.*
