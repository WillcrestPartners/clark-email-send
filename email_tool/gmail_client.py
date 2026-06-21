"""
Handles all Gmail API calls: sending email and copying to Sent folder.
Uses a Service Account with domain-wide delegation — no per-user login required.
"""

import base64
import json
import os
from email.mime.text import MIMEText

from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _get_service(sender_email: str):
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")

    if raw:
        info = json.loads(raw)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    elif path:
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    else:
        raise EnvironmentError(
            "Neither GOOGLE_SERVICE_ACCOUNT_JSON nor GOOGLE_SERVICE_ACCOUNT_PATH is set."
        )

    # Impersonate the sender so email comes from clark@willcrestpartners.com
    delegated = creds.with_subject(sender_email)
    return build("gmail", "v1", credentials=delegated)


def send_email(sender: str, to: str, subject: str, body: str, copy_to_sent: bool = True) -> str:
    """Sends the email and optionally copies it to the Sent folder. Returns the message ID."""
    service = _get_service(sender)

    message = MIMEText(body)
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    payload = {"raw": raw}

    sent = service.users().messages().send(userId="me", body=payload).execute()

    sent_folder_copied = False
    if copy_to_sent:
        try:
            service.users().messages().modify(
                userId="me",
                id=sent["id"],
                body={"addLabelIds": ["SENT"]},
            ).execute()
            sent_folder_copied = True
        except Exception:
            pass  # copy-to-sent is best-effort; send already succeeded

    return sent["id"], sent_folder_copied


# ── inbound / threaded helpers (Phase 1 email command bus) ──────────────────

def send_threaded(
    sender: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str = None,
    references: str = None,
    copy_to_sent: bool = True,
) -> tuple:
    """Send an email, optionally threaded into an existing conversation.

    Sets the In-Reply-To and References MIME headers when provided so the
    reply appears in the same Gmail thread as the original message.
    Returns (message_id, sent_folder_copied) like send_email().
    """
    service = _get_service(sender)

    message = MIMEText(body)
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    payload = {"raw": raw}

    sent = service.users().messages().send(userId="me", body=payload).execute()

    sent_folder_copied = False
    if copy_to_sent:
        try:
            service.users().messages().modify(
                userId="me",
                id=sent["id"],
                body={"addLabelIds": ["SENT"]},
            ).execute()
            sent_folder_copied = True
        except Exception:
            pass

    return sent["id"], sent_folder_copied


def list_unread_message_ids(mailbox: str, max_results: int = 25) -> list:
    """Return Gmail message IDs for unread messages in the mailbox's inbox.

    Uses the query 'is:unread in:inbox' for the MVP. Processed messages are
    marked read (UNREAD removed) via mark_read() so they are not re-polled.
    """
    service = _get_service(mailbox)
    resp = (
        service.users()
        .messages()
        .list(userId="me", q="is:unread in:inbox", maxResults=max_results)
        .execute()
    )
    return [m["id"] for m in resp.get("messages", [])]


def get_message_raw(mailbox: str, msg_id: str) -> dict:
    """Fetch a single message in raw (RFC822) form.

    Returns the Gmail API message resource; the RFC822 bytes live under
    the base64url-encoded 'raw' field. email_parse.parse_message() accepts
    this dict directly.
    """
    service = _get_service(mailbox)
    return (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="raw")
        .execute()
    )


def mark_read(mailbox: str, msg_id: str) -> None:
    """Mark a message as read by removing the UNREAD label."""
    service = _get_service(mailbox)
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


def list_labels(mailbox: str) -> list:
    """List labels for the mailbox — a cheap readonly call for diagnostics."""
    service = _get_service(mailbox)
    resp = service.users().labels().list(userId="me").execute()
    return resp.get("labels", [])
