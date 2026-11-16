"""LiveTextPacer (VOICE_CALL_LIVE_TEXT_RFC §4.2, §4.4, §4.7): Lelik's phrases are released by the played audio, at
their anchors; the caller's lines keep the order the turns happened; `seq` runs across every frame.

The cut, the snapshot, the memory bound and the counts: `test_voice_live_text_pacer_cut.py`."""
from src.domain.voice_live_text_pacer import LiveTextPacer

BPM = 8  # μ-law 8 kHz: bytes per ms
_CALLER = "We have a booking under RM-55120."
_P1 = "Abbiamo una prenotazione "
_P2 = "con il codice "
_P3 = "RM-55120."


def _reply(pacer: LiveTextPacer, item: str = "item-a8", start: int = 1000) -> None:
    pacer.reply_started()
    pacer.reply_audio(item, start)


def _ops(frames):
    return [(frame["line"], frame["op"], frame.get("text", frame.get("status"))) for frame in frames]


# ------------------------------------------------------------------ anchors


def test_a_phrase_with_an_offset_is_released_when_its_audio_plays():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=1000)
    pacer.reply_delta("item-a8", _P2, 2240, sent_bytes=40_000)
    anchor = 1000 + 2240 * BPM

    assert pacer.tick(anchor - 1) == []
    assert _ops(pacer.tick(anchor)) == [("l1", "append", _P2)]


def test_a_phrase_without_an_offset_is_anchored_where_it_arrived():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=1000)
    pacer.reply_delta("item-a8", _P1, None, sent_bytes=5000)

    assert pacer.tick(4999) == []
    assert _ops(pacer.tick(5000)) == [("l1", "append", _P1)]


def test_a_phrase_before_its_items_audio_is_held_then_anchored_at_that_audio():
    pacer = LiveTextPacer(BPM)
    pacer.reply_started()
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_delta("item-a8", _P2, 1920, sent_bytes=0)

    assert pacer.tick(10**9) == []  # held: nothing is known of where its audio starts

    pacer.reply_audio("item-a8", 3000)
    assert _ops(pacer.tick(3000)) == [("l1", "append", _P1)]
    assert pacer.tick(3000 + 1920 * BPM - 1) == []
    assert _ops(pacer.tick(3000 + 1920 * BPM)) == [("l1", "append", _P2)]


def test_a_delta_without_an_item_counts_from_the_lines_first_item():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, "item-a8", start=2000)
    pacer.reply_delta(None, _P1, 100, sent_bytes=0)

    assert pacer.tick(2000 + 100 * BPM - 1) == []
    assert _ops(pacer.tick(2000 + 100 * BPM)) == [("l1", "append", _P1)]


def test_a_second_item_of_the_reply_counts_from_its_own_start():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, "item-a8", start=0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_audio("item-a9", 50_000)
    pacer.reply_delta("item-a9", _P3, 0, sent_bytes=0)

    assert _ops(pacer.tick(49_999)) == [("l1", "append", _P1)]
    assert _ops(pacer.tick(50_000)) == [("l1", "append", _P3)]


# ------------------------------------------------------------------ release


def test_nothing_is_released_below_the_anchor():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=0)
    pacer.reply_delta("item-a8", _P1, 500, sent_bytes=0)

    for played in (0, 1000, 500 * BPM - 1):
        assert pacer.tick(played) == []


def test_one_tick_releases_several_phrases_as_one_append_joined_verbatim():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_delta("item-a8", _P2, 1000, sent_bytes=0)
    pacer.reply_delta("item-a8", _P3, 2000, sent_bytes=0)

    frames = pacer.tick(1000 * BPM)
    assert _ops(frames) == [("l1", "append", _P1 + _P2)]
    assert frames[0]["who"] == "lelik"
    assert "kind" not in frames[0]
    assert _ops(pacer.tick(2000 * BPM)) == [("l1", "append", _P3)]


def test_the_lag_delays_the_release():
    pacer = LiveTextPacer(BPM, lag_ms=120)
    _reply(pacer, start=0)
    pacer.reply_delta("item-a8", _P1, 1000, sent_bytes=0)

    assert pacer.tick(1000 * BPM) == []
    assert pacer.tick((1000 + 120) * BPM - 1) == []
    assert _ops(pacer.tick((1000 + 120) * BPM)) == [("l1", "append", _P1)]


def test_an_empty_delta_is_ignored():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=0)
    pacer.reply_delta("item-a8", "", 0, sent_bytes=0)

    assert pacer.tick(10**6) == []
    assert pacer.stats["phrases"] == 0


def test_a_delta_before_any_reply_is_dropped():
    pacer = LiveTextPacer(BPM)
    pacer.reply_delta("item-a8", _P1, None, sent_bytes=0)

    assert pacer.tick(10**6) == []
    assert pacer.stats["dropped"] == 1


# ------------------------------------------------------------------ end


def test_the_line_ends_done_only_after_its_last_phrase_is_released():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_delta("item-a8", _P3, 2000, sent_bytes=0)
    pacer.reply_done()

    assert _ops(pacer.tick(0)) == [("l1", "append", _P1)]
    assert pacer.tick(2000 * BPM - 1) == []
    assert _ops(pacer.tick(2000 * BPM)) == [("l1", "append", _P3), ("l1", "end", "done")]
    end = pacer.snapshot()["lines"][0]
    assert end["status"] == "done"
    assert pacer.tick(10**9) == []


def test_an_end_frame_has_no_who():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_done()

    end = pacer.tick(0)[-1]
    assert end == {"v": 1, "seq": end["seq"], "type": "line", "line": "l1", "op": "end", "status": "done"}


