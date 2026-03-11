#!/usr/bin/env python3
"""
Count email candidates matching the label filter — no LLM, just Gmail API.

Usage:
  python scripts/email/count_email_candidates.py
  python scripts/email/count_email_candidates.py --after 2024-01-01
  python scripts/email/count_email_candidates.py --no-filter
"""

import os
import sys
import time
import argparse
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent.parent / "memory" / "gmail_token.json"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
LABEL_FILTER = "{category:primary category:updates} -in:spam"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Count Gmail candidates (no LLM)")
    parser.add_argument("--after", type=str, default="2026-01-01", help="Date filter: YYYY-MM-DD (default: 2026-01-01)")
    parser.add_argument("--no-filter", action="store_true", help="Disable label filter")
    return parser.parse_args()


def get_credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print(f"ERROR: credentials.json not found at {CREDENTIALS_FILE}")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return creds


def build_query(after: Optional[str], no_filter: bool) -> str:
    parts = []
    if not no_filter:
        parts.append(LABEL_FILTER)
    if after:
        parts.append(f"after:{after.replace('-', '/')}")
    return " ".join(parts) if parts else ""


def count_messages(session, query: str) -> int:
    total = 0
    page_token = None
    page = 0

    while True:
        params: dict = {"maxResults": 500}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token

        resp = session.get(f"{GMAIL_BASE}/messages", params=params)
        resp.raise_for_status()
        data = resp.json()

        batch = len(data.get("messages", []))
        total += batch
        page += 1
        print(f"  Page {page}: +{batch} (total so far: {total})", end="\r")

        page_token = data.get("nextPageToken")
        if not page_token:
            break

        time.sleep(0.1)

    print()
    return total


def main() -> None:
    args = parse_args()

    from google.auth.transport.requests import AuthorizedSession

    print("Authenticating...")
    creds = get_credentials()
    session = AuthorizedSession(creds)

    query = build_query(args.after, args.no_filter)
    print(f"Query: {query!r}\n")

    print("Counting...")
    total = count_messages(session, query)

    print(f"\nResult: {total:,} emails match the filter")
    if args.after:
        print(f"Period: from {args.after} to now")
    if not args.no_filter:
        print(f"Filter: {LABEL_FILTER}")


if __name__ == "__main__":
    main()
