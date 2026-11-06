"""
GmailProviderAdapter — implements EmailProviderPort using Gmail REST API.
See docs/10_rfcs/GMAIL_EMAIL_INDEXING_RFC.md §2.1.2.

Auth:
  - Uses stored access_token from OAuthCredential (Bearer token)
  - Caller checks expiry and calls refresh_token() before passing credentials
  - Token refresh calls Google's OAuth2 token endpoint directly

Gmail API endpoints used:
  GET  /gmail/v1/users/me/messages          — list message IDs
  GET  /gmail/v1/users/me/messages/{id}     — fetch individual message
  POST https://oauth2.googleapis.com/token  — refresh access token
"""

import asyncio
import base64
import html as _html_lib
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
from typing import Dict, List, Optional, Tuple

import aiohttp

from ..domain.email import (
    EmailFullContent,
    EmailMetadata,
    EmailPart,
)
from ..domain.link_url import link_with_url
from ..domain.oauth_credential import OAuthCredential
from ..domain.exceptions import (
    EmailMessageGone,
    EmailPartTooLarge,
    EmailPartUnavailable,
    OAuthGrantRevokedError,
    OAuthScopeInsufficientError,
)
from ..ports.email_provider_port import EmailProviderPort
from ..utils.logger import logger

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Parallel fetch semaphore: Gmail recommends ≤10 concurrent requests per user
_FETCH_SEMAPHORE_LIMIT = 10
# One call of the parts surface (list_parts / get_part) is bounded: a timeout is "the mailbox did not answer".
_PARTS_TIMEOUT_S = 60
_SCOPE_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}


