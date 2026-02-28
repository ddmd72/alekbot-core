#!/usr/bin/env python3
"""
Email Classification POC — agentic version (Gemini Pro + tool use)

Gemini Pro receives all email metadata, decides which emails need full body/attachments,
calls the get_email_details tool autonomously, then classifies everything in one pass.

Setup:
  pip install google-auth-oauthlib
  Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs
    → Desktop app → Download JSON → save as scripts/email/credentials.json

Usage:
  python scripts/email/test_email_classification_poc.py
  python scripts/email/test_email_classification_poc.py --count 100 --after 2025-01-01
  python scripts/email/test_email_classification_poc.py --model gemini-2.0-pro-exp --save
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime
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
RESULTS_DIR = Path(__file__).parent.parent / "memory"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Default pre-filter: inbox emails that are Updates or uncategorized.
# Excludes Promotions, Social, Forums, Spam — the noise categories.
LABEL_FILTER = "{category:primary category:updates} -in:spam"

DEFAULT_MODEL = "gemini-flash-latest"

CLASSIFICATION_PROMPT = """\
class EmailFactExtractor extends Agent {

    taxonomy {

        /**
         * Email-to-Memory Extraction Agent
         *
         * PURPOSE: Scan email metadata and extract confirmed facts for long-term personal memory.
         *
         * A CONFIRMED FACT: A specific real-world event that definitively occurred for this
         * person, with concrete details that will remain useful 30+ days from now.
         *
         * Philosophy: "Extract what already happened. Discard everything else."
         */

        categories: {
            travel:       "Flight/train/hotel booking confirmation with reference number"
            finance:      "Transaction receipt, wire transfer, invoice — money that moved"
            healthcare:   "Appointment confirmed, lab results delivered, prescription issued"
            work:         "Contract signed, offer accepted, project decision made"
            legal:        "Agreement, permit, visa, registration — document delivered"
            personal:     "Life event: delivery confirmed, subscription cancelled, plan changed"
            subscription: "Renewal charged, cancellation confirmed — action completed"
        }

        negative_constraints {

            @critical
            rule Ephemeral_Email_Exclusions() {

                instruction: "NEVER classify these as valuable — they are not facts, they are noise"

                exclude: [
                    "Marketing: discounts, flash sales, limited time offers, recommendations",
                    "Newsletters, digests, product announcements, blog posts",
                    "Social notifications: likes, follows, views, connection requests",
                    "Action prompts: 'Please pay', 'Your turn to', 'Don't forget to'",
                    "In-transit updates: 'Being prepared', 'Out for delivery' (not yet confirmed)",
                    "Authentication events: password resets, 2FA codes, login notices",
                    "System alerts: storage warnings, account summaries, unread digests",
                    "Hypotheticals: 'You may have won', 'You could save', 'If you act now'"
                ]

                reasoning_test: "Will this email still be informative and useful in 30 days?"

                if_no: "DISCARD immediately"
            }
        }

        quality_rules: [
            "Be SPECIFIC: extract booking number, amount, date — not vague summaries",
            "Be PAST TENSE: 'User received lab results' not 'User should check results'",
            "Be SELF-CONTAINED: the fact must be understandable without the email",
            "Be SELECTIVE: when the snippet is ambiguous, use the tool before deciding",
            "Be DECISIVE: do not mark valuable if you cannot write a concrete fact"
        ]
    }

    cognitive_process {

        instruction: "Execute ALL steps for EACH email. Use <thinking> to reason."

        steps: [
            "1. SCAN: Read subject, sender, date, and snippet for each email.",

            "2. APPLY REASONING TEST:",
            "   <thinking>",
            "   Ask: 'Will this email still be informative and useful in 30 days?'",
            "   If NO → DISCARD. Do not proceed further for this email.",
            "   If YES or UNCERTAIN → continue to Step 3.",
            "   </thinking>",

            "3. INSPECT when needed:",
            "   If snippet is empty, cut off, or the category is ambiguous:",
            "   → call get_email_details([email_id]) to fetch full body + attachment names.",
            "   Attachment filenames alone can confirm value (contract.pdf, lab_result.pdf, invoice.pdf).",
            "   If still inconclusive after full body → DISCARD.",

            "4. EXTRACT the confirmed fact:",
            "   Write one self-contained sentence in past tense with all key specifics.",
            "   Include reference numbers, amounts, dates, and named entities where present.",
            "   Assign category from taxonomy.",
            "   Assign 3–8 lowercase tags: category + specific entities.",

            "5. OUTPUT a valid JSON array covering ALL emails — no exceptions:",
            "   [{email_id, valuable, category, fact, tags, reason}]",
            "   valuable=false entries: fact=null, tags=[], category=null"
        ]
    }
}

