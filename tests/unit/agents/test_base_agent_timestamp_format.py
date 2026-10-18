"""User-message timestamp: year and weekday are stated, the zone is the user's own.

The stamp used to be `[Oct 18, 08:14 CEST]`: no year, no weekday, so the model had to guess the year from its
training and compute "Friday" or "the day after tomorrow" by itself. The format is derived from `created_at`
only, so a message keeps the same text on every request and the history cache is unaffected.
"""
import re
from datetime import datetime, timezone

import pytest

from src.agents.base_agent import BaseAgent
from src.domain.llm import Message, MessagePart
from src.domain.agent import AgentConfig

# 2023-11-14 22:13:20 UTC, a Tuesday
CREATED_AT = 1700000000


class _Agent(BaseAgent):
    async def can_handle(self, message):
        return True

    async def execute(self, message):
        return None


def _stamped(timezone):
    agent = _Agent(AgentConfig(agent_id="a", agent_type="mock"))
    agent._user_timezone = timezone
    msg = Message(role="user", parts=[MessagePart(text="hello")], created_at=CREATED_AT)
    return agent._inject_timestamps([msg])[0].parts[0].text


def test_stamp_has_year_weekday_and_the_users_zone():
    assert _stamped("Europe/Madrid") == "[2023-11-14 Tue 23:13 CET] hello"


def test_another_zone_moves_the_date_and_weekday_with_it():
    assert _stamped("Pacific/Auckland") == "[2023-11-15 Wed 11:13 NZDT] hello"


def test_unknown_zone_falls_back_to_utc():
    assert _stamped("Not/AZone") == "[2023-11-14 Tue 22:13 UTC] hello"


def test_stamp_is_stable_across_calls():
    assert _stamped("Europe/Madrid") == _stamped("Europe/Madrid")


# --- Format pin -------------------------------------------------------------------------------------------
# The exact shape is part of the prompt contract. Changing it is allowed only on purpose, by editing this
# pattern with a reason; an accidental edit of the strftime string fails here.
STAMP = re.compile(r"^\[(\d{4})-(\d{2})-(\d{2}) (Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{2}):(\d{2}) ([A-Za-z0-9+\-]+)\] ")

SUMMER_AT = int(datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc).timestamp())  # Friday, Madrid on CEST
WINTER_AT = int(datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc).timestamp())  # Saturday, Madrid on CET


def _stamp_at(created_at, timezone_name="Europe/Madrid", text="hello"):
    agent = _Agent(AgentConfig(agent_id="a", agent_type="mock"))
    agent._user_timezone = timezone_name
    msg = Message(role="user", parts=[MessagePart(text=text)], created_at=created_at)
    return agent._inject_timestamps([msg])[0].parts[0].text


@pytest.mark.parametrize("created_at", [CREATED_AT, SUMMER_AT, WINTER_AT])
@pytest.mark.parametrize("zone", ["Europe/Madrid", "Pacific/Auckland", "America/Los_Angeles", "UTC"])
def test_stamp_matches_the_pinned_shape(created_at, zone):
    text = _stamp_at(created_at, zone)

    match = STAMP.match(text)
    assert match, f"stamp shape changed: {text!r}"
    assert text.endswith("] hello")


def test_stamp_is_the_very_first_thing_in_the_text():
    """Anchors are prepended BEFORE the stamp (base_agent docstring); nothing may precede it here."""
    assert _stamp_at(CREATED_AT).startswith("[2023-")


def test_weekday_matches_the_date():
    text = _stamp_at(SUMMER_AT)
    year, month, day, weekday = STAMP.match(text).group(1, 2, 3, 4)

    assert datetime(int(year), int(month), int(day)).strftime("%a") == weekday


def test_zone_label_follows_the_message_date_not_the_reading_date():
    """A winter message says CET and a summer one CEST, whenever they are read."""
    assert " CET] " in _stamp_at(WINTER_AT)
    assert " CEST] " in _stamp_at(SUMMER_AT)


def test_only_the_first_text_part_is_stamped():
    agent = _Agent(AgentConfig(agent_id="a", agent_type="mock"))
    agent._user_timezone = "Europe/Madrid"
    msg = Message(role="user", parts=[MessagePart(text="first"), MessagePart(text="second")], created_at=CREATED_AT)

    out = agent._inject_timestamps([msg])[0].parts

    assert STAMP.match(out[0].text) and out[0].text.endswith("] first")
    assert out[1].text == "second"


def test_model_messages_are_not_stamped():
    agent = _Agent(AgentConfig(agent_id="a", agent_type="mock"))
    msg = Message(role="model", parts=[MessagePart(text="answer")], created_at=CREATED_AT)

    assert agent._inject_timestamps([msg])[0].parts[0].text == "answer"
