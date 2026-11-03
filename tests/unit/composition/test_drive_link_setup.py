"""`build_drive_link_service`: the link service main() builds (store on the env-prefixed collection, the aiohttp stream,
the authorization service's grant check, the resource URI's host)."""
from types import SimpleNamespace

import pytest

from src.adapters.firestore_drive_link_store import FirestoreDriveLinkStore
from src.adapters.http_media_stream_adapter import HttpMediaStreamAdapter
from src.composition.drive_link_setup import build_drive_link_service
from tests.support.firestore_eval import EvalDb

AUTH = SimpleNamespace(mcp_resource_uri="https://mcp.example.test/mcp")
ENV = SimpleNamespace(drive_links_collection="development_drive_links")


class Authz:
    async def grant_allowed(self, user_id, grant_id):
        return False


def test_the_service_is_built_from_its_parts():
    authz, drive = Authz(), object()
    links = build_drive_link_service(db_client=EvalDb(), env_config=ENV, drive_service=drive,  # type: ignore[arg-type]
                                     authorization_service=authz, digest_key=bytes(range(32)),  # type: ignore[arg-type]
                                     auth_config=AUTH)  # type: ignore[arg-type]
    assert isinstance(links._store, FirestoreDriveLinkStore) and isinstance(links._media, HttpMediaStreamAdapter)
    assert links._drive is drive and links._grant_allowed == authz.grant_allowed
    assert links._base_url == "https://mcp.example.test"


def test_no_drive_service_raises():
    with pytest.raises(RuntimeError, match="drive service"):
        build_drive_link_service(db_client=EvalDb(), env_config=ENV, drive_service=None,
                                 authorization_service=Authz(), digest_key=bytes(range(32)),  # type: ignore[arg-type]
                                 auth_config=AUTH)  # type: ignore[arg-type]
