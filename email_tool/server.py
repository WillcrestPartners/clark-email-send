"""
Clark Email MCP Server

Exposes four tools to Claude:
  - send_email        - compose and send an email (with confirmation gate)
  - show_dashboard    - admin-only view of users, limits, and recent activity
  - check_my_access   - any user can see their own access status
  - run_diagnostics   - full health check: credentials, scopes, access, limits

To run locally:
    cd email_tool && python server.py

On Lambda the same tools + Starlette app (server.build_app) are served by
lambda_web.handler; inbound polling runs in lambda_poll.handler. See
infra/template.yaml and notes/DEPLOY-LAMBDA.md.
"""

import os
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

import access_control
import audit_log
import clark_client
import diagnostics
import gmail_client
import oauth
import poller

load_dotenv()


def _stateless_http() -> bool:
    """Whether to run MCP in stateless HTTP mode (required on Lambda).

    On Lambda each invocation is independent, so MCP session state cannot be
    kept in memory between requests. MCP_STATELESS_HTTP=true makes every
    request self-contained. Local/long-running deploys leave it unset for the
    default stateful streamable-HTTP behaviour.
    """
    return os.environ.get("MCP_STATELESS_HTTP", "").strip().lower() in ("1", "true", "yes")


mcp = FastMCP(
    "Clark Email Tool",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8080)),
    stateless_http=_stateless_http(),
)

SENDER = os.environ.get("SENDER_EMAIL", "clark@willcrestpartners.com")

# Caller identity is resolved by the OAuth middleware (oauth.py) from the
# connector's bearer token — no tool takes a caller_email argument anymore
# (specs/connector-oauth.md in willcrestpartners/clark). During the cutover
# window (CONNECTOR_AUTH_REQUIRED=false) a legacy self-asserted caller_email
# in the request body is still honored when no token is present; a forged
# caller_email argument is otherwise ignored entirely.


@mcp.tool()
def send_email(
    to: str,
    subject: str,
    body: str,
    confirmed: bool = False,
) -> str:
    """
    Send an email from clark@willcrestpartners.com.

    IMPORTANT: Always call this twice.
    First call: confirmed=False -> returns a preview for the human to review.
    Second call: confirmed=True -> actually sends the email.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text.
        confirmed: Must be True to actually send. Use False first to show preview.
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    try:
        access_control.get_user(caller_email)
        access_control.check_daily_limit(caller_email)
    except (ValueError, RuntimeError) as e:
        audit_log.log_attempt(caller_email, to, subject, "failed", reason=str(e))
        return f"Cannot send: {e}"

    if not confirmed:
        return (
            f"EMAIL PREVIEW - please confirm before sending:\n\n"
            f"From:    {SENDER}\n"
            f"To:      {to}\n"
            f"Subject: {subject}\n"
            f"{'-' * 40}\n"
            f"{body}\n"
            f"{'-' * 40}\n\n"
            f"To send this email, call send_email again with confirmed=True.\n"
            f"To cancel, simply do not call it again."
        )

    # Consume the daily limit atomically BEFORE sending (increment-then-check),
    # so concurrent invocations cannot both slip under the limit. Refund it if
    # the Gmail send itself fails.
    try:
        access_control.consume_daily_limit(caller_email)
    except (ValueError, RuntimeError) as e:
        audit_log.log_attempt(caller_email, to, subject, "failed", reason=str(e))
        return f"Cannot send: {e}"

    try:
        copy_to_sent = access_control._load_config()["global"].get("copy_to_sent_folder", True)
        message_id, sent_folder_copied = gmail_client.send_email(SENDER, to, subject, body, copy_to_sent)
        audit_log.log_attempt(caller_email, to, subject, "sent", message_id=message_id)
        note = "" if sent_folder_copied else " (note: could not copy to clark's Sent folder)"
        return f"Email sent successfully to {to}.{note}"
    except Exception as e:
        access_control.refund_send(caller_email)
        audit_log.log_attempt(caller_email, to, subject, "failed", reason=str(e))
        return f"Failed to send email: {e}"


@mcp.tool()
def show_dashboard() -> str:
    """
    Admin-only: show current settings, authorized users, and recent activity.
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    if not access_control.is_admin(caller_email):
        return f"Access denied. {caller_email} does not have admin privileges."

    data = access_control.get_dashboard_data()
    g = data["global"]

    lines = [
        "CLARK EMAIL TOOL - ADMIN DASHBOARD",
        "=" * 50,
        f"Sender:              {g['sender_email']}",
        f"Default daily limit: {g['default_daily_limit']} emails/user",
        f"Confirmation gate:   {'on' if g['confirmation_required'] else 'off'}",
        f"Copy to Sent folder: {'yes' if g['copy_to_sent_folder'] else 'no'}",
        "",
        "AUTHORIZED USERS",
        "-" * 70,
        f"{'Email':<35} {'Role':<8} {'Limit':<7} {'Today':<7} {'Active'}",
        "-" * 70,
    ]

    for u in data["users"]:
        active_str = "yes" if u["active"] else "no (suspended)"
        lines.append(
            f"{u['email']:<35} {u['role']:<8} {u['daily_limit']:<7} "
            f"{u['sent_today']:<7} {active_str}"
        )

    recent = audit_log.get_recent(10)
    lines += ["", "RECENT ACTIVITY (last 10)", "-" * 70]
    if not recent:
        lines.append("No activity yet.")
    for entry in recent:
        status_str = "sent" if entry["status"] == "sent" else f"FAILED: {entry.get('reason', '')}"
        lines.append(
            f"{entry['time'][:16]}  {entry['user']:<30} -> {entry['to']:<25} {status_str}"
        )

    return "\n".join(lines)


