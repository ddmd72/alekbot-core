"""OAuthConnectService error branches of slice 1a: a flow record that no longer names a known connector, an unreadable
sealed PKCE verifier, and a store step whose update finds the record gone. World and fakes: test_oauth_connect_service."""
from tests.unit.services.test_oauth_connect_service import World


async def test_a_flow_of_an_unknown_kind_is_a_state_error_and_exchanges_nothing():
    w = World()
    state = await w.start()
    (record,) = w.flows.data.values()
    record["kind"] = "retired_kind"
    out = await w.complete(state)
    assert (out.status, out.kind) == ("state", None)
    assert w.edge.exchanges == 0 and await w.held() == [] and w.activated == []


async def test_an_unreadable_verifier_is_a_state_error_and_exchanges_nothing():
    w = World()
    state = await w.start()
    (record,) = w.flows.data.values()
    record["pkce_verifier_ct"] = "not-a-sealed-verifier"
    out = await w.complete(state)
    assert (out.status, out.kind) == ("state", "gmail")
    assert w.edge.exchanges == 0 and await w.held() == [] and w.activated == []


async def test_an_update_that_finds_no_record_falls_back_to_saving_and_removes_nothing():
    w = World()
    await w.complete(await w.start())
    (first,) = await w.held()
    w.edge.scopes = ("gmail.read", "extra")

    async def record_gone(credential):
        return False

    w.store.repo.update = record_gone
    assert (await w.complete(await w.start())).status == "connected"
    (second,) = await w.held()
    assert second.credential_id == first.credential_id        # the save kept the account's record: never removed
    assert second.access_token == "at-new" and "extra" in second.scopes
    assert w.removed == []
