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
    def build_alert(date_str: str, email_count: int) -> str:
        return (
            f"[DAILY EMAIL REVIEW] {date_str}\n"
            f"{email_count} emails received in the last 24 hours. "
            f"Full email data is in email_for_triage {{}} in your system context.\n"
            f"\n"
            f"You are acting as the user's personal secretary. Draw on everything you know about "
            f"this person — their interests, work, relationships, context — to make the analysis "
            f"personally relevant. This is an analysis task, not a summary.\n"
            f"\n"
            f"Language: write the report and your reply in the user's language.\n"
            f"\n"
            f"Each entry contains: email_id, from, subject, date, snippet, body (full cleaned text), "
            f"attachments (filenames only). For newsletters and digests body is often mostly links "
            f"— rely on subject and snippet.\n"
            f"\n"
            f"---\n"
            f"\n"
            f"Classify every email — nothing dropped:\n"
            f"  [ACTION]  — requires a response, decision, or follow-up\n"
            f"  [FYI]     — informational, personally relevant\n"
            f"  [DIGEST]  — newsletter, roundup, digest\n"
            f"  [NOISE]   — promotional, irrelevant\n"
            f"\n"
            f"After classifying, use intent search_memory to retrieve facts relevant to [ACTION] "
            f"and [FYI] emails: senders you know, mentioned companies, topics, open threads. "
            f"A few targeted searches cover more than one per email.\n"
            f"\n"
            f"For emails with attachments worth reading, use intent get_email_attachment "
            f"to fetch the content.\n"
            f"\n"
            f"Search the web where it genuinely sharpens the analysis: context on senders, news "
            f"about mentioned events or companies, product updates, regulatory changes. "
            f"Don't search for the sake of it.\n"
            f"\n"
            f"---\n"
            f"\n"
            f"When your analysis is complete, delegate to specialist for HTML page creation. "
            f"The report must be in the user's language — the same language as this instruction. "
            f"Write the content in the user's usual communication style — the tone and voice they expect from you. "
            f"The specialist owns visual design and layout; you provide the content.\n"
            f"Pass the full analysis — do not summarise or compress:\n"
            f"  • Every email with its classification tag ([ACTION]/[FYI]/[DIGEST]/[NOISE]) — nothing dropped\n"
            f"  • For [ACTION] and [FYI]: your analysis and why it matters to this person\n"
            f"  • Memory facts retrieved via search_memory: what you know about the sender, company, or topic\n"
            f"  • Attachment content fetched via get_email_attachment\n"
            f"  • Web research findings: context on senders, events, companies, product updates, regulatory changes\n"
            f"  • Suggested next steps for every [ACTION] item\n"
            f"  • Conclusion: the single most critical item the user must act on today\n"
            f"Each email subject must be a clickable Gmail link: "
            f"https://mail.google.com/mail/u/0/#all/{{email_id}}\n"
            f"STRICTLY FORBIDDEN in the query: any mention of style, theme, colors, layout, "
            f"visual design, or aesthetic preferences — even if recalled from memory. "
            f"The specialist owns all design decisions unconditionally.\n"
            f"\n"
            f"Then reply to the user with one short paragraph — the single most important thing "
            f"they need to act on today."
        )

    async def _refresh_if_needed(self, creds: OAuthCredentials) -> Optional[OAuthCredentials]:
        now_utc = datetime.now(timezone.utc)
        if creds.token_expiry and creds.token_expiry <= now_utc + timedelta(minutes=5):
            try:
                creds = await self._email_provider.refresh_token(creds)
                await self._oauth.save_credentials(creds)
            except Exception as exc:
                logger.warning(
                    f"[EmailReview] token refresh failed for {creds.user_id[:8]}: {exc}"
                )
                return None
        return creds
