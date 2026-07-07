"""
Records every email attempt to stdout (captured by AWS CloudWatch Logs)
and optionally to a local file for development use.
"""

import datetime
import json
import os
import sys
from pathlib import Path

# Every audit line also goes to stdout -> CloudWatch, which is the durable
# record on Lambda. The local file is a convenience for the dashboard's
# "recent activity"; on Lambda point AUDIT_LOG_PATH at /tmp (the only writable
# dir) — it survives within a warm container but not across cold starts.
LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", str(Path(__file__).parent / "audit_log.jsonl")))


def log_attempt(
    user_email: str,
    to: str,
    subject: str,
    status: str,
    reason: str = None,
    message_id: str = None,
) -> None:
    entry = {
        "time": datetime.datetime.utcnow().isoformat(),
        "user": user_email,
        "to": to,
        "subject": subject,
        "status": status,
    }
    if reason:
        entry["reason"] = reason
    if message_id:
        entry["message_id"] = message_id

    # Print to stdout — Lambda/ECS send this to CloudWatch Logs automatically;
    # CloudWatch is the durable audit record.
    print(f"AUDIT: {json.dumps(entry)}", flush=True)

    # Also write to file for local development (ephemeral on Lambda)
    try:
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def get_recent(limit: int = 20) -> list:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text().strip().splitlines()
    entries = [json.loads(line) for line in lines if line]
    return list(reversed(entries[-limit:]))
