"""
Checks whether a user is authorized to send email and enforces daily limits.
Reads from config.json, which lives on the server and is never in GitHub.
"""

import datetime
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            "config.json not found. Copy config.json.example to config.json "
            "and fill in your team's details."
        )
    return json.loads(CONFIG_PATH.read_text())


def _save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def get_user(email: str) -> dict:
    """Returns the user config dict, or raises ValueError if not authorized."""
    config = _load_config()
    user = config["users"].get(email)
    if not user:
        raise ValueError(
            f"{email} is not in the authorized user list. "
            "Ask an admin to add you to config.json."
        )
    if not user.get("active", False):
        raise ValueError(
            f"Access for {email} is currently suspended. Contact an admin."
        )
    return user


def check_daily_limit(email: str) -> int:
    """Returns remaining sends for today. Raises RuntimeError if limit is hit."""
    config = _load_config()
    user = get_user(email)
    limit = user.get("daily_limit", config["global"]["default_daily_limit"])
    today = datetime.date.today().isoformat()
    counts = config.get("daily_counts", {})
    sent_today = counts.get(today, {}).get(email, 0)

    remaining = limit - sent_today
    if remaining <= 0:
        raise RuntimeError(
            f"Daily send limit of {limit} reached for {email}. "
            "Resets at midnight. An admin can raise your limit in config.json."
        )
    return remaining


def record_send(email: str) -> None:
    """Increments this user's daily send counter."""
    config = _load_config()
    today = datetime.date.today().isoformat()
    counts = config.setdefault("daily_counts", {})
    day = counts.setdefault(today, {})
    day[email] = day.get(email, 0) + 1
    _save_config(config)


def is_admin(email: str) -> bool:
    config = _load_config()
    user = config["users"].get(email, {})
    return user.get("role") == "admin"


def get_dashboard_data() -> dict:
    """Returns all data needed to render the admin dashboard."""
    config = _load_config()
    today = datetime.date.today().isoformat()
    today_counts = config.get("daily_counts", {}).get(today, {})

    users_summary = []
    for email, user in config["users"].items():
        limit = user.get("daily_limit", config["global"]["default_daily_limit"])
        users_summary.append({
            "email": email,
            "name": user.get("name", ""),
            "role": user.get("role", "user"),
            "daily_limit": limit,
            "sent_today": today_counts.get(email, 0),
            "active": user.get("active", False),
        })

    return {
        "global": config["global"],
        "users": users_summary,
    }


def update_user(admin_email: str, target_email: str, changes: dict) -> str:
    """Admin-only: update a user's settings. Returns a confirmation message."""
    if not is_admin(admin_email):
        raise PermissionError(f"{admin_email} does not have admin access.")
    config = _load_config()
    if target_email not in config["users"]:
        raise ValueError(f"{target_email} is not in the user list.")
    config["users"][target_email].update(changes)
    _save_config(config)
    return f"Updated {target_email}: {changes}"


def add_user(admin_email: str, new_email: str, name: str, role: str = "user", daily_limit: int = None) -> str:
    """Admin-only: add a new user."""
    if not is_admin(admin_email):
        raise PermissionError(f"{admin_email} does not have admin access.")
    config = _load_config()
    if new_email in config["users"]:
        raise ValueError(f"{new_email} is already in the user list.")
    limit = daily_limit or config["global"]["default_daily_limit"]
    config["users"][new_email] = {
        "name": name,
        "role": role,
        "daily_limit": limit,
        "active": True,
    }
    _save_config(config)
    return f"Added {new_email} ({name}) as {role} with daily limit {limit}."
