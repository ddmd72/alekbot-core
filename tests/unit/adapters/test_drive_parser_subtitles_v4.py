"""Subtitle cues keep their start time as `[hh:mm:ss] text` (EXTERNAL_FILES_RFC §7 U5, A-U5-5)."""
from src.adapters.drive_parser.readers import subtitle_cues
from src.domain.drive_text_gate import is_machine_text

VTT = """WEBVTT

NOTE
rehearsal 00:00:01.000 --> 00:00:02.000

1
00:00:03.250 --> 00:00:05.000
<v Alex>Welcome to the garden meeting.

00:00:05.500 --> 00:00:07.000
<v Kim>The shed permit is filed.

01:02:03.900 --> 01:02:05.000
<v Jordan>Rolled-up line

01:02:05.000 --> 01:02:06.000
<v Jordan>Rolled-up line

02:10.000 --> 02:12.000
Short form &amp; ampersand
"""

SRT = """1
00:00:01,000 --> 00:00:02,500
Hello there.

2
00:01:02,000 --> 00:01:04,000
42
"""


def test_vtt_keeps_start_times_voice_tags_and_the_first_time_of_a_repeat():
    assert subtitle_cues(VTT).splitlines() == [
        "[00:00:03] Alex: Welcome to the garden meeting.",
        "[00:00:05] Kim: The shed permit is filed.",
        "[01:02:03] Jordan: Rolled-up line",
        "[00:02:10] Short form & ampersand",
    ]


def test_srt_keeps_start_times_and_a_numeric_cue_survives():
    assert subtitle_cues(SRT).splitlines() == ["[00:00:01] Hello there.", "[00:01:02] 42"]


def test_the_machine_text_gate_still_passes_the_result():
    topics = ["the shed permit", "the water rota", "the seed order", "the tool library", "the compost bins",
              "the fence repair", "the open day", "the budget for gravel", "the new plots", "the volunteer list"]
    speakers = ["Alex", "Kim", "Jordan"]
    cues = "\n\n".join(
        f"{n}\n00:{n // 60:02d}:{n % 60:02d}.000 --> 00:{n // 60:02d}:{n % 60:02d}.900\n"
        f"<v {speakers[n % 3]}>Point {n}: {topics[n % 10]} needs a decision before week {n % 7 + 1}, said {speakers[(n + 1) % 3]}."
        for n in range(1, 60))
    text = subtitle_cues("WEBVTT\n\n" + cues)
    assert text.splitlines()[0].startswith("[00:00:01] Kim: Point 1")
    assert is_machine_text(text) is False
