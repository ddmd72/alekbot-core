"""A new invite code carries 128 bits (SECRETS_AT_REST_RFC §2.5, S5): 16 random bytes as 26 unpadded base32
characters, grouped 5-5-5-5-6 for display."""
import re
from unittest.mock import AsyncMock, MagicMock

from src.services import invite_code_service
from src.services.invite_code_service import InviteCodeService

SHAPE = re.compile(r"^[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{5}-[A-Z2-7]{6}$")


def _service() -> InviteCodeService:
    repo = MagicMock()
    repo.create = AsyncMock()
    return InviteCodeService(repo, MagicMock(), MagicMock(), MagicMock())


async def test_code_shape_is_26_base32_characters_in_5_5_5_5_6_groups():
    invite = await _service().generate_team_invite("user-1", "acct-1")
    assert SHAPE.match(invite.code)
    assert len(invite.code.replace("-", "")) == 26


async def test_code_is_built_from_exactly_16_random_bytes(monkeypatch):
    asked = []

    def fixed(n):
        asked.append(n)
        return bytes(range(n))

    monkeypatch.setattr(invite_code_service.secrets, "token_bytes", fixed)
    invite = await _service().generate_team_invite("user-1", "acct-1")
    assert asked == [16]
    assert invite.code == "AAAQE-AYEAU-DAOCA-JBIFQ-YDIOB4"


async def test_hint_is_the_last_four_characters_and_codes_differ():
    service = _service()
    first = await service.generate_team_invite("user-1", "acct-1")
    second = await service.generate_team_invite("user-1", "acct-1")
    assert first.hint == first.code[-4:]
    assert first.code != second.code


async def test_self_link_codes_have_the_same_shape():
    invite = await _service().generate_self_link("user-1", "acct-1", "slack")
    assert SHAPE.match(invite.code)
