"""Mutation P3: `delete_client` removes the client document."""
from types import SimpleNamespace

from src.adapters.firestore_mcp_client_repository import FirestoreMCPClientRepository
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb

CLIENTS = "eval_mcp_oauth_clients"


async def test_delete_client_removes_the_document():
    db = EvalDb()
    db.collection(CLIENTS).docs["client-1"] = {"client_id": "client-1"}
    db.collection(CLIENTS).docs["client-2"] = {"client_id": "client-2"}
    repo = FirestoreMCPClientRepository(
        db, SimpleNamespace(mcp_oauth_clients_collection=CLIENTS, mcp_auth_codes_collection="eval_codes",
                            mcp_refresh_tokens_collection="eval_refresh"),
        cipher=AesGcmTestCipher(), digest_key=bytes(range(32)))
    await repo.delete_client("client-1")
    assert set(db.collection(CLIENTS).docs) == {"client-2"}