@mcp.tool()
def run_diagnostics() -> str:
    """
    Run a full diagnostic check on the Clark Email Tool.

    Tests server health (credentials, Gmail scopes, mailbox access) and
    caller status (authorization, account active, daily limit, guardrails).
    Available to all callers — no authorization required to run diagnostics.
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    return diagnostics.run_all(caller_email)


@mcp.tool()
def check_my_access() -> str:
    """
    Check your own access status and remaining sends for today.
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    try:
        user = access_control.get_user(caller_email)
        remaining = access_control.check_daily_limit(caller_email)
        return (
            f"Access confirmed for {caller_email}.\n"
            f"Role: {user['role']}\n"
            f"Daily limit: {user['daily_limit']}\n"
            f"Remaining today: {remaining}"
        )
    except (ValueError, RuntimeError) as e:
        return f"Access check failed: {e}"


# ── inbound command-bus tools (Phase 1) ─────────────────────────────────────

@mcp.tool()
def poll_inbox() -> str:
    """
    Admin-only: trigger one inbound poll sweep now and return a summary.

    The gateway normally polls on a timer; this forces an immediate sweep
    (list unread mail, gate, and route to Clark). Runs NO LLM — gating is
    deterministic and intent classification happens in Clark.
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    if not access_control.is_admin(caller_email):
        return f"Access denied. {caller_email} does not have admin privileges."

    summary = poller.poll_once()
    lines = [
        "INBOUND POLL SUMMARY",
        "-" * 40,
        f"Mailboxes polled:   {summary['mailboxes']}",
        f"Messages listed:    {summary['listed']}",
        f"Acked to Clark:     {summary['acked']}",
        f"Skipped (seen):     {summary['skipped_seen']}",
        f"Ignored (sender):   {summary['ignored_sender']}",
        f"Dropped (hygiene):  {summary['dropped_hygiene']}",
        f"Failed:             {summary['failed']}",
        f"Last successful:    {poller.last_successful_poll()}",
    ]
    if summary["errors"]:
        lines.append("Errors:")
        lines += [f"  - {e}" for e in summary["errors"]]
    return "\n".join(lines)


@mcp.tool()
def verify_sender(from_email: str) -> str:
    """
    Report whether an address is on the cached inbound allow-list.

    Args:
        from_email: The address to check against Clark's authorized-sender list.
    """
    import gateway_gate

    inbound = access_control.get_inbound_config()
    secret = os.environ.get("CLARK_INBOUND_HMAC_SECRET", "")
    senders_url = ""
    for mb in inbound.get("mailboxes", []):
        dest = mb.get("destination", {})
        senders_url = dest.get("authorized_senders_url") or os.environ.get(
            "CLARK_AUTHORIZED_SENDERS_URL", ""
        )
        if senders_url:
            break

    allowed = clark_client.fetch_authorized_senders(senders_url, secret)
    ok = gateway_gate.sender_allowed(from_email, allowed)
    age = clark_client.senders_cache_age()
    age_str = "never refreshed" if age == float("inf") else f"{int(age)}s ago"
    status = "AUTHORIZED" if ok else "NOT on the allow-list"
    return (
        f"{from_email} is {status}.\n"
        f"Allow-list size: {len(allowed)} (refreshed {age_str})."
    )


def _connector_base() -> str:
    """Base URL of Clark's /api/connector/* endpoints (the CIM-intake back end)."""
    return os.environ.get("CLARK_CONNECTOR_BASE_URL", "").rstrip("/")


