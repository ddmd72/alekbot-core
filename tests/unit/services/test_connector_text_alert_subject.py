"""The ops alert of a connector's failed first text load names the connector (`ConnectorTextService(alert_subject=)`):
the drive connector's wording is unchanged, the tasks connector has its own, and a caller that names none gets the
generic subject. The alert's detail (which token, why) follows the subject."""
from typing import List

import pytest

from src.composition.drive_connector_tools import build_drive_connector_tools
from src.domain.exceptions import ConnectorTextsUnavailable
from src.services.connector_text_service import GENERIC_ALERT_SUBJECT, ConnectorTextService
from tests.support.drive_connector_texts import RESULT_TOKEN as DRIVE_RESULT_TOKEN
from tests.support.drive_connector_texts import TOOL_TOKEN as DRIVE_TOOL_TOKEN
from tests.support.drive_connector_texts import TokenStore


class Sink:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


def _missing_result_token() -> TokenStore:
    store = TokenStore()
    del store.tokens[DRIVE_RESULT_TOKEN]
    return store


async def test_the_drive_connector_alert_keeps_its_wording():
    sink = Sink()
    with pytest.raises(ConnectorTextsUnavailable):
        await build_drive_connector_tools(conversion_service="conversion", drive_service=None, drive_search=None,
                                          drive_notes=None, token_repo=_missing_result_token(), alert_sink=sink,
                                          links=object())  # type: ignore[arg-type]
    assert len(sink.posts) == 1
    assert sink.posts[0].startswith("Drive connector texts unavailable: ") and DRIVE_RESULT_TOKEN in sink.posts[0]


@pytest.mark.parametrize("subject,expected", [("Task connector texts unavailable", "Task connector texts unavailable"),
                                              (None, GENERIC_ALERT_SUBJECT)])
async def test_the_alert_is_headed_by_the_callers_subject(subject, expected):
    sink = Sink()
    kwargs = {} if subject is None else {"alert_subject": subject}
    texts = ConnectorTextService(_missing_result_token(), tool_token_id=DRIVE_TOOL_TOKEN,
                                 result_token_id=DRIVE_RESULT_TOKEN, required_tool_keys=set(),
                                 required_result_keys=set(), alert_sink=sink, **kwargs)
    with pytest.raises(ConnectorTextsUnavailable):
        await texts.texts()
    assert sink.posts == [f"{expected}: connector texts could not be loaded: token {DRIVE_RESULT_TOKEN} not found"]