async def _raise_for_gmail_status(resp) -> None:
    """`raise_for_status` that first logs the reason of a non-2xx answer and maps a 403 for a missing scope.

    Logs status + `error.status` + `reason` values only (no token, message text or address). A 403 whose reason
    names an insufficient scope raises OAuthScopeInsufficientError, handled like a revoked grant.
    """
    try:
        resp.raise_for_status()
    except aiohttp.ClientResponseError as exc:
        status, reasons = "", []
        try:
            error = json.loads(await resp.text()).get("error")
            if isinstance(error, dict):
                status = str(error.get("status") or "")
                reasons += [str(e.get("reason")) for e in error.get("errors") or [] if isinstance(e, dict)]
                reasons += [str(d.get("reason")) for d in error.get("details") or [] if isinstance(d, dict)]
                if status:
                    reasons.append(status)
        except Exception:
            logger.debug("Gmail API error body unreadable: the status alone is logged", exc_info=True)
        logger.warning(f"⚠️ Gmail API {exc.status}: error.status={status or '-'} reasons={sorted(set(reasons)) or '-'}")
        if exc.status == 403 and _SCOPE_REASONS & set(reasons):
            raise OAuthScopeInsufficientError(
                "Gmail auth failed: the token lacks the Gmail scope (403 insufficient scope); reconnect required"
            ) from exc
        raise


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
        credentials: OAuthCredential,
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
                logger.debug("📬 Gmail list_emails: resuming via pageToken (no q=)")
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
                await _raise_for_gmail_status(resp)
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
                            await _raise_for_gmail_status(resp)
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
        credentials: OAuthCredential,
        email_ids: List[str],
    ) -> Dict[str, EmailFullContent]:
        """Fetches full message content for a list of email IDs: body text + attachment filenames only."""
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
                            await _raise_for_gmail_status(resp)
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
                output[eid] = EmailFullContent(
                    email_id=eid,
                    body_text=body_text,
                    body_html=body_html,
                    attachments=attachment_names,
                )
            except Exception as exc:
                logger.warning(f"⚠️ Failed to parse full content for {eid}: {exc}")

        return output

    # ------------------------------------------------------------------
    # list_parts / get_part (EXTERNAL_FILES_RFC §4.7 P8a)
    # ------------------------------------------------------------------

    async def list_parts(self, credentials: OAuthCredential, message_id: str) -> List[EmailPart]:
        """Every attachment part of the message, in walk order (one `messages.get format=full`).

        The same walk that produced the indexed names (`_iter_attachment_parts`), so a name the index holds is found
        here. Raises EmailMessageGone (404) and EmailPartUnavailable (429, 5xx, timeout, network).
        """
        msg = await self._fetch_message(credentials, message_id)
        return [self._to_email_part(raw, path) for raw, path in self._iter_attachment_parts(msg.get("payload", {}))]

    async def get_part(self, credentials: OAuthCredential, message_id: str, part: EmailPart,
                       max_bytes: int) -> bytes:
        """The bytes of one listed part: `attachments.get` by `part.handle`, or, for an inline part (empty handle),
        the message re-read and the part's own `body.data` decoded.

        Nothing is returned past `max_bytes`: the declared size is checked before the download, the real one after it
        (a lie in the listing raises EmailPartTooLarge as well). Returns b"" when the part has no data.
        """
        if part.size > max_bytes:
            raise EmailPartTooLarge(max_bytes)
        if part.handle:
            data = await self._gmail_json(
                credentials, f"{_GMAIL_BASE}/messages/{message_id}/attachments/{part.handle}", None)
            encoded = data.get("data") or ""
        else:
            msg = await self._fetch_message(credentials, message_id)
            encoded = ""
            for raw, path in self._iter_attachment_parts(msg.get("payload", {})):
                if self._to_email_part(raw, path).part_id == part.part_id:
                    encoded = raw.get("body", {}).get("data") or ""
                    break
        if len(encoded) * 3 // 4 > max_bytes + 3:
            raise EmailPartTooLarge(max_bytes)
        raw_bytes = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)) if encoded else b""
        if len(raw_bytes) > max_bytes:
            raise EmailPartTooLarge(max_bytes)
        return raw_bytes

    async def _fetch_message(self, credentials: OAuthCredential, message_id: str) -> dict:
        return await self._gmail_json(credentials, f"{_GMAIL_BASE}/messages/{message_id}", {"format": "full"})

    async def _gmail_json(self, credentials: OAuthCredential, url: str, params: Optional[dict]) -> dict:
        """One GET with the typed errors of the parts surface: 404 -> EmailMessageGone; 429, 5xx, timeout and
        network failure -> EmailPartUnavailable; scope/grant errors and other statuses as everywhere else."""
        headers = {"Authorization": f"Bearer {credentials.access_token}"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_PARTS_TIMEOUT_S)) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    await _raise_for_gmail_status(resp)
                    return await resp.json()
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                raise EmailMessageGone("Gmail answered 404 for the message") from exc
            if exc.status == 429 or exc.status >= 500:
                raise EmailPartUnavailable(f"Gmail answered {exc.status}") from exc
            raise
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            logger.warning(f"⚠️ Gmail parts call failed: {type(exc).__name__}")
            raise EmailPartUnavailable(f"Gmail did not answer: {type(exc).__name__}") from exc

    @staticmethod
    def _to_email_part(raw: dict, path: str) -> EmailPart:
        body = raw.get("body", {})
        return EmailPart(
            part_id=str(raw.get("partId") or path),
            filename=raw.get("filename", ""),
            mime=raw.get("mimeType", ""),
            size=int(body.get("size") or 0),
            handle=body.get("attachmentId") or "",
        )

    @classmethod
    def _iter_attachment_parts(cls, payload: dict, prefix: str = ""):
        """The one walk behind the indexed names and `list_parts`: yields `(raw part, positional path)`.

        A part with a filename is an attachment and is not descended into (a `message/rfc822` attachment is one part,
        its children are not listed); a `multipart/*` part without one is descended into; anything else is body.
        The path is the 0-based index chain ("1.0"), used only when Gmail sends no `partId`.
        """
        for index, part in enumerate(payload.get("parts", [])):
            path = f"{prefix}{index}"
            if part.get("filename", ""):
                yield part, path
            elif part.get("mimeType", "").startswith("multipart/"):
                yield from cls._iter_attachment_parts(part, f"{path}.")

    # ------------------------------------------------------------------
    # refresh_token
    # ------------------------------------------------------------------

    async def refresh_token(self, credentials: OAuthCredential) -> OAuthCredential:
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
            message = (
                f"Gmail token refresh failed: {data['error']} — {data.get('error_description', '')}"
            )
            if data["error"] == "invalid_grant":
                raise OAuthGrantRevokedError(message)
            raise ValueError(message)

        logger.info(
            f"🔑 Gmail token refreshed for user={credentials.user_id[:8]} "
            f"provider={credentials.kind}"
        )
        # Every other field (the identity included) is carried over; needs_reconnect is cleared, since the refresh
        # succeeded.
        return replace(
            credentials,
            access_token=data["access_token"],
            # Google may not return a new refresh_token; keep the existing one
            refresh_token=data.get("refresh_token") or credentials.refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"]),
            needs_reconnect=False,
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
            email_date = datetime.now(timezone.utc).replace(tzinfo=None)

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
        body_text, body_html = cls._extract_bodies(payload)
        attachments = [raw["filename"] for raw, _ in cls._iter_attachment_parts(payload)]
        return body_text, body_html, attachments

    @classmethod
    def _extract_bodies(cls, payload: dict) -> Tuple[str, Optional[str]]:
        """Body text and HTML of a payload (attachments are skipped)."""
        body_text = ""
        body_html = None

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
                    body_text = " ".join(cls._strip_invisible(_html_lib.unescape(decoded)).split())
        else:
            for part in parts:
                if part.get("filename", ""):
                    continue

                pt = part.get("mimeType", "")
                part_data = part.get("body", {}).get("data", "")

                if pt.startswith("multipart/"):
                    sub_text, sub_html = cls._extract_bodies(part)
                    body_text = body_text or sub_text
                    body_html = body_html or sub_html
                elif pt == "text/plain" and part_data and not body_text:
                    raw = base64.urlsafe_b64decode(part_data + "==").decode(
                        "utf-8", errors="replace"
                    )
                    body_text = " ".join(cls._strip_invisible(_html_lib.unescape(raw)).split())
                elif pt == "text/html" and part_data and not body_html:
                    body_html = base64.urlsafe_b64decode(part_data + "==").decode(
                        "utf-8", errors="replace"
                    )

            if not body_text and body_html:
                body_text = cls._html_to_text(body_html)

        return body_text, body_html

    @staticmethod
    def _strip_invisible(text: str) -> str:
        """Remove zero-width and invisible Unicode characters used as email tracking spacers."""
        return re.sub(
            r"[\u00ad\u034f\u200b-\u200f\u2028\u2029\u2060\ufeff\xa0]",
            " ",
            text,
        )

    @classmethod
    def _html_to_text(cls, html: str) -> str:
        """HTML → plain text via BeautifulSoup: proper entity decoding, structure-aware extraction.

        An external link keeps its cleaned URL after its text, "Read more (https://example.org/a?id=7)", in the
        drive parser's format (`domain/link_url.py`, owner decision O-6); any other link keeps its text only."""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        for link in soup.find_all("a"):
            shown = link_with_url(" ".join(link.get_text(" ", strip=True).split()), str(link.get("href") or ""))
            if shown:
                link.replace_with(f" {shown} ")
        text = soup.get_text(separator=" ")
        text = cls._strip_invisible(text)
        return " ".join(text.split())

