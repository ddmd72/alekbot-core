"""WorkerHandler in the redelivery world: the deep-research failure notice reaches the user's account, and the daily
review fan-out keeps going past a user whose enqueue and mark release both failed (the mutation check's survivors).
Assertions are on the handler's answer, the posts, the agent turn and the created tasks."""
from google.api_core.exceptions import ServiceUnavailable

from src.adapters.firestore_dedup_store import FirestoreDedupStore
from src.domain.user import UserBotConfig, UserProfile
from src.handlers import worker_handler
from src.services.email_review_service import EmailReviewService
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.redelivery_world import ACCOUNT, CHANNEL, T0, USER, RedeliveryWorld, deliver, ok

NOTICE = "The research on heat pumps in Springfield did not finish."


async def test_a_deep_research_failure_notice_is_posted_and_runs_in_the_user_s_account():
    world = RedeliveryWorld()
    world.router.always(ok(NOTICE))
    payload = world.payload("deep_research_failed", user_id=USER, account_id=ACCOUNT, session_id=f"{USER}:{CHANNEL}",
                            origin_platform="slack", query="heat pumps in Springfield", reason="agent_failed")

    answer = await deliver(world.handler, payload, "t-1")

    assert answer[1] == 200
    assert world.texts == [NOTICE]
    [turn] = world.router.messages
    assert turn.context["account_id"] == ACCOUNT


class ReleaseDownMarks(FirestoreDedupStore):
    """The daily-review mark store whose release fails, as a Firestore outage does."""

    async def release(self, event_id):
        raise ServiceUnavailable("firestore unavailable")


async def test_the_daily_review_fan_out_goes_on_after_a_failed_enqueue_whose_mark_release_failed(monkeypatch):
    """u1's enqueue fails and so does the release of its mark; u2 is still enqueued and the tick reports both."""
    oauth = CredentialsStore()
    world = RedeliveryWorld(
        email_review=EmailReviewService(email_provider=None, oauth_credentials=oauth.repo),  # type: ignore[arg-type]
        daily_review_marks=ReleaseDownMarks(db_client=EvalDb(), collection_name="marks", ttl_seconds=172800),
    )
    for user_id, account_id in (("u1", "a1"), ("u2", "a2")):
        await oauth.repo.save_credentials(creds(user_id, "gmail"))
        world.users.put(UserProfile(user_id=user_id, account_id=account_id, config=UserBotConfig(
            timezone="Europe/Berlin", gmail_daily_review=True, gmail_daily_review_hour=9)))
    pin_datetime(monkeypatch, worker_handler, T0)   # 09:00 in Berlin: both users are inside their review window
    world.client.fail_next(exc=ServiceUnavailable("cloud tasks unavailable"))

    answer = await deliver(world.handler, world.payload("start_daily_email_review"))

    assert answer == ({"started": 1, "failed": 1}, 200)
    assert [b["user_id"] for b in world.bodies("daily_email_review")] == ["u2"]
