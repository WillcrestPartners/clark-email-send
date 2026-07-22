"""
Cold-start secret loading for the Lambda deployment.

The ECS deployment injected GOOGLE_SERVICE_ACCOUNT_JSON, CLARK_INBOUND_HMAC_SECRET
and APP_CONFIG_JSON as plaintext task-definition env vars. On Lambda these live
in one Secrets Manager secret (JSON blob) referenced by GATEWAY_SECRETS_ARN.

Importing this module once, before anything reads os.environ, fetches that secret
and populates any of those keys that aren't already set — so all existing code
(gmail_client, clark_client, access_control, ...) keeps reading os.environ
unchanged. A locally-set env var always wins, which keeps local dev working with
no Secrets Manager call.
"""

import json
import os

# Keys we lift out of the JSON secret into the process environment.
# (GATEWAY_MCP_TOKEN was removed 2026-07-22: the optional shared-secret /mcp
# guard it fed is superseded by per-user OAuth — see oauth.py. The Cognito
# pool/client/domain values OAuth needs are NOT secrets and arrive as plain
# Lambda env vars from infra/template.yaml parameters, not from this secret.)
_SECRET_KEYS = (
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "CLARK_INBOUND_HMAC_SECRET",
    "APP_CONFIG_JSON",
)

_loaded = False


def load_secrets() -> None:
    """Populate os.environ from GATEWAY_SECRETS_ARN. Idempotent; no-op locally."""
    global _loaded
    if _loaded:
        return
    _loaded = True

    arn = os.environ.get("GATEWAY_SECRETS_ARN") or os.environ.get("GATEWAY_SECRETS_NAME")
    if not arn:
        return  # local/dev — env vars (or .env) already provide the values

    import boto3  # lazy: only in the Lambda runtime

    client = boto3.client("secretsmanager")
    raw = client.get_secret_value(SecretId=arn)["SecretString"]
    data = json.loads(raw)
    for key in _SECRET_KEYS:
        # Only fill gaps — an explicit env var overrides the secret.
        if key in data and not os.environ.get(key):
            os.environ[key] = (
                data[key] if isinstance(data[key], str) else json.dumps(data[key])
            )


# Load on import so `import bootstrap` at the top of a handler is enough.
load_secrets()
