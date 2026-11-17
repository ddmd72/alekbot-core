"""The date a daily email review is reported under, against the slot date that decided it (P16-C item 2, P16-D2).

Wiring: the real `StartDailyEmailReviewTask` fan-out over the real `EmailReviewService.find_eligible_users`
and the real `GcpTaskQueue` (a Cloud Tasks client that refuses a used name); the created task's body goes to
`DailyEmailReviewTask`, whose review service records the date handed to `build_alert`. `datetime` is pinned in
`src.services.start_daily_email_review_task` and `src.services.daily_email_review_task`; the tick is built from a local wall time of the zone, not typed as a UTC number.
"""
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

import pytest

from src.domain.agent import AgentStatus
from src.domain.notify_result import NotifyResult
from src.domain.user import UserBotConfig, UserProfile
from src.ports.dedup_store import DedupStore
from src.services import daily_email_review_task, start_daily_email_review_task
from src.services.daily_email_review_task import DailyEmailReviewTask
from src.services.gmail_reconnect_flag import GmailReconnectFlag
from src.services.start_daily_email_review_task import StartDailyEmailReviewTask
from src.services.email_review_service import EmailReviewService
from src.services.task_dispatch_service import TaskDispatchService
from src.infrastructure.notification_sla import dispatch_deadline_s
from src.domain.notification_kind import NotificationKind
from tests.support.clock_instants import BERLIN, HELSINKI, NEW_YORK, UTC, pin_datetime
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.recording_task_queue import make_task_queue
from src.composition.connector_registry import ConnectorRegistry

_USER = "u1"
_ACCOUNT = "a1"


class _Marks(DedupStore):
    def __init__(self) -> None:
        self.marks: set[str] = set()

    async def is_duplicate(self, event_id):
        return event_id in self.marks

    async def try_mark_processed(self, event_id):
        if event_id in self.marks:
            return False
        self.marks.add(event_id)
        return True

    async def release(self, event_id):
        self.marks.discard(event_id)


class _Users:
    def __init__(self, profile: UserProfile) -> None:
        self._profile = profile

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self._profile if user_id == self._profile.user_id else None


class _RecordingReview(EmailReviewService):
    """The real eligibility rule; the Gmail fetch is one email, and the alert date is recorded."""

    def __init__(self, oauth) -> None:
        super().__init__(email_provider=None, oauth_credentials=oauth,  # type: ignore[arg-type]
                         connector=ConnectorRegistry().get("gmail"))
        self.alert_dates: List[str] = []

    async def fetch_review_payload(self, user_id: str):
        return [{"email_id": "e1", "subject": "Hello"}]

    def build_alert(self, date_str: str, email_count: int) -> str:  # type: ignore[override]
        self.alert_dates.append(date_str)
        return f"[DAILY EMAIL REVIEW] {date_str}"


class _Notification:
    async def notify(self, **kwargs):
        return NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS)


class _Factory:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


async def _world(zone: str, hour: int):
    store = CredentialsStore()
    await store.repo.save(creds(_USER, "gmail"))
    profile = UserProfile(user_id=_USER, account_id=_ACCOUNT,
                          config=UserBotConfig(timezone=zone, gmail_daily_review=True, gmail_daily_review_hour=hour))
    review = _RecordingReview(store.repo)
    queue, client = make_task_queue()

    marks = _Marks()
    notification = _Notification()
    fan_out = StartDailyEmailReviewTask(
        email_review=review, task_dispatch=TaskDispatchService(queue), user_repo=_Users(profile), marks=marks,
        deadline_seconds=dispatch_deadline_s(NotificationKind.DAILY_DIGEST))
    daily = DailyEmailReviewTask(
        email_review=review, ensure_agents=_Factory().ensure_agents_for_user, notification_service=notification,
        gmail_reconnect_flag=GmailReconnectFlag(
            oauth_credentials=None, mail_connector=None, ensure_agents=_Factory().ensure_agents_for_user,
            notification_service=notification),
        alert_sink=None)
    return fan_out, daily, marks, review, client


def _tick(zone: str, wall: datetime) -> datetime:
    return wall.replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)


async def _run(monkeypatch, zone: str, hour: int, wall: datetime, slot: date):
    fan_out, daily, marks, review, client = await _world(zone, hour)
    pin_datetime(monkeypatch, start_daily_email_review_task, _tick(zone, wall))
    pin_datetime(monkeypatch, daily_email_review_task, _tick(zone, wall))
    result, status = await fan_out({}, {})
    assert (status, result["started"]) == (200, 1)
    assert marks.marks == {f"daily_review:u1:{slot}"}
    (task_type, body), = client.worker_tasks()
    assert task_type == "daily_email_review"
    result, status = await daily(body, {})
    assert (status, result["status"]) == (200, "ok")
    (alert_date,) = review.alert_dates
    return alert_date


# (zone, configured hour, local tick, slot date the fan-out decided on)
_SLOT_DATE_IS_THE_UTC_DATE = [
    pytest.param(BERLIN, 9, datetime(2026, 7, 15, 9, 12), date(2026, 7, 15), id="berlin-09-summer"),
    pytest.param(HELSINKI, 9, datetime(2026, 10, 27, 9, 12), date(2026, 10, 27), id="helsinki-09-winter"),
    pytest.param(NEW_YORK, 9, datetime(2026, 10, 27, 9, 12), date(2026, 10, 27), id="new_york-09"),
]
_SLOT_DATE_DIFFERS_FROM_THE_UTC_DATE = [
    pytest.param(HELSINKI, 0, datetime(2026, 7, 15, 0, 12), date(2026, 7, 15), id="helsinki-00-summer",
                 marks=pytest.mark.xfail(strict=True, raises=AssertionError,
                                         reason="P16-D2: the report date is the UTC date of the run, not the review slot's local date; owner decision")),
    pytest.param(NEW_YORK, 23, datetime(2026, 10, 26, 23, 12), date(2026, 10, 26), id="new_york-23-autumn",
                 marks=pytest.mark.xfail(strict=True, raises=AssertionError,
                                         reason="P16-D2: the report date is the UTC date of the run, not the review slot's local date; owner decision")),
]


@pytest.mark.parametrize("zone,hour,wall,slot", _SLOT_DATE_IS_THE_UTC_DATE)
async def test_report_date_matches_the_slot_when_local_and_utc_dates_agree(monkeypatch, zone, hour, wall, slot):
    assert await _run(monkeypatch, zone, hour, wall, slot) == slot.isoformat()


@pytest.mark.parametrize("zone,hour,wall,slot", _SLOT_DATE_DIFFERS_FROM_THE_UTC_DATE)
async def test_report_date_is_the_slot_date_when_utc_is_already_the_next_or_previous_day(monkeypatch, zone, hour, wall, slot):
    assert _tick(zone, wall).date() != slot, "the case must be one where the UTC date differs from the slot date"
    assert await _run(monkeypatch, zone, hour, wall, slot) == slot.isoformat()
