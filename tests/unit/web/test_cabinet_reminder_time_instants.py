"""Cabinet reminder times at DST transitions (P16-B, defect P16-D5).

`create_reminder` / `update_reminder` (`src/web/user_cabinet_app.py:678-766`) read a naive `due` as the user's local
time with `replace(tzinfo=user_tz)` (`:696`, `:742`): a skipped spring time is stored as a real instant (Berlin 02:30 on
2026-03-29 → 01:30Z) instead of being refused; an ambiguous fall time is stored as fold 0, the first pass (current
behaviour, pinned as passing: refusing or asking is an owner choice); a user whose stored zone is unknown makes
`ZoneInfo(...)` raise `ZoneInfoNotFoundError` (`:693`, `:735`), a `KeyError`, which the routes turn into 500, not 400.

Harness as in `tests/unit/web/test_cabinet_owner_and_tenant_refusals.py`: real session tokens, real user and note
repositories and the real recurrence adapter on `EvalDb`; each outcome is read from the stored document.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from quart import Quart

from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.agent_note import NoteCreate
from src.domain.user import UserBotConfig, UserProfile
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.clock_instants import REPEATED_HOUR, SKIPPED_HOUR, ZONES
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import Tripwire, access_token, session_service
from src.composition.connector_registry import ConnectorRegistry

UTC = timezone.utc
USER = ("u1", "a1")
UNKNOWN_ZONE = "Mars/Olympus_Mons"
D5 = "P16-D5: naive Cabinet reminder times — {}; owner decision"
NOTES = "notes"


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.touched: list = []
        env = SimpleNamespace(orchestrator_notes_collection=NOTES, domain_users_collection="users",
                              is_production=False)
        self.notes = FirestoreAgentNoteAdapter(self.db, env)
        self.users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))
        app = Quart("cabinet_reminder_time_instants")
        app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=Tripwire("invite_service", self.touched),
            session_service=session_service(),
            user_repo=self.users,
            fact_repo=Tripwire("fact_repo", self.touched),
            embedding_service=Tripwire("embedding_service", self.touched),
            agent_note_port=self.notes,
            recurrence_port=DateutilRecurrenceAdapter(), connectors=ConnectorRegistry(),
        ))
        self.client = app.test_client()

    async def with_user(self, zone: str) -> "World":
        await self.users.create_user(UserProfile(user_id=USER[0], account_id=USER[1],
                                                 config=UserBotConfig(timezone=zone)))
        return self

    def stored_dues(self) -> list:
        return [doc["due"].astimezone(UTC) for doc in self.db.collection(NOTES).docs.values()]

    async def create(self, due: str):
        return await self.client.post("/api/user/reminders", headers=_bearer(), json={
            "text": "water the ferns", "instruction": "remind to water the ferns", "due": due})

    async def existing_note(self) -> str:
        note = await self.notes.create_note(NoteCreate(user_id=USER[0], text="water the ferns",
                                                       instruction="remind to water the ferns",
                                                       due=datetime.now(UTC) + timedelta(days=1)))
        return note.note_id

    async def update(self, note_id: str, due: str):
        return await self.client.put(f"/api/user/reminders/{note_id}", headers=_bearer(), json={"due": due})


def _bearer() -> dict:
    return {"Authorization": f"Bearer {access_token(*USER)}"}


def _wall(value: datetime) -> str:
    return value.isoformat(timespec="minutes")


# --------------------------------------------------------------------------- skipped spring time


class TestSkippedSpringTime:

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D5.format("a skipped time is stored (:696)"))
    @pytest.mark.parametrize("zone", ZONES)
    async def test_create_refuses_a_skipped_wall_time(self, zone):
        w = await World().with_user(zone)
        resp = await w.create(_wall(SKIPPED_HOUR[zone].naive))
        assert resp.status_code == 400, f"status {resp.status_code}, stored {[f'{d:%m-%dT%H:%MZ}' for d in w.stored_dues()]}"
        assert w.stored_dues() == []

    @pytest.mark.xfail(strict=True, raises=AssertionError, reason=D5.format("a skipped time is stored (:742)"))
    @pytest.mark.parametrize("zone", ZONES)
    async def test_update_refuses_a_skipped_wall_time(self, zone):
        w = await World().with_user(zone)
        note_id = await w.existing_note()
        before = w.stored_dues()
        resp = await w.update(note_id, _wall(SKIPPED_HOUR[zone].naive))
        assert resp.status_code == 400, f"status {resp.status_code}, stored {[f'{d:%m-%dT%H:%MZ}' for d in w.stored_dues()]}"
        assert w.stored_dues() == before


# --------------------------------------------------------------------------- repeated fall time


class TestRepeatedFallTime:

    @pytest.mark.parametrize("zone", ZONES)
    async def test_create_stores_an_ambiguous_wall_time_as_the_first_pass(self, zone):
        w = await World().with_user(zone)
        resp = await w.create(_wall(REPEATED_HOUR[zone].wall))
        assert resp.status_code == 201
        assert w.stored_dues() == [REPEATED_HOUR[zone].fold0.utc]

    @pytest.mark.parametrize("zone", ZONES)
    async def test_update_stores_an_ambiguous_wall_time_as_the_first_pass(self, zone):
        w = await World().with_user(zone)
        note_id = await w.existing_note()
        resp = await w.update(note_id, _wall(REPEATED_HOUR[zone].wall))
        assert resp.status_code == 200
        assert w.stored_dues() == [REPEATED_HOUR[zone].fold0.utc]


# --------------------------------------------------------------------------- unknown stored zone


class TestUnknownStoredZone:

    @pytest.mark.xfail(strict=True, raises=AssertionError,
                       reason=D5.format("an unknown stored zone raises ZoneInfoNotFoundError (:693) → 500"))
    async def test_create_answers_400(self):
        w = await World().with_user(UNKNOWN_ZONE)
        resp = await w.create(_wall(REPEATED_HOUR[ZONES[0]].wall))
        assert w.stored_dues() == []
        assert resp.status_code == 400, f"status {resp.status_code}"

    @pytest.mark.xfail(strict=True, raises=AssertionError,
                       reason=D5.format("an unknown stored zone raises ZoneInfoNotFoundError (:735) → 500"))
    async def test_update_answers_400(self):
        w = await World().with_user(UNKNOWN_ZONE)
        note_id = await w.existing_note()
        before = w.stored_dues()
        resp = await w.update(note_id, _wall(REPEATED_HOUR[ZONES[0]].wall))
        assert w.stored_dues() == before
        assert resp.status_code == 400, f"status {resp.status_code}"
