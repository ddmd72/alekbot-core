"""The real `GmailProviderAdapter.list_emails` over a recording aiohttp boundary: what query string it sends to Gmail."""
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from tests.support.oauth_credentials_store import creds


async def sent_list_params(date_from: Optional[datetime], date_to: Optional[datetime] = None) -> Dict[str, Any]:
    """The `params` of the first `GET /messages` a review-style `list_emails` call makes (no query, first page)."""
    gets: List[Dict[str, Any]] = []

    def get(url: str, headers: Any = None, params: Any = None):
        gets.append({"url": url, "params": dict(params or {})})
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = AsyncMock(return_value={"messages": []})
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = MagicMock()
    session.get = get
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    adapter = GmailProviderAdapter(client_id="cid", client_secret="secret")
    with patch("src.adapters.gmail_provider_adapter.aiohttp.ClientSession", return_value=session_ctx):
        emails, token = await adapter.list_emails(creds("u1", "gmail"), date_from=date_from, date_to=date_to,
                                                  page_token=None, max_results=100)
    assert (emails, token) == ([], None)
    assert len(gets) == 1
    return gets[0]["params"]
