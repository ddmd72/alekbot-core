import pytest

from src.domain.tone import UserTone


def test_validate_accepts_known_tones():
    assert UserTone.validate("casual") == UserTone.CASUAL
    assert UserTone.validate("friendly") == UserTone.FRIENDLY
    assert UserTone.validate("playful") == UserTone.PLAYFUL
    assert UserTone.validate("neutral") == UserTone.NEUTRAL
    assert UserTone.validate("professional") == UserTone.PROFESSIONAL
    assert UserTone.validate("urgent") == UserTone.URGENT
    assert UserTone.validate("concerned") == UserTone.CONCERNED
    assert UserTone.validate("distressed") == UserTone.DISTRESSED
    assert UserTone.validate("formal") == UserTone.FORMAL


def test_validate_falls_back_to_friendly_on_invalid():
    assert UserTone.validate("mystery") == UserTone.FRIENDLY


@pytest.mark.parametrize("tone,expected", [
    ("casual", True),
    ("friendly", True),
    ("playful", True),
    ("neutral", True),
    ("professional", False),
    ("urgent", False),
    ("concerned", False),
    ("distressed", False),
    ("formal", False),
])
def test_allows_humor(tone, expected):
    assert UserTone.allows_humor(tone) is expected