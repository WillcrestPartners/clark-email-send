# Deploy / cutover runbook — ECS → Lambda

> **Auto-deploy (2026-07-22):** merges to `main` now build + SAM-deploy the
> stack automatically via `.github/workflows/deploy.yml` (GitHub OIDC, no
> stored AWS keys). See **section (g) Auto-deploy** below for the pipeline and
> its one-time IAM-role bootstrap. The manual runbook (b)+(c) below still works
> and is the fallback / first-deploy path; a manual deploy and an auto-deploy
> are the same commands, so they can't drift.
>
> **Earlier note (superseded):** the *original* GitHub Actions workflow that
> docker-pushed to ECR on every push to `main` was removed 2026-07-07 (nothing
> consumed that image after the ECS gateway retired, and its IAM user lacked
> CloudFormation/Lambda permissions). The new workflow is unrelated: it drives
> the SAM/CloudFormation deploy and assumes a purpose-built, scoped role.

> **STATUS — cutover complete (2026-07-06); Lambda is the live gateway.** This is
> now the **standard deploy runbook**, not just a migration doc. For a routine
> code or config change the whole loop is **(b) Build** + **(c) Deploy** (the
> **(a)** prerequisites are already satisfied). Sections **(d) Cutover** and
> **(e) Rollback** are the one-time ECS↔Lambda switchover steps — now **legacy**
> (ECS is retired), kept for history. Last deployed **2026-07-22** (mobile/voice
> connector MCP tools; verified — all 18 tools live).

> **Where to run it:** anywhere with the `Clark-deployer` IAM credentials and the
> AWS CLI configured for **us-east-1** — AWS CloudShell, a GitHub Codespace, or any
> comparable shell. Run every command **from the repo root** (`clark-email-send/`);
> the `cp email_tool/*.py …` line in (b) doubles as your "am I in the right
> directory?" check.

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
  `lambda_poll.handler`. The schedule is hardcoded `Enabled: true` in the
  template — it runs whenever the stack is deployed (no enable/disable parameter).

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
    SecretsArn=arn:aws:secretsmanager:us-east-1:626928146978:secret:clark/email-gateway-HRkKkY
