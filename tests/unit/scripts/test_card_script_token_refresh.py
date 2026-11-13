"""The card scripts keep the Gmail access token fresh across a long run: one refresh once the token is older than
REFRESH_AFTER_S, shared by concurrent callers, none before that."""
import asyncio

from scripts.email.card_script_support import REFRESH_AFTER_S, CardSession, keep_fresh


class _Gmail:
    def __init__(self) -> None:
        self.refreshes = 0

    async def refresh_token(self, stored):
        self.refreshes += 1
        await asyncio.sleep(0)
        return f"fresh-{self.refreshes}"


def _session(gmail, stored="stored", refreshed_at=0.0) -> CardSession:
    return CardSession("u1", "acc1", "initial", gmail, "", "UTC", None, None,
                       stored=stored, refreshed_at=refreshed_at)


async def test_a_young_token_is_not_refreshed():
    gmail = _Gmail()
    session = _session(gmail)
    await keep_fresh(session, now=REFRESH_AFTER_S - 1)
    assert gmail.refreshes == 0 and session.credential == "initial"


async def test_an_old_token_is_refreshed_once_for_concurrent_callers():
    gmail = _Gmail()
    session = _session(gmail)
    await asyncio.gather(*(keep_fresh(session, now=REFRESH_AFTER_S + 1) for _ in range(5)))
    assert gmail.refreshes == 1
    assert session.credential == "fresh-1" and session.refreshed_at == REFRESH_AFTER_S + 1


async def test_a_session_without_a_stored_credential_is_left_alone():
    gmail = _Gmail()
    session = _session(gmail, stored=None)
    await keep_fresh(session, now=REFRESH_AFTER_S * 10)
    assert gmail.refreshes == 0 and session.credential == "initial"
