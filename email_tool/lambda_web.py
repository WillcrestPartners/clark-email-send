"""
Lambda handler for the web surface: MCP (/mcp), the /send relay, and /health.

Wraps the same Starlette ASGI app the local server uses (server.build_app)
with Mangum, behind a Lambda Function URL. The background poll loop is NOT
started here — inbound polling runs in lambda_poll on an EventBridge schedule.

Requires MCP_STATELESS_HTTP=true so MCP holds no cross-request session state.
"""

import bootstrap  # noqa: F401 — loads Secrets Manager values into os.environ on import

from mangum import Mangum

from server import build_app

app = build_app(run_poller=False)
handler = Mangum(app, lifespan="auto")
