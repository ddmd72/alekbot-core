"""Invite codes at rest (SECRETS_AT_REST_RFC §2.5, S5): `FirestoreInviteCodeRepository` and the real
`InviteCodeService` over the evaluating Firestore fake. The doc id is the keyed digest of the normalised code; no field
and no doc id holds the code; typed input of another case, spacing or dashes finds the invite; a list carries the hint.
"""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.domain.billing import BillingAccount
from src.domain.invite_code import InviteCode, InviteType, normalise_invite_code
from src.domain.secret_digest import keyed_digest
from src.domain.user import UserProfile
from src.services.invite_code_service import InviteCodeService
from tests.support.firestore_eval import EvalDb

INVITES, USERS, ACCOUNTS, WHITELIST = "eval_invite_codes_v2", "eval_users", "eval_accounts", "eval_whitelist"
DIGEST_KEY = bytes.fromhex("0f1e2d3c4b5a69788796a5b4c3d2e1f000112233445566778899aabbccddeeff")
OWNER, OWNER_ACC = "user-1", "acct-1"
MEMBER, MEMBER_ACC = "user-2", "acct-2"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
CODE = "ABCDE-FGHIJ-KLMNO-PQRST-UVWXYZ"          # synthetic


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        env = SimpleNamespace(domain_invite_codes_collection=INVITES, domain_users_collection=USERS,
                              domain_whitelist_collection=WHITELIST, is_production=False)
        self.repo = FirestoreInviteCodeRepository(self.db, env, digest_key=DIGEST_KEY)
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.users = FirestoreUserRepository(self.db, env, self.accounts)
        self.service = InviteCodeService(self.repo, self.users, self.accounts,
                                         FirestoreWhitelistRepository(self.db, env))

    @property
    def docs(self) -> dict:
        return self.db.collection(INVITES).docs

    async def team(self) -> None:
        await self.accounts.create_account(BillingAccount(account_id=OWNER_ACC, iam_policy={OWNER: "OWNER"}))
        await self.accounts.create_account(BillingAccount(account_id=MEMBER_ACC, iam_policy={MEMBER: "OWNER"}))
        await self.users.update_user(UserProfile(user_id=MEMBER, email="clara@example.com", account_id=MEMBER_ACC))
        self.db.collection(WHITELIST).docs["config"] = {"allowed_emails": ["clara@example.com"], "allowed_domains": []}


@pytest.fixture
def world():
    return World()


def _invite(code=CODE) -> InviteCode:
    return InviteCode(code=code, user_id=OWNER, account_id=OWNER_ACC, type=InviteType.TEAM_INVITE,
                      expires_at=NOW + timedelta(days=7), created_at=NOW)


def _strings(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    else:
        yield str(value)


async def test_doc_id_is_the_keyed_digest_of_the_normalised_code(world):
    await world.repo.create(_invite())
    assert list(world.docs) == [keyed_digest(DIGEST_KEY, "invite", normalise_invite_code(CODE))]


async def test_the_code_is_in_no_stored_field_and_no_doc_id(world):
    await world.repo.create(_invite())
    plain = normalise_invite_code(CODE)
    blobs = [s for doc_id, doc in world.docs.items() for s in (doc_id, *_strings(doc))]
    assert not any(plain in b.upper().replace("-", "") for b in blobs)
    assert "code" not in next(iter(world.docs.values()))
    assert next(iter(world.docs.values()))["hint"] == "WXYZ"


@pytest.mark.parametrize("typed", [CODE, CODE.lower(), CODE.replace("-", " "), CODE.replace("-", ""), f" {CODE}\n"])
async def test_lookup_by_typed_input_finds_the_invite(world, typed):
    await world.repo.create(_invite())
    found = await world.repo.get_by_code(typed)
    assert found is not None and found.account_id == OWNER_ACC and found.hint == "WXYZ"
    assert normalise_invite_code(found.code) == normalise_invite_code(CODE)


async def test_unknown_or_empty_input_finds_nothing(world):
    await world.repo.create(_invite())
    assert await world.repo.get_by_code("AAAAA-AAAAA-AAAAA-AAAAA-AAAAAA") is None
    assert await world.repo.get_by_code("") is None
    assert await world.repo.get_by_code(" - ") is None


async def test_a_different_digest_key_does_not_find_the_invite(world):
    await world.repo.create(_invite())
    other = FirestoreInviteCodeRepository(world.db, SimpleNamespace(domain_invite_codes_collection=INVITES),
                                          digest_key=b"\x01" * 32)
    assert await other.get_by_code(CODE) is None


async def test_list_returns_the_hint_and_never_the_code(world):
    await world.repo.create(_invite())
    listed = await world.repo.list_by_user(OWNER)
    assert len(listed) == 1 and listed[0].code is None and listed[0].hint == "WXYZ"
    assert await world.repo.list_by_user("user-9") == []


async def test_update_rewrites_the_same_document(world):
    inv = _invite()
    await world.repo.create(inv)
    found = await world.repo.get_by_code(CODE.lower())
    found.mark_used(MEMBER)
    await world.repo.update(found)
    assert len(world.docs) == 1
    assert (await world.repo.get_by_code(CODE)).used_by_user_id == MEMBER


async def test_a_code_is_consumed_once_and_typed_variants_all_work(world):
    await world.team()
    invite = await world.service.generate_team_invite(OWNER, OWNER_ACC)
    typed = invite.code.lower().replace("-", " ")
    await world.service.consume_team_invite(typed, MEMBER)
    assert (await world.users.get_user(MEMBER)).account_id == OWNER_ACC
    with pytest.raises(ValueError, match="expired or already used"):
        await world.service.consume_team_invite(invite.code, MEMBER)
    listed = await world.repo.list_by_user(OWNER)
    assert listed[0].used_by_user_id == MEMBER and listed[0].code is None


async def test_service_creation_stores_a_digest_and_hint_only(world):
    invite = await world.service.generate_team_invite(OWNER, OWNER_ACC)
    plain = normalise_invite_code(invite.code)
    assert list(world.docs) == [keyed_digest(DIGEST_KEY, "invite", plain)]
    assert world.docs[next(iter(world.docs))]["hint"] == plain[-4:] == invite.hint


async def test_no_log_record_holds_the_code_across_create_and_consume(world, caplog):
    caplog.set_level(logging.DEBUG)
    await world.team()
    invite = await world.service.generate_team_invite(OWNER, OWNER_ACC)
    await world.service.consume_team_invite(invite.code, MEMBER)
    with pytest.raises(ValueError):
        await world.service.consume_team_invite(invite.code, MEMBER)
    plain = normalise_invite_code(invite.code)
    for record in caplog.records:
        text = record.getMessage().upper().replace("-", "")
        assert plain not in text, record.getMessage()


def test_the_login_auto_consume_log_names_no_code():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[3] / "src" / "web" / "oauth_app.py").read_text()
    assert "{pending_code}" not in source
