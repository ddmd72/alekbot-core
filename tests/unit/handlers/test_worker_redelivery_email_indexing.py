"""The email-indexing chain delivered more than once (P15 part 2, card G4).

On the production graph (`tests/support/prod_graph.py`: `build_application_graph` over the deploy's sealed environment,
`EvalDb`, recording Cloud Tasks clients), with the HTTP boundary faked (`tests/support/http_boundary_fake.py`): Gmail's
list and message routes. The email classifier (an LLM agent) is replaced on the built service by a scripted one; the
embeddings are `prod_env`'s fake. Deliveries go through the production `WorkerHandler` with the redelivery world's
helpers (`tests/support/redelivery_world.py`); its claim store is the graph's.

`_handle_email_indexing` (`worker_handler.py:386-427`): `load_job_for_execution` (`email_indexing_service.py:218-254`;
`get_job` is a document read) skips a job that is not `running`; `run_indexing_job` (`:274-`) lists one Gmail page
(`gmail_provider_adapter.py:61`, `list_emails`: `GET …/messages`, then `GET …/messages/<id>` per message), classifies,
fetches the valuable ones in full (`:157`, `batch_get_full_content`, same path with `format=full`), stores each email
under `{user}_{email}` (`firestore_indexed_email_repo.py:112`), saves the page cursor on the job, and completes the job
when Gmail gives no `nextPageToken`. A page error is a 200 (`worker_handler.py:412-419`); the next-page enqueue
(`:421-423`) is outside any `try`, so its error releases the claim and Cloud Tasks retries.

The service's clock is pinned to `T0` (its "today" is the incremental job's first date and the page-boundary guard),
so the credentials' expiry is set from `T0`, not from real now: this service, not `graph_auth`, reads it.
"""
import base64
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytest
from google.api_core.exceptions import DeadlineExceeded

from src.domain.email import EmailClassificationResult, EmailMetadata, IndexingJob, OAuthCredentials
from src.ports.email_classifier_port import EmailClassifierPort
from src.services import email_indexing_service
from tests.support import http_boundary_fake
from tests.support.clock_instants import pin_datetime
from tests.support.cloud_tasks_fake import FakeCloudTasksClient
from tests.support.http_boundary_fake import HttpFake, RecordedRequest
from tests.support.oauth_credentials_store import creds
from tests.support.prod_graph import ACCOUNT_ID, USER_ID, ProdGraph, prod_graph  # noqa: F401  (fixture)
from tests.support.redelivery_world import T0, Rendezvous, deliver, deliver_twice, require

pytestmark = pytest.mark.slow

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
JOB = "j1"
PAYLOAD = {"task_type": "email_indexing", "job_id": JOB}
EMAILS = {
    "m1": ("school@example.com", "Parents' meeting moved to Saturday", "Tue, 27 Oct 2026 07:00:00 +0000"),
    "m2": ("shop@example.com", "Your AirPods order shipped", "Tue, 27 Oct 2026 07:30:00 +0000"),
}


class ValuableClassifier(EmailClassifierPort):
    """Every email is valuable, with a fact (no LLM)."""

    async def classify_batch(self, emails: List[EmailMetadata], user_id: str,
                             credentials: Optional[OAuthCredentials] = None) -> List[EmailClassificationResult]:
        return [EmailClassificationResult(email_id=e.email_id, valuable=True, category="personal",
                                          fact=f"The user received '{e.subject}'.", tags=["mail"]) for e in emails]


def _message(email_id: str) -> Dict[str, Any]:
    """A Gmail message as both `format=metadata` and `format=full` read it (`_parse_metadata`, `_parse_payload`)."""
    sender, subject, date = EMAILS[email_id]
    return {
        "id": email_id, "labelIds": ["INBOX"], "snippet": subject,
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
                        {"name": "Date", "value": date}],
            "body": {"data": base64.urlsafe_b64encode(f"{subject}.".encode()).decode()},
        },
    }


def _gmail(fake: HttpFake, pages: Dict[Optional[str], Dict[str, Any]]) -> None:
    """The list route answers the page for the request's `pageToken` (None: the first page); each message has a route."""

    def listing(req: RecordedRequest) -> Tuple[int, Any]:
        return 200, pages[(req.params or {}).get("pageToken")]

    fake.route("GET", f"{GMAIL}/messages", handler=listing)
    for email_id in EMAILS:
        fake.route("GET", f"{GMAIL}/messages/{email_id}", 200, _message(email_id))


