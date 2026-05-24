"""
Writes a record of every email attempt to audit_log.jsonl.
Every line is a JSON object — one per attempt, success or failure.
"""

import datetime
import json
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

    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def get_recent(limit: int = 20) -> list[dict]:
    """Returns the most recent N log entries, newest first."""
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text().strip().splitlines()
    entries = [json.loads(line) for line in lines if line]
    return list(reversed(entries[-limit:]))