def test_a_held_phrase_is_dropped_when_the_reply_ends():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, "item-a8", start=0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_delta("item-a9", _P2, 0, sent_bytes=0)  # item-a9's audio never comes
    pacer.reply_done()

    assert _ops(pacer.tick(0)) == [("l1", "append", _P1), ("l1", "end", "done")]
    assert pacer.stats["dropped"] == 1


def test_a_reply_that_ends_with_no_text_never_reaches_the_page():
    pacer = LiveTextPacer(BPM)
    pacer.reply_started()  # a reply that only calls a tool
    pacer.reply_done()

    assert pacer.tick(10**6) == []
    assert pacer.snapshot()["lines"] == []


def test_reply_done_leaves_a_closed_line_alone_and_a_late_delta_is_dropped():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_done()
    pacer.tick(0)
    pacer.reply_done()
    pacer.reply_delta("item-a8", _P2, 10, sent_bytes=0)

    assert pacer.tick(10**6) == []
    assert pacer.stats["dropped"] == 1


# ------------------------------------------------------------------ caller lines


def test_a_caller_line_allocated_before_the_reply_keeps_the_lower_id():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn("item-u7")       # speech_stopped
    _reply(pacer, "item-a8", start=0)  # xAI: the reply starts before the transcript arrives
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.caller_turn("item-u7")       # turn_committed: the fallback is idempotent
    pacer.caller_text("item-u7", _CALLER)

    frames = pacer.tick(0)
    assert _ops(frames) == [("l1", "set", _CALLER), ("l2", "append", _P1)]
    assert frames[0]["who"] == "caller"


def test_a_caller_line_is_not_paced():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn("item-u7")
    pacer.caller_text("item-u7", _CALLER)

    assert _ops(pacer.tick(0)) == [("l1", "set", _CALLER)]


def test_a_caller_line_is_never_sent_before_it_has_text():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn("item-u7")

    assert pacer.tick(10**6) == []
    assert pacer.snapshot()["lines"] == []


def test_an_empty_transcript_removes_the_line_and_sends_nothing():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn("item-u7")
    pacer.caller_text("item-u7", "  ")
    pacer.caller_turn("item-u8")
    pacer.caller_text("item-u8", _CALLER)

    assert _ops(pacer.tick(0)) == [("l2", "set", _CALLER)]
    assert [line["line"] for line in pacer.snapshot()["lines"]] == ["l2"]


def test_an_empty_transcript_with_no_line_sends_nothing():
    pacer = LiveTextPacer(BPM)
    pacer.caller_text("item-u7", "")
    pacer.caller_text(None, "")

    assert pacer.tick(0) == []
    assert pacer.stats["lines"] == 0


def test_a_transcript_without_an_item_fills_the_oldest_waiting_line():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn(None)
    pacer.caller_turn(None)
    pacer.caller_text(None, _CALLER)
    pacer.caller_text(None, "Lelik, how do I say thank you in Italian?")

    assert _ops(pacer.tick(0)) == [("l1", "set", _CALLER), ("l2", "set", "Lelik, how do I say thank you in Italian?")]


def test_a_transcript_with_no_line_allocates_one():
    pacer = LiveTextPacer(BPM)
    _reply(pacer, start=0)
    pacer.caller_text("item-u7", _CALLER, ask=True)

    frames = pacer.tick(0)
    assert _ops(frames) == [("l2", "set", _CALLER)]
    assert frames[0]["kind"] == "ask"


def test_the_kind_is_fixed_when_the_line_is_allocated():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn("item-u7", ask=False)
    pacer.caller_text("item-u7", _CALLER, ask=True)

    assert "kind" not in pacer.tick(0)[0]


# ------------------------------------------------------------------ kind and seq


def test_an_ask_exchange_marks_both_lines_and_the_snapshot():
    pacer = LiveTextPacer(BPM)
    question = "Lelik, how do I say thank you in Italian?"
    pacer.caller_turn("item-u7", ask=True)
    pacer.reply_started(ask=True)
    pacer.reply_audio("item-a8", 0)
    pacer.caller_text("item-u7", question)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_done()

    frames = pacer.tick(0)
    assert [(f["line"], f["op"], f.get("kind")) for f in frames] == [
        ("l1", "set", "ask"), ("l2", "append", "ask"), ("l2", "end", None)]
    assert [line.get("kind") for line in pacer.snapshot()["lines"]] == ["ask", "ask"]

    pacer.caller_turn("item-u9")
    pacer.caller_text("item-u9", _CALLER)
    pacer.reply_started()
    assert "kind" not in pacer.tick(0)[0]


def test_seq_rises_strictly_across_ticks_and_snapshots():
    pacer = LiveTextPacer(BPM)
    pacer.caller_turn("item-u7")
    pacer.caller_text("item-u7", _CALLER)
    _reply(pacer, start=0)
    pacer.reply_delta("item-a8", _P1, 0, sent_bytes=0)
    pacer.reply_delta("item-a8", _P2, 100, sent_bytes=0)
    pacer.reply_done()

    seqs = [frame["seq"] for frame in pacer.tick(0)]
    seqs.append(pacer.snapshot()["seq"])
    seqs += [frame["seq"] for frame in pacer.tick(100 * BPM)]
    seqs.append(pacer.snapshot()["seq"])

    assert seqs == list(range(1, len(seqs) + 1))
    assert len(seqs) == 6  # set, append, snapshot, append, end, snapshot
