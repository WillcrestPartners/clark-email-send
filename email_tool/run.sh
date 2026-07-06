#!/bin/bash
# Entrypoint for the clark-email-web Lambda under the AWS Lambda Web Adapter.
# LWA is enabled via the layer + AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap; it
# probes PORT and proxies Function URL requests to this uvicorn server. Running
# a persistent server (not a per-invocation adapter) means the Starlette
# lifespan / MCP session manager start exactly once per execution environment.
exec python -m uvicorn lambda_web:app --host 0.0.0.0 --port "${PORT:-8080}"
