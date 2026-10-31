"""The owner's drive scripts act on one connection from slice 1b (MULTI_DRIVE_RFC §4.4, §10, §12): the `--connection`
argument and its default (the user's one live connection, never a guess among several); the transcription switch
moved to the connection (drive_transcribe_flag.py); the initial build reset by the connection's provider account
(drive_initial_build_reset.py). In-memory repository and the evaluating Firestore fake only."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_usage import initial_build_doc_id
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.firestore_eval import EvalDb

_DIR = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
CREDS = "development_oauth_credentials"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conn(cid, tag, *, user="usr-1", cred="cred-1", state=ConnectionState.ACTIVE, transcribe=False, name="Drive"):
    return DriveConnection(connection_id=cid, user_id=user, account_id="acc-1", credential_id=cred,
                           kind="microsoft_onedrive", drive_identity=f"microsoft_onedrive:drv-{cid}", ref_tag=tag,
                           mount_name=name, transcribe_enabled=transcribe, state=state, created_at=NOW,
                           disconnected_at=NOW if state is ConnectionState.DISCONNECTED else None)


async def _repo(*conns):
    repo = FakeDriveConnectionRepository()
    for conn in conns:
        await repo.create(conn)
    return repo


# -- --connection and its default -------------------------------------------------------------------------------

async def test_the_one_live_connection_is_the_default():
    arg = _load("drive_connection_arg")
    repo = await _repo(_conn("conn-1", "a1"), _conn("conn-0", "b2", state=ConnectionState.DISCONNECTED))
    conn, why = await arg.resolve_connection(repo, "usr-1", None)
    assert conn.connection_id == "conn-1" and why == ""


async def test_several_live_connections_are_refused_with_their_ids():
    arg = _load("drive_connection_arg")
    repo = await _repo(_conn("conn-1", "a1"), _conn("conn-2", "b2", state=ConnectionState.NEEDS_RECONNECT,
                                                    name="Drive 2"))
    conn, why = await arg.resolve_connection(repo, "usr-1", None)
    assert conn is None and "conn-1 (Drive)" in why and "conn-2 (Drive 2)" in why and "--connection" in why
    conn, _ = await arg.resolve_connection(repo, "usr-1", "conn-2")
    assert conn.connection_id == "conn-2"


@pytest.mark.parametrize("connection_id, fragment", [(None, "no live drive connection"),
                                                     ("conn-0", "is disconnected"), ("conn-9", "no connection")])
async def test_no_live_connection_a_disconnected_one_or_an_unknown_one_is_refused(connection_id, fragment):
    arg = _load("drive_connection_arg")
    repo = await _repo(_conn("conn-0", "b2", state=ConnectionState.DISCONNECTED))
    conn, why = await arg.resolve_connection(repo, "usr-1", connection_id)
    assert conn is None and fragment in why


async def test_another_users_connection_is_never_found():
    arg = _load("drive_connection_arg")
    repo = await _repo(_conn("conn-1", "a1", user="usr-2"))
    conn, why = await arg.resolve_connection(repo, "usr-1", "conn-1")
    assert conn is None and "no connection" in why


# -- the transcription switch on the connection ------------------------------------------------------------------

async def test_flag_dry_run_writes_nothing_and_says_before_and_after():
    flag = _load("drive_transcribe_flag")
    repo = await _repo(_conn("conn-1", "a1"))
    code, message = await flag.set_flag(repo, "usr-1", enabled=True, apply=False)
    assert code == 0 and (await repo.get("usr-1", "conn-1")).transcribe_enabled is False
    assert "False" in message and "True" in message and "--apply" in message and "conn-1" in message


async def test_flag_apply_sets_the_connections_switch_and_prints_before_and_after():
    flag = _load("drive_transcribe_flag")
    repo = await _repo(_conn("conn-1", "a1"), _conn("conn-2", "b2"))
    code, message = await flag.set_flag(repo, "usr-1", enabled=True, apply=True, connection_id="conn-2")
    assert code == 0 and "before False" in message and "after True" in message
    assert (await repo.get("usr-1", "conn-2")).transcribe_enabled is True
    assert (await repo.get("usr-1", "conn-1")).transcribe_enabled is False      # the other drive is untouched


async def test_flag_off_clears_it_and_already_in_place_writes_nothing():
    flag = _load("drive_transcribe_flag")
    repo = await _repo(_conn("conn-1", "a1", transcribe=True))
    code, message = await flag.set_flag(repo, "usr-1", enabled=True, apply=True)
    assert code == 0 and "already" in message
    code, _ = await flag.set_flag(repo, "usr-1", enabled=False, apply=True)
    assert code == 0 and (await repo.get("usr-1", "conn-1")).transcribe_enabled is False


async def test_flag_without_a_live_connection_is_an_error():
    flag = _load("drive_transcribe_flag")
    code, message = await flag.set_flag(await _repo(), "usr-1", enabled=True, apply=True)
    assert code == 1 and "no live drive connection" in message


async def test_flag_write_that_does_not_stick_fails_loudly():
    flag = _load("drive_transcribe_flag")

    class Deaf(FakeDriveConnectionRepository):
        async def update(self, user_id, connection_id, **fields):
            return await self.get(user_id, connection_id)

    repo = Deaf()
    await repo.create(_conn("conn-1", "a1"))
    code, message = await flag.set_flag(repo, "usr-1", enabled=True, apply=True)
    assert code == 1 and "did not stick" in message


# -- the initial build by the connection's provider account ------------------------------------------------------

async def test_the_build_key_is_the_accounts_record():
    reset = _load("drive_initial_build_reset")
    db = EvalDb()
    db.collection(CREDS).docs["usr-1:cred-1"] = {"user_id": "usr-1", "credential_id": "cred-1", "secret": "sealed",
                                                 "account": {"realm": "microsoft", "subject": "sub-1"}}
    repo = await _repo(_conn("conn-1", "a1"))
    key, why = await reset.build_key(repo, db, CREDS, "usr-1", None)
    assert key == initial_build_doc_id("usr-1", "microsoft", "sub-1") and why == ""


async def test_the_build_key_is_refused_without_the_credential_or_a_connection():
    reset = _load("drive_initial_build_reset")
    db = EvalDb()
    repo = await _repo(_conn("conn-1", "a1"))
    key, why = await reset.build_key(repo, db, CREDS, "usr-1", None)
    assert key is None and "credential is gone" in why
    key, why = await reset.build_key(await _repo(), db, CREDS, "usr-1", None)
    assert key is None and "no live drive connection" in why


async def test_the_firestore_store_reads_and_writes_the_accounts_document():
    reset = _load("drive_initial_build_reset")
    db = EvalDb()
    store = reset.FirestoreBuildStore(db, "development_drive_model_usage", "usr-1")
    key = initial_build_doc_id("usr-1", "microsoft", "sub-1")
    db.collection("development_drive_model_usage").docs[key] = {"user_id": "usr-1", "allowance_usd": 40.0,
                                                                "spent_usd": 40.0, "opened_at": NOW,
                                                                "closed_at": NOW}
    code, message = await reset.reset(store, key, allowance_usd=None, apply=True)
    assert code == 0 and "was closed" in message
    stored = db.collection("development_drive_model_usage").docs[key]
    assert (stored["user_id"], stored["spent_usd"], stored["closed_at"]) == ("usr-1", 0.0, None)
    assert list(db.collection("development_drive_model_usage").docs) == [key]   # no `{user}:initial` written


def test_no_script_reads_the_user_level_flags_any_more():
    for name in ("drive_transcribe_flag", "drive_transcribe_requeue"):
        text = (_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert "config.drive_transcribe_enabled" not in text and "drive_indexing_enabled" not in text, name
