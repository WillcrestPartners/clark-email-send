"""
Sends a single email via the Gmail API.

Usage:
    python send_email.py --to recipient@example.com \
                         --subject "Hello" \
                         --body "Message body here"

The script will show a preview and ask for confirmation before sending.
"""

import argparse
import base64
import os
import sys
from email.mime.text import MIMEText

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

from auth import get_credentials
from guardrails import validate_recipient, check_daily_limit, record_send

load_dotenv()


def build_message(sender: str, to: str, subject: str, body: str) -> dict:
    message = MIMEText(body)
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return {"raw": encoded}


def send(to: str, subject: str, body: str) -> None:
    sender = os.environ.get("SENDER_EMAIL", "clark@willcrestpartners.com")

    # Safety checks before touching the API
    validate_recipient(to)
    check_daily_limit()

    # Show preview and require explicit confirmation
    print("\n" + "=" * 50)
    print("EMAIL PREVIEW")
    print("=" * 50)
    print(f"From:    {sender}")
    print(f"To:      {to}")
    print(f"Subject: {subject}")
    print("-" * 50)
    print(body)
    print("=" * 50)
    answer = input("\nSend this email? Type 'yes' to confirm: ").strip().lower()
    if answer != "yes":
        print("Cancelled. No email was sent.")
        sys.exit(0)

    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)
    message = build_message(sender, to, subject, body)

    try:
        service.users().messages().send(userId="me", body=message).execute()
        record_send()
        print(f"\nEmail sent successfully to {to}.")
    except HttpError as e:
        print(f"\nFailed to send email: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send an email via Gmail API")
    parser.add_argument("--to", required=True, help="Recipient email address")
    parser.add_argument("--subject", required=True, help="Email subject line")
    parser.add_argument("--body", required=True, help="Email body text")
    args = parser.parse_args()

    send(to=args.to, subject=args.subject, body=args.body)
