"""Cabinet settings write routes (P19): timezone, location, voice languages, the Gmail auto-index and daily-review
schedules, the deep-research switch, reminders, and the access-token expiry on the Cabinet API.

The user record, the notes and the whole Cabinet run on stores that evaluate; every write is read back from the stored
document of the token's user, never of another, and a refused request leaves the stored document unchanged.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt.api_jwt
import pytest

from src.adapters.prompt_v3.firestore_agent_profile_repository import FirestoreAgentProfileRepository
from src.domain.device_session_policy import DeviceSessionPolicy
from src.domain.language import LanguageCode
from src.services.language_preference_service import LanguagePreferenceService
from src.services.language_resolver import LanguageResolver
from src.web.cabinet_auth import ACCESS_COOKIE
from tests.support.clock_instants import pin_datetime
from tests.support.redelivery_world import Rendezvous, require
from tests.unit.web.cabinet_routes_world import ALEX, CLARA, CabinetWorld, race
from tests.unit.web.gate_fakes import expired_token, forged_token

T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)
ACCESS = int(DeviceSessionPolicy.ACCESS_TTL.total_seconds())


@pytest.fixture
async def world():
    w = CabinetWorld()
    await w.seed(ALEX)
    await w.seed(CLARA)
    return w


def _config(w: CabinetWorld, user_id: str = "usr-1") -> dict:
    return w.user_doc(user_id)["config"]


# (path, a valid body, the stored config key, the stored value, the key of the GET response that carries it)
_SETTINGS = [
    ("/api/user/timezone", {"timezone": "Europe/Lisbon"}, "timezone", "Europe/Lisbon", "timezone"),
    ("/api/user/location", {"location": "  Springfield "}, "location", "Springfield", "location"),
    ("/api/user/voice-languages", {"voice_languages": ["pt", "en"]}, "voice_languages", ["pt", "en"], "voice_languages"),
    ("/api/gmail/auto-index", {"enabled": True, "hour": 6}, "gmail_auto_index", True, "enabled"),
    ("/api/gmail/daily-review", {"enabled": True, "hour": 21}, "gmail_daily_review", True, "enabled"),
    ("/api/user/deep-research", {"second_pass": True}, "deep_research_second_pass", True, "second_pass"),
]
_IDS = [s[0].rsplit("/", 1)[1] for s in _SETTINGS]


class TestEverySettingIsStoredOnTheCallersUserOnly:
    @pytest.mark.parametrize("path, body, key, value, shown", _SETTINGS, ids=_IDS)
    async def test_the_write_lands_on_the_token_user_and_reads_back(self, world, path, body, key, value, shown):
        other_before = dict(_config(world, "usr-2"))
        resp = await world.client.put(path, headers=world.token(ALEX), json=body)
        assert resp.status_code == 200
        assert _config(world)[key] == value
        assert _config(world, "usr-2") == other_before
        read = await world.client.get(path, headers=world.token(ALEX))
        assert read.status_code == 200
        assert (await read.get_json())[shown] == value
        assert _config(world, "usr-2") == other_before

    @pytest.mark.parametrize("path, body, key, value, shown", _SETTINGS, ids=_IDS)
    async def test_a_token_of_a_user_with_no_record_is_404_and_creates_none(self, world, path, body, key, value, shown):
        ghost = ("usr-9", "acc-9", "ghost@example.com")
        resp = await world.client.put(path, headers=world.token(ghost), json=body)
        assert resp.status_code == 404
        assert "usr-9" not in world.db.collection("users").docs


class TestMalformedValuesAreRefusedAndNothingIsWritten:
    @pytest.mark.parametrize("path, body", [
        ("/api/user/timezone", {}),
        ("/api/user/timezone", {"timezone": "  "}),
        ("/api/user/timezone", {"timezone": "Mars/Olympus"}),
        ("/api/user/voice-languages", {"voice_languages": "pt"}),
        ("/api/user/voice-languages", {"voice_languages": [42]}),
        ("/api/gmail/auto-index", {"hour": 6}),
        ("/api/gmail/auto-index", {"enabled": "yes", "hour": 6}),
        ("/api/gmail/auto-index", {"enabled": True}),
        ("/api/gmail/auto-index", {"enabled": True, "hour": 24}),
        ("/api/gmail/auto-index", {"enabled": True, "hour": -1}),
        ("/api/gmail/auto-index", {"enabled": True, "hour": "6"}),
        ("/api/gmail/daily-review", {"enabled": True, "hour": 24}),
        ("/api/gmail/daily-review", {"enabled": 1, "hour": 5}),
        ("/api/user/deep-research", {}),
        ("/api/user/deep-research", {"second_pass": "true"}),
    ])
    async def test_it_is_400_and_the_stored_user_is_unchanged(self, world, path, body):
        before = dict(world.user_doc("usr-1"))
        resp = await world.client.put(path, headers=world.token(ALEX), json=body)
        assert resp.status_code == 400
        assert world.user_doc("usr-1") == before

    @pytest.mark.parametrize("path", ["/api/gmail/auto-index", "/api/gmail/daily-review"])
    async def test_a_boolean_hour_is_refused(self, world, path):
        """`True` is an `int` in Python: JSON `true` must not be accepted as an hour."""
        before = dict(world.user_doc("usr-1"))
        resp = await world.client.put(path, headers=world.token(ALEX), json={"enabled": True, "hour": True})
        assert resp.status_code == 400
        assert world.user_doc("usr-1") == before

    @pytest.mark.parametrize("path", ["/api/user/timezone", "/api/gmail/auto-index", "/api/user/deep-research"])
    async def test_a_body_that_is_not_json_is_a_4xx(self, world, path):
        resp = await world.client.put(path, headers={**world.token(ALEX), "Content-Type": "application/json"}, data="{nope")
        assert 400 <= resp.status_code < 500


class TestSettingsReads:
    async def test_defaults_are_read_back_for_a_fresh_user(self, world):
        h = world.token(ALEX)
        assert (await (await world.client.get("/api/user/timezone", headers=h)).get_json()) == {"timezone": "UTC"}
        assert (await (await world.client.get("/api/user/location", headers=h)).get_json()) == {"location": ""}
        assert (await (await world.client.get("/api/user/voice-languages", headers=h)).get_json()) == {"voice_languages": []}
        assert (await (await world.client.get("/api/user/deep-research", headers=h)).get_json()) == {"second_pass": False}

    async def test_an_empty_location_and_empty_voice_languages_clear_the_stored_values(self, world):
        h = world.token(ALEX)
        await world.client.put("/api/user/location", headers=h, json={"location": "Springfield"})
        await world.client.put("/api/user/voice-languages", headers=h, json={"voice_languages": ["en"]})
        await world.client.put("/api/user/location", headers=h, json={"location": ""})
        await world.client.put("/api/user/voice-languages", headers=h, json={"voice_languages": []})
        assert _config(world)["location"] is None and _config(world)["voice_languages"] is None

    async def test_the_schedule_reads_return_the_stored_values_of_the_caller(self, world):
        await world.client.put("/api/gmail/auto-index", headers=world.token(CLARA), json={"enabled": True, "hour": 3})
        read = await world.client.get("/api/gmail/auto-index", headers=world.token(ALEX))
        assert await read.get_json() == {"enabled": False, "hour": 8}


class TestASaveKeepsAConcurrentChangeOfAnotherField:
    """Each save writes only the config fields it changes (`update_user_fields`): when another writer read the same
    profile before either wrote, both changes are kept. A `Rendezvous` right after each writer's profile read forces
    that interleaving (a read never yields on `EvalDb`); a serial run fails `rv.met`."""

    async def test_a_timezone_save_during_a_chat_language_switch_keeps_the_language(self, world):
        """The save reads first; the switch reads and writes the language while the save waits, then the save writes:
        the order in which a whole-profile write reverted the switch."""
        language = LanguagePreferenceService(
            world.users, FirestoreAgentProfileRepository(world.db, "profiles", "overrides"),
            SimpleNamespace(invalidate_cache=lambda: None), LanguageResolver(world.users, world.accounts))
        rv = Rendezvous()
        rv.wrap(world.users, "get_user")
        h = world.token(ALEX)

        async def switch_after_the_save_read():
            await rv.reached.wait()
            await language.set_preference("usr-1", LanguageCode.EN, agent_mirror=False)

        resp, _ = await race(
            rv,
            lambda: world.client.put("/api/user/timezone", headers=h, json={"timezone": "Europe/Lisbon"}),
            switch_after_the_save_read)
        require(rv.met and not rv.stalled, "both writers read the profile before either wrote it")
        assert resp.status_code == 200
        config = _config(world)
        assert (config["timezone"], config["preferred_language"], config["agent_mirror"]) == ("Europe/Lisbon", "en", False)

    async def test_two_cabinet_saves_at_once_keep_both(self, world):
        rv = Rendezvous()
        rv.wrap(world.users, "get_user")
        h = world.token(ALEX)
        tz, review = await race(
            rv,
            lambda: world.client.put("/api/user/timezone", headers=h, json={"timezone": "Europe/Lisbon"}),
            lambda: world.client.put("/api/gmail/daily-review", headers=h, json={"enabled": True, "hour": 21}))
        require(rv.met and not rv.stalled, "both saves read the profile before either wrote it")
        assert (tz.status_code, review.status_code) == (200, 200)
        config = _config(world)
        assert (config["timezone"], config["gmail_daily_review"], config["gmail_daily_review_hour"]) == (
            "Europe/Lisbon", True, 21)


# --------------------------------------------------------------------------- reminders

class TestReminderCreate:
    _body = {"text": "call the dentist", "instruction": "remind Alex to call the dentist", "due": "2026-12-01T09:30:00+00:00"}

    async def test_the_note_is_stored_for_the_token_user_with_the_due_instant_in_utc(self, world):
        resp = await world.client.post("/api/user/reminders", headers=world.token(ALEX), json=self._body)
        assert resp.status_code == 201
        (note,) = world.db.collection("notes").docs.values()
        assert note["user_id"] == "usr-1" and note["text"] == "call the dentist"
        assert note["due"] == datetime(2026, 12, 1, 9, 30, tzinfo=timezone.utc)
        listed = await world.client.get("/api/user/reminders", headers=world.token(CLARA))
        assert (await listed.get_json())["reminders"] == []

    @pytest.mark.parametrize("drop", ["text", "instruction", "due"])
    async def test_a_missing_field_is_400_and_nothing_is_stored(self, world, drop):
        body = {k: v for k, v in self._body.items() if k != drop}
        resp = await world.client.post("/api/user/reminders", headers=world.token(ALEX), json=body)
        assert resp.status_code == 400
        assert world.db.collection("notes").docs == {}

    async def test_an_unparseable_due_is_400_and_nothing_is_stored(self, world):
        resp = await world.client.post("/api/user/reminders", headers=world.token(ALEX), json={**self._body, "due": "next tuesday"})
        assert resp.status_code == 400
        assert world.db.collection("notes").docs == {}

    async def test_a_recurrence_without_a_recurrence_service_is_400_and_nothing_is_stored(self, world):
        resp = await world.client.post("/api/user/reminders", headers=world.token(ALEX),
                                       json={**self._body, "recurrence": "every day"})
        assert resp.status_code == 400
        assert world.db.collection("notes").docs == {}


# --------------------------------------------------------------------------- the access-token expiry on the Cabinet API

def _pin(monkeypatch, world: CabinetWorld, instant: datetime) -> None:
    """The instant of the JWT expiry check and of the device sessions' own clock."""
    pin_datetime(monkeypatch, jwt.api_jwt, instant)
    world.kit.clock.set(instant)