@mcp.tool()
def search_companies(query: str) -> str:
    """
    Search Clark for companies that may already exist, so a CIM is not
    duplicated. Matches on company name, project (deal codename), and aliases.

    Args:
        query: Company name or deal codename to look up.
    """
    import urllib.parse

    caller_email, err = oauth.caller_email()
    if err:
        return err
    base = _connector_base()
    secret = os.environ.get("CLARK_INBOUND_HMAC_SECRET", "")
    if not base or not secret:
        return "CIM intake is not configured (missing CLARK_CONNECTOR_BASE_URL or HMAC secret)."
    qs = urllib.parse.urlencode({"caller_email": caller_email, "q": query})
    status, data = clark_client.get_signed(f"{base}/api/connector/companies?{qs}", secret)
    if status != 200 or not isinstance(data, dict):
        return f"Company search failed (HTTP {status}): {data}"
    candidates = data.get("candidates", [])
    if not candidates:
        return f'No existing company matches "{query}". You can create a new one.'
    lines = [
        f"- {c.get('name')}"
        + (f" [project: {c.get('project_name')}]" if c.get("project_name") else "")
        + (f" — {c.get('type')}" if c.get("type") else "")
        + (f", {c.get('city')}, {c.get('state')}" if c.get("city") or c.get("state") else "")
        + f"  (id: {c.get('id')})"
        for c in candidates
    ]
    return (
        f'Possible matches for "{query}" — confirm with the user which to UPDATE, '
        f"or create a new company:\n" + "\n".join(lines)
    )