async def _running_job(graph: ProdGraph, monkeypatch: Any) -> Any:
    """Gmail credentials for `u1` and a `running` incremental job, through the built service's own stores; the scripted
    classifier on the built service; the service's clock at `T0`. Returns the service."""
    service = graph.app.worker_handler._email_indexing
    pin_datetime(monkeypatch, email_indexing_service, T0)
    await service._oauth.save_credentials(creds(USER_ID, "gmail", token_expiry=T0 + timedelta(hours=1)))
    await service._job_repo.create_job(IndexingJob(
        job_id=JOB, user_id=USER_ID, account_id=ACCOUNT_ID, provider="gmail", triggered_by="scheduler",
        status="running", mode="incremental", started_at=T0, updated_at=T0,
    ))
    service._classifier = ValuableClassifier()
    return service


def _job(service: Any) -> Dict[str, Any]:
    return dict(service._job_repo.collection.docs[JOB])


def _email_doc_ids(service: Any) -> List[str]:
    return sorted(service._email_repo.collection.docs)


def _indexing_bodies() -> List[dict]:
    return [b for c in FakeCloudTasksClient.instances for b in c.created_bodies() if b.get("task_type") == "email_indexing"]


def _claim_docs(graph: ProdGraph) -> List[dict]:
    claims = graph.app.worker_handler._task_claims
    docs = graph.offline.db.collection(claims.collection_name).docs
    return [dict(docs[k]) for k in sorted(docs) if "attempt" in docs[k]]


class TestEmailIndexingChain:
    async def test_one_page_delivered_twice_concurrently_stores_each_email_once(self, prod_graph, monkeypatch):
        """Two deliveries of one page under `asyncio.gather` both load the job while it is `running` and both index the
        page: each email is still stored once, under its deterministic id, and the job completes.

        Interleaving: `yield_on_get` on the job collection (`get_job` is a document read) and
        `Rendezvous.wrap(job_repo, "get_job")`, so both deliveries hold the `running` job before either writes. The
        double classification is by design and not asserted (P15-D6 is about the job's start, not the page).
        Regression: a non-deterministic email doc id (two documents per email)."""
        fake = http_boundary_fake.install(monkeypatch)
        _gmail(fake, {None: {"messages": [{"id": "m1"}, {"id": "m2"}]}})
        service = await _running_job(prod_graph, monkeypatch)
        service._job_repo.collection.yield_on_get = True
        rv = Rendezvous()
        rv.wrap(service._job_repo, "get_job")

        twice = await deliver_twice(prod_graph.app.worker_handler, PAYLOAD, gathered=True, rendezvous=rv)

        twice.reraise()
        require(twice.overlapped and rv.met and not rv.stalled, "both deliveries loaded the running job at once")
        assert twice.answers == [({"status": "ok", "has_more": False}, 200)] * 2
        assert _email_doc_ids(service) == [f"{USER_ID}_m1", f"{USER_ID}_m2"]
        assert _job(service)["status"] == "completed"
        assert _indexing_bodies() == []
        assert fake.unrouted == []

    async def test_a_lost_next_page_enqueue_is_retried_from_the_saved_cursor(self, prod_graph, monkeypatch):
        """Page 1 is stored and its cursor `T2` saved, then the next-page create fails before landing: the raise
        propagates (`worker_handler.py:421-423`), the claim is released, and the retry of the same task reads page 2 from
        the saved cursor and completes the job.

        Regression: the enqueue error swallowed into a 200 strands the job `running` with page 2 unread. The
        landed-then-error variant (a second chain) is P15-D4, overstated by the arbiter; not pinned."""
        fake = http_boundary_fake.install(monkeypatch)
        _gmail(fake, {None: {"messages": [{"id": "m1"}], "nextPageToken": "T2"}, "T2": {"messages": [{"id": "m2"}]}})
        service = await _running_job(prod_graph, monkeypatch)
        client = prod_graph.app.worker_handler._task_dispatch._queue.client
        create_task = client.create_task
        lost: List[Dict[str, Any]] = []

        def first_create_lost(request: Dict[str, Any], **kwargs: Any) -> Any:
            if not lost:
                lost.append(request)
                raise DeadlineExceeded("create timed out")
            return create_task(request, **kwargs)

        client.create_task = first_create_lost  # the graph's queue client, this instance only

        try:
            first: Any = await deliver(prod_graph.app.worker_handler, PAYLOAD, "t-ei")
        except DeadlineExceeded as exc:
            first = exc
        assert isinstance(first, DeadlineExceeded)
        assert _claim_docs(prod_graph) == []
        assert _job(service)["next_page_token"] == "T2"

        assert await deliver(prod_graph.app.worker_handler, PAYLOAD, "t-ei") == ({"status": "ok", "has_more": False}, 200)
        assert _email_doc_ids(service) == [f"{USER_ID}_m1", f"{USER_ID}_m2"]
        assert _job(service)["status"] == "completed"
        listed = [(r.params or {}).get("pageToken") for r in fake.requests if r.url == f"{GMAIL}/messages"]
        assert listed == [None, "T2"]
        assert _indexing_bodies() == []
        assert fake.unrouted == []
        assert [d["state"] for d in _claim_docs(prod_graph)] == ["done"]
