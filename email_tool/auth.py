"""
Handles Google OAuth 2.0 authentication for the Gmail API.

First run: opens a browser window asking you to approve the app.
Subsequent runs: uses the saved token so no browser needed.
"""

import os
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from dotenv import load_dotenv

load_dotenv()

# Narrowest scope: send-only. We never request read or delete access.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

TOKEN_PATH = Path(__file__).parent / "token.json"


def get_credentials() -> Credentials:
    creds = None
    credentials_path = os.environ.get("GOOGLE_CREDENTIALS_PATH")
    if not credentials_path:
        raise EnvironmentError(
            "GOOGLE_CREDENTIALS_PATH is not set in your .env file. "
            "See .env.example for instructions."
        )

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    return creds