```

> **Do NOT pass `PollerEnabled`.** That parameter was removed from `template.yaml`
> once cutover finished; the poller's EventBridge schedule is now hardcoded
> `Enabled: true`, so it runs whenever the stack is deployed. Passing
> `PollerEnabled=...` makes CloudFormation fail with *"Parameters: [PollerEnabled]
> do not exist in the template."* The template's other parameters (`ClarkBaseUrl`,
> `SenderEmail`) have defaults and keep their current values when omitted, so
> `SecretsArn` is normally the only override you pass. Because the poller is
> always on, a redeploy **cannot** accidentally disable inbound polling.

### Per-user OAuth parameters (`/mcp` — specs/connector-oauth.md in the Clark repo)

Five more parameters wire the OAuth middleware (`email_tool/oauth.py`). None
are secrets, and like every CloudFormation parameter they **keep their
previously-deployed values when omitted** from `--parameter-overrides` — so
routine deploys after the OAuth cutover still pass only `SecretsArn`.

| Parameter | Value |
|---|---|
| `ConnectorCognitoPoolId` | ClarkAuth stack `UserPoolId` output (e.g. `us-east-1_XXXXXXXXX`) |
| `ConnectorCognitoDomain` | ClarkAuth stack `HostedUiDomain` output (e.g. `https://willcrest-clark.auth.us-east-1.amazoncognito.com`) |
| `ConnectorClientId` | ClarkAuth stack `ConnectorClientId` output (the `clark-connector` app client) |
| `ConnectorAuthRequired` | `false` on the first OAuth deploy (transition: tokens honored, legacy `caller_email` still accepted); flip to `true` once the claude.ai connector is re-added with OAuth working |
| `ConnectorDcrShim` | leave `false` (only for the fallback where claude.ai's Advanced-settings client-credentials entry is unavailable) |

Read the ClarkAuth outputs with:

```bash
aws cloudformation describe-stacks --stack-name ClarkAuth --region us-east-1 \
  --query "Stacks[0].Outputs" --output table
```

The deployed `clark-email-web` Function URL is:

```
https://rsug7xmtqbzyxeqgenw3uragje0xupnj.lambda-url.us-east-1.on.aws/
```

> **Rotated 2026-07-22** (post-cutover hardening, `specs/connector-oauth.md`
> §5 in the Clark repo — the pre-OAuth URL had circulated as the only secret
> gating `/mcp`, so it was deleted + recreated to make that knowledge
> worthless). If this URL ever needs to change again: `aws lambda
> delete-function-url-config` + `create-function-url-config` for
> `clark-email-web`, then repoint `clark/app`'s `EMAIL_GATEWAY_SEND_URL`
> secret (fetch-edit-put the JSON, preserving the other keys), force a new
> ECS deployment (`cluster clark`, `service clark`) so it picks up the new
> secret, and update the claude.ai connector's URL. Verified live 2026-07-22
> as bforster@willcrest.com via the Cowork connector.

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

**If the public Function URL is unreachable** (e.g. an egress policy blocks
`*.lambda-url.on.aws`, as in Claude Code on the web), verify by invoking the
function **directly through the AWS API** instead — same code path, no public URL
needed. Send a Function-URL v2 event via `aws lambda invoke --function-name
clark-email-web` (or `boto3`) and read the `{statusCode, body}` it returns:

```json
{"version":"2.0","rawPath":"/health","rawQueryString":"",
 "headers":{"content-type":"application/json"},
 "requestContext":{"http":{"method":"GET","path":"/health"}},"isBase64Encoded":false}
```

For `tools/list`, POST `/mcp` with an `initialize` request (stateless mode returns
no `mcp-session-id`), then a `tools/list` request; the response body is JSON (or
SSE `data:` lines to parse). This is exactly how the **2026-07-22** deploy was
verified from Claude Code on the web: `/health` → 200 with a fresh
`last_successful_poll`, and `tools/list` → **all 18 tools**, including the 8
mobile/voice tools (`search_contacts`, `get_contact`, `get_company`,
`submit_contact`, `submit_activity`, `sync_granola`, `list_pending_approvals`,
`act_on_approval`).

If a route works on the first request but later routes fail, that is the Mangum
lifespan/MCP-session-manager bug the LWA switch fixed — confirm `run.sh` + the LWA
layer/env are in place.

**OAuth middleware checks** (after the connector-oauth deploy):

- `GET /.well-known/oauth-protected-resource` → **200** naming the Cognito
  issuer in `authorization_servers`.
- With `ConnectorAuthRequired=false`: `POST /mcp` `tools/list` with no token →
  **200**; **no tool schema contains `caller_email`**.
- With `ConnectorAuthRequired=true`: `POST /mcp` with no/garbage token →
  **401** with a `WWW-Authenticate` header pointing at the resource metadata;
  with a fresh token from the connector → 200.
- Full offline coverage: `python3 email_tool/selftest_oauth.py` (runs both
  modes; needs the pip deps but no AWS).

---

## (d) Cutover — ✅ DONE 2026-07-06 (legacy — ECS retired; kept for history)

> One-time ECS→Lambda switchover steps. Complete; **not** part of a routine
> deploy. **Ignore the `PollerEnabled=true` override in step 3** — that parameter
> no longer exists (see §c); the poller is hardcoded on.

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
   `https://msbqvpq53fvvrd5o4o5kxv4jh40syise.lambda-url.us-east-1.on.aws/` —
   **superseded 2026-07-22, see §c for the current URL**):
   - `clark/app` `EMAIL_GATEWAY_SEND_URL` →
     `https://msbqvpq53fvvrd5o4o5kxv4jh40syise.lambda-url.us-east-1.on.aws/send`
   - Cowork MCP connector →
     `https://msbqvpq53fvvrd5o4o5kxv4jh40syise.lambda-url.us-east-1.on.aws/mcp`

---

## (e) Rollback — legacy (ECS is retired; kept for history)

> ECS-based rollback no longer applies. **To roll back a bad Lambda deploy
> today:** re-run **(b)+(c)** from the previous known-good commit — CloudFormation
> updates the stack in place, and a failed update rolls back automatically. The
> steps below are the original cutover-era rollback and reference `PollerEnabled`
> (removed) and ECS (retired).

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

---

## (g) Auto-deploy (GitHub Actions + OIDC)

`.github/workflows/deploy.yml` runs the exact **(b) Build + (c) Deploy** loop
on every push to `main` that touches `email_tool/**`, `infra/**`, or the
workflow itself (also runnable on demand via *Actions → Deploy gateway (SAM) →
Run workflow*). It authenticates with **GitHub OIDC** — a short-lived token
exchanged for a scoped IAM role, so **no AWS keys are stored in GitHub**.

**What the pipeline does and does NOT do**

- Reads the three connector-OAuth wiring values (`UserPoolId`,
  `HostedUiDomain`, `ConnectorClientId`) live from the `ClarkAuth` stack
  outputs, so they never need to be re-typed and stay correct if the pool is
  ever rebuilt.
