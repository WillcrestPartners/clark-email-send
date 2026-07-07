"""
Lambda handler for inbound polling.

Invoked on an EventBridge schedule (~every 60s, replacing the always-on
asyncio poll loop). Runs exactly one poll_once() sweep — list unread mail,
gate deterministically, and POST qualifying messages to Clark.

Overlap safety: reserved concurrency is NOT available in this account
(10-concurrency minimum rule), so sweeps CAN overlap. Correctness comes from
the atomic per-message claims in state_store (DynamoDB conditional put) — at
most one sweep forwards any given message — plus a function timeout kept
below the 60s schedule interval to make overlap rare in the first place.
"""

import json

import bootstrap  # noqa: F401 — loads Secrets Manager values into os.environ on import

import poller


def handler(event, context):
    summary = poller.poll_once()
    # Surface the sweep result to CloudWatch for the same visibility as the
    # ECS AUDIT lines.
    print(f"POLL: {json.dumps(summary)}", flush=True)
    return summary
