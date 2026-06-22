# Email command bus — pending deploy changes

Running list of changes to apply in **one batched maintenance window** so we
only redeploy the ECS service (`clark-email-service`) once. The inbound email
command bus is live and tested end-to-end as of 2026-06-21; these are tuning /
polish items, not blockers.

> **⚠️ TOP PRIORITY (next session): debug inbound reply reliability.**
> The pipeline is live and the happy path works (clean "add a contact" email →
> reply → one-tap approve → contact created), but replies are **not yet
> reliable**. A forwarded "add Rich as a contact" email returned no reply
> (`email_inbound_events.outcome = no_instruction`); a prompt fix shipped
> (Clark PR #3) but a re-test still produced no reply — root cause unconfirmed.
> Diagnose with: `email_inbound_events.outcome`, ECS CloudWatch `AUDIT:` lines
> (`acked`/`ignored`/`dropped`/`no_instruction`), and Vercel function logs for
> `/api/email/inbound` (check `reply_sent`). Separate the cases: gateway didn't
> poll/forward → vs Clark agent returned no proposal → vs `/send` reply failed.
> Suspects: poll timing (still 300s), test email already read, agent extraction
> on dense forwarded threads, outbound `/send` relay.

How a config change reaches production (learned the hard way):
1. ECS → **Task definitions → default-clark-email-service → Create new revision**
2. Expand the **Main** container → **Environment variables** → edit the value
3. **Verify the edit stuck** before creating (copy the value back out / re-open it)
4. **Create** the revision (note the new number)
5. **Service → Update service → pick that exact revision number → Update**
6. Confirm via the health endpoint:
   `https://cl-874b2f3a18c5475dbfbd921b886e8153.ecs.us-east-1.on.aws/health`

---

## 1. Reduce inbound latency: `poll_seconds` 300 → 60  (Option A)

In `APP_CONFIG_JSON`, change the `inbound` block's `poll_seconds` from `300`
to `60`. Cuts worst-case email→reply latency from ~5 min to ~1 min. Negligible
cost (Gmail API quota is generous; service already runs 24/7).

Full value to paste (only `poll_seconds` changed vs. what's live):

```json
{"global":{"sender_email":"clark@willcrestpartners.com","default_daily_limit":20,"confirmation_required":true,"copy_to_sent_folder":true},"users":{"bforster@willcrest.com":{"name":"Bret Forster","role":"admin","daily_limit":20,"active":true},"dnaas@willcrest.com":{"name":"Dominic Naas","role":"user","daily_limit":20,"active":true},"pvillegas@willcrest.com":{"name":"Patricia Villegas","role":"user","daily_limit":20,"active":true},"kosborn@willcrest.com":{"name":"Kristin Osborn","role":"user","daily_limit":20,"active":false}},"inbound":{"enabled":true,"poll_seconds":60,"mailboxes":[{"address":"clark@willcrestpartners.com","destination":{"name":"clark-os","webhook_url":"","authorized_senders_url":""}}]}}
```

---

## 2. Speed up ECS deployments (target group settings — EC2 console)

Deploys currently take ~10 min, mostly load-balancer wait. In **EC2 → Target
Groups** (the group fronting `clark-email-service`):
- **Attributes → Deregistration delay**: `300` → `30` seconds
- **Health checks → Interval**: `30` → `10` seconds
- **Health checks → Healthy threshold**: `3` → `2`

Note: ECS Express manages this target group; re-check these after a deploy in
case Express re-applies its defaults.

---

## Optional / later (not required tomorrow)

- **Authorize more senders.** Only `bforster@willcrest.com` is in
  `authorized_email_senders` today. To let others email Clark, add rows
  (Supabase change, no ECS redeploy):
  ```sql
  INSERT INTO authorized_email_senders (user_id, email_address, can_send_instructions, created_by)
  VALUES ('<USER_UUID>', '<email>', true, '<ADMIN_UUID>');
  ```
- **Near-instant inbound (Option B).** Replace polling with Gmail push via
  Cloud Pub/Sub `watch` (seconds instead of a minute). Larger effort: Pub/Sub
  topic + publish grant + push webhook on the gateway + ~7-day watch renewal.
- **Manual poll tool.** After a redeploy + MCP reconnect, the gateway exposes a
  `poll_inbox` MCP tool to trigger an immediate poll (skip the wait while
  testing).

---

## Reference (current live config)

- Gateway (ECS Fargate): service `clark-email-service`, cluster `default`,
  us-east-1. Image `626928146978.dkr.ecr.us-east-1.amazonaws.com/willcrestpartners/clark-email-send:latest`.
- Health: `https://cl-874b2f3a18c5475dbfbd921b886e8153.ecs.us-east-1.on.aws/health`
  → expect `{"status":"ok","inbound_enabled":true,"last_successful_poll":"<ts>"}`
- Clark (Vercel): `https://clark-six.vercel.app`, production = `main`.
- Inbound polls `is:unread in:inbox` on `clark@willcrestpartners.com`; rejected
  or processed messages are marked read. Audit lines print to CloudWatch as
  `AUDIT: {...}` (`status`: `acked` / `ignored` / `dropped` / `failed`).
