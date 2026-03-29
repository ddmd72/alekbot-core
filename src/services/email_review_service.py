"""
EmailReviewService
==================

Fetches the last 24h of Gmail emails for a user and builds a structured
payload for SmartAgent daily inbox analysis.

Responsibilities:
  - Token refresh (if expiring within 5 minutes)
  - Paginated email metadata fetch (capped at 200)
  - Full content fetch (body text + attachment names, no binaries)
  - Structured JSON payload assembly
  - System alert framing for SmartAgent
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..domain.email import OAuthCredentials
from ..ports.email_provider_port import EmailProviderPort
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..utils.logger import logger


_MAX_EMAILS = 200


class EmailReviewService:

    def __init__(
        self,
        email_provider: EmailProviderPort,
        oauth_credentials: OAuthCredentialsPort,
    ) -> None:
        self._email_provider = email_provider
        self._oauth = oauth_credentials

    async def find_eligible_users(
        self, user_repo: Any, now_utc: datetime
    ) -> List[Tuple[str, str]]:
        """
        Return (user_id, account_id) pairs for Gmail users eligible for daily review
        at the given UTC time (gmail_daily_review=True, hour matches local timezone).
        """
        user_ids = await self._oauth.list_users_by_provider("gmail")
        eligible: List[Tuple[str, str]] = []
        for user_id in user_ids:
            profile = await user_repo.get_user(user_id)
            if not profile:
                continue
            cfg = profile.config
            if not cfg.gmail_daily_review:
                continue
            user_tz = ZoneInfo(cfg.timezone or "UTC")
            if now_utc.astimezone(user_tz).hour != cfg.gmail_daily_review_hour:
                continue
            eligible.append((user_id, profile.account_id))
        return eligible

    async def fetch_review_payload(self, user_id: str) -> Optional[List[dict]]:
        """
        Fetch and structure the last 24h of Gmail emails for user_id.
        Returns None if credentials are missing or token refresh fails.
        Returns an empty list if no emails were received in the period.
        """
        creds = await self._oauth.get_credentials(user_id, "gmail")
        if not creds:
            logger.warning(f"[EmailReview] no credentials for {user_id[:8]}")
            return None

        creds = await self._refresh_if_needed(creds)
        if creds is None:
            return None

        now_utc = datetime.now(timezone.utc)
        date_from = now_utc - timedelta(hours=24)

        all_metadata = []
        page_token = None
        while len(all_metadata) < _MAX_EMAILS:
            batch, page_token = await self._email_provider.list_emails(
                credentials=creds,
                date_from=date_from,
                date_to=None,  # Gmail before: is day-exclusive; omit to include today's emails
                page_token=page_token,
                max_results=min(100, _MAX_EMAILS - len(all_metadata)),
            )
            all_metadata.extend(batch)
            if not page_token:
                break

        if not all_metadata:
            return []

        full_content = await self._email_provider.batch_get_full_content(
            credentials=creds,
            email_ids=[m.email_id for m in all_metadata],
            deep=False,
        )

        return [
            {
                "email_id": meta.email_id,
                "from": meta.from_address,
                "subject": meta.subject,
                "date": meta.date.isoformat(),
                "snippet": meta.snippet,
                "body": (
                    full_content[meta.email_id].body_text
                    if meta.email_id in full_content and full_content[meta.email_id].body_text
                    else ""
                ),
                "attachments": (
                    full_content[meta.email_id].attachments
                    if meta.email_id in full_content
                    else []
                ),
            }
            for meta in all_metadata
        ]

    @staticmethod
    def build_alert(date_str: str, emails: List[dict]) -> str:
        return (
            f"[DAILY EMAIL REVIEW] {date_str}\n"
            f"{len(emails)} emails received in the last 24 hours.\n"
            f"\n"
            f"The user configured this daily inbox brief themselves. "
            f"This is not a standing instruction to summarise — it is a standing instruction to work. "
            f"Act as a personal secretary doing a thorough inbox review.\n"
            f"\n"
            f"Language rule: write the entire report and response message in the user's language. "
            f"You know which language the user communicates in — use it throughout.\n"
            f"\n"
            f"Each entry contains: email_id, from, subject, date, snippet, body (full cleaned text), "
            f"attachments (filenames only). Newsletter/digest bodies are often mostly links — "
            f"rely on subject + snippet for initial triage.\n"
            f"\n"
            f"---\n"
            f"\n"
            f"## PHASE 0 — Triage (before any tool calls)\n"
            f"\n"
            f"Scan all {len(emails)} emails. Assign each a disposition tag:\n"
            f"  [ACTION]  — requires a response, decision, or follow-up from the user\n"
            f"  [FYI]     — informational, personally relevant, worth reading\n"
            f"  [DIGEST]  — newsletter, roundup, automated digest\n"
            f"  [NOISE]   — promotional, irrelevant, unsubscribe candidate\n"
            f"\n"
            f"Every email must get a tag. Nothing dropped.\n"
            f"Output the triage map before proceeding to Phase 1.\n"
            f"\n"
            f"## PHASE 1 — Deep reads\n"
            f"\n"
            f"Call get_email_details(email_id) for:\n"
            f"- Every [ACTION] email — no exceptions\n"
            f"- [FYI] emails where snippet/body is insufficient to understand the full content\n"
            f"- Any attachment worth reading: call get_email_attachment(email_id, filename)\n"
            f"\n"
            f"Minimum: get_email_details on at least 3 emails (or all [ACTION] + [FYI] if fewer).\n"
            f"\n"
            f"## PHASE 2 — Research\n"
            f"\n"
            f"Call search_web for topics, senders, events, or companies that need external context:\n"
            f"news, product updates, regulatory changes, background on unfamiliar senders, etc.\n"
            f"Minimum: 2 search_web calls if any [ACTION] or [FYI] email warrants it.\n"
            f"\n"
            f"---\n"
            f"\n"
            f"{json.dumps(emails, ensure_ascii=False, indent=2)}\n"
            f"\n"
            f"---\n"
            f"You know who the user is — draw on that knowledge to make the analysis personally relevant.\n"
            f"\n"
            f"Produce two outputs:\n"
            f"1. An HTML page (create_html_page) — the main artifact. Full analysis: per-email coverage "
            f"with disposition tags, highlights, action items, research findings, unsubscribe candidates. "
            f"Every email in the triage map must appear. "
            f"For every email mentioned, render its subject as a clickable link using the format: "
            f"https://mail.google.com/mail/u/0/#all/{{email_id}} — this opens the email on web and "
            f"in the Gmail mobile app. The user will read it at their convenience.\n"
            f"2. A response message — one short paragraph: the most important finding or action item. "
            f"This is what the user sees first in the chat."
        )

    async def _refresh_if_needed(self, creds: OAuthCredentials) -> Optional[OAuthCredentials]:
        now_utc = datetime.now(timezone.utc)
        if creds.token_expiry:
            expiry = creds.token_expiry.replace(tzinfo=timezone.utc) if creds.token_expiry.tzinfo is None else creds.token_expiry
        if creds.token_expiry and expiry <= now_utc + timedelta(minutes=5):
            try:
                creds = await self._email_provider.refresh_token(creds)
                await self._oauth.save_credentials(creds)
            except Exception as exc:
                logger.warning(
                    f"[EmailReview] token refresh failed for {creds.user_id[:8]}: {exc}"
                )
                return None
        return creds
