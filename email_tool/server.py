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


@mcp.tool()
def send_email(
    caller_email: str,
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
        caller_email: The email address of the person requesting the send.
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text.
        confirmed: Must be True to actually send. Use False first to show preview.
    """
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
def show_dashboard(caller_email: str) -> str:
    """
    Admin-only: show current settings, authorized users, and recent activity.

    Args:
        caller_email: Must be an admin user.
    """
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
def run_diagnostics(caller_email: str) -> str:
    """
    Run a full diagnostic check on the Clark Email Tool.

    Tests server health (credentials, Gmail scopes, mailbox access) and
    caller status (authorization, account active, daily limit, guardrails).
    Available to all callers — no authorization required to run diagnostics.

    Args:
        caller_email: Your email address (used to check your specific access and limits).
    """
    return diagnostics.run_all(caller_email)


@mcp.tool()
def check_my_access(caller_email: str) -> str:
    """
    Check your own access status and remaining sends for today.

    Args:
        caller_email: Your email address.
    """
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
def poll_inbox(caller_email: str) -> str:
    """
    Admin-only: trigger one inbound poll sweep now and return a summary.

    The gateway normally polls on a timer; this forces an immediate sweep
    (list unread mail, gate, and route to Clark). Runs NO LLM — gating is
    deterministic and intent classification happens in Clark.

    Args:
        caller_email: Must be an admin user.
    """
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
def verify_sender(caller_email: str, from_email: str) -> str:
    """
    Report whether an address is on the cached inbound allow-list.

    Args:
        caller_email: Your email address.
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


@mcp.tool()
def send_approval_notification(
    caller_email: str,
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
        caller_email: Must be an admin user.
        to: Recipient email address.
        subject: Email subject line.
        body: Email body text.
        in_reply_to: Optional Message-ID to set as In-Reply-To (threading).
        references: Optional References header value (threading).
    """
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
import hmac as _hmac
import json
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Mount


def _token_guard(app, token: str):
    """Optional shared-secret gate for the MCP mount.

    The Function URL is public (AuthType NONE) and MCP tools trust a
    self-asserted caller_email, so URL secrecy is otherwise the only barrier.
    When GATEWAY_MCP_TOKEN is set (via the clark/email-gateway secret), /mcp
    requires `Authorization: Bearer <token>` or `X-Clark-Gateway-Token:
    <token>`; when unset, behavior is unchanged (opt-in so enabling it can be
    coordinated with the Cowork connector's header config). /send is not
    gated — it has its own HMAC check.
    """
    if not token:
        return app

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            supplied = headers.get("authorization", "")
            if supplied.lower().startswith("bearer "):
                supplied = supplied[7:]
            else:
                supplied = headers.get("x-clark-gateway-token", "")
            if not _hmac.compare_digest(supplied, token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"error": "unauthorized"}'})
                return
        await app(scope, receive, send)

    return guarded


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

    app = Starlette(
        lifespan=lifespan,
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/send", send_relay, methods=["POST"]),
            Mount("/", _token_guard(mcp_app, os.environ.get("GATEWAY_MCP_TOKEN", ""))),
        ],
    )

    return app


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(build_app(run_poller=True), host="0.0.0.0", port=port)
