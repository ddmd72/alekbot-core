"""Cabinet settings write routes (P19): timezone, location, voice languages, the Gmail auto-index and daily-review
schedules, the deep-research switch, reminders, and the session cap on the Cabinet API.

The user record, the notes and the whole Cabinet run on stores that evaluate; every write is read back from the stored
document of the token's user, never of another, and a refused request leaves the stored document unchanged.
"""
from datetime import datetime, timedelta, timezone

import jwt.api_jwt
import pytest

import src.services.session_service as session_module
from tests.support.clock_instants import pin_datetime
from tests.unit.web.cabinet_routes_world import ALEX, CLARA, CabinetWorld
from tests.unit.web.gate_fakes import expired_token, forged_token

T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)
DAY = 86400


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

    @pytest.mark.xfail(strict=True, raises=AssertionError,
                       reason="P19-D4: JSON `true` passes the `isinstance(hour, int)` check as hour 1 and is stored as a "
                              "bool (user_cabinet_app.py:1336-1337, 1378-1379); the route says 'hour must be integer 0-23'")
    @pytest.mark.parametrize("path", ["/api/gmail/auto-index", "/api/gmail/daily-review"])
    async def test_a_boolean_hour_is_refused(self, world, path):
        """`True` is an `int` in Python: JSON `true` must not be accepted as an hour."""
        before = dict(world.user_doc("usr-1"))
        resp = await world.client.put(path, headers=world.token(ALEX), json={"enabled": True, "hour": True})
        assert resp.status_code == 400
        assert world.user_doc("usr-1") == before

    @pytest.mark.xfail(strict=True, raises=AssertionError,
                       reason="P19-D3: a request body that is not JSON answers 500 on every Cabinet write route "
                              "(get_json raises BadRequest, caught by `except Exception`, user_cabinet_app.py:537-559); no repo spec: "
                              "HTTP convention (RFC 9110 section 15.5.1), a malformed request body is a client error (400)")
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


# --------------------------------------------------------------------------- the 24 h cap on the Cabinet API

def _pin(monkeypatch, instant: datetime) -> None:
    pin_datetime(monkeypatch, session_module, instant)
    pin_datetime(monkeypatch, jwt.api_jwt, instant)


class TestTheCabinetApiHonoursTheSessionCap:
    async def test_a_session_token_writes_until_24_h_and_is_refused_after_with_nothing_written(self, monkeypatch, world):
        _pin(monkeypatch, T0)
        headers = world.token(ALEX)
        _pin(monkeypatch, T0 + timedelta(seconds=DAY - 1))
        assert (await world.client.put("/api/user/location", headers=headers, json={"location": "Springfield"})).status_code == 200
        _pin(monkeypatch, T0 + timedelta(seconds=DAY + 1))
        resp = await world.client.put("/api/user/location", headers=headers, json={"location": "Porto"})
        assert resp.status_code == 401
        assert _config(world)["location"] == "Springfield"

    async def test_the_cookie_session_is_capped_the_same_way(self, monkeypatch, world):
        _pin(monkeypatch, T0)
        world.client.set_cookie("localhost", "access_token", world.token(ALEX)["Authorization"][7:])
        _pin(monkeypatch, T0 + timedelta(seconds=DAY - 1))
        assert (await world.client.put("/api/user/location", json={"location": "Springfield"})).status_code == 200
        _pin(monkeypatch, T0 + timedelta(seconds=DAY + 1))
        resp = await world.client.put("/api/user/location", json={"location": "Porto"})
        assert resp.status_code == 401
        assert _config(world)["location"] == "Springfield"

    @pytest.mark.parametrize("token", [expired_token(), forged_token()], ids=["expired", "forged"])
    async def test_an_expired_or_forged_token_writes_nothing(self, world, token):
        resp = await world.client.put("/api/user/location", headers={"Authorization": f"Bearer {token}"}, json={"location": "Porto"})
        assert resp.status_code == 401
        assert _config(world)["location"] is None
