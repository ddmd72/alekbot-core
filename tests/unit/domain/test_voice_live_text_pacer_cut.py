"""LiveTextPacer (VOICE_CALL_LIVE_TEXT_RFC §4.3, §4.7, §4.10): the barge-in cut keeps only what was played, the
snapshot is bounded and holds only what the page was sent, old lines are forgotten, and `stats` counts."""
import json

from src.domain.voice_control_protocol import encode
from src.domain.voice_live_text_pacer import LiveTextPacer

BPM = 8
_CALLER = "We have a booking under RM-55120."
_P1 = "Abbiamo una prenotazione "
_P2 = "con il codice "
_P3 = "RM-55120."


def _ops(frames):
    return [(frame["line"], frame["op"], frame.get("text", frame.get("status"))) for frame in frames]


def _three_phrase_reply(pacer: LiveTextPacer, ask: bool = False) -> None:
    pacer.reply_started(ask=ask)
    pacer.reply_audio("item-a8", 0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_delta("item-a8", _P2, 1000, sent_bytes=0)
    pacer.reply_delta("item-a8", _P3, 2000, sent_bytes=0)


def _shown_line(pacer: LiveTextPacer, number: int, text: str) -> None:
    pacer.caller_turn(f"item-u{number}")
    pacer.caller_text(f"item-u{number}", text)


# ------------------------------------------------------------------ cut


def test_a_cut_keeps_what_was_played_drops_the_rest_and_ends_interrupted():
    pacer = LiveTextPacer(BPM, lag_ms=1500)
    _three_phrase_reply(pacer)
    assert pacer.tick(1000 * BPM) == []  # the lag holds the first two phrases back

    pacer.cut(1000 * BPM)  # the caller barges in: what played is released, the lag ignored

    assert _ops(pacer.tick(1000 * BPM)) == [("l1", "append", _P1 + _P2), ("l1", "end", "interrupted")]
    assert pacer.stats["dropped"] == 1


def test_later_deltas_of_a_cut_reply_are_dropped():
    pacer = LiveTextPacer(BPM)
    _three_phrase_reply(pacer)
    pacer.tick(0)
    pacer.cut(0)
    pacer.tick(0)

    pacer.reply_delta("item-a8", "mais texto", 3000, sent_bytes=0)
    pacer.reply_delta(None, "mais texto", None, sent_bytes=0)
    pacer.reply_audio("item-a8", 0)
    pacer.reply_done()

    assert pacer.tick(10**9) == []
    assert pacer.snapshot()["lines"] == [{"line": "l1", "who": "lelik", "text": _P1, "status": "interrupted"}]


def test_an_unbound_item_after_the_cut_lands_on_the_closed_line_and_is_dropped():
    pacer = LiveTextPacer(BPM)
    _three_phrase_reply(pacer)
    pacer.cut(0)
    pacer.tick(0)

    pacer.reply_delta("item-a9", "mais texto", 0, sent_bytes=0)

    assert pacer.tick(10**9) == []


def test_a_reply_never_shown_is_dropped_silently_at_the_cut():
    pacer = LiveTextPacer(BPM)
    _three_phrase_reply(pacer)

    pacer.cut(-1)  # nothing of it played

    assert pacer.tick(10**9) == []
    assert pacer.snapshot()["lines"] == []
    assert pacer.stats["dropped"] == 3


def test_a_cut_of_a_fully_played_reply_after_reply_done_ends_done():
    pacer = LiveTextPacer(BPM, lag_ms=500)
    _three_phrase_reply(pacer)
    pacer.reply_done()

    pacer.cut(2000 * BPM)

    assert _ops(pacer.tick(2000 * BPM)) == [("l1", "append", _P1 + _P2 + _P3), ("l1", "end", "done")]


def test_a_cut_before_reply_done_ends_interrupted_even_with_nothing_dropped():
    pacer = LiveTextPacer(BPM)
    _three_phrase_reply(pacer)

    pacer.cut(2000 * BPM)

    assert _ops(pacer.tick(2000 * BPM))[-1] == ("l1", "end", "interrupted")


def test_a_cut_drops_held_phrases():
    pacer = LiveTextPacer(BPM)
    pacer.reply_started()
    pacer.reply_audio("item-a8", 0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_delta("item-a9", _P2, 0, sent_bytes=0)  # its audio never came
    pacer.reply_done()

    pacer.cut(10**6)

    assert _ops(pacer.tick(10**6)) == [("l1", "append", _P1), ("l1", "end", "interrupted")]


def test_a_cut_leaves_caller_lines_and_ended_lines_alone():
    pacer = LiveTextPacer(BPM)
    _shown_line(pacer, 7, _CALLER)
    _three_phrase_reply(pacer)
    pacer.reply_done()
    pacer.tick(2000 * BPM)

    pacer.cut(0)

    assert pacer.tick(10**9) == []
    lines = pacer.snapshot()["lines"]
    assert [(line["line"], line.get("status")) for line in lines] == [("l1", None), ("l2", "done")]


# ------------------------------------------------------------------ snapshot


def test_the_snapshot_holds_only_text_already_sent():
    pacer = LiveTextPacer(BPM)
    _shown_line(pacer, 7, _CALLER)
    _three_phrase_reply(pacer)
    pacer.tick(0)
    pacer.caller_turn("item-u9")
    pacer.caller_text("item-u9", "Lelik, how do I say thank you in Italian?")  # queued, not sent yet

    assert pacer.snapshot()["lines"] == [
        {"line": "l1", "who": "caller", "text": _CALLER},
        {"line": "l2", "who": "lelik", "text": _P1},
    ]


def test_the_snapshot_is_followed_by_frames_without_doubling_a_line():
    pacer = LiveTextPacer(BPM)
    _three_phrase_reply(pacer)
    pacer.tick(0)
    pacer.cut(1000 * BPM)  # releases _P2, sent at the next tick

    snap = pacer.snapshot()
    frames = pacer.tick(1000 * BPM)

    assert snap["lines"] == [{"line": "l1", "who": "lelik", "text": _P1}]
    assert _ops(frames) == [("l1", "append", _P2), ("l1", "end", "interrupted")]
    assert all(frame["seq"] > snap["seq"] for frame in frames)


def test_the_snapshot_keeps_the_last_fifty_visible_lines():
    pacer = LiveTextPacer(BPM)
    for number in range(1, 61):
        _shown_line(pacer, number, f"turn {number}")
        pacer.tick(0)

    lines = pacer.snapshot()["lines"]
    assert len(lines) == 50
    assert lines[0]["line"] == "l11" and lines[-1]["line"] == "l60"


def test_the_snapshot_stays_within_its_byte_cap_by_dropping_the_oldest_lines():
    pacer = LiveTextPacer(BPM)
    for number in range(1, 51):
        _shown_line(pacer, number, f"{number:02d} " + "Albergo Immaginario " * 34)  # ~650 bytes each
        pacer.tick(0)

    snap = pacer.snapshot()
    assert len(encode(snap).encode("utf-8")) <= 20_000
    assert snap["lines"][-1]["line"] == "l50"
    numbers = [int(line["line"][1:]) for line in snap["lines"]]
    assert numbers == list(range(numbers[0], 51)) and numbers[0] > 1


def test_one_oversize_line_keeps_only_its_end():
    pacer = LiveTextPacer(BPM, max_snapshot_bytes=300)
    _shown_line(pacer, 1, _CALLER)
    pacer.tick(0)
    long_text = "Immaginario " * 108 + "THE END"
    _shown_line(pacer, 2, long_text)
    pacer.tick(0)

    snap = pacer.snapshot()
    raw = encode(snap).encode("utf-8")
    assert len(raw) <= 300
    assert [line["line"] for line in snap["lines"]] == ["l2"]
    kept = snap["lines"][0]["text"]
    assert kept.endswith("THE END") and long_text.endswith(kept) and len(kept) < len(long_text)
    assert json.loads(raw) == snap


def test_a_cap_too_small_for_any_line_gives_an_empty_snapshot():
    pacer = LiveTextPacer(BPM, max_snapshot_bytes=10)
    _shown_line(pacer, 1, _CALLER)
    pacer.tick(0)

    assert pacer.snapshot()["lines"] == []


def test_the_snapshot_leaves_out_unreleased_text():
    pacer = LiveTextPacer(BPM)
    _three_phrase_reply(pacer)
    pacer.tick(1000 * BPM)

    assert pacer.snapshot()["lines"] == [{"line": "l1", "who": "lelik", "text": _P1 + _P2}]


def test_snapshot_lines_carry_status_and_kind():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn("item-u7", ask=True)
    pacer.caller_text("item-u7", "Lelik, how do I say thank you in Italian?")
    _three_phrase_reply(pacer, ask=True)
    pacer.reply_done()
    pacer.tick(2000 * BPM)

    assert pacer.snapshot()["lines"] == [
        {"line": "l1", "who": "caller", "text": "Lelik, how do I say thank you in Italian?", "kind": "ask"},
        {"line": "l2", "who": "lelik", "text": _P1 + _P2 + _P3, "status": "done", "kind": "ask"},
    ]


# ------------------------------------------------------------------ memory and stats


def test_more_than_fifty_lines_forgets_the_oldest():
    pacer = LiveTextPacer(BPM)
    _three_phrase_reply(pacer)  # l1 holds two unreleased phrases
    pacer.tick(0)
    for number in range(2, 52):
        pacer.caller_turn(f"item-u{number}")

    pacer.reply_delta("item-a8", "mais texto", 0, sent_bytes=0)  # its line is forgotten
    pacer.caller_text("item-u51", _CALLER)

    assert _ops(pacer.tick(10**9)) == [("l51", "set", _CALLER)]
    assert pacer.stats["dropped"] == 3  # two forgotten with l1, one late delta


def test_a_late_transcript_of_a_forgotten_turn_gets_a_new_line():
    pacer = LiveTextPacer(BPM, max_lines=2)
    pacer.caller_turn("item-u1")
    pacer.caller_turn("item-u2")
    pacer.caller_turn("item-u3")

    pacer.caller_text("item-u1", _CALLER)

    assert _ops(pacer.tick(0)) == [("l4", "set", _CALLER)]


def test_stats_count_lines_phrases_offsets_releases_and_drops():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn("item-u7")
    pacer.caller_text("item-u7", _CALLER)
    pacer.reply_started()
    pacer.reply_audio("item-a8", 0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_delta("item-a8", _P2, None, sent_bytes=500)
    pacer.reply_delta("item-a8", _P3, 2000, sent_bytes=0)
    pacer.tick(500)
    pacer.cut(500)

    assert dict(pacer.stats) == {"lines": 2, "phrases": 3, "phrases_with_offset": 2, "released": 2, "dropped": 1}
