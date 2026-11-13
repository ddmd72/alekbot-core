"""The daily review's mail window across the DST transitions (P16-C item 3, D3).

The window is the last 25 hours by exact instant (owner decision 2026-11-13): `date_from = now - 25 h`, `date_to=None`,
`exact_instants=True`, which the Gmail adapter sends as `after:<epoch seconds>` (no `before:`). The day-granular
`after:YYYY/MM/DD` it replaced is read by Gmail as midnight Pacific, which left a daily gap when the review slot falls
before Pacific midnight in UTC. 25 h, not 24 h: the fall-back day is 25 hours long, so two consecutive daily reviews
then meet exactly (no gap, no overlap); on an ordinary day they overlap by one hour and on the spring-forward day by two,
which the owner accepts.

Each review here runs through the real Gmail adapter over the aiohttp boundary fake, so the `after:` asserted is the one
the review actually sends, not a re-derivation.
"""
from datetime import datetime, timedelta
from typing import List
from zoneinfo import ZoneInfo

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.ports.email_provider_port import EmailProviderPort
from src.services import email_review_service
from src.services.email_review_service import EmailReviewService
from tests.support import http_boundary_fake
from tests.support.clock_instants import UTC, ZONES, pin_datetime, transition
from tests.support.oauth_credentials_store import CredentialsStore, creds
from src.composition.connector_registry import ConnectorRegistry

_USER = "u1"
_FAR = datetime(2099, 1, 1, tzinfo=UTC)
_MESSAGES = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


class _RecordingProvider(EmailProviderPort):
    """Records the window arguments of every list call and forwards it to the real Gmail adapter (empty mailbox)."""

    def __init__(self) -> None:
        self.calls: List[dict] = []
        self._gmail = GmailProviderAdapter("cid", "csecret")

    async def list_emails(self, credentials, date_from=None, date_to=None, page_token=None, max_results=100,
                          query=None, exact_instants=False):
        self.calls.append({"date_from": date_from, "date_to": date_to, "exact_instants": exact_instants})
        return await self._gmail.list_emails(credentials, date_from=date_from, date_to=date_to, page_token=page_token,
                                             max_results=max_results, query=query, exact_instants=exact_instants)

    async def list_headers(self, credentials, *, after=None, before=None, key=None, limit=100):
        return [], False

    async def batch_get_full_content(self, credentials, email_ids, deep=False):
        return {}

    async def refresh_token(self, credentials):
        raise AssertionError("the credentials are far from expiry; no refresh is expected")

    async def list_parts(self, credentials, message_id):
        raise NotImplementedError

    async def get_part(self, credentials, message_id, part, max_bytes):
        raise NotImplementedError


async def _review_at(monkeypatch, instant: datetime) -> dict:
    """One review at `instant`: its recorded list call plus the `after:` instant the adapter put on the wire."""
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", _MESSAGES, 200, {"messages": []})
    store = CredentialsStore()
    await store.repo.save(creds(_USER, "gmail", token_expiry=_FAR))
    provider = _RecordingProvider()
    service = EmailReviewService(provider, store.repo, connector=ConnectorRegistry().get("gmail"))
    pin_datetime(monkeypatch, email_review_service, instant)
    assert await service.fetch_review_payload(_USER) == []
    (call,) = provider.calls
    assert fake.unrouted == []
    (req,) = [r for r in fake.requests if r.url == _MESSAGES]
    q = dict(req.params)["q"]
    assert q.startswith("after:") and "before:" not in q and " " not in q, q
    sent_after = datetime.fromtimestamp(int(q[len("after:"):]), tz=UTC)
    return {"now": instant, "sent_after": sent_after, **call}


def _consecutive_slots(zone: str, kind: str):
    """Two daily slots at 09:12 local: the day before the transition and the transition day, as UTC instants."""
    tz = ZoneInfo(zone)
    t = transition(zone, kind, "T").utc
    local_day2 = datetime.combine(t.astimezone(tz).date(), datetime.min.time()) + timedelta(hours=9, minutes=12)
    second_now = local_day2.replace(tzinfo=tz).astimezone(UTC)
    first_now = (local_day2 - timedelta(days=1)).replace(tzinfo=tz).astimezone(UTC)
    return t, first_now, second_now


@pytest.mark.parametrize("zone", ZONES)
async def test_consecutive_slots_across_the_fall_back_night_leave_no_gap(monkeypatch, zone):
    tz = ZoneInfo(zone)
    fall, first_now, second_now = _consecutive_slots(zone, "fall")
    assert second_now - first_now == timedelta(hours=25), "the fall-back day is 25 hours long"

    first = await _review_at(monkeypatch, first_now)
    second = await _review_at(monkeypatch, second_now)

    for review in (first, second):
        assert review["date_to"] is None and review["exact_instants"] is True
        assert review["date_from"] == review["now"] - timedelta(hours=25)
        # The query actually sent carries that exact instant (epoch seconds), not a calendar day.
        assert review["sent_after"] == review["date_from"]
    # The 25 h window spans the 25 h day exactly: the second review starts where the first one read up to.
    assert second["sent_after"] == first["now"]
    # The repeated hour lies wholly inside the second window.
    assert second["sent_after"] <= fall - timedelta(hours=1) and fall + timedelta(hours=1) <= second_now
    # The union of the two windows is contiguous and covers the whole local day before the transition.
    first_local_midnight = datetime.combine(first_now.astimezone(tz).date(), datetime.min.time()).replace(tzinfo=tz)
    assert first["sent_after"] <= first_local_midnight.astimezone(UTC)


@pytest.mark.parametrize("zone", ZONES)
async def test_consecutive_slots_across_the_spring_forward_day_overlap(monkeypatch, zone):
    _, first_now, second_now = _consecutive_slots(zone, "spring")
    assert second_now - first_now == timedelta(hours=23), "the spring-forward day is 23 hours long"

    first = await _review_at(monkeypatch, first_now)
    second = await _review_at(monkeypatch, second_now)

    for review in (first, second):
        assert review["date_to"] is None and review["exact_instants"] is True
        assert review["date_from"] == review["now"] - timedelta(hours=25)
        assert review["sent_after"] == review["date_from"]
    assert second["sent_after"] == first["now"] - timedelta(hours=2), "the exact windows overlap by two hours"
