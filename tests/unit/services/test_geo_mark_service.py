"""GeoMarkService (GEO_MARKS_RFC §4.4, §4.5) over the real link store and session store on the evaluating Firestore fake,
the real task queue over the Cloud Tasks client fake, and the real localization: what ends up stored, posted and
queued."""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.domain.geo_mark import LINK_DIGEST_PURPOSE, LINK_TTL_S, GeoFix, GeoLinkBinding, GeoMark, GeoPlace, GeoSource
from src.domain.secret_digest import fingerprint, keyed_digest
from src.domain.ui_messages import UIMessage
from src.locales import en, uk
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.reverse_geocoding_port import ReverseGeocodingPort
from src.services.geo_mark_service import (
    PAGE_MESSAGES, TURN_DEADLINE_S, GeoMarkService, GeoRedeemStatus,
)
from tests.support.fact_review_world import ReviewSessionStore
from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.support.recording_task_queue import make_task_queue

DIGEST_KEY = b"g" * 32
BASE_URL = "https://app.example/"
LINKS = "t_geo_mark_links"
NOW = datetime(2026, 11, 18, 18, 40, tzinfo=timezone.utc)
ADDRESS = "Main Street, Springfield"
SESSION = "usr-1:C1"
QUESTION = "Is the Iron Temple gym open?"


def _binding(text: str = "") -> GeoLinkBinding:
    return GeoLinkBinding(user_id="usr-1", account_id="acc-1", platform="slack", channel_id="C1",
                          session_id=SESSION, platform_user_id="U1", language="en", text=text)


def _fix(accuracy: Optional[float] = 5.0, source: GeoSource = GeoSource.SLACK_LINK) -> GeoFix:
    return GeoFix.parse(41.1, -8.6, accuracy, NOW, source)


class Geocoder(ReverseGeocodingPort):
    def __init__(self, address: Optional[str] = ADDRESS, raises: bool = False) -> None:
        self.address, self.raises = address, raises
        self.asked: List[Tuple[float, float]] = []

    async def look_up(self, latitude: float, longitude: float) -> GeoPlace:
        self.asked.append((latitude, longitude))
        if self.raises:
            raise RuntimeError("broken adapter")
        return GeoPlace(self.address, (), ())


class Channel:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.texts: List[str] = []

    async def send_message(self, text: str, thread_id: Optional[str] = None, link_list=None) -> None:
        if self.fail:
            raise RuntimeError("slack down")
        self.texts.append(text)


class Channels(NotificationChannelFactoryPort):
    def __init__(self) -> None:
        self.channels: Dict[Tuple[str, str], Channel] = {}
        self.unconfigured = False
        self.fail = False

    def create(self, platform: str, channel_id: str):  # type: ignore[override]
        if self.unconfigured:
            return None
        return self.channels.setdefault((platform, channel_id), Channel(self.fail))


class Users:
    def __init__(self, zone: Optional[str] = "UTC", raises: bool = False) -> None:
        self.zone, self.raises = zone, raises

    async def get_user(self, user_id: str):
        if self.raises:
            raise RuntimeError("firestore blinked")
        return None if self.zone is None else SimpleNamespace(config=SimpleNamespace(timezone=self.zone))


class World:
    def __init__(self, geocoder: Optional[ReverseGeocodingPort] = None, users: Optional[Users] = None) -> None:
        self.db = EvalDb()
        self.links = FirestoreEphemeralStore(self.db, LINKS)
        self.sessions = ReviewSessionStore(self.db, "t_")
        self.channels = Channels()
        self.queue, self.tasks = make_task_queue()
        self.geocoder = geocoder if geocoder is not None else Geocoder()
        self.users = users or Users()
        self.service = GeoMarkService(
            links=self.links, geocoder=self.geocoder, sessions=self.sessions, channels=self.channels,
            task_queue=self.queue, users=self.users, localization=FileLocalizationAdapter(), digest_key=DIGEST_KEY,
            base_url=BASE_URL, clock=lambda: NOW)

    def docs(self) -> Dict[str, dict]:
        return self.db.collection(LINKS).docs

    def posted(self) -> List[str]:
        channel = self.channels.channels.get(("slack", "C1"))
        return channel.texts if channel else []

    async def history(self) -> List[Tuple[str, List[str]]]:
        return [(m.role, [p.text for p in m.parts]) for m in await self.sessions.messages(SESSION)]

    async def issue(self, text: str = "") -> str:
        url = await self.service.issue_link(_binding(text))
        return url.rsplit("/", 1)[1]


