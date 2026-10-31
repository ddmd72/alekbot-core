"""Transcription dollars in the indexing service (USER_DRIVE_INDEXING_RFC §27.10; plan G8, G9 as revised): the
initial build never pays for them, and a batch collected after a month turn is booked in its RESERVATION's month."""
from src.domain.drive_index_planner import month_key

from .drive_content_world import ContentWorld
from .drive_index_fakes import LegacyConnections, legacy_account

ACCOUNT = legacy_account("u")   # the initial build is keyed by the connection's provider account (MULTI_DRIVE_RFC §10)


async def _handle(w):
    """The handle of the world's one connection: the money path takes a handle (§6)."""
    return (await LegacyConnections(w.users, w.drive).get("u", "u")).handle


async def test_initial_build_never_covers_transcribe():
    w = ContentWorld()
    await w.svc._spend_usd(await _handle(w), "transcribe", 2.0, 0.0, 1)
    build = await w.repo.get_initial_build("u", ACCOUNT.realm, ACCOUNT.subject)
    usage = await w.repo.get_model_usage("u", month_key(w.clock.now))
    assert (build.spent_usd, build.opened_at, build.closed_at) == (0.0, None, None)
    assert (usage.transcribe_usd, usage.transcribe_initial_usd, usage.cap_usd("transcribe")) == (2.0, 0.0, 2.0)
    await w.svc._spend_usd(await _handle(w), "profile", 1.0, 0.0, 1)       # other classes still spend the open build first
    assert (await w.repo.get_initial_build("u", ACCOUNT.realm, ACCOUNT.subject)).spent_usd == 1.0


async def test_cost_after_month_turn_booked_in_reservation_month():
    w = ContentWorld()
    october = month_key(w.clock.now)
    await w.repo.reserve_model_usd("u", october, "transcribe", 3.0)
    w.clock.advance(40 * 86400)                                # collected after the turn
    november = month_key(w.clock.now)
    assert november != october
    await w.svc._spend_usd(await _handle(w), "transcribe", 2.5, 0.0, 1, month=october)
    await w.repo.reserve_model_usd("u", october, "transcribe", -3.0)
    oct_usage = await w.repo.get_model_usage("u", october)
    assert (oct_usage.transcribe_usd, oct_usage.transcribe_reserved_usd) == (2.5, 0.0)
    nov_usage = await w.repo.get_model_usage("u", november)
    assert (nov_usage.transcribe_usd, nov_usage.transcribe_reserved_usd) == (0.0, 0.0)


async def test_default_month_is_the_clocks():
    w = ContentWorld()
    await w.svc._spend_usd(await _handle(w), "transcribe", 1.0, 0.0, 1)
    assert (await w.repo.get_model_usage("u", month_key(w.clock.now))).transcribe_usd == 1.0


async def test_cross_month_booking_writes_no_daily_entry():
    w = ContentWorld()
    october = month_key(w.clock.now)
    w.clock.advance(40 * 86400)
    await w.svc._spend_usd(await _handle(w), "transcribe", 2.5, 0.0, 1, month=october)
    assert (await w.repo.get_model_usage("u", october)).daily == {}
    await w.svc._spend_usd(await _handle(w), "transcribe", 1.0, 0.0, 1)
    assert len((await w.repo.get_model_usage("u", month_key(w.clock.now))).daily) == 1