Emails to classify:
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Email Classification POC — agentic")
    parser.add_argument("--count", type=int, default=50, help="Emails to sample (default: 50)")
    parser.add_argument("--after", type=str, help="Date filter: YYYY-MM-DD")
    parser.add_argument("--query", type=str, default="", help="Additional Gmail search query (ANDed with label filter)")
    parser.add_argument("--batch-size", type=int, default=100, help="Emails per LLM call (default: 100)")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help=f"Gemini model (default: {DEFAULT_MODEL})")
    parser.add_argument("--thinking-budget", type=int, default=0, help="Thinking token budget (0 = disable, default: 0)")
    parser.add_argument("--no-filter", action="store_true", help="Disable default label filter (include all categories)")
    parser.add_argument("--save", action="store_true", help="Save memory-ready facts JSON to scripts/memory/")
    return parser.parse_args()


def get_credentials():
    """OAuth2 installed app flow. Caches token to scripts/memory/gmail_token.json."""
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
                print(f"\nERROR: credentials.json not found at:\n  {CREDENTIALS_FILE}")
                print("\nGet it from: Google Cloud Console → APIs & Services → Credentials")
                print("Create: OAuth 2.0 Client ID → Desktop app → Download JSON → rename to credentials.json")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        print(f"Token cached at {TOKEN_FILE}")

    return creds


def build_query(after: Optional[str], extra_query: str, no_filter: bool = False) -> str:
    parts = []
    if not no_filter:
        parts.append(LABEL_FILTER)
    if after:
        parts.append(f"after:{after.replace('-', '/')}")
    if extra_query:
        parts.append(extra_query)
    return " ".join(parts) if parts else ""


def fetch_message_ids(session, count: int, query: str) -> list[str]:
    ids = []
    page_token = None
    remaining = count

    while remaining > 0:
        params: dict = {"maxResults": min(remaining, 100)}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token

        resp = session.get(f"{GMAIL_BASE}/messages", params=params)
        resp.raise_for_status()
        data = resp.json()

        messages = data.get("messages", [])
        ids.extend(m["id"] for m in messages)
        remaining -= len(messages)
        page_token = data.get("nextPageToken")
        if not page_token or not messages:
            break

    return ids[:count]


def collect_attachment_names(payload: dict) -> list[str]:
    """Recursively collect non-empty filenames from MIME parts (available in format=metadata)."""
    names = []
    filename = payload.get("filename", "")
    if filename:
        names.append(filename)
    for part in payload.get("parts", []):
        names.extend(collect_attachment_names(part))
    return names


def fetch_email_metadata(session, message_id: str) -> dict:
    # Gmail API requires repeated params for metadataHeaders (not comma-separated).
    # Note: format=metadata never returns nested MIME parts, so attachment filenames
    # are only available via format=full (done in the second pass for need_review emails).
    params = [
        ("format", "metadata"),
        ("metadataHeaders", "Subject"),
        ("metadataHeaders", "From"),
        ("metadataHeaders", "Date"),
    ]
    resp = session.get(f"{GMAIL_BASE}/messages/{message_id}", params=params)
    resp.raise_for_status()
    data = resp.json()

    payload = data.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    return {
        "email_id": message_id,
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", ""),
        "date": headers.get("date", ""),
        "snippet": data.get("snippet", ""),
        "attachments": collect_attachment_names(payload),
    }


def extract_text_from_payload(payload: dict) -> str:
    """Recursively extract text/plain from Gmail message payload (MIME tree)."""
    import base64
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = extract_text_from_payload(part)
        if text:
            return text
    return ""


def fetch_email_full(session, message_id: str, max_chars: int = 3000) -> tuple[str, list[str]]:
    """Fetch full email: body text (text/plain, up to max_chars) + attachment filenames.

    format=full is the only Gmail API format that returns the nested MIME parts tree,
    which is required to collect attachment filenames. format=metadata does not return parts.
    """
    resp = session.get(f"{GMAIL_BASE}/messages/{message_id}", params={"format": "full"})
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload", {})
    text = extract_text_from_payload(payload)
    attachments = collect_attachment_names(payload)
    return (text[:max_chars] if text else ""), attachments