@mcp.tool()
def analyze_cim(dropbox_folder_path: str) -> str:
    """
    Locate the CIM in a Dropbox deal folder and extract its facts (server-side;
    the PDF is never uploaded through the chat). Returns the extracted JSON for
    you to review with the user before submitting.

    Args:
        dropbox_folder_path: Full Dropbox folder path, e.g.
            "/Willcrest - Sourcing/Opportunities/Franklin Alliance".
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    base = _connector_base()
    secret = os.environ.get("CLARK_INBOUND_HMAC_SECRET", "")
    if not base or not secret:
        return "CIM intake is not configured (missing CLARK_CONNECTOR_BASE_URL or HMAC secret)."
    status, data = clark_client.post_signed(
        f"{base}/api/connector/analyze-cim",
        secret,
        {"caller_email": caller_email, "dropbox_folder_path": dropbox_folder_path},
    )
    if not isinstance(data, dict):
        return f"CIM analysis failed (HTTP {status})."
    st = data.get("status")
    if st == "ok":
        import json as _json

        return (
            f"Analyzed {data['file']['name']}. Extracted facts (review with the user, "
            f"fill gaps, then call submit_cim_intake):\n"
            + _json.dumps(data.get("extraction", {}), indent=2)
        )
    # non_pdf / empty / error all carry a human-readable message.
    return data.get("message", f"Could not analyze the folder (status {st}, HTTP {status}).")


@mcp.tool()
def submit_cim_intake(payload_json: str) -> str:
    """
    Submit the user-confirmed CIM data to Clark. Creates a single approval
    request and returns one-tap Approve/Reject links to show the user IN THIS
    CHAT — nothing is written to Clark until they approve.

    Args:
        payload_json: A JSON string with the confirmed intake, shape:
            {
              "company": {"mode": "create"|"update", "company_id"?, "name",
                          "fields": {type, website, description, headline, theme,
                                     city, state, employee_count, phone, ...}},
              "financials": [{"year", "revenue_000s", "ebitda_000s"}],
              "banker": {"first_name","last_name","title","firm","email","phone"},
              "key_contacts": [{"first_name","last_name","title","email"}],
              "dropbox_folder_path": "...", "cim_filename": "..."
            }
    """
    import json as _json

    caller_email, err = oauth.caller_email()
    if err:
        return err
    base = _connector_base()
    secret = os.environ.get("CLARK_INBOUND_HMAC_SECRET", "")
    if not base or not secret:
        return "CIM intake is not configured (missing CLARK_CONNECTOR_BASE_URL or HMAC secret)."
    try:
        payload = _json.loads(payload_json)
    except Exception as e:
        return f"payload_json is not valid JSON: {e}"

    status, data = clark_client.post_signed(
        f"{base}/api/connector/cim-intake",
        secret,
        {"caller_email": caller_email, "payload": payload},
    )
    if not isinstance(data, dict):
        return f"CIM intake failed (HTTP {status})."
    if status != 200:
        return f"CIM intake failed (HTTP {status}): {data.get('error', data)}"

    lines = [
        f"Created approval {data.get('approval_id')} ({data.get('risk_level')} risk, "
        f"{data.get('action_count')} action(s)). Nothing has been written yet.",
    ]
    if data.get("approve_url"):
        lines.append(f"Approve: {data['approve_url']}")
        lines.append(f"Reject:  {data['reject_url']}")
    lines.append(f"Review in Clark: {data.get('app_url')}")
    if data.get("ambiguous"):
        lines.append("Note: a reference could not be uniquely resolved — confirm on the approval before approving.")
    return "\n".join(lines)


# ── mobile/voice connector tools ────────────────────────────────────────────
# Thin, signed proxies over Clark's /api/connector/* routes that back the Cowork
# mobile/voice UI (spec: specs/mobile-voice-interface.md; skill: skills/clark in
# willcrestpartners/clark). Same contract as the CIM trio above: the gateway
# resolves caller_email from the connector's OAuth token (oauth.py) and passes
# it through; Clark enforces the authorized-user allow-list + app-layer
# permissions server-side; GET signs the empty body, POST signs the raw JSON
# body; both reuse CLARK_INBOUND_HMAC_SECRET over CLARK_CONNECTOR_BASE_URL. The
# gateway stays a thin transport (no parsing, no LLM, no DB) and returns each
# route's JSON body straight through to the model.

_CONNECTOR_UNCONFIGURED = (
    "Clark connector is not configured "
    "(missing CLARK_CONNECTOR_BASE_URL or HMAC secret)."
)


def _connector_env():
    """Return (base_url, hmac_secret), or (None, None) if either is missing."""
    base = _connector_base()
    secret = os.environ.get("CLARK_INBOUND_HMAC_SECRET", "")
    if not base or not secret:
        return None, None
    return base, secret


def _connector_result(action: str, status: int, data) -> str:
    """Pass a connector route's JSON body back to the model unmodified.

    Detail routes return {"text": ...} already formatted for Claude — surface
    that string directly. Everything else (the list/shortlist routes, the
    approval responses, the Granola sync result) is returned as its JSON body.
    On a non-200 the route's {error} (and {app_url} when present, e.g. the
    High-risk 403 from act_on_approval) is surfaced so the user can act in-app.
    """
    import json as _json

    if not isinstance(data, dict):
        return f"{action} failed (HTTP {status})."
    if status != 200:
        msg = data.get("error", data)
        app_url = data.get("app_url")
        if app_url:
            return f"{action} failed (HTTP {status}): {msg}\nReview in Clark: {app_url}"
        return f"{action} failed (HTTP {status}): {msg}"
    if isinstance(data.get("text"), str):
        return data["text"]
    return _json.dumps(data, indent=2, ensure_ascii=False)


@mcp.tool()
def search_contacts(
    q: str = "",
    role: str = "",
    city: str = "",
    state: str = "",
    limit: int = 0,
) -> str:
    """
    Look someone up in Clark, or build a shortlist of people: a contact's phone
    number or email, quick call-prep, "brokers in Dallas", "lenders in Texas".
    Provide AT LEAST ONE filter; combine them to narrow the shortlist.

    Args:
        q: Free-text name or email fragment (e.g. "Jane Smith", "acme.com").
        role: Contact role/type filter (e.g. "Broker", "Lender"); must be a
            valid Clark contact role.
        city: One or more cities, comma-separated (e.g. "Dallas,Fort Worth").
        state: Two-letter state code (e.g. "TX").
        limit: Max number of results to return (optional).
    """
    import urllib.parse

    caller_email, err = oauth.caller_email()
    if err:
        return err
    base, secret = _connector_env()
    if not base:
        return _CONNECTOR_UNCONFIGURED
    params = {"caller_email": caller_email}
    if q:
        params["q"] = q
    if role:
        params["role"] = role
    if city:
        params["city"] = city
    if state:
        params["state"] = state
    if limit:
        params["limit"] = limit
    qs = urllib.parse.urlencode(params)
    status, data = clark_client.get_signed(f"{base}/api/connector/contacts?{qs}", secret)
    return _connector_result("Contact search", status, data)


@mcp.tool()
def get_contact(contact_id: str) -> str:
    """
    Pull a single contact's full record from Clark by id — phone/mobile, email,
    company, role, and recent activity for call-prep. Get the id first from
    search_contacts.

    Args:
        contact_id: The contact's Clark id (UUID).
    """
    import urllib.parse

    caller_email, err = oauth.caller_email()
    if err:
        return err
    base, secret = _connector_env()
    if not base:
        return _CONNECTOR_UNCONFIGURED
    qs = urllib.parse.urlencode({"caller_email": caller_email})
    status, data = clark_client.get_signed(
        f"{base}/api/connector/contacts/{urllib.parse.quote(contact_id)}?{qs}", secret
    )
    return _connector_result("Contact lookup", status, data)


@mcp.tool()
def get_company(company_id: str) -> str:
    """
    Pull a single company/deal record from Clark by id — profile, financials,
    banker, key contacts, and status. Get the id first from search_companies.

    Args:
        company_id: The company's Clark id (UUID).
    """
    import urllib.parse

    caller_email, err = oauth.caller_email()
    if err:
        return err
    base, secret = _connector_env()
    if not base:
        return _CONNECTOR_UNCONFIGURED
    qs = urllib.parse.urlencode({"caller_email": caller_email})
    status, data = clark_client.get_signed(
        f"{base}/api/connector/companies/{urllib.parse.quote(company_id)}?{qs}", secret
    )
    return _connector_result("Company lookup", status, data)


@mcp.tool()
def submit_contact(payload_json: str, source_text: str = "") -> str:
    """
    Add a person to Clark (a new relationship, a recruiting candidate, a banker
    met at a conference), optionally with the company they belong to and a first
    activity. Creates a single approval request and returns one-tap
    Approve/Reject links to show the user IN THIS CHAT — nothing is written to
    Clark until they approve.

    Args:
        payload_json: A JSON string with the confirmed contact, shape:
            {
              "contact": {"first_name"*, "last_name"*, "title"?, "role"?,
                          "email"?, "secondary_email"?, "phone"?, "mobile"?,
                          "city"?, "state"?, "specialty"?, "description"?},
              "company"?: {"mode": "existing", "company_id"}
                        | {"mode": "create", "name", "fields"?},
              "activity"?: {"activity_type"?, "subject"*, "notes"?,
                            "activity_date"?}
            }
            (* required). Risk is Medium when company.mode='create', else Low.
        source_text: Optional raw text the contact was dictated/derived from.
    """
    import json as _json

    caller_email, err = oauth.caller_email()
    if err:
        return err
    base, secret = _connector_env()
    if not base:
        return _CONNECTOR_UNCONFIGURED
    try:
        payload = _json.loads(payload_json)
    except Exception as e:
        return f"payload_json is not valid JSON: {e}"

    body = {"caller_email": caller_email, "payload": payload}
    if source_text:
        body["source_text"] = source_text
    status, data = clark_client.post_signed(
        f"{base}/api/connector/submit-contact", secret, body
    )
    return _connector_result("Add-contact request", status, data)


@mcp.tool()
def submit_activity(payload_json: str, source_text: str = "") -> str:
    """
    Log a call or meeting note in Clark, or re-code an existing activity's links.
    Use for "log a call with…", "add a note to…", dictated call notes. Names can
    be dictated (contact_names/company_names) — Clark resolves them, or returns
    candidates when ambiguous. Creates a single (Low-risk) approval request and
    returns one-tap Approve/Reject links to show the user IN THIS CHAT — nothing
    is written until they approve.

    Args:
        payload_json: A JSON string, shape:
            {
              "activity_id"?,      // present = UPDATE mode (edit / re-code links)
              "contact_ids"?, "company_ids"?,
              "contact_names"?,    // dictated names; unambiguous ones resolve to
                                   // ids, ambiguous ones return pick-list candidates
              "activity_type"?,    // default "Phone Call"
              "subject"?,          // required in CREATE mode
              "notes"?, "activity_date"?  // activity_date is YYYY-MM-DD
            }
        source_text: Optional raw text the note was dictated/derived from.
    """
    import json as _json

    caller_email, err = oauth.caller_email()
    if err:
        return err
    base, secret = _connector_env()
    if not base:
        return _CONNECTOR_UNCONFIGURED
    try:
        payload = _json.loads(payload_json)
    except Exception as e:
        return f"payload_json is not valid JSON: {e}"

    body = {"caller_email": caller_email, "payload": payload}
    if source_text:
        body["source_text"] = source_text
    status, data = clark_client.post_signed(
        f"{base}/api/connector/submit-activity", secret, body
    )
    return _connector_result("Log-activity request", status, data)


@mcp.tool()
def sync_granola(folder: str = "") -> str:
    """
    Pull in recent Granola call/meeting notes from the team folders and import
    them into Clark as activities. Use for "sync my Granola notes", "pull in the
    Granola meetings". Runs immediately (no approval). Omit folder to sweep all
    team folders, or name one to scope the run.

    Args:
        folder: Optional team-folder name to limit the sync to.
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    base, secret = _connector_env()
    if not base:
        return _CONNECTOR_UNCONFIGURED
    body = {"caller_email": caller_email}
    if folder:
        body["folder"] = folder
    status, data = clark_client.post_signed(
        f"{base}/api/connector/sync-granola", secret, body
    )
    return _connector_result("Granola sync", status, data)


