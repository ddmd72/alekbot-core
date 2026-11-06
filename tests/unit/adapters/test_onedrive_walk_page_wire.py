"""Wire test: a OneDrive folder listing asks Graph for at most 200 children per page, whatever page the caller wants
(DRIVE_SCOPE_WALK_RFC §5.5 `WALK_PAGE`; plan Delta 10: Graph's children maximum is not verified, so a walk keeps the
page size the re-listing always used). Mock boundary: aiohttp.ClientSession (the shared routed fake)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.microsoft.onedrive_adapter import OneDriveAdapter
from src.composition.connector_registry import ConnectorRegistry
from src.domain.drive_index_params import RELIST_PAGE, WALK_PAGE
from src.domain.provider_account import ProviderAccount
from tests.support.drive_handles import drive_handle
from tests.support.http_boundary_fake import install
from tests.support.oauth_credentials_store import CredentialsStore, creds

CHILDREN = "https://graph.microsoft.com/v1.0/me/drive/items/f-rd/children"


async def _listing(monkeypatch, limit):
    fake = install(monkeypatch)
    fake.route("GET", CHILDREN, 200, {"value": [{"id": "i-plan", "name": "Tom's plan.docx", "file": {},
                                                 "parentReference": {"id": "f-rd"}}]})
    store = CredentialsStore()
    credential_id = await store.repo.save(creds(
        "usr-1", "microsoft_onedrive", access_token="tok-a", token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        account=ProviderAccount(realm="microsoft", subject="subject-1", label="owner1@example.com")))
    adapter = OneDriveAdapter(store.repo, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))
    page = await adapter.list_children_changes(drive_handle("usr-1", "conn-1", credential_id), "f-rd", limit=limit)
    assert fake.unrouted == []
    [request] = fake.requests_to("GET", CHILDREN)
    return page, request.url


async def test_a_walk_page_is_clamped_to_200(monkeypatch):
    page, url = await _listing(monkeypatch, WALK_PAGE)
    assert url == f"{CHILDREN}?$top=200"
    assert [c.item_id for c in page.changes] == ["i-plan"]


@pytest.mark.parametrize("limit", [3, RELIST_PAGE])
async def test_a_page_at_or_under_200_is_asked_as_is(monkeypatch, limit):
    _, url = await _listing(monkeypatch, limit)
    assert url == f"{CHILDREN}?$top={limit}"