def _parse_json_response(text: str) -> list[dict]:
    """Extract JSON array from model text response."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        print(f"WARNING: Could not parse LLM response:\n{text[:400]}")
        return []


def _print_thinking(text: str) -> None:
    """Print model thinking to terminal with visual separation."""
    lines = text.strip().splitlines()
    print("\n  ┌─ THINKING " + "─" * 60)
    for line in lines:
        print(f"  │ {line}")
    print("  └" + "─" * 71 + "\n")


def classify_with_tools(
    genai_client, emails: list[dict], session, model: str, thinking_budget: int = 8192
) -> list[dict]:
    """Classify emails using Gemini Pro with function calling + optional thinking.

    The model receives all email metadata, autonomously decides which emails need
    full body/attachments via get_email_details tool, then returns classifications.
    """
    from google.genai import types

    get_details_fn = types.FunctionDeclaration(
        name="get_email_details",
        description=(
            "Fetch full body text and attachment filenames for emails that need deeper analysis. "
            "Use when snippet is empty or too short, or when attachments are the key signal."
        ),
        parameters=types.Schema(
            type="object",
            properties={
                "email_ids": types.Schema(
                    type="array",
                    items=types.Schema(type="string"),
                    description="email_id values to fetch full details for",
                )
            },
            required=["email_ids"],
        ),
    )
    tool = types.Tool(function_declarations=[get_details_fn])

    config_kwargs: dict = {"tools": [tool]}
    if thinking_budget > 0:
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=thinking_budget,
            include_thoughts=True,
        )
    config = types.GenerateContentConfig(**config_kwargs)

    # Build subject lookup for readable tool call logs
    subject_by_id = {e["email_id"]: e["subject"] for e in emails}

    emails_json = json.dumps(
        [
            {
                "email_id": e["email_id"],
                "subject": e["subject"],
                "from": e["from"],
                "date": e["date"],
                "snippet": e.get("snippet", ""),
            }
            for e in emails
        ],
        ensure_ascii=False,
        indent=2,
    )

    contents: list = [{"role": "user", "parts": [{"text": CLASSIFICATION_PROMPT + emails_json}]}]

    MAX_TURNS = 4
    for turn in range(1, MAX_TURNS + 1):
        print(f"  [turn {turn}] Calling {model}...")
        response = genai_client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        candidate = response.candidates[0]
        parts = candidate.content.parts

        # Print thinking if present
        thought_parts = [p for p in parts if getattr(p, "thought", False)]
        for tp in thought_parts:
            if getattr(tp, "text", None):
                _print_thinking(tp.text)

        fn_calls = [p for p in parts if getattr(p, "function_call", None)]
        if not fn_calls:
            # Final response — extract JSON from non-thought text parts
            text = "".join(
                p.text for p in parts
                if getattr(p, "text", None) and not getattr(p, "thought", False)
            )
            print(f"  [turn {turn}] Final response received ({len(text)} chars)")
            return _parse_json_response(text)

        # Log tool calls with email subjects
        requested_ids: list[str] = []
        for p in fn_calls:
            requested_ids.extend(p.function_call.args.get("email_ids", []))

        print(f"  [turn {turn}] Tool call → get_email_details for {len(requested_ids)} email(s):")
        for eid in requested_ids:
            subj = subject_by_id.get(eid, eid)
            print(f"    • {subj[:70]}")

        # Fetch details for requested IDs
        details = []
        for eid in requested_ids:
            body, attachments = fetch_email_full(session, eid)
            details.append({"email_id": eid, "body": body, "attachments": attachments})
            time.sleep(0.05)
        print(f"  [turn {turn}] Fetched {len(details)} full email(s), returning to model...")

        # Append model turn to history
        contents.append(candidate.content)

        # Append tool responses — one per function call part
        tool_parts = []
        for p in fn_calls:
            ids_in_call = p.function_call.args.get("email_ids", [])
            call_details = [d for d in details if d["email_id"] in ids_in_call]
            tool_parts.append(
                types.Part.from_function_response(
                    name="get_email_details",
                    response={"result": call_details},
                )
            )
        contents.append(types.Content(role="user", parts=tool_parts))

    print("WARNING: Max turns reached without final classification")
    return []


def truncate(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def print_results(emails: list[dict], classifications: dict) -> None:
    cat_abbr = {
        "travel": "travel",
        "finance": "financ",
        "healthcare": "health",
        "work": "work",
        "legal": "legal",
        "personal": "person",
        "subscription": "sub",
    }

    print()
    header = f"{'#':>3}  {'From':<32}  {'Subject':<36}  {'V':2}  {'Cat':6}  Fact / Reason"
    print(header)
    print("─" * 130)

    valuable_count = 0
    category_counts: dict[str, int] = {}

    for i, email in enumerate(emails, 1):
        cls = classifications.get(email["email_id"], {})
        valuable = cls.get("valuable")  # True | False | None
        category = cls.get("category") or "—"
        fact = cls.get("fact") or ""
        tags = cls.get("tags") or []
        reason = cls.get("reason") or ""

        if valuable is True:
            v_mark = "✅"
            valuable_count += 1
            category_counts[category] = category_counts.get(category, 0) + 1
        elif valuable is None:
            v_mark = "🔍"
        else:
            v_mark = "❌"

        from_raw = email["from"]
        from_short = truncate(
            from_raw.split("<")[0].strip() if "<" in from_raw else from_raw, 32
        )
        subject_short = truncate(email["subject"], 36)
        cat_short = cat_abbr.get(category, category[:6])

        if valuable is True:
            fact_short = truncate(fact, 55)
            tags_str = "  [" + ", ".join(tags) + "]" if tags else ""
            print(f"{i:>3}  {from_short:<32}  {subject_short:<36}  {v_mark}  {cat_short:<6}  {fact_short}")
            if tags_str:
                print(f"     {'':32}  {'':36}        {tags_str}")
        elif valuable is None:
            print(f"{i:>3}  {from_short:<32}  {subject_short:<36}  {v_mark}  {'—':<6}  need full body")
        else:
            print(f"{i:>3}  {from_short:<32}  {subject_short:<36}  {v_mark}  {'—':<6}  ↳ {truncate(reason, 55)}")

    total = len(emails)
    print("─" * 125)
    pct = 100 * valuable_count // total if total else 0
    print(f"\nValuable: {valuable_count}/{total} ({pct}%)")
    if category_counts:
        cat_str = "  ".join(f"{k}={v}" for k, v in sorted(category_counts.items()))
        print(f"Categories: {cat_str}")
    print()


def main() -> None:
    args = parse_args()

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        print("ERROR: GEMINI_API_KEY not set in environment / .env")
        sys.exit(1)

    from google.auth.transport.requests import AuthorizedSession
    from google import genai

    print("Authenticating with Gmail...")
    creds = get_credentials()
    session = AuthorizedSession(creds)

    genai_client = genai.Client(api_key=gemini_api_key)

    query = build_query(args.after, args.query, args.no_filter)
    desc = f"Fetching {args.count} emails"
    if query:
        desc += f" (query: {query!r})"
    print(desc + "...")

    message_ids = fetch_message_ids(session, args.count, query)
    if not message_ids:
        print("No emails found.")
        return

    print(f"Fetching metadata for {len(message_ids)} messages...")
    emails = []
    for i, msg_id in enumerate(message_ids, 1):
        email = fetch_email_metadata(session, msg_id)
        emails.append(email)
        if i % 10 == 0:
            print(f"  {i}/{len(message_ids)}")
        time.sleep(0.05)

    total_batches = (len(emails) + args.batch_size - 1) // args.batch_size
    print(f"\nClassifying with {args.model} in {total_batches} batch(es) of {args.batch_size}...")
    all_classifications: dict = {}

    for i in range(0, len(emails), args.batch_size):
        batch = emails[i : i + args.batch_size]
        batch_num = i // args.batch_size + 1
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} emails)...")
        results = classify_with_tools(genai_client, batch, session, args.model, args.thinking_budget)
        for r in results:
            all_classifications[r["email_id"]] = r

    print_results(emails, all_classifications)

    if args.save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = RESULTS_DIR / f"email_facts_{ts}.json"

        # Build email metadata lookup for enriching facts
        email_meta = {e["email_id"]: e for e in emails}

        facts = [
            {
                "email_id": r["email_id"],
                "category": r.get("category"),
                "fact": r.get("fact"),
                "tags": r.get("tags") or [],
                "metadata": {
                    "subject": email_meta[r["email_id"]]["subject"],
                    "from": email_meta[r["email_id"]]["from"],
                    "date": email_meta[r["email_id"]]["date"],
                },
            }
            for r in all_classifications.values()
            if r.get("valuable") is True and r["email_id"] in email_meta
        ]

        out_file.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(),
                    "query": query,
                    "model": args.model,
                    "total_fetched": len(emails),
                    "valuable_count": len(facts),
                    "facts": facts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"Facts saved to {out_file} ({len(facts)} valuable emails)")


if __name__ == "__main__":
    main()
