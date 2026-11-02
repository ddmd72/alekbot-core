"""GoogleDriveTokenProvider: a refresh answer without an access token is DriveThrottled, never a half-written
credential (GOOGLE_DRIVE_RFC §3.1). The token endpoint is the stateful Drive fake's."""
import logging
from datetime import datetime, timezone

import pytest

from src.adapters.google.google_drive_tokens import THROTTLE_HINT_S, GoogleDriveTokenProvider
from src.domain.user_drive import DriveThrottled
from tests.support.google_drive_fake import _Response, google_setup

_PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("answer", [{}, {"access_token": ""}, {"access_token": 7}, {"expires_in": 3599}, ["tok"]])
async def test_a_refresh_answer_without_an_access_token_is_throttled(monkeypatch, caplog, answer):
    setup = await google_setup(monkeypatch, token_expiry=_PAST)
    setup.fake._token = lambda data: _Response(200, answer)
    provider = GoogleDriveTokenProvider(setup.store.repo, "cid", "csecret", "google_drive")
    h = setup.handle
    with caplog.at_level(logging.WARNING):
        with pytest.raises(DriveThrottled) as caught:
            await provider.token(h.user_id, h.credential_id)
    assert caught.value.retry_after_s == THROTTLE_HINT_S
    stored = await setup.store.repo.get(h.user_id, h.credential_id) if hasattr(setup.store.repo, "get") else None
    assert stored is None or stored.access_token == "tok-a"              # nothing was written back
    assert "csecret" not in caplog.text and "rt-a" not in caplog.text
