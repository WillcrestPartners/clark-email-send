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

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


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

    if copy_to_sent:
        # Gmail API sends do not automatically appear in Sent — we add the label manually
        service.users().messages().modify(
            userId="me",
            id=sent["id"],
            body={"addLabelIds": ["SENT"]},
        ).execute()

    return sent["id"]
