"""
Safety checks that run before any email is sent.

Prevents: invalid addresses, mass sending, and disallowed recipients.
"""

import os
import re
import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

COUNTER_FILE = Path(__file__).parent / ".send_count"
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_recipient(address: str) -> None:
    """Raises ValueError if the address looks invalid or is not in the allowlist."""
    address = address.strip()
    if not EMAIL_REGEX.match(address):
        raise ValueError(f"'{address}' does not look like a valid email address.")

    allowed_domains_raw = os.environ.get("ALLOWED_RECIPIENT_DOMAINS", "").strip()
    if allowed_domains_raw:
        allowed = [d.strip().lower() for d in allowed_domains_raw.split(",")]
        domain = address.split("@")[1].lower()
        if domain not in allowed:
            raise ValueError(
                f"Recipient domain '{domain}' is not in the approved list: {allowed}\n"
                "Update ALLOWED_RECIPIENT_DOMAINS in your .env file to add it."
            )


def check_daily_limit() -> None:
    """Raises RuntimeError if today's send count is at or above the daily limit."""
    limit = int(os.environ.get("DAILY_SEND_LIMIT", "20"))
    today = datetime.date.today().isoformat()
    count = 0

    if COUNTER_FILE.exists():
        lines = COUNTER_FILE.read_text().strip().splitlines()
        for line in lines:
            if line.startswith(today + ":"):
                count = int(line.split(":")[1])
                break

    if count >= limit:
        raise RuntimeError(
            f"Daily send limit of {limit} reached. "
            "No more emails will be sent today. "
            "Update DAILY_SEND_LIMIT in .env if you need to raise this cap."
        )


def record_send() -> None:
    """Increments today's send counter."""
    today = datetime.date.today().isoformat()
    lines = []
    updated = False

    if COUNTER_FILE.exists():
        lines = COUNTER_FILE.read_text().strip().splitlines()

    new_lines = []
    for line in lines:
        if line.startswith(today + ":"):
            count = int(line.split(":")[1]) + 1
            new_lines.append(f"{today}:{count}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"{today}:1")

    COUNTER_FILE.write_text("\n".join(new_lines) + "\n")
