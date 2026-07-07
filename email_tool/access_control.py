"""
Checks whether a user is authorized to send email and enforces daily limits.

Config is loaded from the APP_CONFIG_JSON environment variable in production
(populated from the clark/email-gateway secret in AWS Secrets Manager at cold
start — see bootstrap.py). Falls back to config.json for local development.

Daily send counts live in DynamoDB when GATEWAY_TABLE is set (Lambda) so they
are shared across containers; locally they are in memory and reset on restart.
Permanent user/settings changes are made by editing APP_CONFIG_JSON inside the
clark/email-gateway secret.
"""

import datetime
import json
import os
from pathlib import Path

import state_store

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


def get_inbound_config() -> dict:
    """Return the 'inbound' config block, or a disabled default if absent."""
    config = _load_config()
    return config.get("inbound", {"enabled": False, "poll_seconds": 300, "mailboxes": []})


def get_user(email: str) -> dict:
    config = _load_config()
    user = config["users"].get(email)
    if not user:
        raise ValueError(
            f"{email} is not in the authorized user list. "
            "Ask an admin to add you to APP_CONFIG_JSON in the "
            "clark/email-gateway secret (AWS Secrets Manager)."
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
    if state_store.enabled():
        sent_today = state_store.get_daily_count(email, today)
    else:
        sent_today = _daily_counts.get(today, {}).get(email, 0)
    remaining = limit - sent_today
    if remaining <= 0:
        raise RuntimeError(
            f"Daily send limit of {limit} reached for {email}. Resets at "
            "midnight. An admin can raise your limit by updating "
            "APP_CONFIG_JSON in the clark/email-gateway secret."
        )
    return remaining


def consume_daily_limit(email: str) -> int:
    """Atomically count a send against email's daily limit.

    Increments first, then checks the post-increment total, so two concurrent
    sends cannot both slip under the limit (the read-then-write race in
    check_daily_limit + record_send). Backs the increment out and raises if
    the limit would be exceeded. Returns sends remaining after this one.
    """
    config = _load_config()
    user = get_user(email)
    limit = user.get("daily_limit", config["global"]["default_daily_limit"])
    today = datetime.date.today().isoformat()
    if state_store.enabled():
        new_count = state_store.increment_daily_count(email, today)
        if new_count > limit:
            state_store.decrement_daily_count(email, today)
            raise RuntimeError(
                f"Daily send limit of {limit} reached for {email}. Resets at "
                "midnight. An admin can raise your limit by updating "
                "APP_CONFIG_JSON in the clark/email-gateway secret."
            )
        return limit - new_count
    # Local/in-memory path (single process, no real race).
    check_daily_limit(email)
    record_send(email)
    sent_today = _daily_counts.get(today, {}).get(email, 0)
    return limit - sent_today


def refund_send(email: str) -> None:
    """Back out one consumed send (e.g. the Gmail send itself failed)."""
    today = datetime.date.today().isoformat()
    if state_store.enabled():
        state_store.decrement_daily_count(email, today)
        return
    day = _daily_counts.get(today, {})
    if day.get(email, 0) > 0:
        day[email] -= 1


def record_send(email: str) -> None:
    today = datetime.date.today().isoformat()
    if state_store.enabled():
        state_store.increment_daily_count(email, today)
        return
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
    use_store = state_store.enabled()

    users_summary = []
    for email, user in config["users"].items():
        limit = user.get("daily_limit", config["global"]["default_daily_limit"])
        sent_today = (
            state_store.get_daily_count(email, today)
            if use_store
            else today_counts.get(email, 0)
        )
        users_summary.append({
            "email": email,
            "name": user.get("name", ""),
            "role": user.get("role", "user"),
            "daily_limit": limit,
            "sent_today": sent_today,
            "active": user.get("active", False),
        })

    return {
        "global": config["global"],
        "users": users_summary,
    }


# NOTE: user management happens by editing APP_CONFIG_JSON in the
# clark/email-gateway secret (see notes/DEPLOY-LAMBDA.md). The old
# update_user/add_user helpers were removed: they were not exposed as MCP
# tools, and their in-memory writes would silently vanish on Lambda.
