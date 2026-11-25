"""The final pass as one agent task (MCP_MEMORY_SUBMISSIONS_RFC §9.1, §9.3): the task and result codecs, the user
message (oldest receipt first, exactly the ten keys, never the raw submission) and the `reported_date` span."""
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.domain.memory_inbox import (
    ANSWER_WINDOW,
    Basis,
    Candidate,
    CandidateFields,
    CandidateStatus,
    DraftClass,
    Relation,
    RelationKind,
    SourceCopy,
)
from src.domain.memory_inbox_final_task import (
    INBOX_CANDIDATES_HEADER,
    LINE_KEYS,
    decode_final_result,
    decode_final_task,
    encode_final_result,
    encode_final_task,
    final_message,
    final_span,
)

UTC = timezone.utc
D1 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
D2 = datetime(2026, 11, 24, 18, 30, tzinfo=UTC)
RAW_INFORMATION = "Alex said Alex now plays tennis on Sundays, I think"


def c(cid: str = "k1", received: datetime = D1, text: str = "Alex plays tennis on Sundays",
      **over: Any) -> Candidate:
    source = SourceCopy(submission_id=f"s-{cid}", received_at=received, client_name="Claude",
                        information=RAW_INFORMATION, basis=Basis.SAID, said_when="", happens_when="")
    fields = dict(
        candidate_id=cid, user_id="u1", account_id="a1", status=CandidateStatus.CONFIRMED, created_at=D2,
        answer_by=D2 + ANSWER_WINDOW, language="en", source_ids=(source.submission_id,), sources=(source,), text=text,
        happens_when="every Sunday", said_when="mid-November 2026", basis=Basis.SAID,
        draft=DraftClass(directive=False, temporal_class="dynamic", context_priority="medium",
                         expiry={"kind": "horizon", "days": 90}),
        relation=Relation(RelationKind.REFINES, "f1", "User trains at the Iron Temple gym"),
        proposed=CandidateFields(text=text, happens_when="", said_when="", basis=Basis.SAID), edited=True,
        confirmed_at=D2 + timedelta(hours=1),
    )
    fields.update(over)
    return Candidate(**fields)


def test_the_task_and_result_round_trip():
    task = (c("k1"), c("k2", D2, confirmed_at=None, edited=False, proposed=None))
    assert decode_final_task(encode_final_task(task)) == task
    assert decode_final_result(encode_final_result(ended=True, wrote=3)) == (True, 3)
    assert decode_final_result(encode_final_result(ended=False, wrote=0)) == (False, 0)


@pytest.mark.parametrize("payload", [{}, {"inbox_final": []}, {"inbox_final": [{"id": "k1"}]}])
def test_a_payload_without_candidates_is_refused(payload):
    with pytest.raises(ValueError):
        decode_final_task(payload)


@pytest.mark.parametrize("result", [{"ended": 1, "wrote": 0}, {"ended": True, "wrote": -1},
                                    {"ended": True, "wrote": True}, {"ended": True}])
def test_a_result_not_in_the_encoded_form_is_refused(result):
    with pytest.raises(ValueError):
        decode_final_result(result)


def test_candidates_are_sent_oldest_received_first():
    lines = final_message([c("late", D2), c("early", D1), c("mid", D1 + timedelta(days=1))], "UTC").split("\n")
    assert lines[0] == INBOX_CANDIDATES_HEADER
    assert [json.loads(line)["candidate_id"] for line in lines[1:]] == ["early", "mid", "late"]


def test_a_line_carries_exactly_the_ten_keys_and_never_the_raw_submission():
    cand = c()
    message = final_message([cand], "Europe/Lisbon")
    line = json.loads(message.split("\n")[1])
    assert set(json.loads(message.split("\n")[1])) == {"candidate_id", "text", "happens_when", "said_when", "basis",
                                                        "received", "confirmed", "edited", "draft", "relation"}
    assert tuple(line) == LINE_KEYS
    assert (line["received"], line["confirmed"], line["edited"], line["said_when"]) == \
           ("2026-11-20 Fri 10:00 WET", "2026-11-24 Tue 19:30 WET", True, "mid-November 2026")
    assert line["relation"] == {"kind": "refines", "fact_id": "f1", "fact_text": "User trains at the Iron Temple gym"}
    assert line["draft"]["expiry"] == {"kind": "horizon", "days": 90}
    assert RAW_INFORMATION not in final_message([cand], "UTC")


def test_the_span_runs_a_year_back_from_the_oldest_receipt_to_the_newest():
    assert final_span([c(received=D1), c(received=D2)]) == (D1 - timedelta(days=365), D2)
    with pytest.raises(ValueError):
        final_span([])
