"""
GmailProviderAdapter — implements EmailProviderPort using Gmail REST API.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Auth:
  - Uses stored access_token from OAuthCredentials (Bearer token)
  - Caller checks expiry and calls refresh_token() before passing credentials
  - Token refresh calls Google's OAuth2 token endpoint directly

Gmail API endpoints used:
  GET  /gmail/v1/users/me/messages          — list message IDs
  GET  /gmail/v1/users/me/messages/{id}     — fetch individual message
  POST https://oauth2.googleapis.com/token  — refresh access token
"""

import asyncio
import base64
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

import aiohttp

from ..domain.email import (
    EmailFullContent,
    EmailMetadata,
    OAuthCredentials,
)
from ..ports.email_provider_port import EmailProviderPort
from ..utils.logger import logger

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Parallel fetch semaphore: Gmail recommends ≤10 concurrent requests per user
_FETCH_SEMAPHORE_LIMIT = 10


class GmailProviderAdapter(EmailProviderPort):
    """
    Implements EmailProviderPort for Gmail.

    Requires client_id and client_secret from GCP OAuth2 app credentials.
    These are loaded from env vars by the ServiceContainer and injected here.
    """

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        logger.info("📧 GmailProviderAdapter initialized")

    # ------------------------------------------------------------------
    # list_emails
    # ------------------------------------------------------------------

    async def list_emails(
        self,
        credentials: OAuthCredentials,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page_token: Optional[str] = None,
        max_results: int = 100,
        query: Optional[str] = None,
    ) -> Tuple[List[EmailMetadata], Optional[str]]:
        """
        Fetches up to max_results email metadata for one page.

        Strategy (RFC §2.2):
          1. GET /messages?maxResults=N&q=QUERY&pageToken=TOKEN → list of {id, threadId}
          2. Fetch individual message metadata in parallel (semaphore=10)
        """
        headers = {"Authorization": f"Bearer {credentials.access_token}"}
        sem = asyncio.Semaphore(_FETCH_SEMAPHORE_LIMIT)

        async with aiohttp.ClientSession() as session:
            # Step 1: List message IDs
            params: Dict = {"maxResults": max_results}
            if page_token:
                # When resuming via pageToken, Gmail continues the original query
                # embedded in the token. Passing q= alongside pageToken may override
                # the token's date filter and return emails outside the original range.
                params["pageToken"] = page_token
                logger.debug(f"📬 Gmail list_emails: resuming via pageToken (no q=)")
            else:
                # First page: build q= from caller query + date filters
                q_parts = []
                if query:
                    q_parts.append(query)
                if date_from:
                    q_parts.append(f"after:{date_from.strftime('%Y/%m/%d')}")
                if date_to:
                    q_parts.append(f"before:{date_to.strftime('%Y/%m/%d')}")
                if q_parts:
                    params["q"] = " ".join(q_parts)
                logger.info(f"📬 Gmail list_emails: q={params.get('q')!r} date_from={date_from} date_to={date_to}")

            async with session.get(
                f"{_GMAIL_BASE}/messages", headers=headers, params=params
            ) as resp:
                resp.raise_for_status()
                list_data = await resp.json()

            message_ids = [m["id"] for m in list_data.get("messages", [])]
            next_page_token = list_data.get("nextPageToken")

            if not message_ids:
                return [], next_page_token

            # Step 2: Fetch metadata per message in parallel
            async def fetch_meta(msg_id: str) -> Optional[dict]:
                async with sem:
                    try:
                        async with session.get(
                            f"{_GMAIL_BASE}/messages/{msg_id}",
                            headers=headers,
                            params={
                                "format": "metadata",
                                "metadataHeaders": ["Subject", "From", "Date"],
                            },
                        ) as resp:
                            resp.raise_for_status()
                            return await resp.json()
                    except Exception as exc:
                        logger.warning(
                            f"⚠️ Failed to fetch metadata for {msg_id}: {exc}"
                        )
                        return None

            metas = await asyncio.gather(*[fetch_meta(mid) for mid in message_ids])

            emails = []
            for meta in metas:
                if meta is None:
                    continue
                try:
                    emails.append(self._parse_metadata(meta))
                except Exception as exc:
                    logger.warning(
                        f"⚠️ Failed to parse metadata for {meta.get('id')}: {exc}"
                    )

        logger.info(
            f"📬 list_emails: fetched {len(emails)}/{len(message_ids)} "
            f"next_page={'yes' if next_page_token else 'no'}"
        )
        return emails, next_page_token

    # ------------------------------------------------------------------
    # batch_get_full_content
    # ------------------------------------------------------------------

    async def batch_get_full_content(
        self,
        credentials: OAuthCredentials,
        email_ids: List[str],
        deep: bool = False,
    ) -> Dict[str, EmailFullContent]:
        """
        Fetches full message content for a list of email IDs.

        deep=False: body text + attachment filenames only
        deep=True:  also downloads attachment binaries (for markitdown parsing)
        """
        headers = {"Authorization": f"Bearer {credentials.access_token}"}
        sem = asyncio.Semaphore(_FETCH_SEMAPHORE_LIMIT)

        async def fetch_one(email_id: str) -> Optional[dict]:
            async with sem:
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(
                            f"{_GMAIL_BASE}/messages/{email_id}",
                            headers=headers,
                            params={"format": "full"},
                        ) as resp:
                            resp.raise_for_status()
                            return await resp.json()
                except Exception as exc:
                    logger.warning(
                        f"⚠️ Failed to fetch full content for {email_id}: {exc}"
                    )
                    return None

        results = await asyncio.gather(*[fetch_one(eid) for eid in email_ids])

        output: Dict[str, EmailFullContent] = {}
        for msg in results:
            if msg is None:
                continue
            eid = msg["id"]
            try:
                body_text, body_html, attachment_names = self._parse_payload(
                    msg.get("payload", {})
                )
                attachment_binaries: Dict[str, bytes] = {}
                if deep and attachment_names:
                    attachment_binaries = await self._fetch_attachment_binaries(
                        credentials, eid, msg.get("payload", {}), headers
                    )
                output[eid] = EmailFullContent(
                    email_id=eid,
                    body_text=body_text,
                    body_html=body_html,
                    attachments=attachment_names,
                    attachment_binaries=attachment_binaries,
                )
            except Exception as exc:
                logger.warning(f"⚠️ Failed to parse full content for {eid}: {exc}")

        return output

    # ------------------------------------------------------------------
    # refresh_token
    # ------------------------------------------------------------------

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        """Exchange refresh_token for a new access_token via Google OAuth2 endpoint."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": credentials.refresh_token,
                    "grant_type": "refresh_token",
                },
            ) as resp:
                data = await resp.json()

        if "error" in data:
            raise ValueError(
                f"Gmail token refresh failed: {data['error']} — {data.get('error_description', '')}"
            )

        logger.info(
            f"🔑 Gmail token refreshed for user={credentials.user_id[:8]} "
            f"provider={credentials.provider}"
        )
        return OAuthCredentials(
            user_id=credentials.user_id,
            provider=credentials.provider,
            access_token=data["access_token"],
            # Google may not return a new refresh_token; keep the existing one
            refresh_token=data.get("refresh_token") or credentials.refresh_token,
            token_expiry=datetime.utcnow() + timedelta(seconds=data["expires_in"]),
            scopes=credentials.scopes,
            email_address=credentials.email_address,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_metadata(msg: dict) -> EmailMetadata:
        """Convert a Gmail messages.get(format=metadata) response to EmailMetadata."""
        headers_list = msg.get("payload", {}).get("headers", [])
        headers_map = {h["name"].lower(): h["value"] for h in headers_list}

        date_str = headers_map.get("date", "")
        try:
            email_date = parsedate_to_datetime(date_str).astimezone(timezone.utc).replace(tzinfo=None)
        except Exception:
            email_date = datetime.utcnow()

        return EmailMetadata(
            email_id=msg["id"],
            provider="gmail",
            subject=headers_map.get("subject", ""),
            from_address=headers_map.get("from", ""),
            date=email_date,
            labels=msg.get("labelIds", []),
            snippet=msg.get("snippet", ""),
        )

    @classmethod
    def _parse_payload(
        cls, payload: dict
    ) -> Tuple[str, Optional[str], List[str]]:
        """
        Recursively extract body text, HTML body, and attachment filenames
        from a Gmail message payload.
        """
        body_text = ""
        body_html = None
        attachments: List[str] = []

        mime_type = payload.get("mimeType", "")
        parts = payload.get("parts", [])
        body_data = payload.get("body", {}).get("data", "")

        if not parts:
            # Simple non-multipart message
            if body_data:
                decoded = base64.urlsafe_b64decode(body_data + "==").decode(
                    "utf-8", errors="replace"
                )
                if "html" in mime_type:
                    body_html = decoded
                    body_text = cls._html_to_text(decoded)
                else:
                    body_text = decoded
        else:
            for part in parts:
                filename = part.get("filename", "")
                if filename:
                    attachments.append(filename)
                    continue

                pt = part.get("mimeType", "")
                part_data = part.get("body", {}).get("data", "")

                if pt.startswith("multipart/"):
                    sub_text, sub_html, sub_att = cls._parse_payload(part)
                    body_text = body_text or sub_text
                    body_html = body_html or sub_html
                    attachments.extend(sub_att)
                elif pt == "text/plain" and part_data and not body_text:
                    body_text = base64.urlsafe_b64decode(part_data + "==").decode(
                        "utf-8", errors="replace"
                    )
                elif pt == "text/html" and part_data and not body_html:
                    body_html = base64.urlsafe_b64decode(part_data + "==").decode(
                        "utf-8", errors="replace"
                    )

            if not body_text and body_html:
                body_text = cls._html_to_text(body_html)

        return body_text, body_html, attachments

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Minimal HTML → plain text: strip tags, normalize whitespace."""
        # Remove <script> and <style> blocks
        html = re.sub(
            r"<(script|style)[^>]*>.*?</\1>",
            "",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Remove all remaining tags
        html = re.sub(r"<[^>]+>", " ", html)
        # Normalize whitespace
        return " ".join(html.split())

    async def _fetch_attachment_binaries(
        self,
        credentials: OAuthCredentials,
        email_id: str,
        payload: dict,
        headers: dict,
    ) -> Dict[str, bytes]:
        """Download attachment binaries for deep=True path."""
        binaries: Dict[str, bytes] = {}
        sem = asyncio.Semaphore(_FETCH_SEMAPHORE_LIMIT)

        async def download(filename: str, attachment_id: str) -> None:
            async with sem:
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(
                            f"{_GMAIL_BASE}/messages/{email_id}/attachments/{attachment_id}",
                            headers=headers,
                        ) as resp:
                            resp.raise_for_status()
                            data = await resp.json()
                    raw = base64.urlsafe_b64decode(data["data"] + "==")
                    binaries[filename] = raw
                except Exception as exc:
                    logger.warning(
                        f"⚠️ Failed to download attachment {filename}: {exc}"
                    )

        tasks = []
        for part in payload.get("parts", []):
            filename = part.get("filename", "")
            attachment_id = part.get("body", {}).get("attachmentId")
            if filename and attachment_id:
                tasks.append(download(filename, attachment_id))

        if tasks:
            await asyncio.gather(*tasks)

        return binaries
