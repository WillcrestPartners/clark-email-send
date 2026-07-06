# Deploy / cutover runbook — ECS → Lambda

Migrates the email gateway from the always-on ECS Fargate service
(`clark-email-service`) to two Lambda functions defined by SAM in
`infra/template.yaml`:

- **`clark-email-web`** — Starlette app (`/mcp` + `/send` + `/health`) via Mangum,
  behind a Lambda **Function URL**. MCP stateless (`MCP_STATELESS_HTTP=true`).
  Handler `lambda_web.handler`.
- **`clark-email-poller`** — `poller.poll_once()` once per invocation, driven by
  an EventBridge schedule `rate(1 minute)`, reserved concurrency 1. Handler
  `lambda_poll.handler`. Schedule gated by the `PollerEnabled` CFN parameter.

The gateway↔Clark contract is **unchanged**: `X-Clark-Signature =
"sha256=" + HMAC-SHA256(raw body)`; the allow-list GET signs the empty string;
same envelope v1 and `/send` payloads. `clark_client.py` was not modified.

Region: **us-east-1**. Account: **626928146978**.

---

## (a) Prerequisites

- **Secrets Manager secret `clark/email-gateway`** created, JSON with keys:
  - `GOOGLE_SERVICE_ACCOUNT_JSON` — service account key JSON
  - `CLARK_INBOUND_HMAC_SECRET` — **set equal to** Clark's `clark/app`
    `CLARK_INBOUND_HMAC_SECRET` so signatures match
  - `APP_CONFIG_JSON` — user list + global + `inbound` block (incl.
    `poll_seconds:60`; actual cadence is the EventBridge rate)

  Note its ARN → used as `SecretsArn=<arn>` below.
- **S3 bucket** for CloudFormation packaging:
  `cdk-hnb659fds-assets-626928146978-us-east-1`
- **DynamoDB table** `clark-email-gateway` (PK `pk`, GSI `rfc822-index`, TTL `ttl`)
  is created by the SAM template.
- AWS CLI configured for us-east-1 with deploy permissions.

---

## (b) Build

```bash
rm -rf build && mkdir build
cp email_tool/*.py build/
pip install -r email_tool/requirements.txt -t build/ \
  --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all:
```

---

## (c) Deploy

```bash
BUCKET=cdk-hnb659fds-assets-626928146978-us-east-1

aws cloudformation package \
  --template-file infra/template.yaml \
  --s3-bucket "$BUCKET" \
  --output-template-file infra/packaged.yaml

aws cloudformation deploy \
  --template-file infra/packaged.yaml \
  --stack-name clark-email-gateway \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides SecretsArn=<arn> PollerEnabled=false
```

Deploy first with **`PollerEnabled=false`** so the poller does not run while ECS
is still live (avoids double-processing inbound mail). Grab the `clark-email-web`
Function URL from the stack outputs — call it `<FunctionUrl>` below (it ends in
`/`).

---

## (d) Cutover

1. **Verify the web function** against `<FunctionUrl>` while ECS still serves prod:
   - `GET <FunctionUrl>health` → `{"status":"ok",...}`
   - Signed `POST <FunctionUrl>send` with a test payload
     (`X-Clark-Signature: sha256=<hmac of raw body>`) → 200.
   - Signed allow-list GET (`authorized-senders`, HMAC over the empty string) → 200.
2. **Scale ECS to 0** (stops the old poll loop + old `/send` host):

   ```bash
   aws ecs update-service --cluster default \
     --service clark-email-service --desired-count 0
   ```
3. **Redeploy the stack with the poller on:**

   ```bash
   aws cloudformation deploy --template-file infra/packaged.yaml \
     --stack-name clark-email-gateway --capabilities CAPABILITY_IAM \
     --parameter-overrides SecretsArn=<arn> PollerEnabled=true
   ```
4. **Repoint Clark:**
   - `clark/app` `EMAIL_GATEWAY_SEND_URL` → `<FunctionUrl>send`
   - Cowork MCP connector → `<FunctionUrl>mcp`

---

## (e) Rollback

1. Redeploy stack with `PollerEnabled=false` (stops the Lambda poller).
2. Scale ECS back up:

   ```bash
   aws ecs update-service --cluster default \
     --service clark-email-service --desired-count 1
   ```
3. Revert `clark/app` `EMAIL_GATEWAY_SEND_URL` (and the Cowork connector) to the
   old ECS host.

---

## (f) Verification (end-to-end)

- Forward an email to `clark@willcrestpartners.com` → one-tap reply → approve →
  confirm the record lands in RDS.
- **CloudWatch logs**:
  - `/aws/lambda/clark-email-poller` — `POLL:` sweep lines and `AUDIT:` lines
    (`acked` / `ignored` / `dropped` / `failed` / `no_instruction`).
  - `/aws/lambda/clark-email-web` — `/send` relay + MCP activity.
- `GET <FunctionUrl>health` → `inbound_enabled:true` and a recent
  `last_successful_poll`.
