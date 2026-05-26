"""
Records every email attempt to stdout (captured by AWS CloudWatch Logs)
and optionally to a local file for development use.
"""

import datetime
import json
import sys
from pathlib import Path

LOG_PATH = Path(__file__).parent / "audit_log.jsonl"


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

    # Print to stdout — AWS App Runner sends this to CloudWatch Logs automatically
    print(f"AUDIT: {json.dumps(entry)}", flush=True)

    # Also write to file for local development (file won't persist in App Runner)
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