def _key(raw_id: str) -> str:
    return "geo_link:" + keyed_digest(DIGEST_KEY, LINK_DIGEST_PURPOSE, raw_id)


def _confirmation(address: str = ADDRESS) -> str:
    return en.UI_STRINGS[UIMessage.GEO_SAVED.value].format(address=address, accuracy="±5 m", time="18:40")


def _no_position_or_address(caplog) -> None:
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "41.1" not in text and "-8.6" not in text and ADDRESS not in text and QUESTION not in text


class TestIssueAndPeek:

    async def test_the_link_is_stored_under_its_keyed_digest_and_the_raw_id_is_nowhere(self):
        w = World()

        url = await w.service.issue_link(_binding(QUESTION))

        raw_id = url.removeprefix("https://app.example/geo/")
        assert len(raw_id) == 22 and url == f"https://app.example/geo/{raw_id}"
        assert list(w.docs()) == [_key(raw_id)]
        stored = w.docs()[_key(raw_id)]
        assert stored["value"] == _binding(QUESTION).to_record()
        assert raw_id not in repr(stored)

    async def test_the_link_lives_ten_minutes(self):
        w = World()
        before = datetime.now(timezone.utc)

        raw_id = await w.issue()

        expires = w.docs()[_key(raw_id)]["expires_at"]
        assert LINK_TTL_S == 600
        assert 600 <= (expires - before).total_seconds() <= 605

    async def test_two_links_are_two_records(self):
        w = World()
        assert await w.issue() != await w.issue()
        assert len(w.docs()) == 2

    async def test_peek_reads_without_consuming(self):
        w = World()
        raw_id = await w.issue(QUESTION)

        assert await w.service.peek_link(raw_id) == _binding(QUESTION)
        assert await w.service.peek_link(raw_id) == _binding(QUESTION)
        assert _key(raw_id) in w.docs()

    @pytest.mark.parametrize("raw_id", ["short", "A" * 23, "A" * 21 + "/", ""])
    async def test_a_wrong_shaped_id_reads_no_store(self, raw_id, monkeypatch):
        w = World()

        async def tripwire(key):
            raise AssertionError("the store was read")

        monkeypatch.setattr(w.links, "get", tripwire)
        monkeypatch.setattr(w.links, "get_and_delete", tripwire)
        assert await w.service.peek_link(raw_id) is None
        assert (await w.service.redeem_link(raw_id, _fix())).status is GeoRedeemStatus.GONE

    async def test_an_unknown_link_peeks_none(self):
        assert await World().service.peek_link("A" * 22) is None

    async def test_a_malformed_record_peeks_none_and_redeems_gone(self, caplog):
        w = World()
        raw_id = "B" * 22
        await w.links.set(_key(raw_id), {"user_id": "usr-1"}, ttl_s=60)

        assert await w.service.peek_link(raw_id) is None
        assert (await w.service.redeem_link(raw_id, _fix())).status is GeoRedeemStatus.GONE
        assert any("malformed link record" in r.getMessage() for r in caplog.records)


