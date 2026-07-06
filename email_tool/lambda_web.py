"""
ASGI entrypoint for the web surface: MCP (/mcp), the /send relay, and /health.

Served by a real uvicorn process under the AWS Lambda Web Adapter (see
infra/template.yaml + run.sh), so the Starlette lifespan — including the MCP
StreamableHTTPSessionManager, which may only be entered once per instance —
runs exactly once per execution environment, just as it did on ECS.

(Mangum was tried and rejected here: it re-runs the ASGI lifespan on every
invocation, which re-enters the session manager and raises "can only be called
once per instance", breaking every route after the first request.)

The background poll loop is NOT started here — inbound polling runs in
lambda_poll on an EventBridge schedule. MCP_STATELESS_HTTP=true keeps MCP from
holding cross-request session state.
"""

import bootstrap  # noqa: F401 — loads Secrets Manager values into os.environ on import

from server import build_app

# uvicorn (started by run.sh) imports this module and serves `app`.
app = build_app(run_poller=False)
