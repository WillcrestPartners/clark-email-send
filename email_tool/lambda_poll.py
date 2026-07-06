"""
Lambda handler for inbound polling.

Invoked on an EventBridge schedule (~every 60s, replacing the always-on
asyncio poll loop). Runs exactly one poll_once() sweep — list unread mail,
gate deterministically, and POST qualifying messages to Clark. Reserved
concurrency of 1 keeps sweeps from overlapping on the shared mailbox.
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
