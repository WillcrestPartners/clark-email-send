"""
Clark Email MCP Server

Exposes three tools to Claude:
  • send_email        — compose and send an email (with confirmation gate)
  • show_dashboard    — admin-only view of users, limits, and recent activity
  • check_my_access   — any user can see their own access status

To run locally:
    cd email_tool && python server.py

To deploy: push to GitHub; Railway picks it up automatically.
"""

import os
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv

import access_control
import audit_log
import gmail_client

load_dotenv()

mcp = FastMCP(
    "Clark Email Tool",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8080)),
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
    First call: confirmed=False → returns a preview for the human to review.
    Second call: confirmed=True → actually sends the email.

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
            f"EMAIL PREVIEW — please confirm before sending:\n\n"
            f"From:    {SENDER}\n"
            f"To:      {to}\n"
            f"Subject: {subject}\n"
            f"{'─' * 40}\n"
            f"{body}\n"
            f"{'─' * 40}\n\n"
            f"To send this email, call send_email again with confirmed=True.\n"
            f"To cancel, simply do not call it again."
        )

    try:
        copy_to_sent = access_control._load_config()["global"].get("copy_to_sent_folder", True)
        message_id = gmail_client.send_email(SENDER, to, subject, body, copy_to_sent)
        access_control.record_send(caller_email)
        audit_log.log_attempt(caller_email, to, subject, "sent", message_id=message_id)
        return f"Email sent successfully to {to}."
    except Exception as e:
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
        "CLARK EMAIL TOOL — ADMIN DASHBOARD",
        "=" * 50,
        f"Sender:              {g['sender_email']}",
        f"Default daily limit: {g['default_daily_limit']} emails/user",
        f"Confirmation gate:   {'on' if g['confirmation_required'] else 'off'}",
        f"Copy to Sent folder: {'yes' if g['copy_to_sent_folder'] else 'no'}",
        "",
        "AUTHORIZED USERS",
        f"{'─' * 70}",
        f"{'Email':<35} {'Role':<8} {'Limit':<7} {'Today':<7} {'Active'}",
        f"{'─' * 70}",
    ]

    for u in data["users"]:
        active_str = "✓" if u["active"] else "✗ suspended"
        lines.append(
            f"{u['email']:<35} {u['role']:<8} {u['daily_limit']:<7} "
            f"{u['sent_today']:<7} {active_str}"
        )

    recent = audit_log.get_recent(10)
    lines += ["", "RECENT ACTIVITY (last 10)", f"{'─' * 70}"]
    if not recent:
        lines.append("No activity yet.")
    for entry in recent:
        status_str = "✓ sent" if entry["status"] == "sent" else f"✗ FAILED: {entry.get('reason', '')}"
        lines.append(
            f"{entry['time'][:16]}  {entry['user']:<30} → {entry['to']:<25} {status_str}"
        )

    return "\n".join(lines)


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


if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route, Mount

    port = int(os.environ.get("PORT", 8080))

    async def health(request: Request) -> Response:
        return Response("OK", status_code=200)

    mcp_app = mcp.streamable_http_app()
    app = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Mount("/", mcp_app),
    ])

    uvicorn.run(app, host="0.0.0.0", port=port)
