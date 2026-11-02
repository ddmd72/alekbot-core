"""P2b-8 (MULTI_DRIVE_RFC §10, D8): the initial build is per provider account. Two connections of two accounts each open
their own allowance; a new connection of an account that already has a record (a reconnect after a disconnect) opens
none: it reads the same record (`initial_build_doc_id`). Technical ids only."""
from dataclasses import replace

from src.domain.drive_index_params import INITIAL_BUILD_USD
from src.domain.drive_usage import initial_build_doc_id
from src.domain.provider_account import ProviderAccount
from tests.support.drive_connection_service_fake import connection

from .drive_multi_connection_world import USER, MultiWorld

ALEX = ProviderAccount(realm="realm-x", subject="alex@example.com", label="")
TOM = ProviderAccount(realm="realm-x", subject="tom@acme.example", label="")
SPEND = 5.0


async def test_two_connections_of_two_accounts_each_open_their_own_allowance():
    w = MultiWorld()
    one = await w.connect("conn-1", ref_tag="a1")
    two = await w.connect("conn-2", ref_tag="b2", drive_identity="fake_b:drive-2")
    one = replace(one, provider_account=ALEX)
    two = replace(two, provider_account=TOM)
    await w.repo.spend_initial_build(USER, ALEX.realm, ALEX.subject, SPEND, w.clock.now)
    first = await w.svc._initial_build(one.handle, await w.svc._account(one.handle, one))
    second = await w.svc._initial_build(two.handle, await w.svc._account(two.handle, two))
    assert (first.allowance_usd, first.spent_usd) == (INITIAL_BUILD_USD, SPEND)
    assert (second.allowance_usd, second.spent_usd) == (INITIAL_BUILD_USD, 0.0)   # its own, untouched
    assert initial_build_doc_id(USER, ALEX.realm, ALEX.subject) != initial_build_doc_id(USER, TOM.realm, TOM.subject)


async def test_a_new_connection_of_a_known_account_opens_no_new_allowance():
    w = MultiWorld()
    old = replace(await w.connect("conn-1", ref_tag="a1"), provider_account=ALEX)
    await w.repo.spend_initial_build(USER, ALEX.realm, ALEX.subject, SPEND, w.clock.now)
    again = replace(connection(USER, "conn-9", ref_tag="c3", drive_identity="fake_a:drive-1"), provider_account=ALEX)
    before = dict(w.repo.initial)
    after = await w.svc._initial_build(again.handle, await w.svc._account(again.handle, again))
    assert after.spent_usd == SPEND and after == await w.svc._initial_build(old.handle, ALEX)
    assert dict(w.repo.initial) == before          # reading it opened and wrote nothing
