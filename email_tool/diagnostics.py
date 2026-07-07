"""
Health and access diagnostics for the Clark Email Tool.
Called by the run_diagnostics MCP tool in server.py.
"""

import datetime
import json
import os

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


# ── formatters ────────────────────────────────────────────────────────────────

def _ok(label, detail=""):
    return True, f"✅ CLEAR  {label}" + (f": {detail}" if detail else "")

def _err(label, detail=""):
    return False, f"❌ ERROR  {label}" + (f": {detail}" if detail else "")

def _skip(label, detail=""):
    return None, f"⏭️  SKIP   {label}" + (f": {detail}" if detail else "")

def _info(label, detail=""):
    return None, f"ℹ️  INFO   {label}" + (f": {detail}" if detail else "")

def _warn(label, detail=""):
    return None, f"⚠️  WARN   {label}" + (f": {detail}" if detail else "")


# ── shared helpers ────────────────────────────────────────────────────────────

def _get_sa_info():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _get_config():
    raw = os.environ.get("APP_CONFIG_JSON")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _get_sender():
    return os.environ.get("SENDER_EMAIL", "clark@willcrestpartners.com")


def _refresh_token(info, scope):
    """Attempt a token refresh for the given scope to verify it is authorized."""
    import httplib2
    from google.oauth2 import service_account
    from google.auth.transport.httplib2 import Request
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[scope]
    ).with_subject(_get_sender())
    creds.refresh(Request(httplib2.Http()))


# ── server health tests ───────────────────────────────────────────────────────

def t_env_sa_json():
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        return _ok("Env: GOOGLE_SERVICE_ACCOUNT_JSON", "set")
    return _err("Env: GOOGLE_SERVICE_ACCOUNT_JSON", "not set — email sending will fail")


def t_env_app_config():
    if os.environ.get("APP_CONFIG_JSON"):
        return _ok("Env: APP_CONFIG_JSON", "set")
    return _err("Env: APP_CONFIG_JSON", "not set — no authorized users")


def t_env_sender():
    val = os.environ.get("SENDER_EMAIL")
    if val:
        return _ok("Env: SENDER_EMAIL", val)
    return _info("Env: SENDER_EMAIL", f"not set — using default ({_get_sender()})")


def t_sa_json_parses():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return _skip("Service account JSON", "env var not set")
    try:
        json.loads(raw)
        return _ok("Service account JSON", "parses correctly")
    except json.JSONDecodeError as e:
        return _err("Service account JSON", f"invalid JSON — {e}")


def t_sa_json_fields():
    info = _get_sa_info()
    if info is None:
        return _skip("Service account fields", "JSON not available")
    required = ["type", "client_email", "private_key", "token_uri"]
    missing = [f for f in required if f not in info]
    if missing:
        return _err("Service account fields", f"missing: {', '.join(missing)}")
    if info.get("type") != "service_account":
        return _err("Service account fields", f"type is '{info.get('type')}', expected 'service_account'")
    # Deliberately do NOT echo client_email: this tool is reachable on the
    # public Function URL and the SA identity is useful recon.
    return _ok("Service account fields", "all required fields present")


def t_app_config_parses():
    raw = os.environ.get("APP_CONFIG_JSON")
    if not raw:
        return _skip("App config JSON", "env var not set")
    try:
        json.loads(raw)
        return _ok("App config JSON", "parses correctly")
    except json.JSONDecodeError as e:
        return _err("App config JSON", f"invalid JSON — {e}")


def t_app_config_users():
    config = _get_config()
    if config is None:
        return _skip("App config users", "config not available")
    users = config.get("users", {})
    if not users:
        return _err("App config users", "no users configured")
    active = sum(1 for u in users.values() if u.get("active", False))
    return _ok("App config users", f"{len(users)} configured, {active} active")


def t_google_creds():
    info = _get_sa_info()
    if info is None:
        return _skip("Google credentials", "service account JSON not available")
    try:
        from google.oauth2 import service_account
        service_account.Credentials.from_service_account_info(info, scopes=[SEND_SCOPE])
        return _ok("Google credentials", "service account initialized successfully")
    except Exception as e:
        return _err("Google credentials", str(e))