- Passes `SecretsArn` (a non-secret ARN, in the workflow `env`).
- **Deliberately does not pass `ConnectorAuthRequired` or `ConnectorDcrShim`.**
  `aws cloudformation deploy` preserves the previously-deployed value of any
  parameter it isn't given, so a routine merge ships code but can never flip
  `/mcp` enforcement on or off. Change the flag only with a deliberate one-off
  manual deploy (pass `ConnectorAuthRequired=true`) — see section (c).
- Never passes `PollerEnabled` (removed — see §c).

### One-time bootstrap (privileged; do once)

The pipeline assumes an IAM role that must exist first. Requires an identity
that can create IAM roles (an admin, or `clark-deployer` if it has `iam:*` on
`role/clark-email-gateway-*`). The account already has the GitHub OIDC provider
(created by Clark's CDK), so we reuse it.

1. **Confirm the OIDC provider ARN** (expected
   `arn:aws:iam::626928146978:oidc-provider/token.actions.githubusercontent.com`):

   ```bash
   aws iam list-open-id-connect-providers
   ```

2. **Create the deploy role**, trusting *only* this repo's `main` branch:

   ```bash
   cat > /tmp/trust.json <<'JSON'
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": { "Federated": "arn:aws:iam::626928146978:oidc-provider/token.actions.githubusercontent.com" },
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringEquals": {
           "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
           "token.actions.githubusercontent.com:sub": "repo:WillcrestPartners/clark-email-send:ref:refs/heads/main"
         }
       }
     }]
   }
   JSON
   aws iam create-role --role-name clark-email-gateway-deployer \
     --assume-role-policy-document file:///tmp/trust.json \
     --description "GitHub Actions OIDC role: SAM-deploy the email gateway from main"
   ```

3. **Attach a scoped permissions policy.** The dangerous verb (IAM) is limited
   to the stack's own auto-created roles; the service verbs are account-wide
   for deploy reliability (tighten later if desired):

   ```bash
   cat > /tmp/perms.json <<'JSON'
   {
     "Version": "2012-10-17",
     "Statement": [
       { "Sid": "Cfn",    "Effect": "Allow", "Action": "cloudformation:*", "Resource": "*" },
       { "Sid": "Lambda", "Effect": "Allow", "Action": "lambda:*",         "Resource": "*" },
       { "Sid": "Dynamo", "Effect": "Allow", "Action": "dynamodb:*",       "Resource": "*" },
       { "Sid": "Events", "Effect": "Allow", "Action": "events:*",         "Resource": "*" },
       { "Sid": "Logs",   "Effect": "Allow", "Action": "logs:*",           "Resource": "*" },
       { "Sid": "AssetBucket", "Effect": "Allow",
         "Action": ["s3:GetObject","s3:PutObject","s3:GetBucketLocation","s3:ListBucket"],
         "Resource": [
           "arn:aws:s3:::cdk-hnb659fds-assets-626928146978-us-east-1",
           "arn:aws:s3:::cdk-hnb659fds-assets-626928146978-us-east-1/*"
         ] },
       { "Sid": "IamForStackRolesOnly", "Effect": "Allow",
         "Action": ["iam:CreateRole","iam:DeleteRole","iam:GetRole","iam:PassRole",
                    "iam:AttachRolePolicy","iam:DetachRolePolicy","iam:PutRolePolicy",
                    "iam:DeleteRolePolicy","iam:GetRolePolicy","iam:TagRole","iam:UntagRole",
                    "iam:ListRolePolicies","iam:ListAttachedRolePolicies"],
         "Resource": "arn:aws:iam::626928146978:role/clark-email-gateway-*" }
     ]
   }
   JSON
   aws iam put-role-policy --role-name clark-email-gateway-deployer \
     --policy-name gateway-deploy --policy-document file:///tmp/perms.json

   aws iam get-role --role-name clark-email-gateway-deployer \
     --query Role.Arn --output text   # <- the role ARN for the next step
   ```

4. **Tell the workflow the role ARN** (GitHub repo *variable*, not a secret —
   it's just an ARN). Either in the UI (repo → Settings → Secrets and variables
   → Actions → **Variables** → New: `AWS_DEPLOY_ROLE_ARN` = the ARN), or:

   ```bash
   gh variable set AWS_DEPLOY_ROLE_ARN \
     --repo WillcrestPartners/clark-email-send \
     --body arn:aws:iam::626928146978:role/clark-email-gateway-deployer
   ```

5. **Protect `main`** (recommended for this security-critical service): repo →
   Settings → Branches → add a rule for `main` requiring a pull request +
   review before merge, so an auto-deploy always corresponds to a reviewed
   change.

After this, every merge to `main` deploys the gateway. Watch a run under the
repo's **Actions** tab; a green run ends with `Successfully created/updated
stack` (or "No changes to deploy").