class TestRedeemWithoutText:

    async def test_one_pair_lands_on_the_bound_session_and_nothing_is_queued(self):
        w = World()
        raw_id = await w.issue()

        result = await w.service.redeem_link(raw_id, _fix())

        assert result.status is GeoRedeemStatus.SAVED
        assert result.mark == GeoMark(_fix(), ADDRESS)
        [(role_u, [mark_text]), (role_m, [line])] = await w.history()
        assert (role_u, role_m) == ("user", "model")
        assert mark_text.startswith("[Geo mark:") and ADDRESS in mark_text and "41.10000, -8.60000" in mark_text
        assert line == _confirmation()
        assert w.posted() == [_confirmation()]
        assert w.tasks.worker_tasks() == []
        assert w.docs() == {}

    async def test_the_link_is_single_use(self):
        w = World()
        raw_id = await w.issue()

        assert (await w.service.redeem_link(raw_id, _fix())).status is GeoRedeemStatus.SAVED
        assert (await w.service.redeem_link(raw_id, _fix())).status is GeoRedeemStatus.GONE
        assert len(await w.history()) == 2

    async def test_of_two_concurrent_redeems_one_wins(self, monkeypatch):
        w = World()
        raw_id = await w.issue()
        w.db.collection(LINKS).yield_on_get = True         # both read the record before either commits
        take, entered, both_in = w.links.get_and_delete, [], asyncio.Event()

        async def rendezvous(key):
            entered.append(key)
            if len(entered) == 2:
                both_in.set()
            await asyncio.wait_for(both_in.wait(), 2)
            return await take(key)

        monkeypatch.setattr(w.links, "get_and_delete", rendezvous)
        with eval_transactions():
            results = await asyncio.gather(w.service.redeem_link(raw_id, _fix()), w.service.redeem_link(raw_id, _fix()))

        assert both_in.is_set()                            # the two takes overlapped
        assert sorted(r.status.value for r in results) == ["gone", "saved"]
        assert len(await w.history()) == 2 and len(w.posted()) == 1

    async def test_a_failed_post_still_saves_the_pair(self, caplog):
        w = World()
        w.channels.fail = True
        raw_id = await w.issue()

        assert (await w.service.redeem_link(raw_id, _fix())).status is GeoRedeemStatus.SAVED
        assert [role for role, _ in await w.history()] == ["user", "model"]
        assert any("confirmation post failed" in r.getMessage() for r in caplog.records)

    async def test_no_channel_for_the_platform_still_saves_the_pair(self, caplog):
        w = World()
        w.channels.unconfigured = True
        raw_id = await w.issue()

        assert (await w.service.redeem_link(raw_id, _fix())).status is GeoRedeemStatus.SAVED
        assert len(await w.history()) == 2
        assert any("no slack channel" in r.getMessage() for r in caplog.records)

    async def test_a_failed_append_is_not_saved_and_the_link_is_gone(self):
        w = World()
        w.sessions.fail = True
        raw_id = await w.issue()

        result = await w.service.redeem_link(raw_id, _fix())

        assert result.status is GeoRedeemStatus.NOT_SAVED and result.mark is not None
        assert w.docs() == {}


class TestRedeemWithText:

    async def test_one_turn_is_queued_with_the_frozen_payload_and_nothing_is_appended(self):
        w = World()
        raw_id = await w.issue(QUESTION)
        digest = keyed_digest(DIGEST_KEY, LINK_DIGEST_PURPOSE, raw_id)

        result = await w.service.redeem_link(raw_id, _fix())

        assert result.status is GeoRedeemStatus.TURN_QUEUED
        [(task_type, body)] = w.tasks.worker_tasks()
        assert task_type == "geo_mark_turn"
        mark_text = body.pop("mark_text")
        assert body == {"task_type": "geo_mark_turn", "platform": "slack", "user_id": "usr-1", "account_id": "acc-1",
                        "channel_id": "C1", "session_id": SESSION, "platform_user_id": "U1", "text": QUESTION,
                        "language": "en", "link_fp": fingerprint(digest)}
        assert mark_text.startswith("[Geo mark:") and QUESTION not in mark_text
        [request] = w.tasks.create_task_requests
        assert request["task"]["dispatch_deadline"].seconds == TURN_DEADLINE_S == 1800
        assert request["task"]["name"].endswith(f"geo_mark_turn-{hashlib.sha256(digest.encode()).hexdigest()[:32]}")
        assert await w.history() == []
        assert w.posted() == [_confirmation()]

    async def test_a_failed_enqueue_keeps_the_mark_as_the_pair(self, monkeypatch):
        w = World()
        raw_id = await w.issue(QUESTION)

        def refuse(request, **kwargs):
            raise RuntimeError("queue down")

        monkeypatch.setattr(w.tasks, "create_task", refuse)

        result = await w.service.redeem_link(raw_id, _fix())

        assert result.status is GeoRedeemStatus.QUESTION_NOT_SENT
        [(_, [mark_text]), (_, [line])] = await w.history()
        assert mark_text.startswith("[Geo mark:") and QUESTION not in mark_text and line == _confirmation()

    async def test_a_failed_enqueue_and_a_failed_append_are_not_saved(self, monkeypatch):
        w = World()
        w.sessions.fail = True
        raw_id = await w.issue(QUESTION)
        monkeypatch.setattr(w.tasks, "create_task", lambda request, **kw: (_ for _ in ()).throw(RuntimeError("down")))

        assert (await w.service.redeem_link(raw_id, _fix())).status is GeoRedeemStatus.NOT_SAVED

    async def test_the_mark_is_in_the_owners_language_and_zone(self):
        w = World(users=Users("Europe/Berlin"))
        binding = GeoLinkBinding(**{**_binding(QUESTION).to_record(), "language": "uk"})
        raw_id = (await w.service.issue_link(binding)).rsplit("/", 1)[1]

        await w.service.redeem_link(raw_id, _fix())

        [(_, body)] = w.tasks.worker_tasks()
        assert "2026-11-18 19:40 (UTC+01:00)" in body["mark_text"]
        assert w.geocoder.asked == [(41.1, -8.6)]      # names come in English (§4.3); the line is in uk
        assert w.posted() == [uk.UI_STRINGS[UIMessage.GEO_SAVED.value].format(
            address=ADDRESS, accuracy="±5 m", time="19:40")]