def t_gmail_send_scope():
    info = _get_sa_info()
    if info is None:
        return _skip("Gmail send scope", "service account JSON not available")
    try:
        _refresh_token(info, SEND_SCOPE)
        return _ok("Gmail send scope", "authorized — token obtained successfully")
    except Exception as e:
        err = str(e)
        if "invalid_grant" in err:
            return _err("Gmail send scope", "invalid_grant — check domain-wide delegation is enabled in Google Workspace Admin")
        if "unauthorized_client" in err:
            return _err("Gmail send scope", "unauthorized_client — client ID not authorized for this scope in Workspace Admin")
        return _err("Gmail send scope", err)


def t_gmail_modify_scope():
    info = _get_sa_info()
    if info is None:
        return _skip("Gmail modify scope", "service account JSON not available")
    try:
        _refresh_token(info, MODIFY_SCOPE)
        return _ok("Gmail modify scope", "authorized — copy-to-Sent folder will work")
    except Exception as e:
        err = str(e)
        if "invalid_grant" in err:
            return _err("Gmail modify scope", "not authorized in Workspace Admin — emails will still send but won't appear in clark's Sent folder")
        if "unauthorized_client" in err:
            return _err("Gmail modify scope", "unauthorized_client — add gmail.modify scope in Workspace Admin Domain-wide Delegation")
        return _err("Gmail modify scope", err)


def t_gmail_readonly_scope():
    info = _get_sa_info()
    if info is None:
        return _skip("Gmail readonly scope", "service account JSON not available")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[READONLY_SCOPE]
        ).with_subject(_get_sender())
        service = build("gmail", "v1", credentials=creds)
        # A real list call confirms the readonly scope is granted end-to-end.
        service.users().messages().list(userId="me", maxResults=1).execute()
        return _ok("Gmail readonly scope", "authorized — inbound polling can read mail")
    except Exception as e:
        err = str(e)
        if "invalid_grant" in err:
            return _err("Gmail readonly scope", "invalid_grant — check domain-wide delegation")
        if "unauthorized_client" in err or "403" in err or "scope" in err.lower():
            return _err("Gmail readonly scope", "add gmail.readonly to Domain-wide Delegation in Workspace Admin")
        return _err("Gmail readonly scope", err)


def t_inbound_status():
    import access_control
    import poller
    inbound = access_control.get_inbound_config() if hasattr(access_control, "get_inbound_config") else {}
    if not inbound.get("enabled"):
        return _info("Inbound command bus", "disabled (no poll loop running)")
    mailboxes = ", ".join(mb.get("address", "?") for mb in inbound.get("mailboxes", [])) or "none"
    last = poller.last_successful_poll() or "never"
    return _info(
        "Inbound command bus",
        f"enabled, poll_seconds={inbound.get('poll_seconds', 300)}, "
        f"mailboxes=[{mailboxes}], last_successful_poll={last}"
    )


def t_sender_mailbox():
    info = _get_sa_info()
    sender = _get_sender()
    if info is None:
        return _skip(f"Sender mailbox ({sender})", "service account JSON not available")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=[MODIFY_SCOPE]
        ).with_subject(sender)
        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        addr = profile.get("emailAddress", sender)
        return _ok("Sender mailbox", f"{addr} accessible and impersonation confirmed")
    except Exception as e:
        err = str(e)
        if "invalid_grant" in err:
            return _err("Sender mailbox", f"{sender} could not be impersonated — verify domain-wide delegation in Workspace Admin")
        if "403" in err or "scope" in err.lower():
            return _err("Sender mailbox", "gmail.modify scope not yet authorized — authorize it in Workspace Admin to enable this check")
        return _err("Sender mailbox", f"{sender} inaccessible — {err}")


# ── caller status tests ───────────────────────────────────────────────────────

def t_caller_authorized(caller_email):
    config = _get_config()
    if config is None:
        return _skip("Caller authorized", "app config not available")
    users = config.get("users", {})
    if caller_email not in users:
        return _err("Caller authorized", f"{caller_email} is not in the authorized user list — ask an admin to add you to APP_CONFIG_JSON in the clark/email-gateway secret")
    role = users[caller_email].get("role", "user")
    return _ok("Caller authorized", f"{caller_email} (role: {role})")


