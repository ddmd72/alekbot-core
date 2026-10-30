"""/api/gmail/status reads the Gmail indexing state and the latest indexing job under the Gmail provider key
(slice 1a): both repositories here answer only for that key, so a status that asks under another key reads as never
indexed and idle. Real credential store over EvalDb; the two indexing repositories are in-memory fakes."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from quart import Quart

from src.adapters.firestore_credential_store import FirestoreCredentialStore
from src.composition.connector_registry import ConnectorRegistry
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import creds

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
USER = "user-1"
NEWEST, OLDEST = datetime(2026, 10, 20, tzinfo=timezone.utc), datetime(2026, 1, 5, tzinfo=timezone.utc)


class IndexedEmails:
    async def get_indexing_state(self, user_id, provider):
        if (user_id, provider) != (USER, "gmail"):
            return None
        return SimpleNamespace(indexed_through=NEWEST, oldest_indexed_through=OLDEST)


class Jobs:
    def __init__(self, status: str) -> None:
        self.status = status

    async def get_latest_job(self, user_id, provider):
        if (user_id, provider) != (USER, "gmail"):
            return None
        return SimpleNamespace(status=self.status, job_id="job-1")


async def _status(job_status: str) -> dict:
    db = EvalDb(indexes=INDEXES_FILE, groups={"eval_credentials": "development_credentials"})
    store = FirestoreCredentialStore(db, SimpleNamespace(credentials_collection="eval_credentials"),
                                     cipher=AesGcmTestCipher())
    await store.save(creds(USER, "gmail"))
    sessions = MagicMock()
    sessions.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": "acct-1", "role": "owner"})
    bp = create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=sessions, user_repo=MagicMock(), fact_repo=MagicMock(),
        embedding_service=MagicMock(), oauth_credentials_port=store, connectors=ConnectorRegistry(),
        indexed_email_repo=IndexedEmails(), email_job_repo=Jobs(job_status))
    app = Quart("test_gmail_status_indexing")
    app.register_blueprint(bp)
    async with app.test_client() as client:
        resp = await client.get("/api/gmail/status", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    return await resp.get_json()


async def test_status_reports_the_indexed_range_and_a_running_job():
    body = await _status("running")
    assert body["indexed_through"] == NEWEST.isoformat()
    assert body["oldest_indexed_through"] == OLDEST.isoformat()
    assert body["indexing_active"] is True and body["active_job_id"] == "job-1"


async def test_status_of_a_finished_job_is_idle():
    body = await _status("completed")
    assert body["indexed_through"] == NEWEST.isoformat()
    assert body["indexing_active"] is False and body["active_job_id"] is None
