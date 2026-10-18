"""User-message timestamp: year and weekday are stated, the zone is the user's own.

The stamp used to be `[Oct 18, 08:14 CEST]`: no year, no weekday, so the model had to guess the year from its
training and compute "Friday" or "the day after tomorrow" by itself. The format is derived from `created_at`
only, so a message keeps the same text on every request and the history cache is unaffected.
"""
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
