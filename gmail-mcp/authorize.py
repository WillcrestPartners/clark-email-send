#!/usr/bin/env python3
"""One-time OAuth2 authorization. Run this once to generate token.json."""

from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

CONFIG_DIR = Path.home() / ".config" / "claude-gmail"
CLIENT_SECRETS = CONFIG_DIR / "client_secrets.json"
TOKEN_FILE = CONFIG_DIR / "token.json"

if not CLIENT_SECRETS.exists():
    raise FileNotFoundError(
        f"client_secrets.json not found at {CLIENT_SECRETS}\n"
        "Download it from Google Cloud Console → APIs & Services → Credentials"
    )

print("Opening browser for authorization...")
print("Sign in as clark@willcrestpartners.com\n")

flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
creds = flow.run_local_server(port=0)
TOKEN_FILE.write_text(creds.to_json())

print(f"\nAuthorization complete. Token saved to {TOKEN_FILE}")
print("You will not need to authorize again unless you revoke access.")
