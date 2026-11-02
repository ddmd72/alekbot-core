"""OAuthConnectService.start(add=True): the flow carries intent ADD to the edge; an ADD consent of the account of a
live connection is its reconnect (MULTI_DRIVE_RFC E33)."""
from src.domain.flow_intent import FlowIntent
from tests.unit.services.test_oauth_connect_service import DRIVES, World


def _capture(world: World) -> list:
    seen: list = []
    original = world.edge.authorization_url

    def spy(flow, spec, redirect_uri, *, state):
        seen.append(flow.intent)
        return original(flow, spec, redirect_uri, state=state)

    world.edge.authorization_url = spy
    return seen


async def test_add_flag_gives_intent_add_and_default_is_connect():
    w = World()
    seen = _capture(w)
    await w.start("google_drive", add=True)
    await w.start("google_drive")
    assert seen == [FlowIntent.ADD, FlowIntent.CONNECT]


async def test_reconnect_wins_over_add():
    w = World()
    await w.complete(await w.start("gmail"))
    held = (await w.held("gmail"))[0]
    seen = _capture(w)
    await w.start("gmail", credential_id=held.credential_id, add=True)
    assert seen == [FlowIntent.RECONNECT]


async def test_add_consent_of_a_held_account_is_its_reconnect():
    w = World()
    await w.complete(await w.start("google_drive", add=True))
    first = await w.held("google_drive")
    assert len(first) == 1
    out = await w.complete(await w.start("google_drive", add=True))
    again = await w.held("google_drive")
    assert out.status == "connected"
    assert [c.credential_id for c in again] == [c.credential_id for c in first]
    assert DRIVES.multiple


async def test_add_consent_of_another_account_is_a_second_credential():
    w = World()
    await w.complete(await w.start("google_drive", add=True))
    w.edge.subject = "sub-2"
    await w.complete(await w.start("google_drive", add=True))
    assert len(await w.held("google_drive")) == 2