class TestRecord:

    async def test_record_posts_appends_the_pair_and_returns_the_line(self):
        w = World()
        channel = Channel()
        mark = await w.service.locate(_fix(source=GeoSource.TELEGRAM_PIN))

        line = await w.service.record(mark, user_id="usr-1", session_id=SESSION, channel=channel, language="en")

        assert line == _confirmation() and channel.texts == [line]
        [(_, [mark_text]), (_, [model])] = await w.history()
        assert "as a Telegram location" in mark_text and model == line

    async def test_a_failed_post_still_appends(self):
        w = World()
        mark = await w.service.locate(_fix())
        await w.service.record(mark, user_id="usr-1", session_id=SESSION, channel=Channel(fail=True), language="en")
        assert len(await w.history()) == 2

    async def test_a_failed_append_raises(self, caplog):
        w = World()
        w.sessions.fail = True
        mark = await w.service.locate(_fix())
        with pytest.raises(Exception):
            await w.service.record(mark, user_id="usr-1", session_id=SESSION, channel=Channel(), language="en")
        assert any("history append failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("users", [Users(raises=True), Users(zone=None), Users(zone="Mars/Olympus")],
                             ids=["unreadable", "no-user", "unknown-zone"])
    async def test_an_unreadable_time_zone_is_utc(self, users):
        w = World(users=users)
        mark = await w.service.locate(_fix())

        line = await w.service.record(mark, user_id="usr-1", session_id=SESSION, channel=Channel(), language="en")

        assert line.endswith("18:40)")
        [(_, [mark_text]), _] = await w.history()
        assert "2026-11-18 18:40 (UTC+00:00)" in mark_text

    async def test_without_an_address_the_line_says_so(self):
        w = World(geocoder=Geocoder(address=None))
        mark = await w.service.locate(_fix(accuracy=None, source=GeoSource.TELEGRAM_PIN))

        line = await w.service.record(mark, user_id="usr-1", session_id=SESSION, channel=Channel(), language="en")

        assert line == en.UI_STRINGS[UIMessage.GEO_SAVED_NO_ADDRESS.value].format(accuracy="±?", time="18:40")


class TestLocate:

    async def test_no_geocoder_gives_no_address(self):
        w = World()
        service = GeoMarkService(
            links=w.links, geocoder=None, sessions=w.sessions, channels=w.channels, task_queue=w.queue,
            users=w.users, localization=FileLocalizationAdapter(), digest_key=DIGEST_KEY, base_url=BASE_URL)
        assert await service.locate(_fix()) == GeoMark(_fix(), None)

    async def test_a_raising_geocoder_gives_no_address(self):
        assert (await World(geocoder=Geocoder(raises=True)).service.locate(_fix())).address is None


class TestPageStrings:

    @pytest.mark.parametrize("language,module", [("en", en), ("uk", uk), ("xx", en)])
    def test_every_page_string_in_the_language(self, language, module):
        strings = World().service.page_strings(language)
        assert set(strings) == {m.value for m in PAGE_MESSAGES}
        assert {m.value for m in PAGE_MESSAGES} == {m.value for m in UIMessage if m.name.startswith("GEO_PAGE_")}
        assert all(strings[k] == module.UI_STRINGS[k] for k in strings)


async def test_no_log_line_holds_the_position_the_address_or_the_question(caplog):
    caplog.set_level(logging.DEBUG)
    w = World()
    for text in ("", QUESTION):
        raw_id = await w.issue(text)
        await w.service.peek_link(raw_id)
        await w.service.redeem_link(raw_id, _fix())
        await w.service.redeem_link(raw_id, _fix())
    mark = await w.service.locate(_fix())
    await w.service.record(mark, user_id="usr-1", session_id=SESSION, channel=Channel(fail=True), language="en")

    assert any("[GeoMark]" in r.getMessage() for r in caplog.records)
    _no_position_or_address(caplog)
