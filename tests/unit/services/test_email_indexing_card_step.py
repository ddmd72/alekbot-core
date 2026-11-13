"""EmailIndexingService with a card step attached (email index v2 §6.5, slice 6): the step runs after the v1 save with
exactly the saved mail, a failing step never stops the page, a revoked grant propagates, and the v1 save is identical
with and without a step."""
import logging

import pytest

from src.domain.email import EmailFullContent
from src.domain.exceptions import OAuthGrantRevokedError
from tests.unit.services.test_email_indexing_service import _make_credentials, _make_job
from tests.unit.services.test_email_indexing_skip_stored import _build


class RecordingStep:
    def __init__(self, repo, raises=None) -> None:
        self.repo, self.raises = repo, raises
        self.calls = []
        self.saved_before = None

    async def card_page(self, *, credentials, user_id, account_id, mails):
        self.saved_before = self.repo.save_batch.await_count
        self.calls.append((credentials, user_id, account_id, list(mails)))
        if self.raises:
            raise self.raises


def _with_content(service):
    service._gmail.batch_get_full_content.side_effect = lambda credentials, email_ids, **kw: {
        e: EmailFullContent(email_id=e, body_text=f"Booking RM-55120 for {e}", body_html=None,
                            attachments=["RM-55120.pdf"], recipients=("alex@example.com",)) for e in email_ids}


async def _run(service):
    job = _make_job()
    creds = _make_credentials()
    await service.run_indexing_job(job, creds, "acct-1", mode="incremental")
    return job, creds


async def test_the_step_runs_after_the_v1_save_with_exactly_the_saved_mail():
    service, repo, _ = _build({})
    _with_content(service)
    step = RecordingStep(repo)
    service.attach_card_step(step)

    job, creds = await _run(service)

    (got_creds, user_id, account_id, mails), = step.calls
    saved = repo.save_batch.await_args.args[0]
    assert step.saved_before == 1 and got_creds is creds and (user_id, account_id) == (job.user_id, "acct-1")
    assert [m.meta.email_id for m in mails] == [e.email_id for e in saved] == ["m1", "m2", "m3"]
    assert all(m.content is not None and m.content.email_id == m.meta.email_id for m in mails)
    assert mails[0].meta.subject == saved[0].subject


async def test_a_mail_whose_full_content_failed_is_handed_over_without_content():
    service, repo, _ = _build({})
    service._gmail.batch_get_full_content.return_value = {}
    step = RecordingStep(repo)
    service.attach_card_step(step)

    await _run(service)

    assert all(m.content is None for m in step.calls[0][3])


async def test_a_failing_step_is_logged_and_the_page_completes(caplog):
    service, repo, _ = _build({})
    step = RecordingStep(repo, raises=RuntimeError("store down"))
    service.attach_card_step(step)
    service._job_repo.update_job.reset_mock()

    with caplog.at_level(logging.ERROR):
        job, _ = await _run(service)

    assert job.emails_stored == 3 and service._job_repo.update_job.await_count >= 1
    assert any("[EmailCard] page step failed: RuntimeError" in r.getMessage() for r in caplog.records)


async def test_a_revoked_grant_from_the_step_propagates():
    service, repo, _ = _build({})
    service.attach_card_step(RecordingStep(repo, raises=OAuthGrantRevokedError("invalid_grant")))
    with pytest.raises(OAuthGrantRevokedError):
        await _run(service)


async def test_the_v1_save_is_identical_with_and_without_a_step():
    plain, plain_repo, _ = _build({})
    stepped, stepped_repo, _ = _build({})
    stepped.attach_card_step(RecordingStep(stepped_repo))

    await _run(plain)
    await _run(stepped)

    dump = lambda repo: [e.model_dump(exclude={"indexed_at", "email_date", "vector", "tags_vector", "metadata_vector"}) for e in repo.save_batch.await_args.args[0]]
    assert dump(plain_repo) == dump(stepped_repo)


async def test_without_a_step_nothing_is_called():
    service, repo, _ = _build({})
    assert service._card_step is None
    job, _ = await _run(service)
    assert job.emails_stored == 3