def t_caller_active(caller_email):
    config = _get_config()
    if config is None:
        return _skip("Account active", "app config not available")
    user = config.get("users", {}).get(caller_email)
    if user is None:
        return _skip("Account active", "user not in config")
    if not user.get("active", False):
        return _err("Account active", f"{caller_email} is suspended — contact an admin to restore access")
    return _ok("Account active", f"{caller_email} is enabled")


def t_daily_limit(caller_email):
    import access_control
    import state_store
    config = _get_config()
    if config is None:
        return _skip("Daily limit", "app config not available")
    user = config.get("users", {}).get(caller_email)
    if user is None:
        return _skip("Daily limit", "user not in config")
    default_limit = config.get("global", {}).get("default_daily_limit", 20)
    limit = user.get("daily_limit", default_limit)
    today = datetime.date.today().isoformat()
    # Real counts live in DynamoDB on Lambda; the in-memory dict is local-dev only.
    if state_store.enabled():
        sent_today = state_store.get_daily_count(caller_email, today)
    else:
        sent_today = access_control._daily_counts.get(today, {}).get(caller_email, 0)
    remaining = limit - sent_today
    if remaining <= 0:
        return _err("Daily limit", f"{sent_today} of {limit} sends used today — limit reached, resets at midnight")
    return _ok("Daily limit", f"{sent_today} of {limit} sends used today, {remaining} remaining")


def t_global_settings():
    config = _get_config()
    if config is None:
        return []
    g = config.get("global", {})
    lines = []
    conf = g.get("confirmation_required", True)
    _, line = _info(
        "Confirmation gate",
        "ON — preview required before every send" if conf else "OFF — emails send immediately without preview"
    )
    lines.append(line)
    copy_sent = g.get("copy_to_sent_folder", True)
    _, line = _info("Copy to Sent folder", "enabled" if copy_sent else "disabled")
    lines.append(line)
    default_limit = g.get("default_daily_limit", 20)
    _, line = _info(
        "Google Workspace sending quota",
        f"Clark's configured limit is {default_limit}/day per user. Google Workspace hard cap is 2,000 emails/day total."
    )
    lines.append(line)
    # (The old "multiple ECS tasks" per-task count warning was removed: on
    # Lambda, daily counts are shared in DynamoDB across all containers.)
    return lines


# ── main entry point ──────────────────────────────────────────────────────────

def run_all(caller_email: str) -> str:
    server_tests = [
        t_env_sa_json,
        t_env_app_config,
        t_env_sender,
        t_sa_json_parses,
        t_sa_json_fields,
        t_app_config_parses,
        t_app_config_users,
        t_google_creds,
        t_gmail_send_scope,
        t_gmail_modify_scope,
        t_gmail_readonly_scope,
        t_sender_mailbox,
        t_inbound_status,
    ]

    server_lines = []
    s_passed = s_failed = 0

    for test in server_tests:
        result, line = test()
        server_lines.append(line)
        if result is True:
            s_passed += 1
        elif result is False:
            s_failed += 1

    caller_lines = []
    c_passed = c_failed = 0

    for fn in [t_caller_authorized, t_caller_active, t_daily_limit]:
        result, line = fn(caller_email)
        caller_lines.append(line)
        if result is True:
            c_passed += 1
        elif result is False:
            c_failed += 1

    caller_lines += t_global_settings()

    sep = "═" * 62

    if s_failed == 0:
        server_summary = f"SERVER: {s_passed} passed — all clear"
    else:
        server_summary = f"SERVER: {s_passed} passed, {s_failed} failed"

    if c_failed == 0:
        caller_summary = "CALLER: all clear"
    else:
        caller_summary = f"CALLER: {c_failed} issue(s) blocking sends"

    return "\n".join([
        "CLARK DIAGNOSTIC PANEL",
        sep,
        "SERVER HEALTH",
        *server_lines,
        "",
        f"CALLER STATUS  ─  {caller_email}",
        *caller_lines,
        sep,
        f"{server_summary}   {caller_summary}",
    ])
