"""
Wire tests for OneDriveAdapter's error classes (USER_DRIVE_INDEXING_RFC §4, §7.7, §8.2).

A rejected refresh is DriveReauthRequired (still a DriveNotConnectedError for Step 1 callers); a 401
that survives a successful forced refresh is DriveAccessDenied, never a lost grant (spike A6);
429/503 still failing after the adapter's own retries is DriveThrottled with the provider's
Retry-After, capped at TICK_DEADLINE_S; a token-endpoint failure that is not a rejection is DriveThrottled
too (systemic, §8.2). Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md).
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.adapters.microsoft.graph_auth import GraphReauthRequired
from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.drive_index_params import TICK_DEADLINE_S
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveAccessDenied, DriveNotConnectedError, DriveReauthRequired, DriveThrottled
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get


def _resp(json_data=None, status=200, headers=None):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = headers or {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=json.dumps(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(get):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.get.side_effect = get
    return s


def _adapter():
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get.return_value = OAuthCredential(
        user_id="u1",
        account_id="acct-1",
        kind=ONEDRIVE_PROVIDER,
        integration=Integration.DRIVE,
        account=ProviderAccount(realm="microsoft", subject="", label=""),
        access_token="tok",
        refresh_token="r",
        token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        scopes=[],
    )
    mirror_get(oauth)
    return OneDriveAdapter(oauth, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))


async def test_rejected_refresh_is_reauth_required():
    adapter = _adapter()
    adapter._tokens.headers = AsyncMock(side_effect=GraphReauthRequired("invalid_grant"))
    with patch("aiohttp.ClientSession", return_value=_session([])), pytest.raises(DriveReauthRequired) as exc:
        await adapter.get_changes("u1", None)
    assert isinstance(exc.value, DriveNotConnectedError)    # Step 1 callers keep their mapping


async def test_401_after_successful_refresh_is_access_denied():
    adapter = _adapter()
    adapter._tokens.headers = AsyncMock(return_value={"Authorization": "Bearer t"})
    session = _session([_resp({}, status=401), _resp({}, status=401)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveAccessDenied) as exc:
        await adapter.get_change("u1", "X!1")
    assert not isinstance(exc.value, DriveNotConnectedError)   # never read as a lost grant
    forced = [c for c in adapter._tokens.headers.await_args_list if c.kwargs == {"force_refresh": True}]
    assert len(forced) == 1


async def test_throttling_after_retries_is_drive_throttled_with_retry_after():
    adapter = _adapter()
    session = _session([_resp({}, status=429, headers={"Retry-After": "120"}) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session), \
            patch("src.adapters.microsoft.onedrive_adapter.asyncio.sleep", new=AsyncMock()) as sleep, \
            pytest.raises(DriveThrottled) as exc:
        await adapter.get_change("u1", "X!1")
    assert exc.value.retry_after_s == 120.0                 # the provider's value, not the 30 s cap
    assert [c.args[0] for c in sleep.await_args_list] == [30.0, 30.0, 30.0]
    assert session.get.call_count == 4                      # first try + 3 retries


async def test_throttled_without_retry_after_uses_the_default_hint():
    adapter = _adapter()
    session = _session([_resp({}, status=503) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session), \
            patch("src.adapters.microsoft.onedrive_adapter.asyncio.sleep", new=AsyncMock()), \
            pytest.raises(DriveThrottled) as exc:
        await adapter.get_change("u1", "X!1")
    assert exc.value.retry_after_s == 60.0


async def test_unparseable_retry_after_uses_the_default_hint():
    adapter = _adapter()
    date = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    session = _session([_resp({}, status=429, headers=date) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session), \
            patch("src.adapters.microsoft.onedrive_adapter.asyncio.sleep", new=AsyncMock()), \
            pytest.raises(DriveThrottled) as exc:
        await adapter.get_change("u1", "X!1")
    assert exc.value.retry_after_s == 60.0


async def test_huge_retry_after_is_capped_at_the_tick_deadline():
    adapter = _adapter()
    session = _session([_resp({}, status=429, headers={"Retry-After": "9999999"}) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session), \
            patch("src.adapters.microsoft.onedrive_adapter.asyncio.sleep", new=AsyncMock()), \
            pytest.raises(DriveThrottled) as exc:
        await adapter.get_change("u1", "X!1")
    assert exc.value.retry_after_s == float(TICK_DEADLINE_S)    # the fan-out re-ticks after that anyway


@pytest.mark.parametrize("failure", [ValueError("MS token refresh failed (503)"),
                                     aiohttp.ClientConnectionError("reset")])
async def test_failed_token_refresh_is_systemic_not_per_item(failure):
    # A token-endpoint outage must end the tick (§8.2 systemic), never count against the item read.
    adapter = _adapter()
    adapter._tokens.headers = AsyncMock(side_effect=failure)
    with patch("aiohttp.ClientSession", return_value=_session([])), pytest.raises(DriveThrottled) as exc:
        await adapter.get_change("u1", "X!1")
    assert exc.value.retry_after_s == 60.0
