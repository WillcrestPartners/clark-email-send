#!/usr/bin/env python3
"""Gmail MCP Server — OAuth2 user credentials (no domain-wide delegation)."""

import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

CONFIG_DIR = Path.home() / ".config" / "claude-gmail"
CLIENT_SECRETS = CONFIG_DIR / "client_secrets.json"
TOKEN_FILE = CONFIG_DIR / "token.json"

mcp = FastMCP("gmail")


def _gmail():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _extract_body(payload: dict) -> str:
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType") in ("text/plain", "text/html"):
            result = _extract_body(part)
            if result:
                return result
    return ""


@mcp.tool()
def send_email(to: str, subject: str, body: str, cc: Optional[str] = None) -> str:
    """Send an email from clark@willcrestpartners.com."""
    svc = _gmail()
    msg = MIMEMultipart()
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc
    msg.attach(MIMEText(body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return f"Sent. Message ID: {result['id']}"


@mcp.tool()
def list_emails(max_results: int = 10, query: str = "") -> str:
    """List recent emails. Supports Gmail query syntax (e.g. 'is:unread from:boss@example.com')."""
    svc = _gmail()
    params: dict = {"userId": "me", "maxResults": max_results}
    if query:
        params["q"] = query
    result = svc.users().messages().list(**params).execute()
    messages = result.get("messages", [])
    if not messages:
        return "No messages found."
    output = []
    for msg in messages:
        detail = svc.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        h = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        snippet = detail.get("snippet", "")[:120]
        output.append(
            f"ID: {msg['id']}\nFrom: {h.get('From','')}\n"
            f"Subject: {h.get('Subject','')}\nDate: {h.get('Date','')}\n"
            f"Snippet: {snippet}"
        )
    return "\n\n---\n\n".join(output)


@mcp.tool()
def search_emails(query: str, max_results: int = 10) -> str:
    """Search emails with Gmail query syntax. E.g. 'from:alice subject:invoice after:2024/01/01'."""
    return list_emails(max_results=max_results, query=query)


@mcp.tool()
def get_email(message_id: str) -> str:
    """Fetch the full content of an email by its message ID."""
    svc = _gmail()
    msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
    h = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    body = _extract_body(msg["payload"])
    return (
        f"From: {h.get('From','')}\n"
        f"To: {h.get('To','')}\n"
        f"Subject: {h.get('Subject','')}\n"
        f"Date: {h.get('Date','')}\n\n"
        f"{body}"
    )


@mcp.tool()
def reply_to_email(message_id: str, body: str) -> str:
    """Reply to an email, preserving its thread."""
    svc = _gmail()
    original = svc.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "To", "Subject", "Message-ID", "References"],
    ).execute()
    h = {h["name"]: h["value"] for h in original["payload"]["headers"]}
    thread_id = original["threadId"]

    msg = MIMEText(body, "plain")
    msg["to"] = h.get("From", "")
    subject = h.get("Subject", "")
    msg["subject"] = subject if subject.startswith("Re:") else f"Re: {subject}"
    msg["In-Reply-To"] = h.get("Message-ID", "")
    refs = h.get("References", "")
    mid = h.get("Message-ID", "")
    msg["References"] = f"{refs} {mid}".strip()

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = svc.users().messages().send(
        userId="me", body={"raw": raw, "threadId": thread_id}
    ).execute()
    return f"Reply sent. Message ID: {result['id']}"


@mcp.tool()
def mark_as_read(message_id: str) -> str:
    """Mark an email as read."""
    svc = _gmail()
    svc.users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()
    return f"Message {message_id} marked as read."


@mcp.tool()
def get_thread(thread_id: str) -> str:
    """Get all messages in an email thread."""
    svc = _gmail()
    thread = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    output = []
    for msg in thread.get("messages", []):
        h = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        body = _extract_body(msg["payload"])[:2000]
        output.append(f"From: {h.get('From','')}\nDate: {h.get('Date','')}\n\n{body}")
    return ("\n\n" + "=" * 60 + "\n\n").join(output)


if __name__ == "__main__":
    mcp.run()
