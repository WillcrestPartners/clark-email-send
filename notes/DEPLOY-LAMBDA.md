# Deploy / cutover runbook — ECS → Lambda

> **Deploys are manual** (build + package + deploy below). The old GitHub
> Actions workflow that docker-pushed to ECR on every push to `main` was
> removed 2026-07-07: nothing consumes that image since the ECS gateway was
> retired, and its IAM user lacked the CloudFormation/Lambda permissions a
> real CI deploy would need.


Migrates the email gateway from the always-on ECS Fargate service
(`clark-email-service`) to two Lambda functions defined by SAM in
`infra/template.yaml`:

- **`clark-email-web`** — Starlette app (`/mcp` + `/send` + `/health`) run as a
  persistent **uvicorn** server (`python -m uvicorn lambda_web:app`) under the
  **AWS Lambda Web Adapter (LWA)**, behind a Lambda **Function URL**. MCP stateless
  (`MCP_STATELESS_HTTP=true`). Handler is `run.sh`; LWA is enabled by the layer
  `arn:aws:lambda:us-east-1:753240598075:layer:LambdaAdapterLayerX86:25` plus env
  `AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap`, `AWS_LWA_INVOKE_MODE=buffered`,
  `PORT=8080`. (Mangum was removed: it re-runs the ASGI lifespan per invocation and
  re-enters the MCP `StreamableHTTPSessionManager`, which is only enterable once,
  breaking every route after the first request.)
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

  Its ARN is `arn:aws:secretsmanager:us-east-1:626928146978:secret:clark/email-gateway-HRkKkY`
  → used as `SecretsArn=<arn>` below.
- **S3 bucket** for CloudFormation packaging:
  `cdk-hnb659fds-assets-626928146978-us-east-1`
- **DynamoDB table** `clark-email-gateway` (PK `pk`, GSI `rfc822-index`, TTL `ttl`)
  is created by the SAM template.
- AWS CLI configured for us-east-1 with deploy permissions.

---

## (b) Build

```bash
rm -rf build && mkdir build && cp email_tool/*.py build/ && cp email_tool/run.sh build/ && chmod +x build/run.sh && pip install -r email_tool/requirements.txt -t build/ --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all:
```

`run.sh` is the LWA handler — it must be copied into `build/` and made executable.
`mangum` is no longer in `requirements.txt`; uvicorn serves the ASGI app under LWA.

---

## (c) Deploy

**Package must be run from the `infra/` directory.** `aws cloudformation package`
ignores SAM `Globals.CodeUri`, so `CodeUri` is set explicitly per-function in the
template, and package resolves relative `CodeUri` paths (`../build/`) relative to the
working directory — so `cd infra` first.

```bash
cd infra && aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket cdk-hnb659fds-assets-626928146978-us-east-1 \
  --s3-prefix clark-email-gateway \
  --output-template-file packaged.yaml \
  --region us-east-1

aws cloudformation deploy \
  --template-file packaged.yaml \
  --stack-name clark-email-gateway \
  --capabilities CAPABILITY_IAM \
  --region us-east-1 \
  --parameter-overrides \
    SecretsArn=arn:aws:secretsmanager:us-east-1:626928146978:secret:clark/email-gateway-HRkKkY \
    PollerEnabled=false
```

Deploy first with **`PollerEnabled=false`** so the poller does not run while ECS
is still live (avoids double-processing inbound mail). The deployed
`clark-email-web` Function URL is:

```
https://msbqvpq53fvvrd5o4o5kxv4jh40syise.lambda-url.us-east-1.on.aws/
```

Call it `<FunctionUrl>` below (it ends in `/`).

---

## (c2) Verify (via `aws lambda invoke`)

Smoke-test the web function (LWA + uvicorn) before cutover:

- `GET /health` → **200** `{"status":"ok",...}`.
- `POST /send` with a **bad** `X-Clark-Signature` → **401** (signature rejected).
- `POST /send` with a **correctly-signed empty body** → **400** `missing to`
  (this proves the HMAC secret loaded from Secrets Manager — the signature passed
  and only payload validation failed).
- `POST /mcp` `initialize` then `tools/list` → **200** returning the tool list.
  The MCP endpoint is `/mcp` (**no trailing slash**).

If a route works on the first request but later routes fail, that is the Mangum
lifespan/MCP-session-manager bug the LWA switch fixed — confirm `run.sh` + the LWA
layer/env are in place.

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
     --region us-east-1 \
     --parameter-overrides \
       SecretsArn=arn:aws:secretsmanager:us-east-1:626928146978:secret:clark/email-gateway-HRkKkY \
       PollerEnabled=true
   ```
4. **Repoint Clark** (Function URL
   `https://msbqvpq53fvvrd5o4o5kxv4jh40syise.lambda-url.us-east-1.on.aws/`):
   - `clark/app` `EMAIL_GATEWAY_SEND_URL` →
     `https://msbqvpq53fvvrd5o4o5kxv4jh40syise.lambda-url.us-east-1.on.aws/send`
   - Cowork MCP connector →
     `https://msbqvpq53fvvrd5o4o5kxv4jh40syise.lambda-url.us-east-1.on.aws/mcp`

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