@mcp.tool()
def list_pending_approvals() -> str:
    """
    Show what's waiting for the user's approval in Clark — "anything waiting for
    my approval?", "what's in my approval queue?". Returns the pending requests
    (newest first) with their id, risk level, and summary so the user can then
    approve or reject one by id via act_on_approval.
    """
    import urllib.parse

    caller_email, err = oauth.caller_email()
    if err:
        return err
    base, secret = _connector_env()
    if not base:
        return _CONNECTOR_UNCONFIGURED
    qs = urllib.parse.urlencode({"caller_email": caller_email})
    status, data = clark_client.get_signed(
        f"{base}/api/connector/approvals?{qs}", secret
    )
    return _connector_result("Approvals list", status, data)


@mcp.tool()
def act_on_approval(approval_id: str, decision: str) -> str:
    """
    Approve or reject a pending Clark approval by id. Call this ONLY on the
    user's explicit spoken/typed approve-or-reject instruction — never on your
    own initiative. High-risk requests cannot be decided here: Clark rejects
    them server-side (403) and they must be reviewed in the app (this note is a
    courtesy; Clark enforces it, along with no-self-approval). Idempotent — a
    request that was already decided returns its current status.

    Args:
        approval_id: The id of the approval to act on (from list_pending_approvals).
        decision: Either "approve" or "reject".
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    base, secret = _connector_env()
    if not base:
        return _CONNECTOR_UNCONFIGURED
    status, data = clark_client.post_signed(
        f"{base}/api/connector/approvals/act",
        secret,
        {"caller_email": caller_email, "approval_id": approval_id, "decision": decision},
    )
    return _connector_result("Approval decision", status, data)


@mcp.tool()
def send_approval_notification(
    to: str,
    subject: str,
    body: str,
    in_reply_to: str = None,
    references: str = None,
) -> str:
    """
    Admin-only: manually relay a reply from clark@willcrestpartners.com,
    threaded into the original conversation when In-Reply-To/References given.

    This is a thin wrapper over the same threaded send used by the /send relay.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text.
        in_reply_to: Optional Message-ID to set as In-Reply-To (threading).
        references: Optional References header value (threading).
    """
    caller_email, err = oauth.caller_email()
    if err:
        return err
    if not access_control.is_admin(caller_email):
        return f"Access denied. {caller_email} does not have admin privileges."

    try:
        copy_to_sent = access_control._load_config()["global"].get("copy_to_sent_folder", True)
        message_id, copied = gmail_client.send_threaded(
            SENDER, to, subject, body, in_reply_to, references, copy_to_sent
        )
        audit_log.log_attempt(caller_email, to, subject, "sent", message_id=message_id)
        note = "" if copied else " (note: could not copy to clark's Sent folder)"
        return f"Reply sent to {to}.{note}"
    except Exception as e:
        audit_log.log_attempt(caller_email, to, subject, "failed", reason=str(e))
        return f"Failed to send reply: {e}"


# ── ASGI app factory (shared by the local server and the Lambda web handler) ─

import asyncio
import json
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount

# (The old GATEWAY_MCP_TOKEN shared-secret guard was removed here: it was
# opt-in, never enabled in production, and is superseded by the per-user
# OAuth middleware in oauth.py. If the clark/email-gateway secret still
# carries a GATEWAY_MCP_TOKEN key it is now ignored.)


def build_app(run_poller: bool = True) -> Starlette:
    """Construct the Starlette ASGI app: MCP mount + /health + /send.

    run_poller controls the in-process background poll loop. It is True for
    the always-on local/server deploy and False on Lambda, where an external
    EventBridge schedule drives poller.poll_once() via lambda_poll.handler.
    """
    mcp_app = mcp.streamable_http_app()

    async def _poll_loop():
        """Background task: poll the inbox on a timer while inbound is enabled."""
        inbound = access_control.get_inbound_config()
        interval = int(inbound.get("poll_seconds", 300))
        while True:
            try:
                await asyncio.to_thread(poller.poll_once)
            except Exception as e:  # never let the loop die
                audit_log.log_attempt("system", "", "", "failed",
                                      reason=f"poll_loop: {e}")
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(app):
        poll_task = None
        inbound = access_control.get_inbound_config()
        if run_poller and inbound.get("enabled"):
            poll_task = asyncio.create_task(_poll_loop())
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                if poll_task is not None:
                    poll_task.cancel()
                    try:
                        await poll_task
                    except asyncio.CancelledError:
                        pass

    async def health(request: Request) -> Response:
        inbound = access_control.get_inbound_config()
        return JSONResponse({
            "status": "ok",
            "inbound_enabled": bool(inbound.get("enabled")),
            "last_successful_poll": poller.last_successful_poll(),
        })

    # ── OAuth discovery (RFC 9728 / MCP auth) — public, no auth required ────
    # claude.ai resolves the authorization server from these documents:
    # 401 on /mcp -> WWW-Authenticate.resource_metadata -> protected-resource
    # metadata -> Cognito issuer (its OIDC config names all real endpoints).

    async def oauth_protected_resource(request: Request) -> Response:
        base = oauth.host_base(request.scope)
        return JSONResponse(
            oauth.protected_resource_metadata(base),
            headers=oauth.WELL_KNOWN_HEADERS,
        )

    async def oauth_as_metadata(request: Request) -> Response:
        # Served only with the DCR shim on (Cognito has no RFC 7591 endpoint):
        # mirrors the pool's OIDC metadata with registration pointed at /register.
        base = oauth.host_base(request.scope)
        meta = oauth.authorization_server_metadata(base)
        if meta is None:
            return JSONResponse({"error": "authorization server metadata unavailable"},
                                status_code=503)
        return JSONResponse(meta, headers=oauth.WELL_KNOWN_HEADERS)

    async def oauth_register(request: Request) -> Response:
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except Exception:
            body = {}
        return JSONResponse(oauth.register_response(body), status_code=201,
                            headers=oauth.WELL_KNOWN_HEADERS)

    async def send_relay(request: Request) -> Response:
        """Outbound relay: Clark POSTs replies here; we send them threaded.

        Verifies X-Clark-Signature (HMAC-SHA256 over the raw body) using
        CLARK_INBOUND_HMAC_SECRET, then sends from clark@ with In-Reply-To /
        References set for threading.
        """
        secret = os.environ.get("CLARK_INBOUND_HMAC_SECRET", "")
        raw = await request.body()
        signature = request.headers.get("X-Clark-Signature", "")
        if not secret or not clark_client.verify(raw, secret, signature):
            return JSONResponse({"error": "invalid signature"}, status_code=401)

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        to = payload.get("to")
        subject = payload.get("subject", "")
        body = payload.get("body", "")
        if not to:
            return JSONResponse({"error": "missing 'to'"}, status_code=400)

        try:
            copy_to_sent = access_control._load_config()["global"].get(
                "copy_to_sent_folder", True
            )
            message_id, copied = gmail_client.send_threaded(
                SENDER,
                to,
                subject,
                body,
                in_reply_to=payload.get("in_reply_to"),
                references=payload.get("references"),
                copy_to_sent=copy_to_sent,
            )
            audit_log.log_attempt("clark-relay", to, subject, "sent",
                                  message_id=message_id)
            return JSONResponse({
                "status": "sent",
                "message_id": message_id,
                "sent_folder_copied": copied,
                "gateway_message_id": payload.get("gateway_message_id"),
            })
        except Exception as e:
            audit_log.log_attempt("clark-relay", to, subject, "failed", reason=str(e))
            return JSONResponse({"error": str(e)}, status_code=502)

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/send", send_relay, methods=["POST"]),
        Route("/.well-known/oauth-protected-resource",
              oauth_protected_resource, methods=["GET"]),
        # Path-suffixed variant (RFC 9728 for resource "/mcp") — some clients
        # request the metadata at the resource-specific path.
        Route("/.well-known/oauth-protected-resource/mcp",
              oauth_protected_resource, methods=["GET"]),
    ]
    if oauth.dcr_shim_enabled():
        routes += [
            Route("/.well-known/oauth-authorization-server",
                  oauth_as_metadata, methods=["GET"]),
            Route("/.well-known/oauth-authorization-server/mcp",
                  oauth_as_metadata, methods=["GET"]),
            Route("/.well-known/openid-configuration",
                  oauth_as_metadata, methods=["GET"]),
            Route("/register", oauth_register, methods=["POST"]),
        ]
    # The MCP mount goes last and is the only thing behind the OAuth
    # middleware — /send keeps HMAC, /health stays public, and the poller
    # never routes through here at all.
    routes.append(Mount("/", oauth.middleware(mcp_app)))

    app = Starlette(lifespan=lifespan, routes=routes)

    return app


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(build_app(run_poller=True), host="0.0.0.0", port=port)
