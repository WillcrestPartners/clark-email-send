"""
Checks whether a user is authorized to send email and enforces daily limits.

Config is loaded from the APP_CONFIG_JSON environment variable in production
(AWS App Runner). Falls back to config.json file for local development.

Daily send counts are kept in memory — they reset if the server restarts,
which is acceptable for this use case. Permanent user/settings changes
require updating APP_CONFIG_JSON in the AWS App Runner console.
"""

import datetime
import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

# In-memory state — resets on restart
_config_cache = None
_daily_counts: dict = {}  # { "YYYY-MM-DD": { "email": count } }


def _load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    env_config = os.environ.get("APP_CONFIG_JSON")
    if env_config:
        _config_cache = json.loads(env_config)
    elif CONFIG_PATH.exists():
        _config_cache = json.loads(CONFIG_PATH.read_text())
    else:
        raise FileNotFoundError(
            "No config found. Set APP_CONFIG_JSON as an environment variable "
            "in AWS App Runner, or create config.json for local development."
        )
    return _config_cache


def _save_config(config: dict) -> None:
    global _config_cache
    _config_cache = config
    if not os.environ.get("APP_CONFIG_JSON") and CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(config, indent=2))


def get_user(email: str) -> dict:
    config = _load_config()
    user = config["users"].get(email)
    if not user:
        raise ValueError(
            f"{email} is not in the authorized user list. "
            "Ask an admin to add you via the AWS App Runner console."
        )
    if not user.get("active", False):
        raise ValueError(
            f"Access for {email} is currently suspended. Contact an admin."
        )
    return user


def check_daily_limit(email: str) -> int:
    config = _load_config()
    user = get_user(email)
    limit = user.get("daily_limit", config["global"]["default_daily_limit"])
    today = datetime.date.today().isoformat()
    sent_today = _daily_counts.get(today, {}).get(email, 0)
    remaining = limit - sent_today
    if remaining <= 0:
        raise RuntimeError(
            f"Daily send limit of {limit} reached for {email}. "
            "Resets at midnight (or on server restart). "
            "An admin can raise your limit by updating APP_CONFIG_JSON in AWS."
        )
    return remaining


def record_send(email: str) -> None:
    today = datetime.date.today().isoformat()
    _daily_counts.setdefault(today, {})
    _daily_counts[today][email] = _daily_counts[today].get(email, 0) + 1


def is_admin(email: str) -> bool:
    config = _load_config()
    user = config["users"].get(email, {})
    return user.get("role") == "admin"


def get_dashboard_data() -> dict:
    config = _load_config()
    today = datetime.date.today().isoformat()
    today_counts = _daily_counts.get(today, {})

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
    if not is_admin(admin_email):
        raise PermissionError(f"{admin_email} does not have admin access.")
    config = _load_config()
    if target_email not in config["users"]:
        raise ValueError(f"{target_email} is not in the user list.")
    config["users"][target_email].update(changes)
    _save_config(config)
    return (
        f"Updated {target_email}: {changes}\n"
        "Note: this change is active until the next server restart. "
        "To make it permanent, update APP_CONFIG_JSON in AWS App Runner."
    )


def add_user(admin_email: str, new_email: str, name: str, role: str = "user", daily_limit: int = None) -> str:
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
    return (
        f"Added {new_email} ({name}) as {role} with daily limit {limit}.\n"
        "Note: to make this permanent, update APP_CONFIG_JSON in AWS App Runner."
    )