class TestTheCabinetApiHonoursTheAccessTokenExpiry:
    async def test_an_access_token_writes_until_its_expiry_and_is_refused_after_with_nothing_written(self, monkeypatch, world):
        _pin(monkeypatch, world, T0)
        headers = world.token(ALEX)
        _pin(monkeypatch, world, T0 + timedelta(seconds=ACCESS - 1))
        assert (await world.client.put("/api/user/location", headers=headers, json={"location": "Springfield"})).status_code == 200
        _pin(monkeypatch, world, T0 + timedelta(seconds=ACCESS + 1))
        resp = await world.client.put("/api/user/location", headers=headers, json={"location": "Porto"})
        assert resp.status_code == 401
        assert _config(world)["location"] == "Springfield"

    async def test_the_access_cookie_expires_the_same_way(self, monkeypatch, world):
        _pin(monkeypatch, world, T0)
        world.client.set_cookie("localhost", ACCESS_COOKIE, world.token(ALEX)["Authorization"][7:])
        _pin(monkeypatch, world, T0 + timedelta(seconds=ACCESS - 1))
        assert (await world.client.put("/api/user/location", json={"location": "Springfield"})).status_code == 200
        _pin(monkeypatch, world, T0 + timedelta(seconds=ACCESS + 1))
        resp = await world.client.put("/api/user/location", json={"location": "Porto"})
        assert resp.status_code == 401
        assert _config(world)["location"] == "Springfield"

    @pytest.mark.parametrize("token", [expired_token(), forged_token()], ids=["expired", "forged"])
    async def test_an_expired_or_forged_token_writes_nothing(self, world, token):
        resp = await world.client.put("/api/user/location", headers={"Authorization": f"Bearer {token}"}, json={"location": "Porto"})
        assert resp.status_code == 401
        assert _config(world)["location"] is None
